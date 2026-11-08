"""Ticket 06: hallucination incidents, driven through Seam 1 against the real simulator."""
from __future__ import annotations

import pytest

from controller import queries, simtime
from controller.__main__ import main as cli_main
from controller.credits import RESERVE, Credits, ReserveHeld
from controller.loop import Controller
from controller.registry import Overlay
from controller.simclient import SimClient

from .harness import Killed, troubled_client

WF = "nw_renewal"


def hallucinating_world(world, flags: dict[str, float], workflow: str = WF, days: int = 3):
    """`flags`: prompt -> the fraction of its answered calls that get flagged, on live calls only."""
    client = world(days=days, history_days=2)
    troubled, trouble = troubled_client(client, approval_only=True)
    trouble.hallucinate[workflow] = flags
    return client, troubled, trouble


def policy(client, workflow: str = WF) -> dict:
    return client.get(f"/workflows/{workflow}").json()["governance"]


def live_prompt(client, workflow: str = WF) -> str:
    return client.get(f"/workflows/{workflow}/config").json()["config"]["prompt_version"]


def test_a_hallucinating_prompt_is_replaced_by_a_validated_fix_and_the_fix_is_verified(world, db_path):
    client, troubled, trouble = hallucinating_world(world, {"P1": 0.25})

    Controller(troubled, db_path).run()

    [incident] = queries.incidents(db_path, WF)
    assert incident["status"] == "verified" and incident["bad_prompt"] == "P1"
    assert incident["detected_minute"] >= incident["onset_minute"]
    [fix] = [p for p in queries.proposals(db_path, WF) if p["kind"] == "hallucination" and p["status"] == "applied"]
    assert live_prompt(client) == fix["proposed_config"]["prompt_version"] == incident["fix_prompt"] != "P1"
    assert fix["impact_score"] >= 0.9 and fix["affected_leads"] > 0
    assert fix["evidence"]["validation_run_id"]
    assert fix["evidence"]["validation"]["pass_rate"] >= policy(client)["min_validation_pass_rate"]
    assert fix["evidence"]["validation"]["n"] == 300
    assert trouble.requests["/llm/suggest-fixes"] == 1
    assert incident["minutes_onset_to_applied"] == fix["applied_minute"] - incident["onset_minute"]


def test_the_audit_shows_how_long_the_fix_took_from_onset(world, db_path, capsys):
    _, troubled, _ = hallucinating_world(world, {"P1": 0.25})
    Controller(troubled, db_path).run()
    [incident] = queries.incidents(db_path, WF)

    answer = queries.why(db_path, WF, incident["applied_minute"])

    assert answer["incident"]["incident_id"] == incident["incident_id"]
    assert answer["incident"]["minutes_onset_to_applied"] == incident["applied_minute"] - incident["onset_minute"]
    assert cli_main(["why", WF, "--at", "D2 23:45", "--db", str(db_path)]) == 0
    assert f"{incident['minutes_onset_to_applied']} minutes after onset" in capsys.readouterr().out


def test_a_fix_that_does_not_bring_the_flag_rate_down_escalates_and_is_not_tried_again(world, db_path):
    every_prompt = {p: 0.25 for p in ("P1", "P3", "P4", "P5")}
    _, troubled, trouble = hallucinating_world(world, every_prompt)

    Controller(troubled, db_path).run()

    [incident] = queries.incidents(db_path, WF)
    assert incident["status"] == "escalated" and incident["end_reason"] == "not_recovered"
    assert trouble.requests["/llm/suggest-fixes"] == 1
    assert len([p for p in queries.proposals(db_path, WF) if p["kind"] == "hallucination"]) == 1


def test_killed_before_the_fix_is_sent_restarts_without_paying_again_or_sending_twice(world, db_path):
    client, troubled, trouble = hallucinating_world(world, {"P1": 0.25})
    trouble.kill("POST", "/approvals")

    with pytest.raises(Killed):
        Controller(troubled, db_path).run()
    paid = client.get("/sim/state").json()["credits_remaining"]
    runs = trouble.requests["/validation/runs"]
    assert runs >= 1

    Controller(troubled, db_path).run()

    assert client.get("/sim/state").json()["credits_remaining"] == paid and trouble.requests["/validation/runs"] == runs
    sent = client.get("/approvals", params={"workflow_id": WF}).json()
    assert [a["proposal_id"] for a in sent if "hallucination" in a["proposal_id"]] == [f"{WF}-hallucination-1"]
    assert queries.incidents(db_path, WF)[0]["status"] == "verified"


def test_killed_after_validating_but_before_recording_the_proposal_does_not_validate_again(world, db_path):
    client, troubled, trouble = hallucinating_world(world, {"P1": 0.25})
    trouble.kill("GET", "/config/history")  # governance reads it while recording the Proposal

    with pytest.raises(Killed):
        Controller(troubled, db_path).run()
    assert all("validation_run_id" in c for c in queries.incidents(db_path, WF)[0]["candidates"])
    runs = trouble.requests["/validation/runs"]
    assert runs >= 1

    Controller(troubled, db_path).run()

    assert trouble.requests["/validation/runs"] == runs and trouble.requests["/llm/suggest-fixes"] == 1
    assert queries.incidents(db_path, WF)[0]["status"] == "verified"


def test_a_preference_overlay_that_would_delay_the_fix_is_set_aside_and_the_summary_says_why(world, db_path):
    client, troubled, _ = hallucinating_world(world, {"P1": 0.25})
    tenant = client.get(f"/workflows/{WF}").json()["tenant_id"]

    Controller(troubled, db_path, overlays={tenant: Overlay(not_before_hour=16)}).run()

    fix = next(p for p in queries.proposals(db_path, WF) if p["kind"] == "hallucination")
    assert fix["sent_minute"] % 1440 < 16 * 60
    assert "client's preference" in fix["change_summary"]


def test_a_workflow_with_no_spike_raises_no_incident_and_spends_no_credits(world, db_path):
    client, troubled, trouble = hallucinating_world(world, {}, days=2)

    Controller(troubled, db_path).run()

    assert queries.incidents(db_path, WF) == []
    assert client.get("/sim/state").json()["credits_remaining"] == 200
    assert trouble.requests["/llm/suggest-fixes"] == trouble.requests["/validation/runs"] == 0
    assert "below_threshold" in [d["outcome"] for d in queries.decisions(db_path, WF, "hallucination")]


def test_only_an_incident_may_spend_into_the_credit_reserve(world):
    client = world(days=1, history_days=1)
    credits = Credits(SimClient(client))
    left = lambda: client.get("/sim/state").json()["credits_remaining"]

    with pytest.raises(ReserveHeld):
        while True:
            credits.validate_prompt(WF, "P1", 1000)
    assert RESERVE <= left() < RESERVE + 10

    credits.validate_prompt(WF, "P1", 1000, incident=True)
    assert left() < RESERVE
    with pytest.raises(ReserveHeld):
        credits.suggest_fixes(WF, "still not an incident")


@pytest.mark.slow
def test_on_public_02_the_bad_prompt_is_detected_and_a_validated_revert_is_applied_and_verified(world, db_path):
    client = world("public_02_incidents", days=10, history_days=14)

    Controller(client, db_path).run()

    workflows = [w["workflow_id"] for w in client.get("/workflows").json()]
    incidents = [(wf, i) for wf in workflows for i in queries.incidents(db_path, wf)]
    wf, incident = next((wf, i) for wf, i in incidents if (i["cause"]["prompt_change"] or {}).get("actor") == "supervisor")
    change = incident["cause"]["prompt_change"]
    assert incident["status"] == "verified"
    assert incident["fix_prompt"] == incident["cause"]["known_good"] == change["from"]
    assert incident["minutes_onset_to_applied"] is not None
    fix = next(p for p in queries.proposals(db_path, wf) if p["kind"] == "hallucination" and p["status"] == "applied")
    assert f"Reverts the {simtime.label(change['minute'])} supervisor edit {change['from']} -> {change['to']} because" in fix["change_summary"]
    assert fix["impact_score"] >= 0.9
    assert fix["evidence"]["validation"]["pass_rate"] >= policy(client, wf)["min_validation_pass_rate"]
    assert fix["evidence"]["validation"]["n"] == 300 and fix["evidence"]["validation_run_id"]
    assert [h["changed_dims"] for h in client.get(f"/workflows/{wf}/config/history").json()
            if h["proposal_id"] == fix["proposal_id"]] == [["prompt_version"]]
    assert {c["source"] for c in incident["candidates"]} == {"known_good"}  # the LLM is only for an incident with no known-good prompt
