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
from .composer import describe_change
from .credits import RESERVE
from .store import connect, experiment_view, incident_view, load_partition


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
    """The loop's health: cursor, ingest lag, decisions per hour, credit burn and active alerts."""
    conn = connect(db, readonly=True)
    try:
        r = conn.execute("SELECT * FROM run").fetchone()
        if r is None:
            return {"started": False}
        now = r["sim_minute_seen"]
        decisions_last_hour = conn.execute("SELECT COUNT(*) FROM decisions WHERE minute > ? AND kind != 'loop'", (now - 60,)).fetchone()[0]
        burn = _credit_burn(conn, now)
        alerts = [dict(a) for a in conn.execute("SELECT condition, message, raised_minute FROM alerts "
                                                "WHERE cleared_minute IS NULL ORDER BY alert_id")]
        notes = _notes(conn, now)
    except sqlite3.OperationalError:  # the controller hasn't created its schema yet
        return {"started": False}
    finally:
        conn.close()
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
        "decisions_last_hour": decisions_last_hour,
        "credits": burn,
        "alerts": alerts,
        "notes": notes,
    }


def _notes(conn: sqlite3.Connection, now: int) -> list[dict]:
    """What did not page but is worth seeing, newest first: rejections, expiries, inconclusive experiments, retries."""
    rows = conn.execute(
        "SELECT * FROM decisions WHERE minute > ? AND (kind = 'loop' "
        "OR (kind = 'proposal' AND (outcome IN ('send_failed', 'apply_failed') "
        "OR json_extract(detail, '$.to') IN ('rejected', 'expired', 'stale', 'refused'))) "
        "OR (kind = 'experiment' AND outcome IN ('inconclusive', 'no_candidate', 'blocked')) "
        "OR (kind = 'hallucination' AND outcome = 'escalated')) ORDER BY decision_id DESC",
        (now - simtime.MINUTES_PER_DAY,)).fetchall()
    return [{"minute": r["minute"], "workflow_id": r["workflow_id"], "kind": r["kind"], "outcome": r["outcome"], "proposal_id": r["proposal_id"],
             "detail": {k: v for k, v in json.loads(r["detail"]).items() if k != "experiment"}} for r in rows]


def _credit_burn(conn: sqlite3.Connection, now: int) -> dict | None:
    """Credits left, spent since the controller started, and spent in the last simulated day."""
    first = conn.execute("SELECT credits_remaining FROM credit_ticks ORDER BY minute LIMIT 1").fetchone()
    if first is None:
        return None
    left = conn.execute("SELECT credits_remaining FROM credit_ticks ORDER BY minute DESC LIMIT 1").fetchone()[0]
    day_ago = conn.execute("SELECT credits_remaining FROM credit_ticks WHERE minute <= ? ORDER BY minute DESC LIMIT 1",
                           (now - simtime.MINUTES_PER_DAY,)).fetchone()
    return {"remaining": left, "spent": first[0] - left, "spent_last_day": (day_ago or first)[0] - left, "reserve": RESERVE}


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
    return {**{k: r[k] for k in ("proposal_id", "workflow_id", "kind", "route", "status", "base_version", "replaces",
                                 "created_minute", "sent_minute", "approval_id", "expires_at", "apply_by",
                                 "decided_minute", "applied_minute", "applied_version")},
            "incident_response": bool(r["incident"]), "reasons": json.loads(r["reasons"]) if r["reasons"] else [],
            **json.loads(r["body"])}


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


def experiments(db: str | Path, workflow_id: str) -> list[dict]:
    """This workflow's experiments, oldest first, and where each one has got to."""
    conn = connect(db, readonly=True)
    try:
        return [experiment_view(r) for r in conn.execute("SELECT * FROM experiments WHERE workflow_id = ? ORDER BY seq",
                                                         (workflow_id,))]
    finally:
        conn.close()


def incidents(db: str | Path, workflow_id: str) -> list[dict]:
    """This workflow's hallucination incidents, oldest first: what was seen, its fix and how it ended."""
    conn = connect(db, readonly=True)
    try:
        return [incident_view(r) for r in conn.execute("SELECT * FROM incidents WHERE workflow_id = ? ORDER BY seq",
                                                       (workflow_id,))]
    finally:
        conn.close()


# -- why and heard: every answer comes from the store ----------------------------------------------
_INITIAL_CHANGE = {"version": 1, "actor": "business_default", "applied_at": None, "changed_dims": [],
                   "proposal_id": None, "approval_id": None}
_RECENT_DECISIONS = 10
_MINUTES_PER_WEEK = 7 * simtime.MINUTES_PER_DAY


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
            "experiment": _experiment_at(conn, workflow_id, at_minute),
            "incident": _incident_behind(conn, proposal),
            "decisions": _decisions_behind(conn, workflow_id, at_minute, in_force["proposal_id"], since),
        }
    finally:
        conn.close()


def _incident_behind(conn: sqlite3.Connection, proposal: dict | None) -> dict | None:
    """The incident this Proposal was the fix for, if it was one."""
    incident_id = None if proposal is None else proposal["evidence"].get("incident_id")
    row = None if incident_id is None else conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
    return None if row is None else incident_view(row)


def _experiment_at(conn: sqlite3.Connection, workflow_id: str, at_minute: int) -> dict | None:
    """The workflow's experiment as it stood then: each state change's decision carries the experiment."""
    for r in conn.execute("SELECT * FROM decisions WHERE workflow_id = ? AND kind = 'experiment' AND minute <= ? "
                          "ORDER BY decision_id DESC", (workflow_id, at_minute)):
        detail = json.loads(r["detail"])
        if "experiment" in detail:
            return {**detail["experiment"], "last_decision": r["outcome"], "decided_at": simtime.label(r["minute"]),
                    "dimensions": detail.get("dimensions")}
    return None


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
    experiment = conn.execute("SELECT * FROM decisions WHERE workflow_id = ? AND kind = 'experiment' AND minute <= ? "
                              "ORDER BY decision_id", (workflow_id, at_minute)).fetchall()
    rows = {r["decision_id"]: r for r in (*recent, *story, *attempts, *experiment)}
    return [_decision_view(rows[i]) for i in sorted(rows)]


def weekly_report(db: str | Path) -> dict:
    """Every auto-applied change, by simulated week: the world's record of it, and the controller's reasons."""
    conn = connect(db, readonly=True)
    try:
        d0 = _d0(conn)
        events = [e for e in (json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM world_events WHERE type = 'config_changed' ORDER BY event_id"))
            if e["actor"] == "controller_auto"]
        rows = {e["proposal_id"]: conn.execute("SELECT * FROM proposals WHERE proposal_id = ?",
                                               (e["proposal_id"],)).fetchone() for e in events}
    finally:
        conn.close()
    weeks: dict[int, list[dict]] = {}
    for e in events:
        minute = simtime.minute_of(e["ts"], d0)
        weeks.setdefault(minute // _MINUTES_PER_WEEK, []).append(_weekly_entry(e, rows[e["proposal_id"]], minute))
    return {"weeks": [{"label": f"Week {w + 1} (D{w * 7} to D{w * 7 + 6})", "changes": changes}
                      for w, changes in sorted(weeks.items())]}


def _weekly_entry(event: dict, row: sqlite3.Row | None, minute: int) -> dict:
    """`row` is None only if the store lost the Proposal: the change is still listed, without its reasons."""
    p = None if row is None else _proposal_view(row)
    return {"workflow_id": event["workflow_id"], "at": simtime.label(minute), "version": event["version"],
            "proposal_id": event["proposal_id"], "changed_dims": event["changed_dims"],
            "incident_response": False if p is None else p["incident_response"],
            "what": list(event["changed_dims"]) if p is None else [describe_change(d, c) for d, c in sorted(p["diff"].items())],
            "why": None if p is None else p["rationale"]}


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


# -- supervisor: the single screen -----------------------------------------------------------------
_NOT_ACTED = ("held", "blocked", "dropped", "too_few_connects", "nothing_to_test", "already_escalated",
              "recently_cleared", "not_applicable", "already_in_place")
_NOT_ACTED_SHOWN = 8
_CLOSED_PROPOSALS = ("rejected", "refused", "expired", "withdrawn", "failed")


def supervisor(db: str | Path, workflow_id: str) -> dict | None:
    """What a supervisor needs for one workflow: what's tested, what needs approval, today's changes, what wasn't acted on.

    None if the controller has not analysed the workflow. Cohort ids in change text are replaced by plain names.
    """
    try:
        conn = connect(db, readonly=True)
    except sqlite3.OperationalError:  # no store yet
        return None
    try:
        run = conn.execute("SELECT sim_minute_seen FROM run").fetchone()
        part = load_partition(conn, workflow_id)
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if run is None or part is None:
        return None
    now = run[0]
    today = now // simtime.MINUTES_PER_DAY
    names = {c.id: c.name for c in part.cohorts}

    def plain(text: str) -> str:
        for cid in sorted(names, key=len, reverse=True):
            text = text.replace(cid, names[cid])
        return text

    props = proposals(db, workflow_id)

    def entry(p: dict) -> dict:
        return {"proposal_id": p["proposal_id"], "status": p["status"], "what": plain(p["change_summary"]),
                "why": plain(p.get("rationale") or ""), "impact_score": p.get("impact_score"),
                "incident_response": p["incident_response"], "route": p["route"]}

    pending = sorted((entry(p) for p in props if p["status"] in ("pending", "submitting")),
                     key=lambda e: -(e["impact_score"] or 0))
    changes_today = [{**entry(p), "at": simtime.label(p["applied_minute"])} for p in props
                     if p["applied_minute"] is not None and p["applied_minute"] // simtime.MINUTES_PER_DAY == today]
    exps = experiments(db, workflow_id)
    testing = [e for e in exps if e["end_minute"] is None]
    not_acted: list[dict] = []
    for e in exps:
        if e["status"] == "inconclusive":
            not_acted.append({"minute": e["end_minute"], "what": f"Experiment {e['experiment_id']} ended inconclusive",
                              "reason": plain(str(e["end_reason"]).replace("_", " "))})
    for d in decisions(db, workflow_id):
        if d["outcome"].endswith(_NOT_ACTED):
            not_acted.append({"minute": d["minute"], "what": f"{d['kind'].replace('_', ' ')}: {d['outcome'].replace('_', ' ')}",
                              "reason": plain(str(_reason_of(d["detail"])))})
    for p in props:
        if p["status"] in _CLOSED_PROPOSALS:
            not_acted.append({"minute": p["decided_minute"] or p["created_minute"],
                              "what": f"Proposal {p['proposal_id']} {p['status']}: {plain(p['change_summary'])}",
                              "reason": plain("; ".join(p["reasons"]))})
    not_acted.sort(key=lambda n: -(n["minute"] or 0))
    seen, deduped = set(), []
    for n in not_acted:  # a held decision repeats every loop; show each distinct one once, latest first
        key = (n["what"], n["reason"])
        if key not in seen:
            seen.add(key)
            deduped.append({**n, "at": simtime.label(n["minute"] or 0)})
    return {"workflow_id": workflow_id, "now": simtime.label(now), "pending": pending, "changes_today": changes_today,
            "testing": [{"experiment_id": e["experiment_id"], "status": e["status"],
                         "candidates": {d: o for d, o in (e["candidates"] or {}).items()},
                         "design": {d: plain(f"{p['control']} against {', '.join(p['challengers'])}")
                                    for d, p in sorted(e["design"]["dimensions"].items())}} for e in testing],
            "incidents": incidents(db, workflow_id), "not_acted": deduped[:_NOT_ACTED_SHOWN]}


def _reason_of(detail: dict) -> object:
    for key in ("hold", "why", "reason", "reasons", "violations"):
        if detail.get(key):
            return detail[key]
    return ""
