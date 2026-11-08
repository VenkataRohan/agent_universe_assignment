"""What a Proposal predicts: its dry run and the leads it will reach. Read-only over the store.

The dry run replays the last 3 days' traffic under the current and the proposed allocation, using
each option's own measured rates, so the only thing that differs between the two is the mix.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from statistics import NormalDist
from typing import NamedTuple

from .cohorts import FEATURES, Partition
from .composer import ALLOCATION_DIMS, Composed, config_cohort_of, effective_dist, effective_hours
from .simtime import MINUTES_PER_DAY
from .store import Store, load_partition

DRY_RUN_HOURS = 72
_Z95 = NormalDist().inv_cdf(0.975)


class Calls(NamedTuple):
    dials: int = 0
    connected: int = 0
    engaged: int = 0


def calls_by_caller(rows: Iterable[sqlite3.Row]) -> dict[str, Calls]:
    return _calls_by(rows, "caller_id")


def _calls_by(rows: Iterable[sqlite3.Row], dim: str) -> dict[str, Calls]:
    out: dict[str, Calls] = {}
    for r in rows:
        c = out.get(r[dim], Calls())
        out[r[dim]] = Calls(c.dials + r["dials"], c.connected + r["connected"], c.engaged + r["engaged"])
    return out


def affected_leads(store: Store, workflow: Mapping, composed: Composed, now: int) -> int:
    """Leads this change will reach: active campaigns of affected cohorts (unless locked) plus tomorrow's."""
    first_active_day = now // MINUTES_PER_DAY - len(workflow["cadence"]) + 1
    total = affected = hours_affected = 0
    for r in store.active_leads_by_cell(workflow["workflow_id"], first_active_day):
        total += r["leads"]
        features = {f: r[f] for f in FEATURES}
        cohort_before, cohort_after = (config_cohort_of(c, features) for c in (composed.base_config, composed.config))
        hours_change = effective_hours(composed.base_config, cohort_before) != effective_hours(composed.config, cohort_after)
        if hours_change:
            hours_affected += r["leads"]
        if hours_change or any(effective_dist(composed.base_config, cohort_before, d) != effective_dist(composed.config, cohort_after, d)
                               for d in ALLOCATION_DIMS):
            affected += r["leads"]
    # A schedule applies to running campaigns whatever the mid-flight rule says.
    running = hours_affected if workflow["mid_flight"] == "next_campaign_only" else affected
    return running + round(workflow["daily_batch"]["size"] * (affected / total if total else 1.0))


def dry_run(store: Store, workflow: Mapping, composed: Composed, now: int) -> dict:
    wf, hour = workflow["workflow_id"], now // 60
    rows = store.option_counts(wf, hour - DRY_RUN_HOURS + 1, hour)
    before, after = composed.base_config, composed.config
    objective = workflow["objective"]
    primary, guardrail = objective["primary"], next(iter(objective["guardrail"])).removesuffix("_max_drop")
    cohorts = _cohort_replays(rows, before, after, load_partition(store.conn, wf))
    risky = sorted(c for c, r in cohorts.items() if any(at_risk(r[k]) for k in (primary, guardrail)))
    return {"window_hours": DRY_RUN_HOURS, "dials_replayed": sum(r["dials"] for r in rows),
            "primary_kpi": primary, "guardrail_kpi": guardrail, "overall": _replay(rows, before, after),
            "cohorts": cohorts, "cohorts_at_risk": risky}


def at_risk(kpi: Mapping) -> bool:
    """The predicted drop is larger than the prediction's own uncertainty."""
    return kpi["current"] is not None and kpi["proposed"] is not None and kpi["current"] - kpi["proposed"] > kpi["margin"]


def _cohort_replays(rows: list, before: Mapping, after: Mapping, partition: Partition | None) -> dict[str, dict]:
    if partition is None:
        return {}
    by_cohort: dict[str, list] = {c.id: [] for c in partition.cohorts}
    for r in rows:
        by_cohort[partition.cohort_of({f: r[f] for f in FEATURES})].append(r)
    return {cid: _replay(cells, before, after) for cid, cells in by_cohort.items() if cells}


def _replay(rows: list, before: Mapping, after: Mapping) -> dict:
    """Predicted KPIs of this traffic under both allocations, with 95% margins on the proposed ones."""
    # Welcome and voice are heard after pickup, so only caller ID moves the pickup prediction (ADR-0007).
    return {"pickup_rate": _predict(rows, before, after, _pickup, ()),
            "engagement_rate": _predict(rows, before, after, _engagement, ("welcome_message", "voice"))}


_Rate = Callable[[Mapping[str, Calls], Mapping[str, float]], tuple[float | None, float]]


def _predict(rows: list, before: Mapping, after: Mapping, rate: _Rate, heard_dims: tuple[str, ...]) -> dict:
    """The rate under the caller-ID mix, moved by how far any change in the mix of `heard_dims` moves it."""
    per_caller = calls_by_caller(rows)
    current, _ = rate(per_caller, _mix(rows, before, "caller_id"))
    proposed, margin = rate(per_caller, _mix(rows, after, "caller_id"))
    variance = margin ** 2
    for dim in heard_dims:
        mix_before, mix_after = _mix(rows, before, dim), _mix(rows, after, dim)
        if mix_before == mix_after or proposed is None:
            continue
        calls = _calls_by(rows, dim)
        (was, _), (will_be, moved_margin) = rate(calls, mix_before), rate(calls, mix_after)
        if was is not None and will_be is not None:
            proposed += will_be - was
            variance += moved_margin ** 2
    return {"current": current, "proposed": None if proposed is None else round(proposed, 4), "margin": round(math.sqrt(variance), 4)}


def _mix(rows: list, config: Mapping, dim: str) -> dict[str, float]:
    """The share of this traffic each option of `dim` would carry under `config`."""
    by_cell: dict[tuple, int] = {}
    for r in rows:
        key = tuple(r[f] for f in FEATURES)
        by_cell[key] = by_cell.get(key, 0) + r["dials"]
    total = sum(by_cell.values())
    mix: dict[str, float] = {}
    for key, dials in by_cell.items():
        for c, w in effective_dist(config, config_cohort_of(config, dict(zip(FEATURES, key))), dim).items():
            mix[c] = mix.get(c, 0.0) + w * dials / total
    return mix


def _pickup(per_caller: Mapping[str, Calls], mix: Mapping[str, float]) -> tuple[float | None, float]:
    calls = {c: per_caller.get(c, Calls()) for c in mix}
    return _weighted_rate(mix, {c: k.connected for c, k in calls.items()}, {c: k.dials for c, k in calls.items()})


def _engagement(per_caller: Mapping[str, Calls], mix: Mapping[str, float]) -> tuple[float | None, float]:
    """Engaged per connect: each caller ID counts by the connects it would bring, not by its dials."""
    calls = {c: per_caller.get(c, Calls()) for c in mix}
    connects = {c: w * calls[c].connected / calls[c].dials for c, w in mix.items() if calls[c].dials}
    return _weighted_rate(connects, {c: calls[c].engaged for c in connects}, {c: calls[c].connected for c in connects})


def _weighted_rate(weights: Mapping[str, float], successes: Mapping[str, int],
                   trials: Mapping[str, int]) -> tuple[float | None, float]:
    """The weighted rate and its 95% margin, over the callers that have any trials."""
    measured = {c: w for c, w in weights.items() if trials[c] and w > 0}
    total = sum(measured.values())
    if not total:
        return None, 0.0
    shares = {c: w / total for c, w in measured.items()}
    rates = {c: successes[c] / trials[c] for c in shares}
    return (round(sum(s * rates[c] for c, s in shares.items()), 4),
            round(_Z95 * math.sqrt(sum(s ** 2 * rates[c] * (1 - rates[c]) / trials[c] for c, s in shares.items())), 4))
