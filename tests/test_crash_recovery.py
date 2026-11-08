"""The crash-recovery script's checks, on hand-made store rows and platform records.
(The kill-and-restart run itself is slow: `python scripts/crash_recovery.py`.)"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("crash_recovery", ROOT / "scripts" / "crash_recovery.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)


def row(pid, status="applied", route="approval", approval_id=None, workflow_id="w"):
    return {"proposal_id": pid, "workflow_id": workflow_id, "status": status, "route": route, "approval_id": approval_id}


def apr(aid, pid, status="applied", workflow_id="w"):
    return {"approval_id": aid, "proposal_id": pid, "workflow_id": workflow_id, "status": status}


def test_clean_world_passes_every_check():
    store = [row("p1", approval_id="a1"), row("p2", status="pending", approval_id="a2"), row("p3", route="auto")]
    approvals = [apr("a1", "p1"), apr("a2", "p2", "pending")]
    history = [{"version": 2, "approval_id": "a1", "proposal_id": "p1"}, {"version": 3, "approval_id": None, "proposal_id": "p3"}]
    assert cr.check_no_duplicate_proposals(store, approvals) == []
    assert cr.check_no_double_apply(history) == []
    assert cr.check_no_lost_proposals(store, approvals, history) == []


def test_a_proposal_id_seen_twice_is_a_duplicate():
    assert cr.check_no_duplicate_proposals([row("p1", approval_id="a1")], [apr("a1", "p1"), apr("a2", "p1")])
    assert cr.check_no_duplicate_proposals([row("p1"), row("p1")], [])


def test_an_approval_or_proposal_applied_twice_is_caught():
    twice = [{"version": 2, "approval_id": "a1", "proposal_id": "p1"}, {"version": 3, "approval_id": "a1", "proposal_id": "p1"}]
    assert cr.check_no_double_apply(twice)
    assert cr.check_no_double_apply([{"version": 2, "approval_id": None, "proposal_id": "p9"},
                                     {"version": 3, "approval_id": None, "proposal_id": "p9"}])
    assert cr.check_no_double_apply([{"version": 1, "approval_id": None, "proposal_id": None},
                                     {"version": 2, "approval_id": None, "proposal_id": None}]) == []


def test_lost_proposals_are_caught_in_both_directions():
    # the platform holds an approval the store never recorded
    assert cr.check_no_lost_proposals([], [apr("a1", "p1", "pending")], [])
    # the store thinks it is pending but the platform has no record of it
    assert cr.check_no_lost_proposals([row("p1", "pending", approval_id="a1")], [], [])
    # the platform applied it but the store never learned
    assert cr.check_no_lost_proposals([row("p1", "approved", approval_id="a1")], [apr("a1", "p1")],
                                      [{"version": 2, "approval_id": "a1", "proposal_id": "p1"}])
    # stuck mid-flight with the run over
    assert cr.check_no_lost_proposals([row("p1", "submitting")], [], [])
    assert cr.check_no_lost_proposals([row("p1", "applying", approval_id="a1")], [apr("a1", "p1", "approved")], [])


def test_a_proposal_the_controller_abandoned_before_sending_is_not_lost():
    assert cr.check_no_lost_proposals([row("p1", "stale")], [], []) == []
    assert cr.check_no_lost_proposals([row("p1", "refused", route="auto")], [], []) == []


def test_grade_is_within_noise_by_conclusion_rate():
    assert cr.check_grade_within_noise(0.417, 0.410, tolerance=0.02) == []
    assert cr.check_grade_within_noise(0.417, 0.30, tolerance=0.02)


def test_kill_points_are_random_sorted_and_inside_the_run():
    pts = cr.kill_points(100.0, 3, seed=7)
    assert pts == sorted(pts) and len(pts) == 3 and all(10 <= p <= 90 for p in pts)
    assert cr.kill_points(100.0, 3, seed=7) == pts
    assert cr.kill_points(100.0, 3, seed=8) != pts
