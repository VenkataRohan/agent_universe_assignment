"""Ticket 10: per-cohort call time, through Seam 1.

The world is the real simulator with the primary KPI rewritten by age band and hour (`Trouble.hour_shaped`), history
included, so a cohort's best hours are known by construction. The controller is read only through its query interface.
"""
from __future__ import annotations

from controller import queries
from controller.loop import Controller

from .harness import troubled_client
from .test_governance import supervisor_edit

KYC, RENEWAL = "nw_kyc", "nw_renewal"
YOUNG = "age_band_18_30"
SCHEDULE = "schedule"


def young_by_day_others_by_evening(features: dict[str, str], hour: int) -> float:
    """18-30s answer before 15:00 and the rest after it, far enough apart that age band is the partition."""
    if features["age_band"] == "18-30":
        return 0.8 if hour < 15 else 0.2
    return 0.15 if hour < 15 else 0.45


def shaped_world(world, workflow: str, kpi: str, days: int = 2, history_days: int = 4):
    client = world(days=days, history_days=history_days)
    troubled, trouble = troubled_client(client, degrade_history=True)
    trouble.no_experiments = True
    trouble.hour_shaped[workflow] = [(kpi, young_by_day_others_by_evening)]
    return client, troubled, trouble


def schedule_proposals(db_path, workflow: str, kind: str = SCHEDULE) -> list[dict]:
    return [p for p in queries.proposals(db_path, workflow) if p["kind"] == kind]


def config(client, workflow: str) -> dict:
    return client.get(f"/workflows/{workflow}/config").json()["config"]


def test_an_approval_only_workflow_gets_a_full_hour_table_per_cohort_through_approval(world, db_path):
    client, troubled, _ = shaped_world(world, KYC, "pickup_rate")

    Controller(troubled, db_path).run()

    proposal, *_ = schedule_proposals(db_path, KYC)
    assert proposal["route"] == "approval" and proposal["status"] == "applied"
    live = config(client, KYC)
    tables = live["schedule"]["hour_weights"]
    assert YOUNG in tables and YOUNG in [c["id"] for c in live["cohorts"]]
    window = [str(h) for h in range(10, 20)]
    for table in tables.values():
        assert sorted(table, key=int) == window and all(0.5 <= w <= 2 for w in table.values())
    assert tables[YOUNG]["10"] > tables[YOUNG]["18"]
    assert tables["default"]["10"] < tables["default"]["18"]
    assert proposal["evidence"]["replay"]["relative_gain"] >= 0.03
    assert "confounded" in proposal["rationale"]
    assert proposal["affected_leads"] > 0  # a schedule reaches running campaigns even under next_campaign_only
    assert proposal["dry_run"]["method"] == "history_replay"
    predicted = proposal["dry_run"]["overall"]["pickup_rate"]
    assert predicted["proposed"] > predicted["current"]


def step_until_applied(controller, db_path, workflow: str, kind: str = SCHEDULE) -> dict:
    while True:
        done = [p for p in schedule_proposals(db_path, workflow, kind) if p["status"] == "applied"]
        if done:
            return done[0]
        assert not controller.step(), f"no applied {kind} Proposal before the scenario ended"


def test_where_schedule_may_be_auto_applied_the_cohorts_go_to_an_approver_and_the_table_then_auto_applies(world, db_path):
    client, troubled, _ = shaped_world(world, RENEWAL, "engagement_rate", days=3)  # the approver's first answer here is no

    Controller(troubled, db_path).run()

    asked = schedule_proposals(db_path, RENEWAL, "schedule_cohorts")
    cohorts = next(p for p in asked if p["status"] == "applied")
    table, *_ = schedule_proposals(db_path, RENEWAL)
    assert {p["route"] for p in asked} == {"approval"} and cohorts["diff"].keys() == {"cohorts"}
    assert cohorts["proposed_config"]["schedule"]["hour_weights"] == {}  # defining them changes nobody's calls
    assert table["route"] == "auto" and table["status"] == "applied" and table["diff"].keys() == {"schedule"}
    history = client.get(f"/workflows/{RENEWAL}/config/history").json()
    assert ("controller_auto", ["schedule"]) in [(h["actor"], h["changed_dims"]) for h in history]
    assert sorted(config(client, RENEWAL)["schedule"]["hour_weights"][YOUNG], key=int) == [str(h) for h in range(9, 21)]


def test_kestrel_gets_its_cohorts_and_table_in_one_proposal_because_nothing_there_may_auto_apply(world, db_path):
    client, troubled, _ = shaped_world(world, "ks_cancel", "engagement_rate")

    Controller(troubled, db_path).run()

    proposal, *_ = schedule_proposals(db_path, "ks_cancel")
    assert proposal["route"] == "approval" and proposal["diff"].keys() == {"cohorts", "schedule"}
    assert schedule_proposals(db_path, "ks_cancel", "schedule_cohorts") == []
    assert [h for h in client.get("/workflows/ks_cancel/config/history").json() if h["actor"] == "controller_auto" and "schedule" in h["changed_dims"]] == []


def outcomes(db_path, workflow: str, kind: str) -> list[str]:
    return [d["outcome"] for d in queries.decisions(db_path, workflow, kind)]


def test_no_table_is_proposed_when_the_replay_predicts_too_little_gain(world, db_path):
    client = world(days=1, history_days=4)  # the public world, whose history predicts well under 3% (seen in its schedule_plan decisions)
    troubled, trouble = troubled_client(client)
    trouble.no_experiments = True

    Controller(troubled, db_path).run()

    for workflow in (KYC, RENEWAL, "ks_cancel"):
        assert schedule_proposals(db_path, workflow) == []
        assert outcomes(db_path, workflow, "schedule_plan") == ["no_gain"]


def test_a_single_history_day_gives_nothing_to_hold_out_so_no_table_is_proposed(world, db_path):
    client, troubled, _ = shaped_world(world, RENEWAL, "engagement_rate", days=1, history_days=1)

    Controller(troubled, db_path).run()

    assert schedule_proposals(db_path, RENEWAL) == [] and schedule_proposals(db_path, RENEWAL, "schedule_cohorts") == []
    assert outcomes(db_path, RENEWAL, "schedule_plan") == ["too_little_history"]


def pickup_against_the_engagement_hours(features: dict[str, str], hour: int) -> float:
    """Each cohort is hardest to reach in the hours it would otherwise engage best."""
    if features["age_band"] == "18-30":
        return 0.1 if hour < 15 else 0.7
    return 0.8 if hour < 15 else 0.1


def test_no_table_is_proposed_when_it_would_break_the_guardrail(world, db_path):
    client, troubled, trouble = shaped_world(world, RENEWAL, "engagement_rate", days=1)
    trouble.hour_shaped[RENEWAL].insert(0, ("pickup_rate", pickup_against_the_engagement_hours))

    Controller(troubled, db_path).run()

    assert schedule_proposals(db_path, RENEWAL) == [] and schedule_proposals(db_path, RENEWAL, "schedule_cohorts") == []
    plan = next(d for d in queries.decisions(db_path, RENEWAL, "schedule_plan"))
    assert plan["outcome"] == "guardrail_fails" and plan["detail"]["replay"]["relative_gain"] >= 0.03
    assert plan["detail"]["replay"]["guardrail_drop"] > 0.02


def test_a_supervisors_recent_schedule_is_left_to_run(world, db_path):
    client, troubled, _ = shaped_world(world, RENEWAL, "engagement_rate")
    controller = Controller(troubled, db_path)
    controller.start()
    flat = {str(h): 1 for h in range(9, 21)}
    supervisor_edit(client, RENEWAL, lambda cfg: cfg["schedule"]["hour_weights"].update(default=flat))

    controller.run()

    assert schedule_proposals(db_path, RENEWAL) == [] and schedule_proposals(db_path, RENEWAL, "schedule_cohorts") == []
    assert config(client, RENEWAL)["schedule"]["hour_weights"] == {"default": flat}
    assert "held" in outcomes(db_path, RENEWAL, SCHEDULE)


def hardly_anybody_engages(features: dict[str, str], hour: int) -> float:
    return 0.1


def test_a_significant_drop_after_the_table_applies_rolls_it_back_and_says_so(world, db_path):
    client, troubled, trouble = shaped_world(world, RENEWAL, "engagement_rate", days=5)
    controller = Controller(troubled, db_path)
    table = step_until_applied(controller, db_path, RENEWAL)
    trouble.hour_shaped[RENEWAL] = [("engagement_rate", hardly_anybody_engages)]

    controller.run()

    rollback, *_ = schedule_proposals(db_path, RENEWAL, "schedule_rollback")
    assert rollback["status"] == "applied" and rollback["route"] == "auto"
    assert config(client, RENEWAL)["schedule"]["hour_weights"] == {}  # what ran before the table
    assert YOUNG in [c["id"] for c in config(client, RENEWAL)["cohorts"]]  # removing them would need an approver for nothing
    assert "confounded" in rollback["rationale"]
    predicted = rollback["dry_run"]["overall"]["engagement_rate"]
    assert predicted["proposed"] > predicted["current"]
    evidence = rollback["evidence"]
    assert evidence["after"]["successes"] / evidence["after"]["trials"] < evidence["before"]["successes"] / evidence["before"]["trials"]
    assert evidence["p_value"] < 0.05
    # The look comes only once 72 complete hours follow the change, and it does not run again.
    assert rollback["created_minute"] // 60 >= table["applied_minute"] // 60 + 73
    assert outcomes(db_path, RENEWAL, "schedule_check") == ["dropped"]


def test_a_table_that_holds_up_stays_and_is_looked_at_once(world, db_path):
    client, troubled, _ = shaped_world(world, RENEWAL, "engagement_rate", days=5)

    Controller(troubled, db_path).run()

    assert schedule_proposals(db_path, RENEWAL, "schedule_rollback") == []
    assert YOUNG in config(client, RENEWAL)["schedule"]["hour_weights"]
    assert outcomes(db_path, RENEWAL, "schedule_check") == ["no_drop"]
