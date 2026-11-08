"""Ticket 08: confirm and promotion, through Seam 1.

The world is the real simulator with engagement rewritten by voice (`Trouble`), so a winner, a loser and a
tie can be had on demand without touching a scenario. The controller is read only through its query interface.
"""
from __future__ import annotations

import math

import pytest

from controller import queries, stats
from controller.__main__ import main as cli_main
from controller.loop import Controller

from .harness import Killed, all_events, troubled_client
from .test_auto_apply import sim_minute
from .test_governance import supervisor_edit

WF, KS = "nw_renewal", "ks_cancel"
CONFIRM, PROMOTE, END = "experiment_confirm", "experiment_promote", "experiment_end"
Z80 = 0.8416


def favour_v3(trouble, workflow: str = WF) -> None:
    """V3 clearly better than control, the other challengers clearly worse, voice the only dimension in play."""
    trouble.voice_only = True
    trouble.steady_pickup.add(workflow)
    trouble.engagement[workflow] = {"V1": 0.80, "V2": 0.10, "V3": 0.95, "V4": 0.10}


def experiment(db_path, workflow: str = WF) -> dict:
    return queries.experiments(db_path, workflow)[0]


def proposals(db_path, kind: str, workflow: str = WF) -> list[dict]:
    return [p for p in queries.proposals(db_path, workflow) if p["kind"] == kind]


def outcomes(db_path, workflow: str = WF) -> list[dict]:
    return queries.decisions(db_path, workflow, "experiment")


def default_table(client, dim: str, workflow: str = WF) -> dict[str, float]:
    return client.get(f"/workflows/{workflow}/config").json()["config"]["allocations"]["default"][dim]


def step_until(controller, db_path, status: str, workflow: str = WF) -> None:
    while not queries.experiments(db_path, workflow) or experiment(db_path, workflow)["status"] != status:
        assert not controller.step(), f"the scenario ended before the experiment reached {status}"


def voice_arms(client, workflow: str, first_version: int, before_hour: int) -> dict[str, dict[str, int]]:
    """Dials, connected and engaged per voice, straight from the world's events."""
    arms: dict[str, dict[str, int]] = {}
    for e in all_events(client):
        if (e["type"] == "call" and e["workflow_id"] == workflow and e["config_version"] >= first_version
                and _hour(e["ts"]) < before_hour):
            arm = arms.setdefault(e["variant"]["voice"], {"dials": 0, "connected": 0, "engaged": 0})
            arm["dials"] += 1
            arm["connected"] += e["connected"]
            arm["engaged"] += e["engaged"]
    return arms


def _hour(ts: str) -> int:
    return sim_minute(ts) // 60


def lower_bound(better: dict, worse: dict) -> float:
    """80% lower bound on the engagement difference, worked from the definition."""
    a, b = better["engaged"] / better["connected"], worse["engaged"] / worse["connected"]
    return a - b - Z80 * math.sqrt(a * (1 - a) / better["connected"] + b * (1 - b) / worse["connected"])


def test_a_screened_candidate_goes_to_confirm_at_50_50_with_an_honest_proposal(world, db_path):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble)

    Controller(troubled, db_path).run()

    exp = experiment(db_path)
    p = proposals(db_path, CONFIRM)[0]
    assert exp["status"] == "confirming" and exp["candidates"] == {"voice": "V3"}
    assert p["status"] == "applied" and p["route"] == "approval"
    assert default_table(client, "voice") == {"V1": 0.5, "V3": 0.5}
    assert exp["confirm_version"] == p["applied_version"]
    plan = p["evidence"]["confirm"]["voice"]
    assert plan["control"] == "V1" and plan["candidate"] == "V3"
    arms = voice_arms(troubled, WF, exp["screen_version"], p["created_minute"] // 60)
    assert plan["arms"] == {v: arms[v] for v in ("V1", "V3")}
    control = arms["V1"]["engaged"] / arms["V1"]["connected"]
    assert plan["planned_per_arm"] == stats.confirm_sample_size_per_arm(control)
    # Lower bound x pickup (concluded calls per dial) x the share moved onto V3 (a sixth of traffic to half) / 5 points.
    pickup = (arms["V1"]["connected"] + arms["V3"]["connected"]) / (arms["V1"]["dials"] + arms["V3"]["dials"])
    assert p["impact_score"] == pytest.approx(lower_bound(arms["V3"], arms["V1"]) * pickup * (0.5 - 1 / 6) / 0.05, abs=1e-3)
    assert p["affected_leads"] > 0
    overall = p["dry_run"]["overall"]["engagement_rate"]
    assert overall["proposed"] > overall["current"] and overall["margin"] > 0


def test_a_winner_on_fresh_data_is_promoted_leaving_exactly_20_percent_on_control(world, db_path):
    client = world(days=5, history_days=1)  # the guardrail needs about two days of dials to rule out a 2-point drop
    troubled, trouble = troubled_client(client)
    favour_v3(trouble, KS)

    Controller(troubled, db_path).run()

    exp = experiment(db_path, KS)
    p = proposals(db_path, PROMOTE, KS)[0]
    assert exp["status"] == "promoted" and p["status"] == "applied" and p["route"] == "approval"
    assert default_table(client, "voice", KS) == {"V3": 0.8, "V1": 0.2}
    won = next(d for d in outcomes(db_path, KS) if d["outcome"] == "winner")
    verdict = won["detail"]["dimensions"]["voice"]
    assert verdict["status"] == "winner" and verdict["primary"]["passed"] and verdict["guardrail"]["passed"]
    assert verdict["harm"]["harmed_cohorts"] == []
    # Only calls dialled under the confirm config: the screen's calls of V1 and V3 are not in the counts.
    fresh = voice_arms(troubled, KS, exp["confirm_version"], won["minute"] // 60)
    since_screen = voice_arms(troubled, KS, exp["screen_version"], won["minute"] // 60)
    assert verdict["arms"] == fresh and fresh["V1"]["connected"] < since_screen["V1"]["connected"]
    assert set(fresh) == {"V1", "V3"}
    # Lower bound x pickup x the 30 points moved from the 50/50 to the promotion / 5 points.
    pickup = (fresh["V1"]["connected"] + fresh["V3"]["connected"]) / (fresh["V1"]["dials"] + fresh["V3"]["dials"])
    assert p["impact_score"] == pytest.approx(lower_bound(fresh["V3"], fresh["V1"]) * pickup * 0.3 / 0.05, abs=1e-3)
    assert p["affected_leads"] > 0
    overall = p["dry_run"]["overall"]["engagement_rate"]
    assert overall["proposed"] > overall["current"] and "cohorts_at_risk" in p["dry_run"]
    assert p["evidence"]["stage"] == "promotion" and p["evidence"]["holdback_share"] == 0.2


def ended_back_on_control(client, db_path, workflow: str = WF) -> None:
    assert default_table(client, "voice", workflow) == {"V1": 1.0}
    assert proposals(db_path, PROMOTE, workflow) == []
    end = proposals(db_path, END, workflow)[0]
    assert end["status"] == "applied" and end["evidence"]["dimensions"] == ["voice"]


def test_a_candidate_failing_guardrail_non_inferiority_is_never_promoted_and_says_why(world, db_path):
    client = world(days=6, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble)
    controller = Controller(troubled, db_path)
    step_until(controller, db_path, "confirming")
    trouble.muted[WF] = {"V3"}  # one in four of its answered calls never connects: a 6-point pickup drop

    controller.run()

    exp = experiment(db_path)
    assert exp["status"] == "inconclusive" and exp["end_reason"] == "blocked_guardrail"
    blocked = next(d for d in outcomes(db_path) if d["outcome"] == "blocked")
    verdict = blocked["detail"]["dimensions"]["voice"]
    assert verdict["status"] == "blocked_guardrail" and verdict["primary"]["passed"] and not verdict["guardrail"]["passed"]
    assert "pickup_rate" in blocked["detail"]["why"] and "V3" in blocked["detail"]["why"]
    ended_back_on_control(client, db_path)


def test_a_candidate_harming_a_cohort_is_never_promoted_and_the_cohort_is_named(world, db_path):
    client = world(days=5, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble)
    trouble.engagement[WF] = {"V1": 0.60, "V2": 0.60, "V3": 0.98, "V4": 0.60}
    controller = Controller(troubled, db_path)
    controller.start()
    cohorts = queries.status(db_path, WF)["workflows"][0]["partition"]["cohorts"]
    harmed = next(c for c in cohorts if c["id"] != "default")
    trouble.harmed[WF] = ("V3", harmed["where"])

    controller.run()

    exp = experiment(db_path)
    assert exp["status"] == "inconclusive" and exp["end_reason"] == "blocked_harm"
    decided = outcomes(db_path)
    assert "waiting_for_data" in [d["outcome"] for d in decided if d["detail"].get("experiment", {}).get("confirm_version")]
    blocked = next(d for d in decided if d["outcome"] == "blocked")
    verdict = blocked["detail"]["dimensions"]["voice"]
    assert verdict["status"] == "blocked_harm" and verdict["primary"]["passed"]
    assert verdict["harm"]["harmed_cohorts"] == [harmed["id"]] and harmed["id"] in blocked["detail"]["why"]
    assert harmed["id"] in proposals(db_path, CONFIRM)[0]["dry_run"]["cohorts_at_risk"]
    ended_back_on_control(client, db_path)


def test_an_experiment_with_no_decision_after_three_looks_is_inconclusive_and_reverts_to_control(world, db_path):
    client = world(days=6, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble)
    controller = Controller(troubled, db_path)
    step_until(controller, db_path, "confirming")
    trouble.engagement[WF] = {"V1": 0.80, "V3": 0.80}  # the candidate was only ahead in the screen

    controller.run()

    exp = experiment(db_path)
    assert exp["status"] == "inconclusive" and exp["end_reason"] == "no_winner" and exp["confirm_looks"] == 3
    assert [d["outcome"] for d in outcomes(db_path) if d["outcome"] in ("confirm_look", "inconclusive")] == ["confirm_look", "confirm_look", "inconclusive"]
    health = queries.health(db_path)  # an inconclusive experiment is a note, not a page
    assert not [a for a in health["alerts"] if exp["experiment_id"] in a["message"]]
    assert "inconclusive" in {n["outcome"] for n in health["notes"]}
    ended_back_on_control(client, db_path)


def test_a_refused_confirm_ends_the_experiment_and_returns_the_screen_split_to_control(world, db_path):
    client = world(days=4, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble)
    controller = Controller(troubled, db_path)
    step_until(controller, db_path, "screened")
    trouble.refuse("POST", "/approvals", 422, "invalid_request")  # the next request out is this confirm

    controller.run()

    first = experiment(db_path)
    assert first["end_reason"] == "request_rejected" and first["confirm_version"] is None
    end = proposals(db_path, END)[0]
    assert proposals(db_path, PROMOTE) == [] and end["status"] == "applied"
    assert end["proposed_config"]["allocations"]["default"]["voice"] == {"V1": 1.0}  # a new screen may follow it


def test_an_expired_confirm_is_rebuilt_from_fresh_data_as_a_new_proposal(world, db_path):
    client = world(days=4, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble)
    controller = Controller(troubled, db_path)
    step_until(controller, db_path, "screened")
    trouble.ttl_hours = 0.25  # expires before an approver sees it
    while not proposals(db_path, CONFIRM) or proposals(db_path, CONFIRM)[0]["status"] != "expired":
        controller.step()
    trouble.ttl_hours = None

    controller.run()

    *_, first, second = proposals(db_path, CONFIRM)
    assert first["status"] == "expired" and second["replaces"] == first["proposal_id"] and second["status"] == "applied"
    assert experiment(db_path)["status"] == "confirming"
    connected = [p["evidence"]["confirm"]["voice"]["arms"]["V1"]["connected"] for p in (first, second)]
    assert connected[1] > connected[0]


def test_a_supervisor_edit_to_the_candidate_during_confirm_aborts_it_and_closes_its_data_at_the_edit(world, db_path):
    client = world(days=4, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble, "ks_cancel")
    controller = Controller(troubled, db_path)
    step_until(controller, db_path, "confirming", "ks_cancel")
    for _ in range(12):
        controller.step()
    version = supervisor_edit(client, "ks_cancel", lambda cfg: cfg["allocations"]["default"].update(voice={"V1": 0.6, "V3": 0.4}))
    edited_hour = client.get("/sim/state").json()["sim_minute"] // 60

    controller.run()

    exp = experiment(db_path, "ks_cancel")
    assert exp["status"] == "aborted" and exp["end_reason"] == "supervisor_edit"
    abort = next(d for d in outcomes(db_path, "ks_cancel") if d["outcome"] == "aborted")
    arms = abort["detail"]["dimensions"]["voice"]["arms"]
    recount = voice_arms(troubled, "ks_cancel", exp["confirm_version"], edited_hour)
    assert arms == {v: {"connected": recount[v]["connected"], "engaged": recount[v]["engaged"]} for v in ("V1", "V3")}
    assert abort["detail"]["edit"]["version"] == version and default_table(client, "voice", "ks_cancel") == {"V1": 0.6, "V3": 0.4}
    assert proposals(db_path, PROMOTE, "ks_cancel") == []


def test_a_kill_right_after_asking_for_confirm_never_sends_it_twice(world, db_path):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble)
    controller = Controller(troubled, db_path)
    step_until(controller, db_path, "screened")
    trouble.kill("POST", "/approvals", after_forward=True)

    with pytest.raises(Killed):
        controller.run()
    assert experiment(db_path)["status"] == "screened" and proposals(db_path, CONFIRM)

    Controller(troubled, db_path).run()

    sent = [a["proposal_id"] for a in client.get("/approvals", params={"workflow_id": WF}).json() if CONFIRM in a["proposal_id"]]
    assert sent == [f"{WF}-{CONFIRM}-1"] and experiment(db_path)["status"] == "confirming"


def test_affected_leads_honour_next_campaign_only(world, db_path):
    client = world(days=1, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.primary["nw_kyc"] = "engagement_rate"  # the only next_campaign_only workflow, shown as engagement-primary

    Controller(troubled, db_path).run()

    locked = [p for p in queries.proposals(db_path, "nw_kyc") if p["kind"] == "experiment_screen"][0]
    running = [p for p in queries.proposals(db_path, WF) if p["kind"] == "experiment_screen"][0]
    assert locked["affected_leads"] == client.get("/workflows/nw_kyc").json()["daily_batch"]["size"]  # tomorrow's campaign only
    assert running["affected_leads"] > client.get(f"/workflows/{WF}").json()["daily_batch"]["size"]   # and everyone mid-campaign


def test_why_shows_the_confirm_verdict(world, db_path, capsys):
    client = world(days=5, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble, KS)
    Controller(troubled, db_path).run()
    won = next(d for d in outcomes(db_path, KS) if d["outcome"] == "winner")

    after = queries.why(db_path, KS, won["minute"])

    assert after["experiment"]["status"] == "confirmed" and after["experiment"]["verdicts"]["voice"]["status"] == "winner"
    assert cli_main(["why", KS, "--at", "D4 12:00", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "voice V3: winner" in out
