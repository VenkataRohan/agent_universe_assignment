"""Ticket 07: the screen stage of an experiment, through Seam 1.

The real simulator's approvals, config history and events are the oracle; the controller is read
only through its query interface.
"""
from __future__ import annotations

import pytest

from controller import queries
from controller.__main__ import main as cli_main
from controller.loop import Controller

from .harness import Killed, all_events, troubled_client
from .test_auto_apply import sim_minute
from .test_governance import supervisor_edit

SCREEN = "experiment_screen"


def screens(db_path, workflow: str) -> list[dict]:
    return [p for p in queries.proposals(db_path, workflow) if p["kind"] == SCREEN]


def experiments(db_path, workflow: str) -> list[dict]:
    return queries.experiments(db_path, workflow)


def share(distribution: dict[str, float], option: str) -> float:
    return distribution.get(option, 0) / sum(distribution.values())


@pytest.mark.parametrize("workflow, welcomes, voices", [
    ("nw_renewal", {"W1", "W2", "W3"}, {"V1", "V2", "V3", "V4"}),
    ("ks_cancel", {"W1", "W2", "W3"}, {"V1", "V2", "V3", "V4"}),
])
def test_an_engagement_primary_workflow_gets_a_factorial_screen_labelled_as_exploration(world, db_path, workflow, welcomes, voices):
    client = world(days=1, history_days=1)

    Controller(client, db_path).run()

    screen = screens(db_path, workflow)[0]
    default = client.get(f"/workflows/{workflow}/config").json()["config"]["allocations"]["default"]
    assert screen["status"] == "applied" and screen["route"] == "approval"
    assert set(default["welcome_message"]) == welcomes and set(default["voice"]) == voices
    assert share(default["welcome_message"], "W1") >= 0.5 and share(default["voice"], "V1") >= 0.5
    assert screen["change_summary"].startswith("Exploration")
    assert screen["evidence"]["label"] == "exploration"
    # No call has ever heard a challenger, so nothing is claimed; three calls in four hear at least one.
    assert screen["evidence"]["exposure_weighted_expected_value"] == 0.0
    assert screen["evidence"]["exposed_share"] == pytest.approx(0.75, abs=1e-4)
    assert "expected value" in screen["change_summary"]
    assert screen["impact_score"] == 0.0
    assert screen["affected_leads"] > 0
    assert experiments(db_path, workflow)[0]["status"] == "screening"


def test_a_pickup_primary_workflow_gets_no_welcome_or_voice_experiment(world, db_path):
    client = world(days=1, history_days=1)

    Controller(client, db_path).run()

    assert screens(db_path, "nw_kyc") == [] and experiments(db_path, "nw_kyc") == []
    default = client.get("/workflows/nw_kyc/config").json()["config"]["allocations"]["default"]
    assert default["welcome_message"] == {"W1": 1} and default["voice"] == {"V1": 1}
    assert queries.decisions(db_path, "nw_kyc", "experiment")[-1]["outcome"] == "not_applicable"


def looks(db_path, workflow: str, outcome: str) -> list[dict]:
    return [d for d in queries.decisions(db_path, workflow, "experiment") if d["outcome"] == outcome]


def test_the_daily_look_eliminates_the_clearly_worse_and_leaves_one_candidate_per_dimension(world, db_path):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.dull["nw_renewal"] = {"V2"}

    Controller(troubled, db_path).run()

    exp = experiments(db_path, "nw_renewal")[0]
    assert exp["status"] in PAST_THE_SCREEN
    assert set(exp["candidates"]) <= {"welcome_message", "voice"} and exp["candidates"].get("voice") != "V2"
    waited, screened = looks(db_path, "nw_renewal", "waiting_for_data"), looks(db_path, "nw_renewal", "screened")
    assert [d["minute"] for d in waited] == [1440 + 540] and [d["minute"] for d in screened] == [2 * 1440 + 540]
    verdict = screened[0]["detail"]["dimensions"]
    assert "V2" in verdict["voice"]["eliminated"]
    assert verdict["voice"]["arms"]["V2"]["engaged"] == 0
    assert all(arm["connected"] >= 200 for dim in verdict.values() for arm in dim["arms"].values())


def test_nothing_is_promoted_from_a_screen(world, db_path):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.dull["nw_renewal"] = {"V2"}

    Controller(troubled, db_path).run()

    assert experiments(db_path, "nw_renewal")[0]["status"] in PAST_THE_SCREEN
    default = client.get("/workflows/nw_renewal/config").json()["config"]["allocations"]["default"]
    assert share(default["welcome_message"], "W1") >= 0.5 and share(default["voice"], "V1") >= 0.5
    assert [p for p in queries.proposals(db_path, "nw_renewal") if p["kind"] == "experiment_promote"] == []


CHALLENGERS = {"W2", "W3", "V2", "V3", "V4"}
PAST_THE_SCREEN = ("screened", "confirm_requested", "confirming")  # a chosen candidate goes straight on to confirm


def test_a_screen_with_no_challenger_ahead_of_control_ends_with_no_candidate_and_returns_to_control(world, db_path):
    client = world(days=4, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.dull["nw_renewal"] = CHALLENGERS

    Controller(troubled, db_path).run()

    only, *others = experiments(db_path, "nw_renewal")
    assert only["status"] == "inconclusive" and only["end_reason"] == "no_candidate" and only["candidates"] == {}
    assert others == []
    default = client.get("/workflows/nw_renewal/config").json()["config"]["allocations"]["default"]
    assert default["welcome_message"] == {"W1": 1.0} and default["voice"] == {"V1": 1.0}
    revert = next(p for p in queries.proposals(db_path, "nw_renewal") if p["kind"] == "experiment_end")
    assert revert["status"] == "applied" and revert["evidence"]["experiment_id"] == only["experiment_id"]


def advance_into_a_screen(controller, db_path, workflow: str, ticks_of_data: int = 12) -> dict:
    while not (found := experiments(db_path, workflow)) or found[-1]["status"] != "screening":
        controller.step()
    for _ in range(ticks_of_data):
        controller.step()
    return experiments(db_path, workflow)[-1]


def recounted_arms(client, workflow: str, dim: str, first_version: int, last_version: int, before_minute: int | None = None) -> dict:
    """Connected and engaged per option, straight from the world's events."""
    arms: dict[str, dict[str, int]] = {}
    for e in all_events(client):
        if (e["type"] == "call" and e["workflow_id"] == workflow and first_version <= e["config_version"] <= last_version
                and (before_minute is None or sim_minute(e["ts"]) // 60 < before_minute // 60)):
            arm = arms.setdefault(e["variant"][dim], {"connected": 0, "engaged": 0})
            arm["connected"] += e["connected"]
            arm["engaged"] += e["engaged"]
    return arms


SUPERVISORS_VOICE = {"V1": 0.69, "V2": 0.11, "V3": 0.1, "V4": 0.1}  # within what the simulator lets a PUT move on ks_cancel


def test_a_supervisor_edit_to_an_experimented_dimension_aborts_the_screen_and_closes_its_data_at_the_edit(world, db_path):
    client = world(days=3, history_days=1)
    troubled, _ = troubled_client(client)
    controller = Controller(troubled, db_path)
    running = advance_into_a_screen(controller, db_path, "ks_cancel")
    edited_at = client.get("/sim/state").json()["sim_minute"]
    version = supervisor_edit(client, "ks_cancel", lambda cfg: cfg["allocations"]["default"].update(voice=SUPERVISORS_VOICE))

    controller.run()

    first = experiments(db_path, "ks_cancel")[0]
    assert first["status"] == "aborted" and first["end_reason"] == "supervisor_edit" and first["candidates"] == {}
    abort = next(d for d in looks(db_path, "ks_cancel", "aborted") if d["detail"]["experiment"]["experiment_id"] == first["experiment_id"])
    assert abort["detail"]["edit"]["version"] == version
    closed = abort["detail"]["dimensions"]["voice"]["arms"]
    assert closed == recounted_arms(troubled, "ks_cancel", "voice", running["screen_version"], version - 1, edited_at)
    # The supervisor's voice stands; only the dimension nobody edited went back to control.
    default = client.get("/workflows/ks_cancel/config").json()["config"]["allocations"]["default"]
    assert share(default["voice"], "V1") == pytest.approx(0.69)
    revert = next(p for p in queries.proposals(db_path, "ks_cancel") if p["kind"] == "experiment_end")
    assert revert["evidence"]["dimensions"] == ["welcome_message"] and "voice" not in revert["diff"]
    # The next screen's expected value is worked from what callers heard up to the edit, nothing after it.
    plan = screens(db_path, "ks_cancel")[1]["evidence"]["design"]["dimensions"]["welcome_message"]
    heard = recounted_arms(troubled, "ks_cancel", "welcome_message", 0, version + 100, edited_at)
    rate = {o: a["engaged"] / a["connected"] for o, a in heard.items()}
    worked = sum(plan["split"][c] / sum(plan["split"].values()) * (rate[c] - rate[plan["control"]]) for c in plan["challengers"])
    assert screens(db_path, "ks_cancel")[1]["evidence"]["exposure_weighted_expected_value"] == pytest.approx(worked, abs=1e-4)


def test_a_supervisor_edit_after_the_screen_has_chosen_still_aborts_it(world, db_path):
    client = world(days=4, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.dull["ks_cancel"] = {"V2"}
    controller = Controller(troubled, db_path)
    while not looks(db_path, "ks_cancel", "screened"):
        controller.step()
    assert experiments(db_path, "ks_cancel")[0]["candidates"]
    supervisor_edit(client, "ks_cancel", lambda cfg: cfg["allocations"]["default"].update(voice=SUPERVISORS_VOICE))

    controller.run()

    first = experiments(db_path, "ks_cancel")[0]
    assert first["status"] == "aborted" and first["end_reason"] == "supervisor_edit" and first["candidates"] == {}


def test_a_supervisor_edit_elsewhere_keeps_the_screen_going_in_a_separate_stratum(world, db_path):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.dull["nw_renewal"] = {"V2"}
    controller = Controller(troubled, db_path)
    running = advance_into_a_screen(controller, db_path, "nw_renewal")
    version = supervisor_edit(client, "nw_renewal", lambda cfg: cfg["allocations"]["default"].update(caller_id={"D1": 0.7, "D2": 0.3}))

    controller.run()

    exp = experiments(db_path, "nw_renewal")[0]
    assert exp["status"] in PAST_THE_SCREEN
    strata = looks(db_path, "nw_renewal", "screened")[0]["detail"]["strata"]
    assert running["screen_version"] in strata and version in strata


def test_a_refused_screen_never_ran_so_it_teaches_nothing_and_is_rebuilt_as_a_new_experiment(world, db_path):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.blank_summary = True  # the approver must reject anything with an empty summary
    controller = Controller(troubled, db_path)
    while not experiments(db_path, "nw_renewal") or experiments(db_path, "nw_renewal")[0]["status"] != "aborted":
        controller.step()
    trouble.blank_summary = False

    controller.run()

    refused, retried, *_ = experiments(db_path, "nw_renewal")
    assert refused["end_reason"] == "request_rejected" and refused["screen_version"] is None and refused["candidates"] == {}
    assert [p for p in queries.proposals(db_path, "nw_renewal") if p["kind"] == "experiment_end"] == []
    first, second, *_ = screens(db_path, "nw_renewal")
    assert first["status"] == "rejected" and second["replaces"] == first["proposal_id"]
    assert second["created_minute"] - first["decided_minute"] >= 1440  # asked again only after the cooldown
    assert retried["status"] in ("screening", "screened")
    # Nobody heard its arms, so the retry's expected value still claims nothing.
    assert second["evidence"]["exposure_weighted_expected_value"] == 0.0


def test_calls_are_attributed_by_recorded_config_version_while_a_locked_campaign_phases_out(world, db_path):
    client = world(days=5, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.primary["nw_kyc"] = "engagement_rate"  # the only next_campaign_only workflow, shown as engagement-primary

    Controller(troubled, db_path).run()

    exp = next(e for e in experiments(db_path, "nw_kyc") if e["screen_version"] is not None)
    look = next(d for d in queries.decisions(db_path, "nw_kyc", "experiment")
                if d["outcome"] in ("waiting_for_data", "screened") and d["detail"]["experiment"]["experiment_id"] == exp["experiment_id"])
    calls = [e for e in all_events(troubled) if e["type"] == "call" and e["workflow_id"] == "nw_kyc"]
    locked = [e for e in calls if e["config_version"] < exp["screen_version"] and sim_minute(e["ts"]) > exp["screen_minute"]]
    assert locked and {e["variant"]["voice"] for e in locked} == {"V1"}  # the phase-in really happened
    in_the_experiment = [e for e in calls if e["config_version"] >= exp["screen_version"] and sim_minute(e["ts"]) <= look["minute"]]
    arms = look["detail"]["dimensions"]["voice"]["arms"]
    for voice in ("V1", "V2"):
        heard = [e for e in in_the_experiment if e["variant"]["voice"] == voice]
        assert arms[voice] == {"connected": sum(e["connected"] for e in heard), "engaged": sum(e["engaged"] for e in heard)}


def test_a_kill_around_the_screen_request_sends_it_exactly_once_on_restart(world, db_path):
    client = world(days=1, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.kill("POST", "/approvals")

    with pytest.raises(Killed):
        Controller(troubled, db_path).run()
    assert client.get("/approvals").json() == []

    Controller(troubled, db_path).run()

    sent = [a["proposal_id"] for a in client.get("/approvals", params={"workflow_id": "nw_renewal"}).json() if SCREEN in a["proposal_id"]]
    assert sent == ["nw_renewal-experiment_screen-1"]
    assert [e["status"] for e in experiments(db_path, "nw_renewal")] == ["screening"]


def test_why_shows_the_experiment_as_it_stood_and_the_screens_decisions(world, db_path, capsys):
    client = world(days=3, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.dull["nw_renewal"] = {"V2"}
    Controller(troubled, db_path).run()
    exp = experiments(db_path, "nw_renewal")[0]
    screened = looks(db_path, "nw_renewal", "screened")[0]

    before = queries.why(db_path, "nw_renewal", screened["minute"] - 15)
    after = queries.why(db_path, "nw_renewal", screened["minute"])

    assert before["experiment"]["status"] == "screening" and before["experiment"]["candidates"] == {}
    assert after["experiment"]["status"] == "screened" and after["experiment"]["candidates"] == exp["candidates"]
    assert "V2" in after["experiment"]["dimensions"]["voice"]["eliminated"]
    outcomes = [d["outcome"] for d in after["decisions"] if d["kind"] == "experiment" and d["outcome"] not in ("held", "confirm_held")]
    assert outcomes[:2] == ["planned", "screen_requested"] and outcomes[-2:] == ["waiting_for_data", "screened"]

    assert cli_main(["why", "nw_renewal", "--at", "D2 09:00", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    for expected in ("experiment", exp["experiment_id"], "screened", "eliminated V2"):
        assert expected in out


def test_a_kill_between_planning_and_proposing_plans_afresh_and_asks_once(world, db_path):
    client = world(days=1, history_days=1)
    troubled, trouble = troubled_client(client)
    trouble.kill("GET", "/workflows/ks_cancel/config/history")  # what building the first Proposal reads

    with pytest.raises(Killed):
        Controller(troubled, db_path).run()
    assert [e["status"] for e in experiments(db_path, "ks_cancel")] == ["planned"] and screens(db_path, "ks_cancel") == []

    Controller(troubled, db_path).run()

    interrupted, fresh = experiments(db_path, "ks_cancel")
    assert interrupted["status"] == "aborted" and interrupted["end_reason"] == "never_requested"
    assert fresh["status"] == "screening"
    sent = [a["proposal_id"] for a in client.get("/approvals", params={"workflow_id": "ks_cancel"}).json() if SCREEN in a["proposal_id"]]
    assert sent == ["ks_cancel-experiment_screen-1"]


@pytest.mark.slow
def test_on_public_01_each_engagement_workflow_gets_an_approved_screen_and_the_pickup_workflow_none(world, db_path):
    client = world(days=8, history_days=14)

    Controller(client, db_path).run()

    assert screens(db_path, "nw_kyc") == [] and experiments(db_path, "nw_kyc") == []
    assert [queries.incidents(db_path, w) for w in ("nw_renewal", "ks_cancel")] == [[], []]
    # No incident is scripted here, so a false alarm on the quiet workflow may fire, and must change nothing.
    assert all(i["end_reason"] == "live_prompt_passed_validation" for i in queries.incidents(db_path, "nw_kyc"))
    assert not [p for p in queries.proposals(db_path, "nw_kyc") if p["kind"] == "hallucination"]
    for workflow in ("nw_renewal", "ks_cancel"):
        applied = [p for p in screens(db_path, workflow) if p["status"] == "applied"]
        assert applied
        for p in applied:
            default = p["proposed_config"]["allocations"]["default"]
            assert share(default["welcome_message"], "W1") >= 0.5 and share(default["voice"], "V1") >= 0.5
        for exp in experiments(db_path, workflow):
            assert set(exp["candidates"]) <= {"welcome_message", "voice"}
            assert exp["status"] != "screened" or exp["candidates"]
