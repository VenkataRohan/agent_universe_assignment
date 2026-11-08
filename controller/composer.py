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


def move_off_caller_ids(degraded: Collection[str]) -> Intent:
    """Take all weight off `degraded`, sharing it among the numbers each allocation already uses."""
    def intent(config: dict) -> None:
        allocations = config["allocations"]
        fallback = _healthy(allocations.get("default", {}).get("caller_id", {}), degraded)
        for cohort, dims in allocations.items():
            dist = dims.get("caller_id")
            if not dist or not any(dist.get(c, 0) > 0 for c in degraded):
                continue
            healthy = _healthy(dist, degraded)
            if healthy:
                total = sum(healthy.values())
                dims["caller_id"] = {c: round(w / total, 6) for c, w in healthy.items()}
            elif cohort != "default" and fallback:
                del dims["caller_id"]  # a cohort with no caller-ID table of its own uses default's
    return intent


def _healthy(dist: Mapping[str, float], degraded: Collection[str]) -> dict[str, float]:
    return {c: w for c, w in dist.items() if c not in degraded and w > 0}


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


def caller_id_dist(config: Mapping, cohort: str) -> dict[str, float]:
    """A cohort's caller-ID weights, normalised; a cohort that omits the dimension uses default's."""
    allocations = config["allocations"]
    dist = allocations.get(cohort, {}).get("caller_id") or allocations.get("default", {}).get("caller_id") or {}
    return _normalised(dist) or {}


def _normalised(dist: Mapping[str, float] | None) -> dict[str, float] | None:
    if not dist:
        return None
    total = sum(dist.values())
    return {k: round(w / total, 6) for k, w in sorted(dist.items()) if w > 0}


def _diff(base: Mapping, new: Mapping) -> dict:
    diff: dict = {}
    if base["cohorts"] != new["cohorts"]:
        diff["cohorts"] = {"from": base["cohorts"], "to": new["cohorts"]}
    before, after = base["allocations"], new["allocations"]
    for dim in ALLOCATION_DIMS:
        per_cohort = {}
        for cohort in sorted({*before, *after}):
            a = _normalised(before.get(cohort, {}).get(dim))
            b = _normalised(after.get(cohort, {}).get(dim))
            if a != b:
                per_cohort[cohort] = {"from": a, "to": b}
        if per_cohort:
            diff[dim] = per_cohort
    for dim in ("prompt_version", "schedule"):
        if base[dim] != new[dim]:
            diff[dim] = {"from": base[dim], "to": new[dim]}
    return diff
