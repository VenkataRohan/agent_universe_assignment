"""Ticket 05: the auto-apply path and caller-ID exploration, through Seam 1.

The real simulator's config history is the oracle for who changed what; the controller is read
only through its query interface. One exception: nothing proposes a voice change until experiments
exist, so the customer-facing boundary is also asked directly of the governance client.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from controller import queries
from controller.__main__ import main as cli_main
from controller.composer import compose, largest_shift
from controller.governance import GovernanceClient
from controller.loop import Controller
from controller.simclient import SimClient
from controller.store import Store

from .harness import Killed, all_events, troubled_client
from .test_governance import supervisor_edit


def config_history(client, workflow: str) -> list[dict]:
    return client.get(f"/workflows/{workflow}/config/history").json()


def approvals(client, workflow: str) -> list[dict]:
    return client.get("/approvals", params={"workflow_id": workflow}).json()


def caller_ids(client, workflow: str) -> dict[str, float]:
    return client.get(f"/workflows/{workflow}/config").json()["config"]["allocations"]["default"]["caller_id"]


def auto_applied(client, workflow: str) -> list[dict]:
    return [h for h in config_history(client, workflow) if h["actor"] == "controller_auto"]


def test_a_degraded_caller_id_is_auto_applied_off_where_policy_allows(world, db_path):
    client = world(days=2, history_days=1)
    troubled, _ = troubled_client(client, degrade={"nw_renewal": "D2"})

    Controller(troubled, db_path).run()

    assert ("nw_renewal-caller_id-1", ["caller_id"]) in [(h["proposal_id"], h["changed_dims"]) for h in auto_applied(client, "nw_renewal")]
    assert "D2" not in caller_ids(client, "nw_renewal")
    assert [a for a in approvals(client, "nw_renewal") if "-caller_id-" in a["proposal_id"]] == []
    moved = next(p for p in queries.proposals(db_path, "nw_renewal") if p["proposal_id"] == "nw_renewal-caller_id-1")
    assert moved["status"] == "applied" and moved["route"] == "auto" and moved["approval_id"] is None


def proposals_of(db_path, workflow: str, kind: str = "caller_id") -> list[dict]:
    return [p for p in queries.proposals(db_path, workflow) if p["kind"] == kind]


def set_voice(config: dict) -> None:
    config["allocations"]["default"]["voice"] = {"V1": 0.9, "V2": 0.1}


def test_a_customer_facing_change_is_never_auto_applied_even_where_the_policy_would_allow_it(world, db_path):
    client = world(days=1, history_days=1)
    workflows = client.get("/workflows").json()
    policy = next(w for w in workflows if w["workflow_id"] == "ks_cancel")["governance"]["auto_apply"]
    assert policy["enabled"] and "voice" in policy["allowed_dims"]  # the latitude this test refuses to use
    relaxed = [{**w, "tenant_id": "no_overlay"} for w in workflows]  # so only ADR-0002 can be what refuses it
    governance = GovernanceClient(Store(db_path), SimClient(client), relaxed)
    composed = compose(client.get("/workflows/ks_cancel/config").json(), set_voice)

    assert largest_shift(composed) == pytest.approx(0.1)
    assert "voice" in governance.auto_apply_refusal("ks_cancel", "experiment", composed, 600)
    assert "voice" in governance.auto_apply_refusal("ks_cancel", "experiment", composed, 600, incident=True)


def test_kestrel_gets_one_labelled_incident_step_within_max_shift_and_the_rest_goes_to_approval(world, db_path):
    client = world(days=2, history_days=1)
    troubled, _ = troubled_client(client, degrade={"ks_cancel": "D2"})

    Controller(troubled, db_path).run()

    step, *rest = proposals_of(db_path, "ks_cancel")
    assert step["route"] == "auto" and step["status"] == "applied" and step["incident_response"] is True
    assert step["change_summary"].startswith("[Incident response]")
    assert step["proposed_config"]["allocations"]["default"]["caller_id"]["D2"] == pytest.approx(0.3, abs=1e-4)
    assert [h["proposal_id"] for h in auto_applied(client, "ks_cancel")] == [step["proposal_id"]]
    assert rest and {p["route"] for p in rest} == {"approval"}
    assert all(p["sent_minute"] % 1440 >= 11 * 60 for p in rest if p["sent_minute"] is not None)
    assert rest[0]["proposed_config"]["allocations"]["default"]["caller_id"] == {"D1": 1.0}


def test_a_403_from_auto_apply_becomes_an_approval_path_proposal_and_both_are_in_the_audit(world, db_path):
    client = world(days=2, history_days=1)
    troubled, trouble = troubled_client(client, degrade={"nw_renewal": "D2"})
    trouble.no_experiments = True
    trouble.refuse("PUT", "/workflows/nw_renewal/config", 403, "auto_apply_not_permitted")

    Controller(troubled, db_path).run()

    all_proposals = queries.proposals(db_path, "nw_renewal")
    refused = next(p for p in all_proposals if p["status"] == "refused")
    fallback = next(p for p in all_proposals if p["replaces"] == refused["proposal_id"])
    assert refused["route"] == "auto" and fallback["route"] == "approval"
    assert "was refused auto-apply" in fallback["change_summary"]
    assert fallback["proposal_id"] in [a["proposal_id"] for a in approvals(client, "nw_renewal")]
    outcomes = [(d["outcome"], d["proposal_id"]) for d in queries.decisions(db_path, "nw_renewal", "proposal")]
    assert ("auto_apply_not_permitted", refused["proposal_id"]) in outcomes
    answer = queries.why(db_path, "nw_renewal", fallback["created_minute"])
    assert ("auto_apply_not_permitted", refused["proposal_id"]) in [(d["outcome"], d["proposal_id"]) for d in answer["decisions"]]


def test_any_hard_refusal_of_an_auto_apply_falls_back_once_instead_of_retrying_every_tick(world, db_path):
    client = world(days=2, history_days=1)
    troubled, trouble = troubled_client(client, degrade={"nw_renewal": "D2"})
    trouble.no_experiments = True
    trouble.refuse("PUT", "/workflows/nw_renewal/config", 422, "invalid_config")

    Controller(troubled, db_path).run()

    all_proposals = queries.proposals(db_path, "nw_renewal")
    refused = [p for p in all_proposals if p["status"] == "refused"]
    assert len(refused) == 1 and refused[0]["route"] == "auto"
    assert next(p for p in all_proposals if p["replaces"] == refused[0]["proposal_id"])["route"] == "approval"
    assert ("invalid_config", refused[0]["proposal_id"]) in [(d["outcome"], d["proposal_id"]) for d in queries.decisions(db_path, "nw_renewal", "proposal")]


@pytest.mark.parametrize("after_forward", [False, True])
def test_a_kill_around_an_auto_apply_applies_it_exactly_once_on_restart(world, db_path, after_forward):
    client = world(days=2, history_days=1)
    troubled, trouble = troubled_client(client, degrade={"nw_renewal": "D2"})
    trouble.kill("PUT", "/workflows/nw_renewal/config", after_forward=after_forward)

    with pytest.raises(Killed):
        Controller(troubled, db_path).run()
    interrupted = next(p for p in queries.proposals(db_path, "nw_renewal") if p["status"] == "applying")

    Controller(troubled, db_path).run()

    assert [h["proposal_id"] for h in auto_applied(client, "nw_renewal")].count(interrupted["proposal_id"]) == 1
    final = next(p for p in queries.proposals(db_path, "nw_renewal") if p["proposal_id"] == interrupted["proposal_id"])
    assert final["status"] == "applied"


EXPLORE = "caller_id_explore"
OPERATIONAL = {"schedule", "caller_id"}


def sim_minute(ts: str) -> int:
    t = datetime.fromisoformat(ts)
    return (t.date() - date(2027, 1, 4)).days * 1440 + t.hour * 60 + t.minute


def share(proposal: dict, caller: str) -> float:
    return proposal["proposed_config"]["allocations"]["default"]["caller_id"].get(caller, 0)


def dials_before(client, workflow: str, caller: str, minute: int) -> int:
    return sum(1 for e in all_events(client) if e["type"] == "call" and e["workflow_id"] == workflow
               and e["variant"]["caller_id"] == caller and sim_minute(e["ts"]) < minute)


def explored_world(world, **trouble_attrs):
    client = world(days=2, history_days=1)
    troubled, trouble = troubled_client(client)
    for name, value in trouble_attrs.items():
        setattr(trouble, name, value)
    return client, troubled


def test_an_untested_caller_id_gets_ten_percent_until_it_has_300_dials_then_an_equal_share(world, db_path):
    client, troubled = explored_world(world, lift={"nw_renewal": "D3"})

    Controller(troubled, db_path).run()

    started, ended, *_ = proposals_of(db_path, "nw_renewal", EXPLORE)
    assert (started["route"], started["status"], started["evidence"]["action"]) == ("auto", "applied", "start")
    assert share(started, "D3") == pytest.approx(0.1, abs=1e-6)
    assert ended["evidence"]["action"] == "equal_share" and ended["status"] == "applied"
    assert share(ended, "D3") == pytest.approx(1 / 3, abs=1e-5)
    assert share(ended, "D1") == pytest.approx(share(ended, "D2"), abs=1e-5)  # the others keep their proportions
    assert dials_before(client, "nw_renewal", "D3", ended["applied_minute"]) >= 300
    assert ended["evidence"]["dials"] >= 300
    # D3 looked far better than the others during exploration, and that must not have got a proven number flagged.
    assert [p for p in proposals_of(db_path, "nw_renewal") if p["created_minute"] < ended["created_minute"]] == []


def test_a_caller_id_that_does_worse_in_exploration_is_dropped_and_its_weight_returns_to_the_proven_ones(world, db_path):
    client, troubled = explored_world(world, dampen={"nw_renewal": "D3"}, no_experiments=True)

    Controller(troubled, db_path).run()

    started, ended, *_ = proposals_of(db_path, "nw_renewal", EXPLORE)
    assert ended["evidence"]["action"] == "drop" and ended["status"] == "applied"
    assert set(ended["proposed_config"]["allocations"]["default"]["caller_id"]) == {"D1", "D2"}
    assert dials_before(client, "nw_renewal", "D3", ended["applied_minute"]) >= 300
    assert ended["evidence"]["caller_id"] == started["evidence"]["caller_id"] == "D3"


def test_moving_off_a_degraded_number_never_raises_a_caller_id_still_being_explored(world, db_path):
    client = world(days=2, history_days=1)
    troubled, _ = troubled_client(client, degrade={"nw_renewal": "D2"})

    Controller(troubled, db_path).run()

    moved = next(p for p in proposals_of(db_path, "nw_renewal") if p["status"] == "applied")
    assert share(moved, "D3") == pytest.approx(0.1, abs=1e-6) and share(moved, "D2") == 0
    assert share(moved, "D1") == pytest.approx(0.9, abs=1e-5)
    assert dials_before(client, "nw_renewal", "D3", moved["applied_minute"]) < 300


def test_exploration_on_kestrel_asks_an_approver_and_nothing_is_auto_applied(world, db_path):
    client = world(days=2, history_days=1)

    Controller(client, db_path).run()

    assert auto_applied(client, "ks_cancel") == []
    explored = proposals_of(db_path, "ks_cancel", EXPLORE)
    assert explored and {p["route"] for p in explored} == {"approval"}
    assert all(p["sent_minute"] % 1440 >= 11 * 60 for p in explored if p["sent_minute"] is not None)


def test_exploration_waits_a_week_after_a_supervisor_changes_the_caller_ids(world, db_path):
    client = world(days=2, history_days=1)
    troubled, _ = troubled_client(client)
    c = Controller(troubled, db_path)
    c.start()
    supervisor_edit(client, "nw_renewal", lambda cfg: cfg["allocations"]["default"].update(caller_id={"D1": 0.7, "D2": 0.3}))

    c.run()

    assert proposals_of(db_path, "nw_renewal", EXPLORE) == []
    assert any("supervisor" in d["detail"].get("hold", "") for d in queries.decisions(db_path, "nw_renewal", EXPLORE))


def test_no_customer_facing_dimension_is_auto_applied_on_any_workflow(world, db_path):
    client = world(days=3, history_days=1)

    Controller(client, db_path).run()

    changes = [h for w in client.get("/workflows").json() for h in auto_applied(client, w["workflow_id"])]
    assert changes  # the path was actually used
    assert all(set(h["changed_dims"]) <= OPERATIONAL for h in changes)


def test_the_weekly_report_lists_every_auto_applied_change_with_what_changed_and_why(world, db_path, capsys):
    client = world(days=2, history_days=1)
    troubled, _ = troubled_client(client, degrade={"nw_renewal": "D2"})
    Controller(troubled, db_path).run()
    from_the_world = sorted(h["proposal_id"] for w in client.get("/workflows").json() for h in auto_applied(client, w["workflow_id"]))
    moved = next(p for p in proposals_of(db_path, "nw_renewal") if p["status"] == "applied")

    report = queries.weekly_report(db_path)

    assert [w["label"] for w in report["weeks"]] == ["Week 1 (D0 to D6)"]
    listed = report["weeks"][0]["changes"]
    assert from_the_world and sorted(c["proposal_id"] for c in listed) == from_the_world
    entry = next(c for c in listed if c["proposal_id"] == moved["proposal_id"])
    assert entry["workflow_id"] == "nw_renewal" and entry["changed_dims"] == ["caller_id"]
    assert entry["incident_response"] is True and entry["why"] == moved["rationale"]
    assert entry["what"] == ["caller_id for default {'D1': 0.45, 'D2': 0.45, 'D3': 0.1} -> {'D1': 0.9, 'D3': 0.1}"]

    assert cli_main(["report", "--weekly", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    for expected in ("Week 1 (D0 to D6)", moved["proposal_id"], "incident response", entry["what"][0], moved["rationale"]):
        assert expected in out


def test_the_weekly_report_says_so_when_nothing_was_auto_applied(world, db_path, capsys):
    client = world(days=1, history_days=1)
    troubled, _ = troubled_client(client, approval_only=True)
    Controller(troubled, db_path).run()

    assert queries.weekly_report(db_path)["weeks"] == []
    assert cli_main(["report", "--weekly", "--db", str(db_path)]) == 0
    assert "No auto-applied changes" in capsys.readouterr().out


def test_why_explains_an_auto_applied_change_from_the_store(world, db_path, capsys):
    client = world(days=2, history_days=1)
    troubled, _ = troubled_client(client, degrade={"nw_renewal": "D2"})
    Controller(troubled, db_path).run()
    moved = next(p for p in proposals_of(db_path, "nw_renewal") if p["status"] == "applied")
    at = moved["applied_minute"] + 60

    answer = queries.why(db_path, "nw_renewal", at)

    assert answer["change"]["actor"] == "controller_auto" and answer["proposal"]["proposal_id"] == moved["proposal_id"]
    assert answer["approval"] is None
    assert cli_main(["why", "nw_renewal", "--at", f"D{at // 1440} {at % 1440 // 60:02d}:{at % 60:02d}", "--db", str(db_path)]) == 0
    assert "controller_auto" in capsys.readouterr().out


def test_an_incident_never_takes_two_auto_applied_steps_in_a_day_to_get_past_max_shift(world, db_path):
    client = world(days=2, history_days=1)
    troubled, _ = troubled_client(client, degrade={"ks_cancel": "D2"})
    c = Controller(troubled, db_path)
    c.step()
    # 0.2 moves at once, and the last 0.1 would fit under max_shift on its own.
    supervisor_edit(client, "ks_cancel", lambda cfg: cfg["allocations"]["default"].update(caller_id={"D1": 0.7, "D2": 0.3}))

    c.run()

    step, *rest = proposals_of(db_path, "ks_cancel")
    assert step["route"] == "auto" and share(step, "D2") == pytest.approx(0.1, abs=1e-4)
    assert [h["proposal_id"] for h in auto_applied(client, "ks_cancel") if "-caller_id-" in h["proposal_id"]] == [step["proposal_id"]]
    assert rest and rest[0]["route"] == "approval"


def test_a_different_number_degrading_the_same_day_still_auto_applies(world, db_path):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.lift["nw_renewal"] = "D3"  # earns D3 an equal share, so there is somewhere for traffic to go
    c = Controller(troubled, db_path)
    while not [p for p in proposals_of(db_path, "nw_renewal", EXPLORE) if p["evidence"]["action"] == "equal_share" and p["status"] == "applied"]:
        c.step()
    trouble.degrade("nw_renewal", "D1")
    while not [p for p in proposals_of(db_path, "nw_renewal") if p["status"] == "applied"]:
        c.step()
    trouble.degrade("nw_renewal", "D2")

    c.run()

    first, second, *_ = [p for p in proposals_of(db_path, "nw_renewal") if p["status"] == "applied"]
    assert first["evidence"]["moved_off"] == ["D1"] and second["evidence"]["moved_off"] == ["D2"]
    assert first["route"] == second["route"] == "auto"
    assert second["created_minute"] - first["created_minute"] < 1440


def test_a_dropped_caller_id_is_not_explored_again_once_its_evidence_has_aged_out(world, db_path):
    client = world(days=9, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.dampen["nw_renewal"] = "D3"
    trouble.no_experiments = True

    Controller(troubled, db_path).run()

    explored = proposals_of(db_path, "nw_renewal", EXPLORE)
    dropped = next(p for p in explored if p["evidence"]["action"] == "drop" and p["evidence"]["caller_id"] == "D3")
    assert dropped["created_minute"] < 2 * 1440  # so its dials left the 7-day window well before the run ended
    assert [p["evidence"]["caller_id"] for p in explored if p["evidence"]["action"] == "start"].count("D3") == 1
