"""Cohorts: a workflow's disjoint partition and how it is chosen. Pure functions, no I/O (ADR-0004).

A partition splits one feature's values into groups. The largest group is `default`, so it never
needs writing into a config; every other group is a cohort with a stable ID derived from its
values. A lead belongs to the cohort whose values include its own, otherwise to `default`: the same
rule the simulator applies, so a feature value history never saw lands in the same place in both.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

FEATURES = ("lang", "city_tier", "age_band", "value_band")
DEFAULT = "default"
MIN_GROUP_SHARE = 0.10

# Primary KPI -> (cell counter it is measured over, cell counter that counts a success).
KPI_COUNTERS = {"pickup_rate": ("dials", "connected"), "engagement_rate": ("connected", "engaged")}

_LANGUAGES = {"as": "Assamese", "bn": "Bengali", "en": "English", "gu": "Gujarati", "hi": "Hindi", "kn": "Kannada",
              "ml": "Malayalam", "mr": "Marathi", "or": "Odia", "pa": "Punjabi", "ta": "Tamil", "te": "Telugu"}


@dataclass(frozen=True)
class Cohort:
    id: str
    name: str
    where: dict[str, list[str]]  # for `default`, the values it held in history (informational)


@dataclass(frozen=True)
class Partition:
    feature: str | None   # None: no feature splits this workflow's traffic
    spread: float | None  # highest group's KPI rate minus lowest group's
    cohorts: tuple[Cohort, ...] = field(default=())  # named cohorts by ID, then `default` last

    def cohort_of(self, features: Mapping[str, str]) -> str:
        """The cohort a lead with these features belongs to."""
        for c in self.cohorts:
            if c.id != DEFAULT and all(features[f] in values for f, values in c.where.items()):
                return c.id
        return DEFAULT

    def config_cohorts(self, only: Collection[str] | None = None) -> list[dict]:
        """The `cohorts` list for a workflow config. `default` is implicit, so it is never written.

        Pass `only` to write just the cohorts a config needs; the rest then fall to `default`.
        """
        return [{"id": c.id, "where": c.where} for c in self.cohorts
                if c.id != DEFAULT and (only is None or c.id in only)]


@dataclass(frozen=True)
class _Group:
    values: tuple[str, ...]
    traffic: int
    successes: int

    @property
    def rate(self) -> float:
        return self.successes / self.traffic


def choose_partition(cells: Iterable[tuple[Mapping[str, str], int, int]]) -> Partition:
    """The single feature whose groups differ most on the KPI. `cells`: (features, traffic, successes)."""
    cells = list(cells)
    best: tuple[str, list[_Group], float] | None = None
    for feature in sorted(FEATURES):
        groups = _merge_small(_groups(cells, feature))
        if len(groups) < 2:
            continue
        spread = max(g.rate for g in groups) - min(g.rate for g in groups)
        if best is None or spread > best[2]:
            best = feature, groups, spread
    if best is None:
        return Partition(None, None, (Cohort(DEFAULT, "everyone", {}),))
    feature, groups, spread = best
    largest = min(groups, key=lambda g: (-g.traffic, g.values))
    named = sorted((Cohort(_cohort_id(feature, g.values), _name(feature, g.values), {feature: list(g.values)})
                    for g in groups if g is not largest), key=lambda c: c.id)
    default = Cohort(DEFAULT, f"everyone else ({_name(feature, largest.values)})", {feature: list(largest.values)})
    return Partition(feature, spread, (*named, default))


def _groups(cells: list[tuple[Mapping[str, str], int, int]], feature: str) -> list[_Group]:
    """One group per value of `feature` that has any traffic."""
    totals: dict[str, list[int]] = {}
    for feats, traffic, successes in cells:
        t = totals.setdefault(feats[feature], [0, 0])
        t[0] += traffic
        t[1] += successes
    return [_Group((v,), t, s) for v, (t, s) in sorted(totals.items()) if t]


def _merge_small(groups: list[_Group]) -> list[_Group]:
    """Fold each group under MIN_GROUP_SHARE of traffic into the group with the nearest KPI rate."""
    total = sum(g.traffic for g in groups)
    while len(groups) > 1:
        smallest = min(groups, key=lambda g: (g.traffic, g.values))
        if smallest.traffic / total >= MIN_GROUP_SHARE:
            break
        groups.remove(smallest)
        nearest = min(groups, key=lambda g: (abs(g.rate - smallest.rate), g.values))
        groups.remove(nearest)
        groups.append(_Group(tuple(sorted(smallest.values + nearest.values)),
                             smallest.traffic + nearest.traffic, smallest.successes + nearest.successes))
    return groups


def _cohort_id(feature: str, values: tuple[str, ...]) -> str:
    return feature + "_" + "_".join(re.sub(r"[^0-9a-z]+", "_", v.lower().replace("+", "plus")).strip("_") for v in values)


def _name(feature: str, values: tuple[str, ...]) -> str:
    if feature == "lang":
        return " or ".join(_LANGUAGES.get(v, v) for v in values) + " speakers"
    joined = " or ".join(values)
    return {"age_band": f"age {joined}", "city_tier": f"city tier {joined}", "value_band": f"{joined} value"}[feature]
