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

from . import simtime
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


def proposals(db: str | Path, workflow_id: str) -> list[dict]:
    """This workflow's Proposals, oldest first, as the controller recorded them."""
    conn = connect(db, readonly=True)
    try:
        return [_proposal_view(r) for r in conn.execute(
            "SELECT * FROM proposals WHERE workflow_id = ? ORDER BY created_minute, proposal_id",
            (workflow_id,))]
    finally:
        conn.close()


def _proposal_view(r: sqlite3.Row) -> dict:
    return {**{k: r[k] for k in ("proposal_id", "workflow_id", "kind", "status", "base_version", "replaces",
                                 "created_minute", "sent_minute", "approval_id", "expires_at", "apply_by",
                                 "decided_minute", "applied_minute", "applied_version")},
            "reasons": json.loads(r["reasons"]) if r["reasons"] else [], **json.loads(r["body"])}


def decisions(db: str | Path, workflow_id: str, kind: str | None = None) -> list[dict]:
    """Every decision on this workflow, oldest first, with the inputs it was made from."""
    conn = connect(db, readonly=True)
    try:
        return [_decision_view(r) for r in conn.execute(
            "SELECT * FROM decisions WHERE workflow_id = ?1 AND (?2 IS NULL OR kind = ?2) ORDER BY decision_id",
            (workflow_id, kind))]
    finally:
        conn.close()


def _decision_view(r: sqlite3.Row) -> dict:
    return {"minute": r["minute"], "kind": r["kind"], "outcome": r["outcome"], "proposal_id": r["proposal_id"],
            "detail": json.loads(r["detail"])}


# -- why and heard: every answer comes from the store ----------------------------------------------
_INITIAL_CHANGE = {"version": 1, "actor": "business_default", "applied_at": None, "changed_dims": [],
                   "proposal_id": None, "approval_id": None}
_RECENT_DECISIONS = 10


def minute_at(db: str | Path, text: str) -> int:
    """`D3 14:00` or an ISO timestamp, as a simulated minute of this store's run."""
    conn = connect(db, readonly=True)
    try:
        return simtime.parse(text, _d0(conn))
    finally:
        conn.close()


def _d0(conn: sqlite3.Connection) -> date:
    return date.fromisoformat(conn.execute("SELECT d0_date FROM run").fetchone()[0])


def _config_changes(conn: sqlite3.Connection, workflow_id: str) -> list[dict]:
    """The config's versions in order, the one in force before any change included."""
    events = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM world_events WHERE workflow_id = ? AND type = 'config_changed' ORDER BY event_id",
        (workflow_id,))]
    return [dict(_INITIAL_CHANGE), *({"version": e["version"], "actor": e["actor"], "applied_at": e["ts"],
                                      "changed_dims": e["changed_dims"], "proposal_id": e["proposal_id"],
                                      "approval_id": e["approval_id"]} for e in events)]


def why(db: str | Path, workflow_id: str, at_minute: int) -> dict:
    """The change in force at a moment: its Proposal, evidence, approval, and what happened next."""
    conn = connect(db, readonly=True)
    try:
        if conn.execute("SELECT 1 FROM workflow_analysis WHERE workflow_id = ?", (workflow_id,)).fetchone() is None:
            raise KeyError(f"no workflow {workflow_id!r} in this store")
        d0 = _d0(conn)
        changes = _config_changes(conn, workflow_id)
        in_force = max((c for c in changes if c["applied_at"] is None or simtime.minute_of(c["applied_at"], d0) <= at_minute),
                       key=lambda c: c["version"])
        row = None if in_force["proposal_id"] is None else conn.execute(
            "SELECT * FROM proposals WHERE proposal_id = ?", (in_force["proposal_id"],)).fetchone()
        proposal = None if row is None else _proposal_view(row)
        since = None if in_force["applied_at"] is None else simtime.minute_of(in_force["applied_at"], d0)
        return {
            "workflow_id": workflow_id, "at_minute": at_minute, "at": simtime.label(at_minute),
            "config_version": in_force["version"], "change": in_force, "proposal": proposal,
            "evidence": None if proposal is None else proposal["evidence"],
            "approval": _approval_decision(conn, workflow_id, in_force["approval_id"]),
            "aftermath": _aftermath(conn, workflow_id, in_force["version"], changes),
            "decisions": _decisions_behind(conn, workflow_id, at_minute, in_force["proposal_id"], since),
        }
    finally:
        conn.close()


def _approval_decision(conn: sqlite3.Connection, workflow_id: str, approval_id: str | None) -> dict | None:
    if approval_id is None:
        return None
    for r in conn.execute("SELECT payload FROM world_events WHERE workflow_id = ? AND type = 'approval_decided' "
                          "ORDER BY event_id", (workflow_id,)):
        e = json.loads(r[0])
        if e["approval_id"] == approval_id:
            return {"approval_id": approval_id, "status": e["status"], "decided_at": e["ts"], "reasons": e["reasons"]}
    return None


def _aftermath(conn: sqlite3.Connection, workflow_id: str, version: int, changes: list[dict]) -> dict:
    return {"under_this_version": _version_kpis(conn, workflow_id, version),
            "under_previous_version": _version_kpis(conn, workflow_id, version - 1) if version > 1 else None,
            "next_change": next((c for c in changes if c["version"] > version), None)}


def _version_kpis(conn: sqlite3.Connection, workflow_id: str, version: int) -> dict:
    """How live calls dialled under one config version went."""
    rows = conn.execute(
        "SELECT v.caller_id caller_id, SUM(a.dials) dials, SUM(a.connected) connected, SUM(a.engaged) engaged "
        "FROM cell_agg a JOIN variants v USING (variant_id) "
        "WHERE a.workflow_id = ? AND a.config_version = ? AND a.day >= 0 GROUP BY v.caller_id",
        (workflow_id, version)).fetchall()
    dials, connected, engaged = (sum(r[k] for r in rows) for k in ("dials", "connected", "engaged"))
    return {"dials": dials, "connected": connected, "engaged": engaged, "pickup_rate": _rate(connected, dials),
            "engagement_rate": _rate(engaged, connected),
            "pickup_by_caller_id": {r["caller_id"]: _rate(r["connected"], r["dials"]) for r in rows}}


def _decisions_behind(conn: sqlite3.Connection, workflow_id: str, at_minute: int, proposal_id: str | None,
                      since_minute: int | None) -> list[dict]:
    """The Proposal in force's whole story, every Proposal event since it took effect (a rejected one
    never comes into force), and the latest decisions made up to that moment."""
    recent = conn.execute("SELECT * FROM decisions WHERE workflow_id = ? AND minute <= ? ORDER BY decision_id DESC LIMIT ?",
                          (workflow_id, at_minute, _RECENT_DECISIONS)).fetchall()
    story = [] if proposal_id is None else conn.execute(
        "SELECT * FROM decisions WHERE proposal_id = ? ORDER BY decision_id", (proposal_id,)).fetchall()
    attempts = conn.execute("SELECT * FROM decisions WHERE workflow_id = ?1 AND kind = 'proposal' "
                            "AND minute <= ?2 AND (?3 IS NULL OR minute >= ?3) ORDER BY decision_id",
                            (workflow_id, at_minute, since_minute)).fetchall()
    rows = {r["decision_id"]: r for r in (*recent, *story, *attempts)}
    return [_decision_view(rows[i]) for i in sorted(rows)]


def heard(db: str | Path, lead_id: str) -> dict:
    """Every call one lead received: the exact variant, and why the config behind it was live."""
    calls = exposures(db, lead_id)
    conn = connect(db, readonly=True)
    try:
        wf = conn.execute("SELECT workflow_id FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        changes = {} if wf is None else {c["version"]: c for c in _config_changes(conn, wf[0])}
        summaries: dict[str, str] = {}
        for c in changes.values():
            if c["proposal_id"] is not None and (p := conn.execute(
                    "SELECT body FROM proposals WHERE proposal_id = ?", (c["proposal_id"],)).fetchone()) is not None:
                summaries[c["proposal_id"]] = json.loads(p[0])["change_summary"]
    finally:
        conn.close()
    return {"lead_id": lead_id, "workflow_id": wf[0] if wf else None, "calls": [
        {**call, "config": {**changes[call["config_version"]],
                            "change_summary": summaries.get(changes[call["config_version"]]["proposal_id"])}}
        for call in calls]}
