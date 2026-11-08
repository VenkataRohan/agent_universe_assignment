"""Config composer: a proposed config is always the current config plus the controller's intents.

Starting from the current config is what preserves supervisor edits: an intent only touches what
it means to change, so everything else (a supervisor's included) carries over untouched. Pure, no I/O.
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

ALLOCATION_DIMS = ("welcome_message", "voice", "caller_id")
MAX_COHORTS = 20
_ROUNDING_MARGIN = 1e-5  # rounded weights must not land a capped move just over its limit

Intent = Callable[[dict], None]  # edits a proposed config in place


@dataclass(frozen=True)
class Composed:
    base_version: int
    base_config: dict
    config: dict
    diff: dict
    changed_dims: tuple[str, ...]


def compose(current: Mapping, *intents: Intent) -> Composed:
    """`current`: the response of GET /workflows/{id}/config."""
    proposed = copy.deepcopy(current["config"])
    for intent in intents:
        intent(proposed)
    diff = _diff(current["config"], proposed)
    return Composed(current["version"], current["config"], proposed, diff, tuple(sorted(diff)))


def move_off_caller_ids(degraded: Collection[str], unproven: Collection[str] = (), limit: float | None = None) -> Intent:
    """Take weight off `degraded`, sharing it among the proven numbers each allocation already uses.

    An `unproven` number keeps exactly the share it has: moving weight must never raise an untested number.
    `limit` caps how much of an allocation moves; without one, all of the degraded numbers' weight goes.
    """
    def intent(config: dict) -> None:
        allocations = config["allocations"]
        fallback = _receivers(_shares(allocations.get("default", {}).get("caller_id", {})), degraded, unproven)
        for cohort, dims in allocations.items():
            shares = _shares(dims.get("caller_id") or {})
            leaving = sum(s for c, s in shares.items() if c in degraded)
            if not leaving:
                continue
            receivers = _receivers(shares, degraded, unproven)
            if receivers:
                moved = leaving if limit is None else min(leaving, max(limit - _ROUNDING_MARGIN, 0.0))
                dims["caller_id"] = _moved(shares, degraded, receivers, leaving, moved)
            elif cohort != "default" and fallback and limit is None:
                del dims["caller_id"]  # a cohort with no caller-ID table of its own uses default's
    return intent


def try_caller_id(caller: str, share: float) -> Intent:
    """Give `caller` this share of default's traffic, scaling the numbers already there to make room."""
    def intent(config: dict) -> None:
        dims = config["allocations"]["default"]
        dims["caller_id"] = {**{c: round(s * (1 - share), 6) for c, s in _shares(dims["caller_id"]).items()}, caller: share}
    return intent


def give_equal_share(caller: str) -> Intent:
    """Give `caller` an equal share of every allocation it is in; the others keep their proportions."""
    def intent(config: dict) -> None:
        for dims in config["allocations"].values():
            shares = _shares(dims.get("caller_id") or {})
            if caller not in shares:
                continue
            equal, others = 1 / len(shares), 1 - shares[caller]
            dims["caller_id"] = {c: round(equal if c == caller else s / others * (1 - equal), 6) for c, s in shares.items()}
    return intent


def set_distribution(dim: str, distribution: Mapping[str, float]) -> Intent:
    """Replace default's weights on one dimension."""
    def intent(config: dict) -> None:
        config["allocations"]["default"][dim] = dict(distribution)
    return intent


def _shares(dist: Mapping[str, float]) -> dict[str, float]:
    total = sum(dist.values())
    return {c: w / total for c, w in dist.items() if w > 0}


def _receivers(shares: Mapping[str, float], degraded: Collection[str], unproven: Collection[str]) -> dict[str, float]:
    return {c: s for c, s in shares.items() if c not in degraded and c not in unproven}


def _moved(shares: Mapping[str, float], degraded: Collection[str], receivers: Mapping[str, float],
           leaving: float, moved: float) -> dict[str, float]:
    """`moved` of the traffic goes from the degraded numbers to the receivers, each pro rata to its share."""
    kept = 1 - moved / leaving
    received = sum(receivers.values())
    out = {c: s * kept if c in degraded else s + moved * s / received if c in receivers else s for c, s in shares.items()}
    return {c: round(s, 6) for c, s in out.items() if round(s, 6) > 0}


def largest_shift(composed: Composed) -> float:
    """The furthest any cohort's welcome, voice or caller-ID distribution moves, in total-variation distance."""
    before, after = composed.base_config, composed.config
    cohorts = {"default", *before["allocations"], *after["allocations"]}
    shifts = [0.0]
    for dim, cohort in ((d, c) for d in ALLOCATION_DIMS for c in cohorts):
        a, b = effective_dist(before, cohort, dim), effective_dist(after, cohort, dim)
        shifts.append(sum(abs(a.get(k, 0) - b.get(k, 0)) for k in {*a, *b}) / 2)
    return round(max(shifts), 6)


def violations(workflow: Mapping, composed: Composed) -> list[str]:
    """Why the platform would refuse this config: options, cohort limit, dialling window."""
    config, options, out = composed.config, workflow["options"], []
    if len(config["cohorts"]) > MAX_COHORTS:
        out.append(f"{len(config['cohorts'])} cohorts, at most {MAX_COHORTS} are allowed")
    for cohort, dims in config["allocations"].items():
        for dim in ALLOCATION_DIMS:
            dist = dims.get(dim)
            if dist is None:
                continue
            unknown = sorted(set(dist) - set(options[dim]))
            if unknown:
                out.append(f"{cohort}: {dim} options {unknown} are not offered on this workflow")
            if sum(dist.values()) <= 0:
                out.append(f"{cohort}: {dim} has no positive weight")
    if config["prompt_version"] not in options["prompt_version"]:
        out.append(f"prompt {config['prompt_version']} is not offered on this workflow")
    start, end = workflow["dialling_window"]
    for table, hours in config["schedule"]["hour_weights"].items():
        outside = sorted(h for h in map(int, hours) if not start <= h < end)
        if outside:
            out.append(f"schedule {table}: hours {outside} are outside the dialling window")
    return out


def config_cohort_of(config: Mapping, features: Mapping[str, str]) -> str:
    """The config cohort a lead falls into: the simulator's first-match rule."""
    for c in config["cohorts"]:
        if all(features[f] in values for f, values in c["where"].items()):
            return c["id"]
    return "default"


def effective_dist(config: Mapping, cohort: str, dim: str) -> dict[str, float]:
    """A cohort's weights on one dimension, normalised; a cohort that omits the dimension uses default's."""
    allocations = config["allocations"]
    return normalised(allocations.get(cohort, {}).get(dim) or allocations.get("default", {}).get(dim)) or {}


def caller_id_dist(config: Mapping, cohort: str) -> dict[str, float]:
    return effective_dist(config, cohort, "caller_id")


def normalised(dist: Mapping[str, float] | None) -> dict[str, float] | None:
    if not dist:
        return None
    total = sum(dist.values())
    return {k: round(w / total, 6) for k, w in sorted(dist.items()) if w > 0}


def describe_change(dim: str, change: Mapping) -> str:
    """One dimension's from -> to, in a line a supervisor can read. A schedule table is too long to quote."""
    if dim == "schedule":
        return "schedule changes"
    if "from" in change:
        return f"{dim} {change['from']} -> {change['to']}"
    return "; ".join(f"{dim} for {cohort} {c['from']} -> {c['to']}" for cohort, c in sorted(change.items()))


def _diff(base: Mapping, new: Mapping) -> dict:
    diff: dict = {}
    if base["cohorts"] != new["cohorts"]:
        diff["cohorts"] = {"from": base["cohorts"], "to": new["cohorts"]}
    before, after = base["allocations"], new["allocations"]
    for dim in ALLOCATION_DIMS:
        per_cohort = {}
        for cohort in sorted({*before, *after}):
            a = normalised(before.get(cohort, {}).get(dim))
            b = normalised(after.get(cohort, {}).get(dim))
            if a != b:
                per_cohort[cohort] = {"from": a, "to": b}
        if per_cohort:
            diff[dim] = per_cohort
    for dim in ("prompt_version", "schedule"):
        if base[dim] != new[dim]:
            diff[dim] = {"from": base[dim], "to": new[dim]}
    return diff
