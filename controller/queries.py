"""Query interface: read-only questions answered from the store. The CLI is a thin wrapper.

Each query opens its own read-only connection, so it can run while the controller writes
(WAL readers never block the writer, nor are blocked by it).
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Collection
from datetime import date, timedelta
from pathlib import Path

from .cohorts import FEATURES, KPI_COUNTERS, Partition
from .store import connect, load_partition


def daily_kpis(db: str | Path) -> list[dict]:
    """Call counters per workflow and local date, summed over feature cells."""
    conn = connect(db, readonly=True)
    try:
        d0 = date.fromisoformat(conn.execute("SELECT d0_date FROM run").fetchone()[0])
        rows = conn.execute(
            "SELECT workflow_id, day, SUM(dials) dials, SUM(connected) connected, SUM(engaged) engaged, "
            "SUM(hallucination_flags) hallucination_flags FROM cell_agg "
            "GROUP BY workflow_id, day ORDER BY workflow_id, day").fetchall()
    finally:
        conn.close()
    return [{"workflow_id": r["workflow_id"], "date": (d0 + timedelta(days=r["day"])).isoformat(),
             "dials": r["dials"], "connected": r["connected"], "engaged": r["engaged"],
             "hallucination_flags": r["hallucination_flags"]} for r in rows]


def health(db: str | Path) -> dict:
    """The loop's ingestion health: cursor, events ingested, ingest lag."""
    conn = connect(db, readonly=True)
    try:
        r = conn.execute("SELECT * FROM run").fetchone()
    except sqlite3.OperationalError:  # the controller hasn't created its schema yet
        r = None
    finally:
        conn.close()
    if r is None:
        return {"started": False}
    lag_minutes = max(0, r["sim_minute_seen"] - r["ingested_through_minute"])
    return {
        "started": True,
        "scenario": r["scenario"],
        "cursor_chunk": r["cursor_chunk"],
        "ingested_through_minute": r["ingested_through_minute"],
        "sim_minute_seen": r["sim_minute_seen"],
        "events_ingested": r["events_ingested"],
        "last_event_id": r["last_event_id"],
        "ingest_lag_ticks": lag_minutes / r["tick_minutes"],
        "seconds_since_last_ingest": round(time.time() - r["updated_at"], 1) if r["updated_at"] else None,
    }


def exposures(db: str | Path, lead_id: str) -> list[dict]:
    """Every call one lead received: exactly which variant it heard, under which config and cohort."""
    conn = connect(db, readonly=True)
    try:
        rows = conn.execute(
            "SELECT e.*, l.person_id, l.campaign_id, v.welcome_message, v.voice, v.caller_id, v.prompt_version "
            "FROM exposures e JOIN leads l USING (lead_id) JOIN variants v USING (variant_id) "
            "WHERE e.lead_id = ? ORDER BY e.event_id", (lead_id,)).fetchall()
    finally:
        conn.close()
    return [{"event_id": r["event_id"], "ts": r["ts"], "person_id": r["person_id"],
             "campaign_id": r["campaign_id"], "attempt_no": r["attempt_no"],
             "config_version": r["config_version"], "cohort_id": r["cohort_id"],
             "variant": {"welcome_message": r["welcome_message"], "voice": r["voice"],
                         "caller_id": r["caller_id"], "prompt_version": r["prompt_version"]},
             "connected": bool(r["connected"]), "engaged": bool(r["engaged"]),
             "disposition": r["disposition"],
             "hallucination_flag": None if r["hallucination_flag"] is None else bool(r["hallucination_flag"])}
            for r in rows]


def world_events(db: str | Path) -> list[dict]:
    """The world's own record of config changes, approval decisions and campaigns, as it sent them."""
    conn = connect(db, readonly=True)
    try:
        return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM world_events ORDER BY event_id")]
    finally:
        conn.close()


def _rate(successes: int, traffic: int) -> float | None:
    return successes / traffic if traffic else None


def status(db: str | Path, workflow_id: str | None = None) -> dict:
    """Each workflow's history baselines and analysis partition."""
    conn = connect(db, readonly=True)
    try:
        rows = conn.execute("SELECT * FROM workflow_analysis WHERE ?1 IS NULL OR workflow_id = ?1 "
                            "ORDER BY workflow_id", (workflow_id,)).fetchall()
        partitions = {r["workflow_id"]: _require_partition(conn, r["workflow_id"]) for r in rows}
        history = {r["workflow_id"]: _cohort_history(conn, r["workflow_id"], r["primary_kpi"]) for r in rows}
    except sqlite3.OperationalError:  # the controller hasn't created its schema yet
        return {"started": False, "workflows": []}
    finally:
        conn.close()
    return {"started": True, "workflows": [{
        "workflow_id": r["workflow_id"],
        "primary_kpi": r["primary_kpi"],
        "partition": _partition_view(partitions[r["workflow_id"]]),
        "cohort_history": history[r["workflow_id"]],
        "baselines": {
            "dials": r["dials"], "connected": r["connected"], "engaged": r["engaged"],
            "hallucination_flags": r["hallucination_flags"],
            "pickup_rate": _rate(r["connected"], r["dials"]),
            "engagement_rate": _rate(r["engaged"], r["connected"]),
            "hallucination_flag_rate": _rate(r["hallucination_flags"], r["connected"]),
        },
    } for r in rows]}


def _partition_view(p: Partition) -> dict:
    return {"feature": p.feature, "spread": p.spread,
            "cohorts": [{"id": c.id, "name": c.name, "where": c.where} for c in p.cohorts]}


def _cohort_history(conn: sqlite3.Connection, workflow_id: str, primary_kpi: str) -> dict[str, dict]:
    """Each cohort's share of the primary KPI's traffic in history, and its rate on that KPI."""
    traffic_col, successes_col = KPI_COUNTERS[primary_kpi]
    kpis = _cohort_kpis(conn, workflow_id, through_day=-1)
    total = sum(k[traffic_col] for k in kpis.values())
    return {cid: {"share": _rate(k[traffic_col], total), "primary_kpi_rate": _rate(k[successes_col], k[traffic_col])}
            for cid, k in kpis.items()}


def config_cohorts(db: str | Path, workflow_id: str, only: Collection[str] | None = None) -> list[dict]:
    """The `cohorts` list to write into this workflow's config (all, or just the `only` IDs)."""
    conn = connect(db, readonly=True)
    try:
        return _require_partition(conn, workflow_id).config_cohorts(only)
    finally:
        conn.close()


def cohort_cells(db: str | Path, workflow_id: str) -> dict[str, list[dict[str, str]]]:
    """The feature cells of each cohort, for every cell the workflow's leads have shown."""
    conn = connect(db, readonly=True)
    try:
        partition = _require_partition(conn, workflow_id)
        out: dict[str, list[dict[str, str]]] = {c.id: [] for c in partition.cohorts}
        for r in conn.execute(
                "SELECT lang, city_tier, age_band, value_band FROM feature_cells WHERE cell_id IN "
                "(SELECT DISTINCT cell_id FROM leads WHERE workflow_id = ?) ORDER BY cell_id", (workflow_id,)):
            feats = dict(zip(FEATURES, r))
            out[partition.cohort_of(feats)].append(feats)
    finally:
        conn.close()
    return out


def cohort_kpis(db: str | Path, workflow_id: str) -> dict[str, dict[str, int]]:
    """Each cohort's call counters over everything ingested, by summing its feature cells."""
    conn = connect(db, readonly=True)
    try:
        return _cohort_kpis(conn, workflow_id, None)
    finally:
        conn.close()


def cohort_hour_rates(db: str | Path, workflow_id: str) -> list[dict]:
    """Per cohort and hour, the primary KPI in history: raw, and adjusted for attempt number."""
    conn = connect(db, readonly=True)
    try:
        partition = _require_partition(conn, workflow_id)
        rows = conn.execute("SELECT * FROM cohort_hour_rates WHERE workflow_id = ?", (workflow_id,)).fetchall()
    finally:
        conn.close()
    position = {c.id: i for i, c in enumerate(partition.cohorts)}
    return [{"cohort_id": r["cohort_id"], "hour": r["hour"], "traffic": r["traffic"], "successes": r["successes"],
             "rate": r["successes"] / r["traffic"], "adjusted_rate": r["adjusted_rate"]}
            for r in sorted(rows, key=lambda r: (position[r["cohort_id"]], r["hour"]))]


def _require_partition(conn: sqlite3.Connection, workflow_id: str) -> Partition:
    partition = load_partition(conn, workflow_id)
    if partition is None:
        raise KeyError(f"no analysis partition for workflow {workflow_id!r}")
    return partition


def _cohort_kpis(conn: sqlite3.Connection, workflow_id: str, through_day: int | None) -> dict[str, dict[str, int]]:
    partition = _require_partition(conn, workflow_id)
    counters = ("dials", "connected", "engaged", "hallucination_flags")
    out = {c.id: dict.fromkeys(counters, 0) for c in partition.cohorts}
    for r in conn.execute(
            "SELECT lang, city_tier, age_band, value_band, SUM(a.dials) dials, SUM(a.connected) connected, "
            "SUM(a.engaged) engaged, SUM(a.hallucination_flags) hallucination_flags "
            "FROM cell_agg a JOIN feature_cells USING (cell_id) "
            "WHERE a.workflow_id = ?1 AND (?2 IS NULL OR a.day <= ?2) GROUP BY a.cell_id", (workflow_id, through_day)):
        kpis = out[partition.cohort_of({f: r[f] for f in FEATURES})]
        for k in counters:
            kpis[k] += r[k]
    return out
