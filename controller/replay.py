"""What a Proposal predicts: its dry run and the leads it will reach. Read-only over the store.

The dry run replays the last 3 days' traffic under the current and the proposed allocation, using
each caller ID's own measured rates, so the only thing that differs between the two is the mix.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from statistics import NormalDist
from typing import NamedTuple

from .cohorts import FEATURES, Partition
from .composer import ALLOCATION_DIMS, Composed, caller_id_dist, config_cohort_of, effective_dist
from .simtime import MINUTES_PER_DAY
from .store import Store, load_partition

DRY_RUN_HOURS = 72
_Z95 = NormalDist().inv_cdf(0.975)


class Calls(NamedTuple):
    dials: int = 0
    connected: int = 0
    engaged: int = 0


def calls_by_caller(rows: Iterable[sqlite3.Row]) -> dict[str, Calls]:
    out: dict[str, Calls] = {}
    for r in rows:
        c = out.get(r["caller_id"], Calls())
        out[r["caller_id"]] = Calls(c.dials + r["dials"], c.connected + r["connected"], c.engaged + r["engaged"])
    return out


def affected_leads(store: Store, workflow: Mapping, composed: Composed, now: int) -> int:
    """Leads this change will reach: active campaigns of affected cohorts (unless locked) plus tomorrow's."""
    first_active_day = now // MINUTES_PER_DAY - len(workflow["cadence"]) + 1
    total = affected = 0
    for r in store.active_leads_by_cell(workflow["workflow_id"], first_active_day):
        total += r["leads"]
        features = {f: r[f] for f in FEATURES}
        cohort_before, cohort_after = (config_cohort_of(c, features) for c in (composed.base_config, composed.config))
        if any(effective_dist(composed.base_config, cohort_before, d) != effective_dist(composed.config, cohort_after, d)
               for d in ALLOCATION_DIMS):
            affected += r["leads"]
    running = 0 if workflow["mid_flight"] == "next_campaign_only" else affected
    return running + round(workflow["daily_batch"]["size"] * (affected / total if total else 1.0))


def dry_run(store: Store, workflow: Mapping, composed: Composed, now: int) -> dict:
    wf, hour = workflow["workflow_id"], now // 60
    rows = store.caller_id_counts(wf, hour - DRY_RUN_HOURS + 1, hour)
    before, after = composed.base_config, composed.config
    objective = workflow["objective"]
    primary, guardrail = objective["primary"], next(iter(objective["guardrail"])).removesuffix("_max_drop")
    cohorts = _cohort_replays(rows, before, after, load_partition(store.conn, wf))
    at_risk = sorted(c for c, r in cohorts.items() if any(_at_risk(r[k]) for k in (primary, guardrail)))
    return {"window_hours": DRY_RUN_HOURS, "dials_replayed": sum(r["dials"] for r in rows),
            "primary_kpi": primary, "guardrail_kpi": guardrail, "overall": _replay(rows, before, after),
            "cohorts": cohorts, "cohorts_at_risk": at_risk}


def _at_risk(kpi: Mapping) -> bool:
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
    per_caller = calls_by_caller(rows)
    current, proposed = _mix(rows, before), _mix(rows, after)
    pickup, engagement = _pickup(per_caller, proposed), _engagement(per_caller, proposed)
    return {"pickup_rate": {"current": _pickup(per_caller, current)[0], "proposed": pickup[0], "margin": pickup[1]},
            "engagement_rate": {"current": _engagement(per_caller, current)[0], "proposed": engagement[0],
                                "margin": engagement[1]}}


def _mix(rows: list, config: Mapping) -> dict[str, float]:
    """The share of this traffic each caller ID would carry under `config`."""
    by_cell: dict[tuple, int] = {}
    for r in rows:
        key = tuple(r[f] for f in FEATURES)
        by_cell[key] = by_cell.get(key, 0) + r["dials"]
    total = sum(by_cell.values())
    mix: dict[str, float] = {}
    for key, dials in by_cell.items():
        for c, w in caller_id_dist(config, config_cohort_of(config, dict(zip(FEATURES, key)))).items():
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
