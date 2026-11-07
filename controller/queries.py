"""Query interface: read-only questions answered from the store. The CLI is a thin wrapper.

Each query opens its own read-only connection, so it can run while the controller writes
(WAL readers never block the writer, nor are blocked by it).
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from .store import connect


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
