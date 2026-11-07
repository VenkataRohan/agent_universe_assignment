"""Ticket 01: loop, ingestion, restart. All tests go through Seam 1 and the query interface."""
from __future__ import annotations

import re
import threading
import time
from datetime import date, timedelta

import pytest

from controller import queries
from controller.__main__ import main as cli_main
from controller.ingest import IngestError
from controller.loop import Controller, ResumeRefused

from .harness import all_events, faulty_client, recount_calls


def assert_store_counts_every_call_the_api_published(client, db_path) -> None:
    """Per workflow and day, the store's KPIs equal a recount of the API's call events."""
    got = {(r["workflow_id"], r["date"]): {k: r[k] for k in ("dials", "connected", "engaged", "hallucination_flags")}
           for r in queries.daily_kpis(db_path)}
    assert got == {k: dict(v) for k, v in recount_calls(client).items()}


def test_runs_scenario_to_finished_and_counts_every_call(world, db_path):
    client = world(days=1, history_days=1)
    Controller(client, db_path).run()

    assert client.get("/sim/state").json()["finished"] is True
    assert_store_counts_every_call_the_api_published(client, db_path)


def test_truncated_chunk_is_reingested_without_gap_or_double_count(world, db_path):
    client = world(days=1, history_days=1)
    flaky, faults = faulty_client(client, chunk=10, times=1)  # a live chunk mid-day
    Controller(flaky, db_path).run()

    assert faults.damaged == 1
    assert_store_counts_every_call_the_api_published(client, db_path)


def test_crash_mid_chunk_then_fresh_controller_resumes_with_identical_aggregates(world, db_path):
    # History is one large chunk; cutting it late means rows were already flushed mid-transaction.
    client = world(days=1, history_days=3)
    broken, faults = faulty_client(client, chunk=0, times=99, keep=0.95, cut_mid_line=True)
    with pytest.raises(IngestError):
        Controller(broken, db_path).run()
    assert faults.damaged >= 1

    Controller(client, db_path).run()

    assert client.get("/sim/state").json()["finished"] is True
    assert_store_counts_every_call_the_api_published(client, db_path)


def test_fresh_controller_on_same_store_and_world_resumes_and_finishes(world, db_path):
    client = world(days=2, history_days=1)
    first = Controller(client, db_path)
    for _ in range(4 * 30):  # to D1 06:00, then "crash" without closing anything
        first.step()
    before = queries.health(db_path)

    Controller(client, db_path).run()

    after = queries.health(db_path)
    assert after["cursor_chunk"] > before["cursor_chunk"] > 0
    assert client.get("/sim/state").json()["finished"] is True
    assert_store_counts_every_call_the_api_published(client, db_path)
    assert after["events_ingested"] == client.get("/sim/state").json()["last_event_id"]


def _cursor(db_path) -> dict:
    h = queries.health(db_path)
    return {k: h[k] for k in ("scenario", "cursor_chunk", "ingested_through_minute", "events_ingested")}


def _run_a_few_ticks(client, db_path, ticks=40):
    c = Controller(client, db_path)
    for _ in range(ticks):
        c.step()
    c.close()


def test_restart_against_a_different_scenario_is_refused(world, db_path):
    _run_a_few_ticks(world("public_01_dev", days=1, history_days=1), db_path)
    before = _cursor(db_path)

    other = world("public_02_incidents", days=1, history_days=1)
    with pytest.raises(ResumeRefused, match="public_01_dev.*public_02_incidents"):
        Controller(other, db_path).run()

    assert _cursor(db_path) == before
    assert other.get("/sim/state").json()["sim_minute"] == 0


def test_restart_against_a_simulator_whose_clock_is_behind_the_cursor_is_refused(world, db_path):
    _run_a_few_ticks(world("public_01_dev", days=1, history_days=1), db_path)
    before = _cursor(db_path)

    restarted = world("public_01_dev", days=1, history_days=1)  # same scenario, new world at minute 0
    with pytest.raises(ResumeRefused, match="behind"):
        Controller(restarted, db_path).run()

    assert _cursor(db_path) == before
    assert restarted.get("/sim/state").json()["sim_minute"] == 0


def test_exposure_log_matches_call_events_one_to_one_for_sampled_leads(world, db_path):
    client = world(days=1, history_days=1)
    Controller(client, db_path).run()

    calls_by_lead: dict[str, list[dict]] = {}
    for ev in all_events(client):
        if ev["type"] == "call":
            calls_by_lead.setdefault(ev["lead_id"], []).append(ev)
    sample = sorted(calls_by_lead)[::50]
    assert len(sample) > 20

    for lead_id in sample:
        expected = [{"event_id": e["event_id"], "ts": e["ts"], "person_id": e["person_id"],
                     "campaign_id": e["campaign_id"], "attempt_no": e["attempt_no"],
                     "config_version": e["config_version"], "cohort_id": e["cohort_id"],
                     "variant": e["variant"], "connected": e["connected"], "engaged": e["engaged"],
                     "disposition": e["disposition"],
                     "hallucination_flag": e["qa"]["hallucination_flag"] if e["qa"] else None}
                    for e in calls_by_lead[lead_id]]
        assert queries.exposures(db_path, lead_id) == expected


def _active_campaigns_per_api(client) -> dict[str, int]:
    """Campaigns whose cadence hasn't run out by today's local date -> their lead count."""
    days = {w["workflow_id"]: len(w["cadence"]) for w in client.get("/workflows").json()}
    today = date.fromisoformat(client.get("/sim/state").json()["ts"][:10])
    return {ev["campaign_id"]: ev["leads"] for ev in all_events(client)
            if ev["type"] == "campaign_created"
            and date.fromisoformat(ev["ts"][:10]) + timedelta(days=days[ev["workflow_id"]]) > today}


def test_lead_cache_holds_only_active_campaigns_and_is_warmed_on_restart(world, db_path):
    client = world(days=3, history_days=3)
    running = Controller(client, db_path)
    for _ in range(4 * (24 + 24 + 12)):  # to D2 12:00
        running.step()

    active = _active_campaigns_per_api(client)
    assert 0 < len(active) < len(
        [e for e in all_events(client) if e["type"] == "campaign_created"])
    assert running.ingestor.cached_campaigns() == set(active)
    assert running.ingestor.cached_leads() == sum(active.values())

    restarted = Controller(client, db_path)
    restarted.start()
    assert restarted.ingestor.cached_campaigns() == set(active)
    assert restarted.ingestor.cached_leads() == sum(active.values())


def test_world_events_are_stored_verbatim(world, db_path):
    client = world(days=1, history_days=1)
    c = Controller(client, db_path)
    for _ in range(4 * 10):  # D0 10:00, inside approver hours
        c.step()
    cur = client.get("/workflows/nw_renewal/config").json()
    cfg = cur["config"]
    cfg["allocations"]["default"]["caller_id"] = {"D1": 0.4, "D2": 0.4, "D3": 0.2}
    assert client.put("/workflows/nw_renewal/config", json={
        "proposal_id": "t-auto", "base_version": cur["version"], "proposed_config": cfg,
        "rationale": "test"}).status_code == 200
    cfg["allocations"]["default"]["voice"] = {"V1": 0.5, "V2": 0.5}
    assert client.post("/approvals", json={
        "proposal_id": "t-apr", "workflow_id": "nw_renewal", "base_version": cur["version"] + 1,
        "proposed_config": cfg, "change_summary": "s", "diff": {"x": 1}, "affected_leads": 1,
        "dry_run": {"x": 1}, "impact_score": 0.1, "rationale": "r"}).status_code == 200
    c.run()

    expected = [e for e in all_events(client)
                if e["type"] in ("config_changed", "approval_decided", "campaign_created")]
    assert {e["type"] for e in expected} == {"config_changed", "approval_decided", "campaign_created"}
    assert queries.world_events(db_path) == expected


def test_health_cli_reports_cursor_events_and_lag_while_the_controller_runs(world, db_path, capsys):
    client = world(days=2, history_days=1)
    done = threading.Event()
    errors: list[BaseException] = []

    def drive() -> None:
        try:
            Controller(client, db_path).run()  # built in this thread: sqlite connections are per-thread
        except BaseException as e:  # surfaced below
            errors.append(e)
        finally:
            done.set()

    threading.Thread(target=drive, daemon=True).start()
    samples = []
    while not done.is_set():
        if db_path.exists() and cli_main(["health", "--db", str(db_path)]) == 0:
            samples.append(capsys.readouterr().out)
        time.sleep(0.02)
    assert not errors

    during = [s for s in samples if "events ingested" in s and "events ingested   0 " not in s]
    assert len(during) >= 3
    for out in during:
        assert re.search(r"cursor\s+chunk \d+, ingested through D-?\d+ \d\d:\d\d", out)
        assert re.search(r"events ingested\s+\d+", out)
        assert re.search(r"ingest lag\s+\d+(\.\d+)? ticks", out)
    final = queries.health(db_path)
    assert final["ingest_lag_ticks"] == 0
    assert final["events_ingested"] == client.get("/sim/state").json()["last_event_id"]
