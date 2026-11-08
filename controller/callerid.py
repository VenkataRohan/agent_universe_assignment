"""Caller-ID responder: notice a degraded number and ask to move weight off it (ADR-0002).

Runs every tick over a rolling day. Every pass leaves one decision behind, "healthy" included, so
"why didn't it change?" can be answered from the store.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from . import stats
from .composer import caller_id_dist, compose, move_off_caller_ids, violations
from .governance import Draft, GovernanceClient
from .replay import Calls, calls_by_caller
from .simclient import SimClient
from .store import Store

KIND = "caller_id"
ROLLING_HOURS = 24


def check(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, now: int) -> None:
    wf = workflow["workflow_id"]
    hour = now // 60
    per_caller = calls_by_caller(store.caller_id_counts(wf, hour - ROLLING_HOURS + 1, hour))
    evidence = _evidence(per_caller)
    flagged = sorted(c for c, e in evidence["callers"].items() if e["degraded"])
    if not flagged:
        return _decided(store, now, wf, "healthy", evidence)
    current = sim.config(wf)
    still_weighted = [c for c in flagged if any(caller_id_dist(current["config"], cohort).get(c, 0) > 0
                                                for cohort in current["config"]["allocations"])]
    if not still_weighted:
        return _decided(store, now, wf, "already_moved", evidence)
    hold = governance.hold_reason(wf, KIND, now)
    if hold is not None:
        return _decided(store, now, wf, "held", {**evidence, "hold": hold})
    composed = compose(current, move_off_caller_ids(still_weighted))
    if not composed.changed_dims:
        return _decided(store, now, wf, "no_healthy_alternative", evidence)
    problems = violations(workflow, composed)
    if problems:
        return _decided(store, now, wf, "blocked", {**evidence, "violations": problems})
    healthy = [c for c, e in evidence["callers"].items() if not e["degraded"]]
    bad, good = _pool(per_caller, still_weighted), _pool(per_caller, healthy)
    named = ", ".join(still_weighted)
    engaged = sum(per_caller[c].engaged for c in healthy)
    governance.propose(Draft(
        workflow_id=wf, kind=KIND, composed=composed, evidence=evidence,
        change_summary=(f"Move traffic off caller ID {named}: pickup {bad.rate:.1%} against {good.rate:.1%} on the "
                        f"other numbers over the last {ROLLING_HOURS} hours ({bad.trials:,} dials)."),
        rationale=(f"Pickup on {named} is significantly and materially below the workflow's other caller IDs over a "
                   f"rolling {ROLLING_HOURS} hours (at least {stats.CALLER_ID_MIN_DROP_PERCENT}% lower, one-sided "
                   f"p < {stats.CALLER_ID_ALPHA}, at least {stats.CALLER_ID_MIN_DIALS} dials). Its weight goes to "
                   "the numbers already in use, in their existing proportions."),
        # Concluded calls per dial gained on the traffic moved: the pickup gap's lower bound x healthy engagement.
        gain_lower_bound=stats.difference_lower_bound(good, bad) * (engaged / good.successes if good.successes else 0.0),
        affected_share=bad.trials / (bad.trials + good.trials),
        incident=True), now)


def _decided(store: Store, now: int, wf: str, outcome: str, detail: dict) -> None:
    with store.transaction() as conn:
        Store.record_decision(conn, now, wf, KIND, outcome, None, detail)


def _pool(per_caller: Mapping[str, Calls], callers: Iterable[str]) -> stats.Counts:
    """Connected out of dials over these callers."""
    callers = list(callers)
    return stats.Counts(sum(per_caller[c].connected for c in callers), sum(per_caller[c].dials for c in callers))


def _evidence(per_caller: Mapping[str, Calls]) -> dict:
    callers = {}
    for c, calls in sorted(per_caller.items()):
        others = _pool(per_caller, (o for o in per_caller if o != c))
        verdict = stats.caller_id_degraded(stats.Counts(calls.connected, calls.dials), others)
        callers[c] = {"dials": calls.dials, "connected": calls.connected, "others_dials": others.trials,
                      "others_connected": others.successes, "p_value": verdict.p_value, "degraded": verdict.holds}
    return {"detector": "caller_id_degradation", "window_hours": ROLLING_HOURS, "callers": callers}
