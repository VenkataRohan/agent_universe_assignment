"""Bootstrapping from history: each workflow's baselines, analysis partition and hourly KPI rates.

Everything here reads only days before D0, so the result can't depend on when the controller
started, how much live data it has seen, or whether it restarted.
"""
from __future__ import annotations

import sqlite3

from .cohorts import FEATURES, KPI_COUNTERS, choose_partition
from .store import Store


def bootstrap_from_history(store: Store, workflows: list[dict]) -> None:
    """Analyse every workflow that hasn't been analysed yet, all in one transaction."""
    with store.transaction() as conn:
        done = Store.analysed_workflows(conn)
        for w in workflows:
            if w["workflow_id"] not in done:
                _analyse(conn, w)


def _analyse(conn: sqlite3.Connection, workflow: dict) -> None:
    wf, primary = workflow["workflow_id"], workflow["objective"]["primary"]
    traffic_col, successes_col = KPI_COUNTERS[primary]
    rows = Store.history_call_counts(conn, wf)
    per_cell: dict[tuple[str, ...], list[int]] = {}
    for r in rows:
        c = per_cell.setdefault(tuple(r[f] for f in FEATURES), [0, 0])
        c[0] += r[traffic_col]
        c[1] += r[successes_col]
    partition = choose_partition((dict(zip(FEATURES, k)), t, s) for k, (t, s) in per_cell.items())
    Store.save_baselines(conn, wf, primary, partition, sum(r["dials"] for r in rows),
                         sum(r["connected"] for r in rows), sum(r["engaged"] for r in rows), sum(r["flags"] for r in rows))
    Store.save_cohorts(conn, wf, partition)
    Store.save_hour_rates(conn, wf, hour_rates(
        [(partition.cohort_of({f: r[f] for f in FEATURES}), r["hour"], r["band"], r[traffic_col], r[successes_col])
         for r in rows]))


def hour_rates(rows: list[tuple[str, int, int, int, int]]) -> list[tuple[str, int, int, int, float]]:
    """Per cohort and hour: (cohort, hour, traffic, successes, attempt-adjusted rate).

    `rows`: (cohort, hour, attempt band, traffic, successes). Later attempts convert differently,
    and the attempt mix differs by hour, so a raw hourly rate partly measures who was being
    retried then. Indirect standardisation removes that: the hour's successes over the successes
    expected from its attempt mix at the cohort's own per-band rates, scaled to the cohort's
    overall rate. It stays defined for hours that saw only some attempt bands.
    """
    band_totals: dict[tuple[str, int], list[int]] = {}
    cohort_totals: dict[str, list[int]] = {}
    hour_totals: dict[tuple[str, int], list[int]] = {}
    for c, h, a, traffic, successes in rows:
        for t in (band_totals.setdefault((c, a), [0, 0]), cohort_totals.setdefault(c, [0, 0]),
                  hour_totals.setdefault((c, h), [0, 0])):
            t[0] += traffic
            t[1] += successes
    expected: dict[tuple[str, int], float] = {}
    for c, h, a, traffic, _ in rows:
        if traffic:
            band_traffic, band_successes = band_totals[(c, a)]
            expected[(c, h)] = expected.get((c, h), 0.0) + traffic * band_successes / band_traffic
    out = []
    for (c, h), (traffic, successes) in sorted(hour_totals.items()):
        if traffic:
            overall = cohort_totals[c][1] / cohort_totals[c][0]
            out.append((c, h, traffic, successes,
                        min(1.0, successes / expected[(c, h)] * overall) if expected[(c, h)] else 0.0))
    return out
