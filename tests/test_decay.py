"""Ticket 09: decay against the holdback, rollback and holdback release, through Seam 1.

The world is the real simulator with engagement rewritten by voice (`Trouble`), so a winner can be made to
decay on demand. The controller is read only through its query interface.
"""
from __future__ import annotations

import pytest

from controller import queries, stats
from controller.loop import Controller

from .harness import Killed, all_events, troubled_client
from .test_auto_apply import sim_minute
from .test_confirm import KS, default_table, experiment, favour_v3, lower_bound, outcomes, proposals, step_until
from .test_governance import supervisor_edit

ROLLBACK = "experiment_rollback"
DAY = 24 * 60


def window_arms(client, workflow: str, first_version: int, first_hour: int, before_hour: int) -> dict[str, dict[str, int]]:
    """Dials, connected and engaged per voice over an hour window, straight from the world's events."""
    arms: dict[str, dict[str, int]] = {}
    for e in all_events(client):
        if (e["type"] == "call" and e["workflow_id"] == workflow and e["config_version"] >= first_version
                and first_hour <= sim_minute(e["ts"]) // 60 < before_hour):
            arm = arms.setdefault(e["variant"]["voice"], {"dials": 0, "connected": 0, "engaged": 0})
            arm["dials"] += 1
            arm["connected"] += e["connected"]
            arm["engaged"] += e["engaged"]
    return arms


def test_a_winner_worse_than_its_holdback_is_rolled_back_through_governance_with_the_evidence(world, db_path):
    client = world(days=9, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble, KS)
    controller = Controller(troubled, db_path)
    step_until(controller, db_path, "promoted", KS)
    trouble.engagement[KS] = {"V1": 0.80, "V3": 0.70}
    trouble.kill("POST", "/approvals", after_forward=True)  # the next request out is the rollback
    trouble.blank_summary = True  # so the approver must turn it down

    with pytest.raises(Killed):
        controller.run()
    controller = Controller(troubled, db_path)
    while not proposals(db_path, ROLLBACK, KS) or proposals(db_path, ROLLBACK, KS)[0]["status"] != "rejected":
        assert not controller.step()
    trouble.blank_summary = False
    controller.run()

    sent = [a["proposal_id"] for a in client.get("/approvals", params={"workflow_id": KS}).json() if ROLLBACK in a["proposal_id"]]
    assert len(sent) == len(set(sent)) and sent[0] == f"{KS}-{ROLLBACK}-1"  # the crash didn't send it twice
    exp = experiment(db_path, KS)
    first, *_, p = proposals(db_path, ROLLBACK, KS)  # refused, the winner kept its traffic and was judged again on fresh data
    assert first["status"] == "rejected" and first["evidence"]["window"] != p["evidence"]["window"]
    assert exp["status"] == "rolled_back" and exp["end_reason"] == "decayed"
    assert p["status"] == "applied" and p["route"] == "approval"
    assert default_table(client, "voice", KS) == {"V1": 1.0}

    # The audit tells the story, and the first look is the one with a full three days behind it.
    told = [d["outcome"] for d in outcomes(db_path, KS)]
    assert told.index("decayed") < told.index("rollback_requested") < told.index("rolled_back")
    signals = [d for d in outcomes(db_path, KS) if d["outcome"] == "decayed"]
    assert signals[0]["minute"] // DAY == exp["promote_minute"] // DAY + 3
    signal = next(d for d in signals if d["minute"] == p["evidence"]["signal_minute"])

    # The evidence is the last 72 hours of calls under the promoted config, recounted from the world's events.
    evidence = p["evidence"]
    hour = signal["minute"] // 60
    arms = window_arms(troubled, KS, exp["promote_version"], hour - 72, hour)
    voice = evidence["dimensions"]["voice"]
    assert evidence["stage"] == "rollback" and voice["winner"] == "V3" and voice["control"] == "V1"
    assert voice["arms"] == arms
    won, held = (stats.Counts(arms[v]["engaged"], arms[v]["connected"]) for v in ("V3", "V1"))
    assert voice["p_value"] == pytest.approx(stats.decayed(won, held).p_value) and voice["p_value"] < 0.05
    # Lower bound on what going back recovers x pickup x the 80% of traffic it moves / 5 points.
    pickup = (arms["V1"]["connected"] + arms["V3"]["connected"]) / (arms["V1"]["dials"] + arms["V3"]["dials"])
    assert p["impact_score"] == pytest.approx(lower_bound(arms["V1"], arms["V3"]) * pickup * 0.8 / 0.05, abs=1e-3)


def test_a_clean_holdback_is_released_only_inside_the_next_proposal(world, db_path):
    client = world(days=14, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble, KS)
    controller = Controller(troubled, db_path)
    step_until(controller, db_path, "promoted", KS)
    promoted = experiment(db_path, KS)["promote_minute"]

    # A full day past the seven clean ones, with nothing else to ask for on this workflow.
    while client.get("/sim/state").json()["sim_minute"] < promoted + 8 * DAY:
        assert not controller.step()

    assert experiment(db_path, KS)["status"] == "promoted"
    assert default_table(client, "voice", KS) == {"V3": 0.8, "V1": 0.2}
    assert [p["proposal_id"] for p in queries.proposals(db_path, KS) if p["created_minute"] > promoted] == []
    assert "no_decay" in [d["outcome"] for d in outcomes(db_path, KS)]

    trouble.voice_only = False  # after a restart the controller can see spare caller IDs, so it has something else to ask
    Controller(troubled, db_path).run()

    exp = experiment(db_path, KS)
    carrier = next(p for p in queries.proposals(db_path, KS) if "holdback_release" in p["evidence"])
    assert exp["status"] == "released" and exp["end_reason"] == "clean_holdback"
    assert default_table(client, "voice", KS) == {"V3": 1.0}
    assert carrier["kind"] == "caller_id_explore" and carrier["status"] == "applied"
    assert carrier["created_minute"] >= promoted + 7 * DAY
    assert carrier["proposed_config"]["allocations"]["default"]["voice"] == {"V3": 1.0}
    assert carrier["diff"]["voice"]["default"] == {"from": {"V1": 0.2, "V3": 0.8}, "to": {"V3": 1.0}} and "caller_id" in carrier["diff"]
    assert "holdback" in carrier["change_summary"] and "7 days" in carrier["rationale"]
    assert carrier["evidence"]["holdback_release"]["dimensions"] == {"voice": {"winner": "V3", "control": "V1"}}
    assert [p for p in queries.proposals(db_path, KS) if p["kind"] == ROLLBACK] == []


def test_a_supervisor_edit_after_promotion_ends_the_comparison_and_is_never_rolled_back(world, db_path):
    client = world(days=8, history_days=1)
    troubled, trouble = troubled_client(client)
    favour_v3(trouble, KS)
    controller = Controller(troubled, db_path)
    step_until(controller, db_path, "promoted", KS)
    trouble.engagement[KS] = {"V1": 0.80, "V3": 0.70}  # without the edit, this decays by D7
    supervisor_edit(client, KS, lambda cfg: cfg["allocations"]["default"].update(voice={"V1": 0.4, "V3": 0.6}))

    controller.run()

    exp = experiment(db_path, KS)
    assert exp["status"] == "aborted" and exp["end_reason"] == "supervisor_edit"
    assert proposals(db_path, ROLLBACK, KS) == []
    assert default_table(client, "voice", KS) == {"V1": 0.4, "V3": 0.6}
    assert not [d for d in outcomes(db_path, KS) if d["outcome"] in ("decayed", "no_decay")]
