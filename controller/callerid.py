"""Caller-ID responder: move off degraded numbers (ADR-0002) and test unused ones on a capped share.

Runs every tick. A degradation pass over a rolling day leaves one decision behind, "healthy"
included, and exploration records each change in its state, so "why didn't it change?" can be
answered from the store.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from . import stats
from .composer import (Composed, caller_id_dist, compose, give_equal_share, move_off_caller_ids, try_caller_id,
                       violations)
from .governance import AUTO_ROUTE, Draft, GovernanceClient
from .replay import Calls, calls_by_caller
from .simclient import SimClient
from .simtime import MINUTES_PER_DAY
from .store import Store

KIND = "caller_id"
EXPLORE_KIND = "caller_id_explore"
ROLLING_HOURS = 24
EVIDENCE_HOURS = 7 * 24  # what counts as "recent" evidence that a number works
EXPLORE_SHARE = 0.10
SUPERVISOR_QUIET_MINUTES = 7 * MINUTES_PER_DAY  # a supervisor's own caller-ID choice is left to run this long


def respond(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, now: int) -> None:
    _explore(store, sim, governance, workflow, now, _check_degraded(store, sim, governance, workflow, now))


# -- degradation ----------------------------------------------------------------------------------
def _check_degraded(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, now: int) -> list[str]:
    """Respond to any degraded caller ID, and return the ones found."""
    wf = workflow["workflow_id"]
    hour = now // 60
    per_caller = calls_by_caller(store.caller_id_counts(wf, hour - ROLLING_HOURS + 1, hour))
    being_explored = _being_explored(store, wf)
    evidence = _evidence(per_caller, being_explored)
    flagged = sorted(c for c, e in evidence["callers"].items() if e["degraded"])
    if not flagged:
        _decided(store, now, wf, KIND, "healthy", evidence)
    else:
        _move_off(store, sim, governance, workflow, now, per_caller, evidence, flagged, being_explored)
    return flagged


def _move_off(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, now: int,
              per_caller: Mapping[str, Calls], evidence: dict, flagged: list[str], being_explored: str | None) -> None:
    wf = workflow["workflow_id"]
    current = sim.config(wf)
    still_weighted = [c for c in flagged if any(caller_id_dist(current["config"], cohort).get(c, 0) > 0
                                                for cohort in current["config"]["allocations"])]
    if not still_weighted:
        return _decided(store, now, wf, KIND, "already_moved", evidence)
    unproven = _unproven(store, workflow, now // 60)
    composed = compose(current, move_off_caller_ids(still_weighted, unproven))
    if not composed.changed_dims:
        return _decided(store, now, wf, KIND, "no_healthy_alternative", evidence)
    problems = violations(workflow, composed)
    if problems:
        return _decided(store, now, wf, KIND, "blocked", {**evidence, "violations": problems})
    # One auto-applied move per number per rolling day: repeated steps would add up to more than max_shift allows.
    may_auto = not set(still_weighted) & _moved_off(store, wf, AUTO_ROUTE, now - MINUTES_PER_DAY)
    auto = may_auto and governance.auto_apply_refusal(wf, KIND, composed, now, incident=True) is None
    if may_auto and not auto:
        # Too big for one auto-apply: stop part of the harm now, once, and let an approver decide the rest.
        step = compose(current, move_off_caller_ids(still_weighted, unproven, limit=governance.max_shift(wf)))
        if step.changed_dims and governance.auto_apply_refusal(wf, KIND, step, now, incident=True) is None:
            composed, auto = step, True
    hold = governance.hold_reason(wf, KIND, now, incident=True, auto=auto)
    if hold is not None:
        return _decided(store, now, wf, KIND, "held", {**evidence, "hold": hold})
    healthy = [c for c, e in evidence["callers"].items() if not e["degraded"] and c != being_explored]
    bad, good = _pool(per_caller, still_weighted), _pool(per_caller, healthy)
    named = ", ".join(still_weighted)
    engaged = sum(per_caller[c].engaged for c in healthy)
    (governance.auto_apply if auto else governance.propose)(Draft(
        workflow_id=wf, kind=KIND, composed=composed, evidence={**evidence, "moved_off": still_weighted},
        change_summary=(f"Move traffic off caller ID {named}: pickup {bad.rate:.1%} against {good.rate:.1%} on the "
                        f"other numbers over the last {ROLLING_HOURS} hours ({bad.trials:,} dials)."),
        rationale=(f"Pickup on {named} is significantly and materially below the workflow's other caller IDs over a "
                   f"rolling {ROLLING_HOURS} hours (at least {stats.CALLER_ID_MIN_DROP_PERCENT}% lower, one-sided "
                   f"p < {stats.CALLER_ID_ALPHA}, at least {stats.CALLER_ID_MIN_DIALS} dials). Its weight goes to "
                   "the numbers with recent evidence, in their existing proportions."),
        # Concluded calls per dial gained on the traffic moved: the pickup gap's lower bound x healthy engagement.
        gain_lower_bound=stats.difference_lower_bound(good, bad) * (engaged / good.successes if good.successes else 0.0),
        affected_share=bad.trials / (bad.trials + good.trials),
        incident=True), now)


# -- exploration ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Plan:
    composed: Composed
    change_summary: str
    rationale: str
    evidence: dict
    gain_lower_bound: float
    affected_share: float


def _explore(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, now: int,
             flagged: list[str]) -> None:
    """At most one caller ID being explored per workflow: start one, or conclude the one running."""
    wf = workflow["workflow_id"]
    if flagged:
        return _noted(store, now, wf, "held", f"caller ID {', '.join(flagged)} is degraded")
    current = sim.config(wf)
    run = store.latest_applied(wf, EXPLORE_KIND)
    being_explored = _being_explored(store, wf)
    table = caller_id_dist(current["config"], "default")
    if run is not None and being_explored in table:
        plan = _conclude(store, workflow, current, table, being_explored, run, now)
    else:
        plan = _start(store, workflow, current, table, now)
    if plan is not None:
        _submit(store, governance, workflow, plan, now)


def _being_explored(store: Store, wf: str) -> str | None:
    """The caller ID whose exploration the controller started and has not concluded."""
    run = store.latest_applied(wf, EXPLORE_KIND)
    evidence = None if run is None else json.loads(run["body"])["evidence"]
    return evidence["caller_id"] if evidence is not None and evidence["action"] == "start" else None


def _start(store: Store, workflow: Mapping, current: Mapping, table: Mapping[str, float], now: int) -> _Plan | None:
    wf, hour = workflow["workflow_id"], now // 60
    edit = store.last_supervisor_edit(wf, "caller_id")
    if edit is not None and now - edit < SUPERVISOR_QUIET_MINUTES:
        _noted(store, now, wf, "held", "a supervisor changed this workflow's caller IDs in the last 7 days")
        return None
    unproven = _unproven(store, workflow, hour)
    if not set(table) - unproven:
        _noted(store, now, wf, "no_proven_numbers", None)
        return None
    in_use = {c for cohort in current["config"]["allocations"] for c in caller_id_dist(current["config"], cohort)}
    candidates = sorted(unproven - in_use - _given_up(store, wf))
    if not candidates:
        _noted(store, now, wf, "nothing_to_explore", None)
        return None
    caller = candidates[0]
    recent = _recent_calls(store, wf, hour).get(caller, Calls())
    return _Plan(
        compose(current, try_caller_id(caller, EXPLORE_SHARE)),
        (f"Try caller ID {caller} on {EXPLORE_SHARE:.0%} of traffic until it has {stats.CALLER_ID_MIN_DIALS} dials: "
         "exploration, not a claimed win."),
        (f"{caller} has {recent.dials} dials in the last {EVIDENCE_HOURS // 24} days, too few to rely on. It gets at "
         f"most {EXPLORE_SHARE:.0%} of traffic until it has {stats.CALLER_ID_MIN_DIALS} dials, then an equal share if "
         "it does at least as well as the numbers in use, or none if it doesn't."),
        {"detector": "caller_id_exploration", "action": "start", "caller_id": caller, "recent_dials": recent.dials,
         "window_hours": EVIDENCE_HOURS},
        0.0, EXPLORE_SHARE)


def _conclude(store: Store, workflow: Mapping, current: Mapping, table: Mapping[str, float], caller: str,
              run: sqlite3.Row, now: int) -> _Plan | None:
    """Compare the number with the others over the calls dialled since its exploration began (the same traffic)."""
    wf = workflow["workflow_id"]
    per_caller = calls_by_caller(store.caller_id_counts(wf, run["applied_minute"] // 60, now // 60, run["applied_version"]))
    tried = per_caller.get(caller, Calls())
    others = _pool(per_caller, [c for c in table if c != caller and c in per_caller])
    if tried.dials < stats.CALLER_ID_MIN_DIALS or not others.trials:
        _noted(store, now, wf, "exploring", None)
        return None
    candidate = stats.Counts(tried.connected, tried.dials)
    keep = stats.screen(others, {caller: candidate}).candidate == caller
    engaged = sum(k.engaged for c, k in per_caller.items() if c != caller and c in table)
    per_connect = engaged / others.successes if others.successes else 0.0
    evidence = {"detector": "caller_id_exploration", "action": "equal_share" if keep else "drop", "caller_id": caller,
                "dials": tried.dials, "connected": tried.connected, "others_dials": others.trials,
                "others_connected": others.successes}
    comparison = f"pickup {candidate.rate:.1%} against {others.rate:.1%} on the numbers in use ({tried.dials:,} dials)"
    if keep:
        new_share = 1 / len(table)
        return _Plan(compose(current, give_equal_share(caller)),
                     f"Give caller ID {caller} an equal share of traffic after exploration: {comparison}.",
                     (f"{caller} did at least as well as the numbers in use over {stats.CALLER_ID_MIN_DIALS}+ dials, and "
                      "was not clearly worse. It takes an equal share; the others keep their proportions."),
                     evidence, stats.difference_lower_bound(candidate, others) * per_connect,
                     max(new_share - table[caller], 0.0))
    composed = compose(current, move_off_caller_ids([caller], _unproven(store, workflow, now // 60) - {caller}))
    if not composed.changed_dims:
        _noted(store, now, wf, "no_proven_alternative", None)
        return None
    return _Plan(composed, f"Stop exploring caller ID {caller}: {comparison}.",
                 (f"{caller} did not beat the numbers in use over {stats.CALLER_ID_MIN_DIALS}+ dials, so it is dropped "
                  "and its traffic returns to them."),
                 evidence, stats.difference_lower_bound(others, candidate) * per_connect, table[caller])


def _submit(store: Store, governance: GovernanceClient, workflow: Mapping, plan: _Plan, now: int) -> None:
    wf = workflow["workflow_id"]
    problems = violations(workflow, plan.composed)
    if problems:
        return _decided(store, now, wf, EXPLORE_KIND, "blocked", {**plan.evidence, "violations": problems})
    auto = governance.auto_apply_refusal(wf, EXPLORE_KIND, plan.composed, now) is None
    hold = governance.hold_reason(wf, EXPLORE_KIND, now, auto=auto)
    if hold is not None:
        return _noted(store, now, wf, "held", hold)
    (governance.auto_apply if auto else governance.propose)(Draft(
        workflow_id=wf, kind=EXPLORE_KIND, composed=plan.composed, change_summary=plan.change_summary,
        rationale=plan.rationale, evidence=plan.evidence, gain_lower_bound=plan.gain_lower_bound,
        affected_share=plan.affected_share), now)


# -- shared ---------------------------------------------------------------------------------------
def _decided(store: Store, now: int, wf: str, kind: str, outcome: str, detail: dict) -> None:
    with store.transaction() as conn:
        Store.record_decision(conn, now, wf, kind, outcome, None, detail)


def _noted(store: Store, now: int, wf: str, outcome: str, hold: str | None) -> None:
    """Exploration's decisions not to act, recorded when they change: the latest row is true until the next one."""
    detail = {} if hold is None else {"hold": hold}
    last = store.last_decision(wf, EXPLORE_KIND)
    if last is None or last["proposal_id"] is not None or (last["outcome"], json.loads(last["detail"])) != (outcome, detail):
        _decided(store, now, wf, EXPLORE_KIND, outcome, detail)


def _unproven(store: Store, workflow: Mapping, hour: int) -> set[str]:
    """The workflow's caller IDs without enough recent dials to be relied on."""
    recent = _recent_calls(store, workflow["workflow_id"], hour)
    return {c for c in workflow["options"]["caller_id"] if recent.get(c, Calls()).dials < stats.CALLER_ID_MIN_DIALS}


def _recent_calls(store: Store, wf: str, hour: int) -> dict[str, Calls]:
    return calls_by_caller(store.caller_id_counts(wf, hour - EVIDENCE_HOURS + 1, hour))


def _moved_off(store: Store, wf: str, route: str | None = None, since_minute: int | None = None) -> set[str]:
    """The caller IDs an applied incident response has taken weight off."""
    return {c for e in store.applied_evidence(wf, KIND, route, since_minute) for c in e["moved_off"]}


def _given_up(store: Store, wf: str) -> set[str]:
    """Numbers that have already failed here. Dropped means dropped, even once their evidence has aged out."""
    dropped = {e["caller_id"] for e in store.applied_evidence(wf, EXPLORE_KIND) if e["action"] == "drop"}
    return dropped | _moved_off(store, wf)


def _pool(per_caller: Mapping[str, Calls], callers: Iterable[str]) -> stats.Counts:
    """Connected out of dials over these callers."""
    callers = list(callers)
    return stats.Counts(sum(per_caller[c].connected for c in callers), sum(per_caller[c].dials for c in callers))


def _evidence(per_caller: Mapping[str, Calls], being_explored: str | None) -> dict:
    """Each number against the others. One still being explored must not get a proven one flagged, so it is not an "other"."""
    callers = {}
    for c, calls in sorted(per_caller.items()):
        others = _pool(per_caller, (o for o in per_caller if o != c and o != being_explored))
        verdict = stats.caller_id_degraded(stats.Counts(calls.connected, calls.dials), others)
        callers[c] = {"dials": calls.dials, "connected": calls.connected, "others_dials": others.trials,
                      "others_connected": others.successes, "p_value": verdict.p_value, "degraded": verdict.holds}
    return {"detector": "caller_id_degradation", "window_hours": ROLLING_HOURS, "callers": callers}
