"""Ticket 04: the governance boundary on the approval path, first used by degraded caller IDs.

Everything goes through Seam 1: the real simulator's approvals and config APIs are the oracle,
and the controller is read only through its query interface.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pytest

from controller import queries
from controller.__main__ import main as cli_main
from controller.loop import Controller
from controller.registry import Overlay

from .harness import Killed, all_events, troubled_client


def approvals(client, workflow: str) -> list[dict]:
    return client.get("/approvals", params={"workflow_id": workflow}).json()


def config_history(client, workflow: str) -> list[dict]:
    return client.get(f"/workflows/{workflow}/config/history").json()


def caller_ids(client, workflow: str) -> dict[str, float]:
    return client.get(f"/workflows/{workflow}/config").json()["config"]["allocations"]["default"]["caller_id"]


def test_degraded_caller_id_is_moved_off_through_an_approved_proposal(world, db_path):
    client = world(days=2, history_days=1)
    troubled, _ = troubled_client(client, degrade={"nw_renewal": "D2"}, approval_only=True)

    Controller(troubled, db_path).run()

    applied = [h for h in config_history(client, "nw_renewal") if h["actor"] == "controller_approved"]
    assert [(h["proposal_id"], h["changed_dims"]) for h in applied] == [("nw_renewal-caller_id-1", ["caller_id"])]
    assert caller_ids(client, "nw_renewal") == {"D1": 1.0}
    assert [p["status"] for p in queries.proposals(db_path, "nw_renewal")] == ["applied"]


FIRST = "nw_renewal-caller_id-1"


def degraded_world(world, workflow="nw_renewal", days=2):
    client = world(days=days, history_days=1)
    troubled, trouble = troubled_client(client, degrade={workflow: "D2"}, approval_only=True)
    return client, troubled, trouble


def statuses(db_path, workflow="nw_renewal") -> list[str]:
    return [p["status"] for p in queries.proposals(db_path, workflow)]


def applied_by_controller(client, workflow="nw_renewal") -> list[str]:
    return [h["proposal_id"] for h in config_history(client, workflow) if h["actor"] == "controller_approved"]


def step_until(controller, done, max_ticks=4 * 72) -> None:
    for _ in range(max_ticks):
        if done():
            return
        controller.step()
    raise AssertionError("condition never reached")


def test_killed_after_recording_but_before_sending_sends_it_exactly_once_on_restart(world, db_path):
    client, troubled, trouble = degraded_world(world)
    trouble.kill("POST", "/approvals")

    with pytest.raises(Killed):
        Controller(troubled, db_path).run()
    assert approvals(client, "nw_renewal") == []
    assert statuses(db_path) == ["submitting"]

    Controller(troubled, db_path).run()

    assert [a["proposal_id"] for a in approvals(client, "nw_renewal")] == [FIRST]
    assert statuses(db_path) == ["applied"]


def test_killed_after_the_request_arrived_does_not_send_it_again_on_restart(world, db_path):
    client, troubled, trouble = degraded_world(world)
    trouble.kill("POST", "/approvals", after_forward=True)

    with pytest.raises(Killed):
        Controller(troubled, db_path).run()
    assert [a["proposal_id"] for a in approvals(client, "nw_renewal")] == [FIRST]

    Controller(troubled, db_path).run()

    assert [a["proposal_id"] for a in approvals(client, "nw_renewal")] == [FIRST]
    assert statuses(db_path) == ["applied"]


def test_a_lost_response_retried_into_duplicate_proposal_id_counts_as_already_sent(world, db_path):
    client, troubled, trouble = degraded_world(world)
    trouble.lost_response("POST", "/approvals")

    Controller(troubled, db_path).run()

    assert [a["proposal_id"] for a in approvals(client, "nw_renewal")] == [FIRST]
    assert statuses(db_path) == ["applied"]
    assert "duplicate_reconciled" in [d["outcome"] for d in queries.decisions(db_path, "nw_renewal", "proposal")]


def test_killed_between_approval_and_apply_applies_exactly_once_on_restart(world, db_path):
    client, troubled, trouble = degraded_world(world)
    trouble.kill("POST", "/apply")

    with pytest.raises(Killed):
        Controller(troubled, db_path).run()
    assert applied_by_controller(client) == []
    assert statuses(db_path) == ["applying"]

    Controller(troubled, db_path).run()

    assert applied_by_controller(client) == [FIRST]
    assert statuses(db_path) == ["applied"]


def test_killed_after_the_apply_went_through_confirms_it_from_config_history(world, db_path):
    client, troubled, trouble = degraded_world(world)
    trouble.kill("POST", "/apply", after_forward=True)

    with pytest.raises(Killed):
        Controller(troubled, db_path).run()
    assert applied_by_controller(client) == [FIRST]
    assert statuses(db_path) == ["applying"]

    restarted = Controller(troubled, db_path)
    restarted.start()

    assert statuses(db_path) == ["applied"]
    restarted.run()
    assert applied_by_controller(client) == [FIRST]
    assert "confirmed_applied" in [d["outcome"] for d in queries.decisions(db_path, "nw_renewal", "proposal")]


def sim_minute(ts: str) -> int:
    t = datetime.fromisoformat(ts)
    return (t.date() - date(2027, 1, 4)).days * 1440 + t.hour * 60 + t.minute


def assert_requests_obey(sent: list[dict], earliest_hour: int, last_hour_exclusive: int, cap: int) -> None:
    minutes = sorted(sim_minute(a["created_at"]) for a in sent)
    assert all(earliest_hour * 60 <= m % 1440 < last_hour_exclusive * 60 for m in minutes), minutes
    assert all(sum(1 for o in minutes if m - 1440 <= o <= m) <= cap for m in minutes), minutes


def expire_every_request(world, degrade, days=3):
    """Each request expires before an approver can see it, so the controller keeps asking until it can't."""
    client = world(days=days, history_days=1)
    troubled, trouble = troubled_client(client, degrade=degrade, approval_only=True)
    trouble.ttl_hours = 0.25
    return client, troubled


def test_requests_respect_the_kestrel_overlay_and_the_policy_of_other_tenants(world, db_path):
    client, troubled = expire_every_request(world, {"ks_cancel": "D2", "nw_renewal": "D2"})

    Controller(troubled, db_path).run()

    kestrel, northwind = approvals(client, "ks_cancel"), approvals(client, "nw_renewal")
    assert len(kestrel) >= 3 and len(northwind) >= 3  # the rules were actually under pressure
    assert_requests_obey(kestrel, earliest_hour=11, last_hour_exclusive=17, cap=2)
    assert_requests_obey(northwind, earliest_hour=10, last_hour_exclusive=17, cap=3)
    held = [d["detail"]["hold"] for d in queries.decisions(db_path, "ks_cancel", "caller_id") if d["outcome"] == "held"]
    assert any("2 of 2" in h for h in held) and any("outside request hours" in h for h in held)


def test_an_overlay_can_only_tighten_the_policy(world, db_path):
    client = world(days=3, history_days=1)
    # Degraded in history too, so it is already flagged at 06:00, when the looser overlay would allow a request.
    troubled, trouble = troubled_client(client, degrade={"nw_renewal": "D2"}, degrade_history=True, approval_only=True)
    trouble.ttl_hours = 0.25
    looser = {"northwind_life": Overlay(max_requests_per_24h=99, not_before_hour=6)}

    Controller(troubled, db_path, overlays=looser).run()

    sent = approvals(client, "nw_renewal")
    assert len(sent) >= 4
    assert_requests_obey(sent, earliest_hour=10, last_hour_exclusive=17, cap=3)


def test_no_request_goes_out_in_an_approvers_last_two_hours(world, db_path):
    client = world(days=2, history_days=1)
    troubled, trouble = troubled_client(client, degrade={"nw_renewal": "D2"}, degrade_history=True, approval_only=True)
    trouble.ttl_hours = 0.25
    late_starter = {"northwind_life": Overlay(not_before_hour=16)}  # leaves 16:00 to 17:00 of a window ending at 19:00

    Controller(troubled, db_path, overlays=late_starter).run()

    sent = approvals(client, "nw_renewal")
    assert len(sent) >= 2  # 16:00 and 16:30, with the cap still leaving room at 17:00
    assert_requests_obey(sent, earliest_hour=16, last_hour_exclusive=17, cap=3)


def test_an_expired_proposal_is_rebuilt_from_fresh_data_under_a_new_id(world, db_path):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client, degrade={"nw_renewal": "D2"}, approval_only=True)
    trouble.ttl_hours = 0.25
    c = Controller(troubled, db_path)
    step_until(c, lambda: statuses(db_path)[:1] == ["expired"])
    trouble.ttl_hours = None

    c.run()

    first, second, *_ = queries.proposals(db_path, "nw_renewal")
    assert first["status"] == "expired"
    assert second["proposal_id"] == "nw_renewal-caller_id-2" and second["replaces"] == first["proposal_id"]
    assert "Replaces nw_renewal-caller_id-1, which expired" in second["change_summary"]
    assert second["evidence"]["callers"]["D2"]["dials"] != first["evidence"]["callers"]["D2"]["dials"]
    sent = [a["proposal_id"] for a in approvals(client, "nw_renewal")]
    assert sent[:2] == [first["proposal_id"], second["proposal_id"]] and len(sent) == len(set(sent))


HOURS = {str(h): (2 if h >= 18 else 1) for h in range(9, 21)}


def supervisor_edit(client, workflow: str, change) -> int:
    """A supervisor changes the config directly (the simulator's only unscheduled way to bump the version)."""
    cur = client.get(f"/workflows/{workflow}/config").json()
    cfg = cur["config"]
    change(cfg)
    r = client.put(f"/workflows/{workflow}/config", json={
        "proposal_id": f"supervisor-{cur['version']}", "base_version": cur["version"], "proposed_config": cfg,
        "rationale": "my team asked for this"})
    assert r.status_code == 200, r.text
    return r.json()["version"]


def test_a_supervisor_edit_makes_a_pending_proposal_stale_and_the_rebuild_keeps_the_edit(world, db_path):
    client, troubled, _ = degraded_world(world, days=2)
    c = Controller(troubled, db_path)
    step_until(c, lambda: statuses(db_path)[:1] == ["pending"])

    version = supervisor_edit(client, "nw_renewal", lambda cfg: cfg["schedule"]["hour_weights"].update(default=HOURS))
    c.step()  # the approver takes at least 30 minutes, so this is still undecided at the platform

    assert statuses(db_path)[0] == "stale"
    assert [a["status"] for a in approvals(client, "nw_renewal")][0] == "pending"
    c.run()

    first, second, *_ = queries.proposals(db_path, "nw_renewal")
    assert first["status"] == "stale"
    assert second["base_version"] == version and second["replaces"] == first["proposal_id"]
    assert second["proposed_config"]["schedule"]["hour_weights"]["default"] == HOURS
    assert FIRST not in applied_by_controller(client)  # approved or not, its base is gone
    live = client.get("/workflows/nw_renewal/config").json()["config"]
    assert live["schedule"]["hour_weights"]["default"] == HOURS  # whatever the approver decided about the rebuild


def test_a_proposal_that_revises_a_supervisor_edit_says_so(world, db_path):
    client, troubled, _ = degraded_world(world, days=2)
    c = Controller(troubled, db_path)
    step_until(c, lambda: statuses(db_path)[:1] == ["pending"])
    edit = {"D1": 0.6, "D2": 0.4}

    supervisor_edit(client, "nw_renewal", lambda cfg: cfg["allocations"]["default"].update(caller_id=edit))
    step_until(c, lambda: len(statuses(db_path)) >= 2)

    rebuilt = queries.proposals(db_path, "nw_renewal")[1]
    assert re.search(r"Revises the D\d+ \d\d:\d\d supervisor edit \(version 2, caller_id\)", rebuilt["change_summary"])
    assert "supervisor edit" in rebuilt["rationale"]
    assert "caller_id for default {'D1': 0.6, 'D2': 0.4} -> {'D1': 1.0}" in rebuilt["change_summary"]
    assert rebuilt["proposed_config"]["allocations"]["default"]["caller_id"] == {"D1": 1.0}


def test_a_supervisor_edit_after_approval_stops_the_apply(world, db_path):
    client, troubled, trouble = degraded_world(world, days=2)
    trouble.kill("POST", "/apply")
    with pytest.raises(Killed):
        Controller(troubled, db_path).run()

    supervisor_edit(client, "nw_renewal", lambda cfg: cfg["schedule"]["hour_weights"].update(default=HOURS))
    Controller(troubled, db_path).run()

    assert FIRST not in applied_by_controller(client)
    assert queries.proposals(db_path, "nw_renewal")[0]["status"] == "stale"


def test_a_rejected_proposal_is_not_asked_again_the_same_day(world, db_path):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client, degrade={"nw_renewal": "D2"}, approval_only=True)
    trouble.blank_summary = True
    c = Controller(troubled, db_path)
    step_until(c, lambda: statuses(db_path)[:1] == ["rejected"])
    trouble.blank_summary = False
    rejected_at = queries.proposals(db_path, "nw_renewal")[0]["decided_minute"]

    c.run()

    first, second, *_ = queries.proposals(db_path, "nw_renewal")
    assert first["reasons"]
    assert second["created_minute"] >= rejected_at + 1440 and second["replaces"] == first["proposal_id"]
    held = [d for d in queries.decisions(db_path, "nw_renewal", "caller_id") if d["outcome"] == "held"]
    assert any("rejected" in d["detail"]["hold"] for d in held)


def test_with_nothing_degraded_every_tick_records_that_it_did_nothing(world, db_path):
    client = world(days=1, history_days=1)

    Controller(client, db_path).run()

    assert [a for a in approvals(client, "nw_renewal") if "-caller_id" in a["proposal_id"]] == []
    decided = queries.decisions(db_path, "nw_renewal", "caller_id")
    assert len(decided) == 4 * 24 - 1  # every tick but the last, which ends the scenario
    assert {d["outcome"] for d in decided} == {"healthy"}
    assert decided[-1]["detail"]["callers"]["D1"]["dials"] > 0


def test_a_permanently_refused_apply_does_not_block_the_workflow(world, db_path):
    client, troubled, trouble = degraded_world(world, days=3)
    trouble.refuse("POST", "/apply", 409, "not_approved")

    Controller(troubled, db_path).run()

    first, second, *_ = queries.proposals(db_path, "nw_renewal")
    assert first["status"] == "stale" and "not_approved" in first["reasons"][0]
    assert second["replaces"] == first["proposal_id"]


def test_why_keeps_a_rejected_proposal_and_says_why_nothing_changed(world, db_path, capsys):
    client = world(days=2, history_days=1)
    troubled, trouble = troubled_client(client, degrade={"nw_renewal": "D2"}, approval_only=True)
    trouble.blank_summary = True
    Controller(troubled, db_path).run()
    rejected = queries.proposals(db_path, "nw_renewal")[0]
    at = rejected["decided_minute"] + 4 * 60  # more ticks later than the "recent decisions" keep

    answer = queries.why(db_path, "nw_renewal", at)

    assert answer["config_version"] == 1
    assert ("rejected", FIRST) in [(d["outcome"], d["proposal_id"]) for d in answer["decisions"]]
    hold = answer["decisions"][-1]["detail"]["hold"]
    assert cli_main(["why", "nw_renewal", "--at", f"D{at // 1440} {at % 1440 // 60:02d}:{at % 60:02d}",
                     "--db", str(db_path)]) == 0
    assert hold in capsys.readouterr().out


def run_to_an_applied_change(world, db_path):
    client, troubled, _ = degraded_world(world, days=2)
    Controller(troubled, db_path).run()
    return client, queries.proposals(db_path, "nw_renewal")[0]


def test_why_explains_the_change_in_force_from_the_store(world, db_path):
    client, proposal = run_to_an_applied_change(world, db_path)
    at = proposal["applied_minute"] + 60

    answer = queries.why(db_path, "nw_renewal", at)

    assert answer["config_version"] == 2
    assert answer["change"]["actor"] == "controller_approved" and answer["change"]["changed_dims"] == ["caller_id"]
    assert answer["proposal"]["proposal_id"] == FIRST and answer["proposal"]["status"] == "applied"
    assert answer["proposal"]["diff"]["caller_id"]["default"] == {"from": {"D1": 0.5, "D2": 0.5}, "to": {"D1": 1.0}}
    assert answer["evidence"]["callers"]["D2"]["degraded"] is True
    assert answer["approval"]["status"] == "approved" and answer["approval"]["approval_id"] == proposal["approval_id"]
    aftermath = answer["aftermath"]
    assert aftermath["under_this_version"]["dials"] > 0 and aftermath["under_previous_version"]["dials"] > 0
    assert "D2" not in aftermath["under_this_version"]["pickup_by_caller_id"]
    assert "D2" in aftermath["under_previous_version"]["pickup_by_caller_id"]
    assert aftermath["next_change"] is None
    assert [d["outcome"] for d in answer["decisions"] if d["proposal_id"] == FIRST][-1] == "applied"


def test_why_before_any_change_shows_the_default_config_and_the_decisions_not_to_act(world, db_path):
    _, proposal = run_to_an_applied_change(world, db_path)

    answer = queries.why(db_path, "nw_renewal", proposal["created_minute"] - 15)

    assert answer["config_version"] == 1 and answer["change"]["actor"] == "business_default"
    assert answer["proposal"] is None and answer["approval"] is None
    assert answer["aftermath"]["next_change"]["version"] == 2
    assert answer["decisions"] and answer["decisions"][-1]["outcome"] in ("healthy", "held")


def calls_by_lead(client) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for ev in all_events(client):
        if ev["type"] == "call" and ev["workflow_id"] == "nw_renewal":
            out.setdefault(ev["lead_id"], []).append(ev)
    return out


def test_heard_shows_each_calls_exact_variant_and_why_that_config_was_live(world, db_path):
    client, proposal = run_to_an_applied_change(world, db_path)
    leads = calls_by_lead(client)
    after = next(l for l, calls in leads.items() if calls[0]["config_version"] == 2)
    before = next(l for l, calls in leads.items() if calls[-1]["config_version"] == 1)

    heard_after, heard_before = queries.heard(db_path, after), queries.heard(db_path, before)

    assert [c["variant"] for c in heard_after["calls"]] == [e["variant"] for e in leads[after]]
    live = heard_after["calls"][0]["config"]
    assert live["version"] == 2 and live["actor"] == "controller_approved"
    assert live["proposal_id"] == FIRST and live["approval_id"] == proposal["approval_id"]
    assert "Move traffic off caller ID D2" in live["change_summary"]
    assert heard_before["calls"][0]["config"] == {"version": 1, "actor": "business_default", "applied_at": None,
                                                  "changed_dims": [], "proposal_id": None, "approval_id": None,
                                                  "change_summary": None}


def test_why_and_heard_answer_on_the_command_line(world, db_path, capsys):
    client, proposal = run_to_an_applied_change(world, db_path)
    lead = next(l for l, calls in calls_by_lead(client).items() if calls[0]["config_version"] == 2)

    at = proposal["applied_minute"] + 60
    assert cli_main(["why", "nw_renewal", "--at", f"D{at // 1440} {at % 1440 // 60:02d}:{at % 60:02d}",
                     "--db", str(db_path)]) == 0
    why_out = capsys.readouterr().out
    assert cli_main(["heard", lead, "--db", str(db_path)]) == 0
    heard_out = capsys.readouterr().out

    for expected in (FIRST, "controller_approved", "Move traffic off caller ID D2", proposal["approval_id"], "aftermath"):
        assert expected in why_out
    assert FIRST in heard_out and lead in heard_out and "caller_id" in heard_out


def test_why_refuses_a_workflow_or_time_it_cannot_answer_for(world, db_path, capsys):
    run_to_an_applied_change(world, db_path)

    assert cli_main(["why", "no_such_workflow", "--at", "D1 12:00", "--db", str(db_path)]) == 1
    assert "no_such_workflow" in capsys.readouterr().err
    assert cli_main(["why", "nw_renewal", "--at", "yesterday-ish", "--db", str(db_path)]) == 1


@pytest.mark.slow
def test_on_public_02_a_degraded_caller_id_is_found_and_moved_off_through_approval(world, db_path):
    client = world("public_02_incidents", days=10, history_days=14)
    troubled, _ = troubled_client(client, approval_only=True)

    Controller(troubled, db_path).run()

    workflows = [w["workflow_id"] for w in client.get("/workflows").json()]
    moved = [(wf, p) for wf in workflows for p in queries.proposals(db_path, wf)
             if p["status"] == "applied" and p["kind"] == "caller_id"]
    assert moved
    wf, proposal = moved[0]
    degraded = [c for c, e in proposal["evidence"]["callers"].items() if e["degraded"]]
    assert degraded and not set(degraded) & set(proposal["proposed_config"]["allocations"]["default"]["caller_id"])
    assert proposal["impact_score"] >= 0.9 and proposal["affected_leads"] > 0 and proposal["dry_run"]["overall"]
    applied = next(h for h in config_history(client, wf) if h["proposal_id"] == proposal["proposal_id"])
    assert applied["actor"] == "controller_approved" and applied["changed_dims"] == ["caller_id"]
    approval = next(a for a in approvals(client, wf) if a["proposal_id"] == proposal["proposal_id"])
    # A decision shows up at the tick boundary after the one it falls before, so the first tick that can see
    # it is under 30 minutes on. Waiting one tick more would take this to 30 or beyond.
    assert 0 <= sim_minute(applied["applied_at"]) - sim_minute(approval["decided_at"]) < 30
    assert_requests_obey(approvals(client, "ks_cancel"), earliest_hour=11, last_hour_exclusive=17, cap=2)
    assert_requests_obey(approvals(client, "nw_renewal"), earliest_hour=10, last_hour_exclusive=17, cap=3)
    assert_requests_obey(approvals(client, "nw_kyc"), earliest_hour=10, last_hour_exclusive=16, cap=2)
