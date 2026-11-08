"""Store: one SQLite file in WAL mode (ADR-0001). Owns the schema and exposes transactions.

Tables written by ingestion (ADR-0003):
- run            run identity (scenario, D0) and the chunk cursor
- leads          lead -> person, campaign, feature cell
- campaigns      one row per campaign_created event (for the active-lead cache)
- feature_cells  lang x city_tier x age_band x value_band -> small int id
- variants       welcome x voice x caller_id x prompt -> small int id
- cell_agg       call counters keyed by workflow, day, hour, config version, cohort, cell,
                 variant and attempt band (1, 2, 3 = "3 or more"; for attempt-adjusted rates)
- exposures      audit-only per-call log: what one lead heard, when, and under which config
- world_events   verbatim config_changed / approval_decided / campaign_created events
- flag_ticks     per workflow, 15-minute tick and prompt: connects and hallucination flags (ADR-0014)

Written once per workflow, after history is in (see `analysis`):
- workflow_analysis  the workflow's primary KPI, its partition's feature and its history baselines
- cohorts            the cohorts of each workflow's analysis partition (`default` included)
- cohort_hour_rates  per cohort and hour, the primary KPI from history, adjusted for attempt number

Written by the governance client and the decide steps:
- proposals  the controller's record of each config change it wants, committed before anything is sent
- decisions  every decide step's outcome and inputs, including "did nothing"
- experiments  each experiment's design and where its state machine has got to
- incidents    each hallucination incident: what was seen, its candidates, its fix and how it ended

Written by the loop at each tick:
- credit_ticks  the credit balance the simulator reported at each tick
- alerts        every page: raised when its condition began to hold, cleared when it stopped
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from .cohorts import Cohort, Partition
from .simtime import minute_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id                       INTEGER PRIMARY KEY CHECK (id = 1),
    scenario                 TEXT    NOT NULL,
    d0_date                  TEXT    NOT NULL,   -- local (IST) date of D0
    tick_minutes             INTEGER NOT NULL,
    cursor_chunk             INTEGER NOT NULL DEFAULT -1,
    ingested_through_minute  INTEGER NOT NULL DEFAULT 0,
    sim_minute_seen          INTEGER NOT NULL DEFAULT 0,
    events_ingested          INTEGER NOT NULL DEFAULT 0,
    last_event_id            INTEGER NOT NULL DEFAULT 0,
    updated_at               REAL    NOT NULL DEFAULT 0  -- wall clock, unix seconds
);
CREATE TABLE IF NOT EXISTS feature_cells (
    cell_id    INTEGER PRIMARY KEY,
    lang       TEXT NOT NULL, city_tier TEXT NOT NULL, age_band TEXT NOT NULL, value_band TEXT NOT NULL,
    UNIQUE (lang, city_tier, age_band, value_band)
);
CREATE TABLE IF NOT EXISTS variants (
    variant_id      INTEGER PRIMARY KEY,
    welcome_message TEXT NOT NULL, voice TEXT NOT NULL, caller_id TEXT NOT NULL, prompt_version TEXT NOT NULL,
    UNIQUE (welcome_message, voice, caller_id, prompt_version)
);
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id   TEXT PRIMARY KEY,
    workflow_id   TEXT    NOT NULL,
    created_day   INTEGER NOT NULL,   -- day index relative to D0 (negative = history)
    leads         INTEGER NOT NULL,
    config_version_at_creation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    lead_id     TEXT PRIMARY KEY,
    person_id   TEXT    NOT NULL,
    workflow_id TEXT    NOT NULL,
    campaign_id TEXT    NOT NULL,
    cell_id     INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS leads_by_campaign ON leads (campaign_id);
CREATE TABLE IF NOT EXISTS cell_agg (
    workflow_id     TEXT    NOT NULL,
    day             INTEGER NOT NULL,
    hour            INTEGER NOT NULL,
    config_version  INTEGER NOT NULL,
    cohort_id       TEXT    NOT NULL,
    cell_id         INTEGER NOT NULL,
    variant_id      INTEGER NOT NULL,
    attempt_band    INTEGER NOT NULL,
    dials               INTEGER NOT NULL,
    connected           INTEGER NOT NULL,
    engaged             INTEGER NOT NULL,
    interested          INTEGER NOT NULL,
    hallucination_flags INTEGER NOT NULL,
    flow_adherence_sum  REAL    NOT NULL,
    duration_s_sum      INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, day, hour, config_version, cohort_id, cell_id, variant_id, attempt_band)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS exposures (
    event_id       INTEGER PRIMARY KEY,
    lead_id        TEXT    NOT NULL,
    ts             TEXT    NOT NULL,
    attempt_no     INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    cohort_id      TEXT    NOT NULL,
    variant_id     INTEGER NOT NULL,
    connected      INTEGER NOT NULL,
    engaged        INTEGER NOT NULL,
    disposition    TEXT    NOT NULL,
    hallucination_flag INTEGER   -- NULL when not connected
);
CREATE INDEX IF NOT EXISTS exposures_by_lead ON exposures (lead_id);
CREATE TABLE IF NOT EXISTS world_events (
    event_id    INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,
    workflow_id TEXT,
    ts          TEXT NOT NULL,
    payload     TEXT NOT NULL      -- the event exactly as the simulator sent it
);
CREATE INDEX IF NOT EXISTS world_events_by_wf ON world_events (workflow_id, type, event_id);
CREATE TABLE IF NOT EXISTS flag_ticks (
    workflow_id    TEXT    NOT NULL,
    minute         INTEGER NOT NULL,   -- the tick's first minute since D0
    prompt_version TEXT    NOT NULL,
    connects       INTEGER NOT NULL,
    flags          INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, minute, prompt_version)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS workflow_analysis (
    workflow_id         TEXT PRIMARY KEY,
    primary_kpi         TEXT    NOT NULL,
    partition_feature   TEXT,               -- NULL: no feature splits this workflow's traffic
    partition_spread    REAL,               -- highest group's KPI rate minus lowest group's
    dials               INTEGER NOT NULL,   -- history totals, the baselines' numerators and denominators
    connected           INTEGER NOT NULL,
    engaged             INTEGER NOT NULL,
    hallucination_flags INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cohorts (
    workflow_id TEXT    NOT NULL,
    cohort_id   TEXT    NOT NULL,
    position    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    where_json  TEXT    NOT NULL,
    PRIMARY KEY (workflow_id, cohort_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS cohort_hour_rates (
    workflow_id   TEXT    NOT NULL,
    cohort_id     TEXT    NOT NULL,
    hour          INTEGER NOT NULL,
    traffic        INTEGER NOT NULL,
    successes     INTEGER NOT NULL,
    adjusted_rate REAL    NOT NULL,
    PRIMARY KEY (workflow_id, cohort_id, hour)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id     TEXT PRIMARY KEY,
    workflow_id     TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    seq             INTEGER NOT NULL,
    status          TEXT    NOT NULL,
    base_version    INTEGER NOT NULL,
    route           TEXT    NOT NULL,   -- approval (POST /approvals) or auto (PUT /config)
    incident        INTEGER NOT NULL,   -- 1: incident response
    body            TEXT    NOT NULL,   -- the POST /approvals body; an auto-apply sends a subset of it
    replaces        TEXT,               -- the Proposal this one was rebuilt from
    created_minute  INTEGER NOT NULL,
    sent_minute     INTEGER,            -- counts against the request cap; NULL if it never reached the platform
    approval_id     TEXT,
    expires_at      TEXT,
    apply_by        TEXT,
    decided_minute  INTEGER,            -- the tick the decision was seen
    applied_minute  INTEGER,
    applied_version INTEGER,
    reasons         TEXT,               -- JSON list: the approver's reasons, or why it was abandoned
    UNIQUE (workflow_id, kind, seq)
);
CREATE INDEX IF NOT EXISTS proposals_by_status ON proposals (workflow_id, status);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    minute      INTEGER NOT NULL,
    workflow_id TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,
    proposal_id TEXT,
    detail      TEXT    NOT NULL   -- JSON: the inputs the decision was made from
);
CREATE INDEX IF NOT EXISTS decisions_by_wf ON decisions (workflow_id, minute);
CREATE INDEX IF NOT EXISTS decisions_by_proposal ON decisions (proposal_id);
CREATE TABLE IF NOT EXISTS credit_ticks (
    minute            INTEGER PRIMARY KEY,
    credits_remaining REAL NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alerts (
    alert_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    key            TEXT    NOT NULL,   -- one occurrence of a condition, e.g. the incident it is about
    condition      TEXT    NOT NULL,
    message        TEXT    NOT NULL,   -- what is wrong and what to look at
    raised_minute  INTEGER NOT NULL,
    cleared_minute INTEGER             -- NULL while active
);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id  TEXT PRIMARY KEY,
    workflow_id    TEXT    NOT NULL,
    seq            INTEGER NOT NULL,
    status         TEXT    NOT NULL,
    design         TEXT    NOT NULL,   -- JSON: per dimension its control, challengers and split
    planned_minute INTEGER NOT NULL,
    screen_version INTEGER,            -- the config version the split went live in
    screen_minute  INTEGER,
    last_look_day  INTEGER,            -- the day of the latest daily look
    candidates     TEXT,               -- JSON: the candidate per dimension, once the screen has chosen
    confirm_version INTEGER,           -- the config version the 50/50 went live in: its data starts here
    confirm_minute  INTEGER,
    confirm_plan    TEXT,              -- JSON: per candidate dimension, its split and planned sample
    confirm_looks   INTEGER NOT NULL DEFAULT 0,  -- looks that had at least half the planned sample
    verdicts        TEXT,              -- JSON: per dimension, the look that settled it
    end_minute     INTEGER,
    end_reason     TEXT,
    split_live   INTEGER NOT NULL DEFAULT 0,  -- 1: it has ended, and its split may still be live
    promote_version INTEGER,           -- the config version the promotion went live in: decay is judged from here
    promote_minute  INTEGER,
    holdbacks       TEXT,              -- JSON: per dimension still held back, its winner and its control
    decay           TEXT,              -- JSON: the decay signal a rollback is being asked for
    clean_since_minute INTEGER,        -- the promotion, or the latest decay signal: the holdback's clean days count from here
    UNIQUE (workflow_id, seq)
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id     TEXT PRIMARY KEY,
    workflow_id     TEXT    NOT NULL,
    seq             INTEGER NOT NULL,
    status          TEXT    NOT NULL,   -- open, verifying, verified, escalated or resolved
    bad_prompt      TEXT    NOT NULL,
    onset_minute    INTEGER NOT NULL,
    detected_minute INTEGER NOT NULL,
    detection       TEXT    NOT NULL,   -- JSON: the window and the test that raised it
    cause           TEXT    NOT NULL,   -- JSON: the prompt change it followed, if any, and the known-good prompt
    candidates      TEXT    NOT NULL DEFAULT '[]',  -- JSON list: each candidate, its source and validation
    suggested       INTEGER NOT NULL DEFAULT 0,     -- 1: the LLM has been asked
    fix_prompt      TEXT,
    applied_minute  INTEGER,
    applied_version INTEGER,
    end_minute      INTEGER,
    end_reason      TEXT,
    UNIQUE (workflow_id, seq)
);
"""


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    """Open the store. Read-only connections never block (or are blocked by) the writer."""
    if readonly:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, isolation_level=None)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def experiment_view(r: sqlite3.Row) -> dict:
    """An experiment as the audit and the query interface show it."""
    documents = ("design", "candidates", "confirm_plan", "verdicts", "holdbacks", "decay")
    return {**{k: r[k] for k in r.keys() if k not in documents}, **{k: json.loads(r[k]) if r[k] else {} for k in documents}}


def incident_view(r: sqlite3.Row) -> dict:
    """An incident as the audit and the query interface show it."""
    documents = ("detection", "cause", "candidates")
    return {**{k: r[k] for k in r.keys() if k not in documents and k != "suggested"},
            **{k: json.loads(r[k]) for k in documents},
            "minutes_onset_to_applied": None if r["applied_minute"] is None else r["applied_minute"] - r["onset_minute"]}


def load_partition(conn: sqlite3.Connection, workflow_id: str) -> Partition | None:
    """A workflow's analysis partition, or None if it hasn't been chosen yet."""
    r = conn.execute("SELECT partition_feature, partition_spread FROM workflow_analysis WHERE workflow_id = ?",
                     (workflow_id,)).fetchone()
    if r is None:
        return None
    cohorts = conn.execute("SELECT cohort_id, name, where_json FROM cohorts WHERE workflow_id = ? ORDER BY position",
                           (workflow_id,)).fetchall()
    return Partition(r[0], r[1], tuple(Cohort(c[0], c[1], json.loads(c[2])) for c in cohorts))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = connect(self.path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT; rolls back on any exception."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    def d0(self) -> date:
        return date.fromisoformat(self.conn.execute("SELECT d0_date FROM run").fetchone()[0])

    @staticmethod
    def history_call_counts(conn: sqlite3.Connection, workflow_id: str) -> list[sqlite3.Row]:
        """Call counters from before D0, per day, feature cell, hour and attempt band."""
        return conn.execute(
            "SELECT a.day day, lang, city_tier, age_band, value_band, a.hour hour, a.attempt_band band, SUM(a.dials) dials, "
            "SUM(a.connected) connected, SUM(a.engaged) engaged, SUM(a.hallucination_flags) flags "
            "FROM cell_agg a JOIN feature_cells USING (cell_id) "
            "WHERE a.workflow_id = ? AND a.day < 0 GROUP BY a.day, a.cell_id, a.hour, a.attempt_band", (workflow_id,)).fetchall()

    @staticmethod
    def analysed_workflows(conn: sqlite3.Connection) -> set[str]:
        return {r[0] for r in conn.execute("SELECT workflow_id FROM workflow_analysis")}

    @staticmethod
    def save_baselines(conn: sqlite3.Connection, workflow_id: str, primary_kpi: str, partition: Partition,
                       dials: int, connected: int, engaged: int, flags: int) -> None:
        conn.execute("INSERT INTO workflow_analysis VALUES (?,?,?,?,?,?,?,?)",
                     (workflow_id, primary_kpi, partition.feature, partition.spread, dials, connected, engaged, flags))

    @staticmethod
    def save_cohorts(conn: sqlite3.Connection, workflow_id: str, partition: Partition) -> None:
        conn.executemany("INSERT INTO cohorts VALUES (?,?,?,?,?)",
                         [(workflow_id, c.id, i, c.name, json.dumps(c.where, sort_keys=True))
                          for i, c in enumerate(partition.cohorts)])

    @staticmethod
    def save_hour_rates(conn: sqlite3.Connection, workflow_id: str, rates: list[tuple[str, int, int, int, float]]) -> None:
        """`rates`: (cohort, hour, traffic, successes, adjusted rate)."""
        conn.executemany("INSERT INTO cohort_hour_rates VALUES (?,?,?,?,?,?)", [(workflow_id, *r) for r in rates])

    # -- run identity and the chunk cursor (the `run` table is written only here) ---------------
    def run_identity(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM run").fetchone()

    def create_run(self, scenario: str, d0_date: str, tick_minutes: int, sim_minute: int) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO run (id, scenario, d0_date, tick_minutes, sim_minute_seen) "
                         "VALUES (1, ?, ?, ?, ?)", (scenario, d0_date, tick_minutes, sim_minute))

    def cursor(self) -> tuple[int, int]:
        """(last chunk ingested, simulated minute the store is complete through)."""
        r = self.conn.execute("SELECT cursor_chunk, ingested_through_minute FROM run").fetchone()
        return r[0], r[1]

    def record_sim_minute(self, minute: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE run SET sim_minute_seen = ?", (minute,))

    def record_credits(self, minute: int, credits_remaining: float) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO credit_ticks VALUES (?, ?)", (minute, credits_remaining))

    def mark_ingested_through(self, minute: int) -> None:
        """Every chunk published up to `minute` is in (a tick may publish none)."""
        with self.transaction() as conn:
            conn.execute("UPDATE run SET ingested_through_minute = MAX(ingested_through_minute, ?)", (minute,))

    @staticmethod
    def advance_cursor(conn: sqlite3.Connection, chunk: int, until_minute: int, events: int,
                       last_event_id: int) -> None:
        """Move the cursor past `chunk`; call inside the transaction that ingests it."""
        conn.execute(
            "UPDATE run SET cursor_chunk = ?, ingested_through_minute = MAX(ingested_through_minute, ?), "
            "events_ingested = events_ingested + ?, last_event_id = MAX(last_event_id, ?), updated_at = ?",
            (chunk, until_minute, events, last_event_id, time.time()))

    # -- proposals and decisions (written only here) ---------------------------------------------
    @staticmethod
    def next_proposal_seq(conn: sqlite3.Connection, workflow_id: str, kind: str) -> int:
        return conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM proposals WHERE workflow_id = ? AND kind = ?",
                            (workflow_id, kind)).fetchone()[0]

    @staticmethod
    def insert_proposal(conn: sqlite3.Connection, **row: Any) -> None:
        conn.execute(f"INSERT INTO proposals ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))

    @staticmethod
    def update_proposal(conn: sqlite3.Connection, proposal_id: str, **fields: Any) -> None:
        conn.execute(f"UPDATE proposals SET {', '.join(f'{k} = ?' for k in fields)} WHERE proposal_id = ?",
                     (*fields.values(), proposal_id))

    @staticmethod
    def record_decision(conn: sqlite3.Connection, minute: int, workflow_id: str, kind: str, outcome: str,
                        proposal_id: str | None, detail: dict) -> None:
        """Call inside the transaction of the state change the decision causes."""
        conn.execute("INSERT INTO decisions (minute, workflow_id, kind, outcome, proposal_id, detail) "
                     "VALUES (?,?,?,?,?,?)",
                     (minute, workflow_id, kind, outcome, proposal_id, json.dumps(detail, sort_keys=True)))

    def note_decision(self, minute: int, workflow_id: str, kind: str, outcome: str, detail: dict) -> None:
        """A decision not to act, recorded when it changes: the latest row stays true until the next one."""
        last = self.last_decision(workflow_id, kind)
        if last is None or (last["outcome"], json.loads(last["detail"])) != (outcome, detail):
            with self.transaction() as conn:
                Store.record_decision(conn, minute, workflow_id, kind, outcome, None, detail)

    def proposal(self, proposal_id: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()

    def open_proposals(self, workflow_id: str, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(f"SELECT * FROM proposals WHERE workflow_id = ? AND status IN ({marks}) "
                                 "ORDER BY created_minute, seq", (workflow_id, *statuses)).fetchall()

    def latest_proposal(self, workflow_id: str, kind: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM proposals WHERE workflow_id = ? AND kind = ? ORDER BY seq DESC LIMIT 1",
                                 (workflow_id, kind)).fetchone()

    def requests_since(self, workflow_id: str, minute: int, excluding: str | None = None) -> int:
        """Requests sent to the platform at or after `minute`, whatever became of them."""
        return self.conn.execute("SELECT COUNT(*) FROM proposals WHERE workflow_id = ? AND sent_minute >= ? "
                                 "AND proposal_id IS NOT ?", (workflow_id, minute, excluding)).fetchone()[0]

    def auto_refused_since(self, workflow_id: str, kind: str, minute: int) -> int:
        """Auto-apply attempts of this kind, created at or after `minute`, that the platform refused."""
        return self.conn.execute("SELECT COUNT(*) FROM proposals WHERE workflow_id = ? AND kind = ? AND route = 'auto' "
                                 "AND status = 'refused' AND created_minute >= ?", (workflow_id, kind, minute)).fetchone()[0]

    def proposal_endings(self) -> list[sqlite3.Row]:
        """Every Proposal with its latest decision's minute and outcome, in order within each workflow and kind."""
        return self.conn.execute("SELECT p.proposal_id, p.workflow_id, p.kind, p.status, p.reasons, MAX(d.decision_id), "
                                 "d.minute ended_minute, d.outcome last_outcome "
                                 "FROM proposals p JOIN decisions d USING (proposal_id) "
                                 "GROUP BY p.proposal_id ORDER BY p.workflow_id, p.kind, p.seq").fetchall()

    def applied_evidence(self, workflow_id: str, kind: str, route: str | None = None,
                         since_minute: int | None = None) -> list[dict]:
        """The evidence of each applied Proposal of this kind, oldest first."""
        return [json.loads(r[0])["evidence"] for r in self.conn.execute(
            "SELECT body FROM proposals WHERE workflow_id = ?1 AND kind = ?2 AND status = 'applied' "
            "AND (?3 IS NULL OR route = ?3) AND (?4 IS NULL OR created_minute >= ?4) ORDER BY seq",
            (workflow_id, kind, route, since_minute))]

    # -- what the decide steps read --------------------------------------------------------------
    def caller_id_counts(self, workflow_id: str, first_hour: int, last_hour: int, since_version: int = 0) -> list[sqlite3.Row]:
        """Per caller ID and feature cell, call counters between two hours since D0 (inclusive).

        `since_version` leaves out calls dialled under earlier config versions.
        """
        return self.conn.execute(
            "SELECT v.caller_id caller_id, lang, city_tier, age_band, value_band, SUM(a.dials) dials, "
            "SUM(a.connected) connected, SUM(a.engaged) engaged "
            "FROM cell_agg a JOIN variants v USING (variant_id) JOIN feature_cells USING (cell_id) "
            "WHERE a.workflow_id = ?1 AND a.day BETWEEN ?2 AND ?3 AND a.day * 24 + a.hour BETWEEN ?4 AND ?5 "
            "AND a.config_version >= ?6 GROUP BY v.caller_id, a.cell_id",
            (workflow_id, first_hour // 24, last_hour // 24, first_hour, last_hour, since_version)).fetchall()

    def latest_applied(self, workflow_id: str, kind: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM proposals WHERE workflow_id = ? AND kind = ? AND status = 'applied' "
                                 "ORDER BY seq DESC LIMIT 1", (workflow_id, kind)).fetchone()

    def last_decision(self, workflow_id: str, kind: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM decisions WHERE workflow_id = ? AND kind = ? "
                                 "ORDER BY decision_id DESC LIMIT 1", (workflow_id, kind)).fetchone()

    def last_supervisor_edit(self, workflow_id: str, dimension: str) -> int | None:
        """The simulated minute a supervisor last changed this dimension, if one ever has."""
        for r in self.conn.execute("SELECT payload FROM world_events WHERE workflow_id = ? AND type = 'config_changed' "
                                   "ORDER BY event_id DESC", (workflow_id,)):
            event = json.loads(r[0])
            if event["actor"] == "supervisor" and dimension in event["changed_dims"]:
                return minute_of(event["ts"], self.d0())
        return None

    # -- alerts (written only here) ---------------------------------------------------------------
    @staticmethod
    def active_alert_keys(conn: sqlite3.Connection) -> set[str]:
        return {r[0] for r in conn.execute("SELECT key FROM alerts WHERE cleared_minute IS NULL")}

    @staticmethod
    def raise_alert(conn: sqlite3.Connection, key: str, condition: str, message: str, minute: int) -> None:
        conn.execute("INSERT INTO alerts (key, condition, message, raised_minute) VALUES (?,?,?,?)", (key, condition, message, minute))

    @staticmethod
    def update_alert(conn: sqlite3.Connection, key: str, message: str) -> None:
        conn.execute("UPDATE alerts SET message = ? WHERE key = ? AND cleared_minute IS NULL", (message, key))

    @staticmethod
    def clear_alert(conn: sqlite3.Connection, key: str, minute: int) -> None:
        conn.execute("UPDATE alerts SET cleared_minute = ? WHERE key = ? AND cleared_minute IS NULL", (minute, key))

    # -- hallucination incidents (written only here) ----------------------------------------------
    def flag_ticks(self, workflow_id: str, first_minute: int, before_minute: int) -> list[sqlite3.Row]:
        """Per tick and prompt, connects and flags of the ticks starting in [first_minute, before_minute), oldest first."""
        return self.conn.execute("SELECT minute, prompt_version, connects, flags FROM flag_ticks "
                                 "WHERE workflow_id = ? AND minute >= ? AND minute < ? ORDER BY minute, prompt_version",
                                 (workflow_id, first_minute, before_minute)).fetchall()

    def prompts_before(self, workflow_id: str, minute: int) -> list[str]:
        """The prompts that took calls before this minute, most recently used first."""
        return [r[0] for r in self.conn.execute(
            "SELECT prompt_version FROM flag_ticks WHERE workflow_id = ? AND minute < ? "
            "GROUP BY prompt_version ORDER BY MAX(minute) DESC", (workflow_id, minute))]

    def baseline_flag_rate(self, workflow_id: str) -> float:
        """Hallucination flags per connect in this workflow's history."""
        r = self.conn.execute("SELECT hallucination_flags, connected FROM workflow_analysis WHERE workflow_id = ?",
                              (workflow_id,)).fetchone()
        return r[0] / r[1] if r is not None and r[1] else 0.0

    @staticmethod
    def next_incident_seq(conn: sqlite3.Connection, workflow_id: str) -> int:
        return conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM incidents WHERE workflow_id = ?",
                            (workflow_id,)).fetchone()[0]

    @staticmethod
    def insert_incident(conn: sqlite3.Connection, **row: Any) -> None:
        conn.execute(f"INSERT INTO incidents ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))

    @staticmethod
    def update_incident(conn: sqlite3.Connection, incident_id: str, **fields: Any) -> None:
        conn.execute(f"UPDATE incidents SET {', '.join(f'{k} = ?' for k in fields)} WHERE incident_id = ?",
                     (*fields.values(), incident_id))

    def incident(self, incident_id: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()

    def live_incident(self, workflow_id: str) -> sqlite3.Row | None:
        """The incident still being worked on, if any."""
        return self.conn.execute("SELECT * FROM incidents WHERE workflow_id = ? AND status IN ('open', 'verifying') "
                                 "ORDER BY seq DESC LIMIT 1", (workflow_id,)).fetchone()

    def unfixed_incidents(self) -> list[sqlite3.Row]:
        """Incidents with no fix in force: still open, or handed to a person. A spike the live prompt answered is neither."""
        return self.conn.execute("SELECT * FROM incidents WHERE status = 'open' OR (status = 'escalated' "
                                 "AND end_reason != 'live_prompt_passed_validation') ORDER BY incident_id").fetchall()

    def prompt_changed_since(self, workflow_id: str, minute: int) -> bool:
        d0 = self.d0()
        return any("prompt_version" in e["changed_dims"] and minute_of(e["ts"], d0) >= minute
                   for e in self.config_changes_after(workflow_id, 0))

    def ruled_out_prompts(self, workflow_id: str) -> set[str]:
        """Prompts that can't be gone back to: those blamed through a prompt change, and fixes that didn't work."""
        rows = self.conn.execute("SELECT bad_prompt, cause, end_reason, fix_prompt FROM incidents WHERE workflow_id = ?",
                                 (workflow_id,)).fetchall()
        return ({r["bad_prompt"] for r in rows if json.loads(r["cause"])["prompt_change"] is not None}
                | {r["fix_prompt"] for r in rows if r["end_reason"] == "not_recovered"})

    def escalated_on(self, workflow_id: str, prompt: str) -> bool:
        """Was an incident on this prompt, or a fix to it, handed to a human?"""
        return self.conn.execute("SELECT 1 FROM incidents WHERE workflow_id = ?1 AND status = 'escalated' "
                                 "AND end_reason != 'live_prompt_passed_validation' "
                                 "AND (bad_prompt = ?2 AND fix_prompt IS NULL OR fix_prompt = ?2)",
                                 (workflow_id, prompt)).fetchone() is not None

    def cleared_since(self, workflow_id: str, prompt: str, minute: int) -> bool:
        """Has this prompt's own validation answered a spike on it at or after this minute?"""
        return self.conn.execute("SELECT 1 FROM incidents WHERE workflow_id = ? AND bad_prompt = ? AND end_minute >= ? "
                                 "AND end_reason = 'live_prompt_passed_validation'",
                                 (workflow_id, prompt, minute)).fetchone() is not None

    # -- experiments (written only here) ---------------------------------------------------------
    @staticmethod
    def next_experiment_seq(conn: sqlite3.Connection, workflow_id: str) -> int:
        return conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM experiments WHERE workflow_id = ?",
                            (workflow_id,)).fetchone()[0]

    @staticmethod
    def insert_experiment(conn: sqlite3.Connection, **row: Any) -> None:
        conn.execute(f"INSERT INTO experiments ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))

    @staticmethod
    def update_experiment(conn: sqlite3.Connection, experiment_id: str, **fields: Any) -> None:
        conn.execute(f"UPDATE experiments SET {', '.join(f'{k} = ?' for k in fields)} WHERE experiment_id = ?",
                     (*fields.values(), experiment_id))

    def experiment(self, experiment_id: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)).fetchone()

    def latest_experiment(self, workflow_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM experiments WHERE workflow_id = ? ORDER BY seq DESC LIMIT 1",
                                 (workflow_id,)).fetchone()

    def applied_carrying(self, workflow_id: str, evidence_key: str, experiment_id: str) -> sqlite3.Row | None:
        """The applied Proposal whose evidence carries this entry for this experiment, if one has."""
        return self.conn.execute("SELECT * FROM proposals WHERE workflow_id = ? AND status = 'applied' "
                                 "AND json_extract(body, ?) = ?",
                                 (workflow_id, f"$.evidence.{evidence_key}.experiment_id", experiment_id)).fetchone()

    def config_changes_after(self, workflow_id: str, version: int) -> list[dict]:
        """The world's `config_changed` events for versions after this one, oldest first."""
        events = (json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM world_events WHERE workflow_id = ? AND type = 'config_changed' ORDER BY event_id", (workflow_id,)))
        return [e for e in events if e["version"] > version]

    def arm_counts(self, workflow_id: str, first_version: int, last_version: int | None = None,
                   before_hour: int | None = None, excluded_cohorts: Collection[str] = ()) -> list[sqlite3.Row]:
        """Per config version, welcome message and voice: connected calls and how many engaged.

        `before_hour` leaves out the calls of that hour since D0 and later. `excluded_cohorts`: config branches
        that dial from their own table, so their calls are not in the split.
        """
        return self.conn.execute(
            "SELECT a.config_version config_version, v.welcome_message welcome_message, v.voice voice, "
            "SUM(a.connected) connected, SUM(a.engaged) engaged "
            "FROM cell_agg a JOIN variants v USING (variant_id) "
            "WHERE a.workflow_id = ?1 AND a.config_version >= ?2 AND (?3 IS NULL OR a.config_version <= ?3) "
            "AND (?4 IS NULL OR a.day * 24 + a.hour < ?4) AND a.cohort_id NOT IN (SELECT value FROM json_each(?5)) "
            "GROUP BY a.config_version, v.welcome_message, v.voice",
            (workflow_id, first_version, last_version, before_hour, json.dumps(list(excluded_cohorts)))).fetchall()

    def arm_cell_counts(self, workflow_id: str, first_version: int, excluded_cohorts: Collection[str] = (),
                        first_hour: int | None = None, before_hour: int | None = None) -> list[sqlite3.Row]:
        """Per config version, welcome message, voice and feature cell: dials, connected calls and how many engaged.

        `first_hour` and `before_hour` (hours since D0) keep only the calls from the first up to, not including, the second.
        """
        return self.conn.execute(
            "SELECT a.config_version config_version, v.welcome_message welcome_message, v.voice voice, lang, city_tier, "
            "age_band, value_band, SUM(a.dials) dials, SUM(a.connected) connected, SUM(a.engaged) engaged "
            "FROM cell_agg a JOIN variants v USING (variant_id) JOIN feature_cells USING (cell_id) "
            "WHERE a.workflow_id = ?1 AND a.config_version >= ?2 AND a.cohort_id NOT IN (SELECT value FROM json_each(?3)) "
            "AND (?4 IS NULL OR a.day * 24 + a.hour >= ?4) AND (?5 IS NULL OR a.day * 24 + a.hour < ?5) "
            "GROUP BY a.config_version, v.welcome_message, v.voice, a.cell_id",
            (workflow_id, first_version, json.dumps(list(excluded_cohorts)), first_hour, before_hour)).fetchall()

    def kpi_totals(self, workflow_id: str, first_hour: int, before_hour: int) -> sqlite3.Row:
        """Dials, connected calls and how many engaged, from the first hour since D0 up to, not including, the second."""
        return self.conn.execute(
            "SELECT COALESCE(SUM(dials), 0) dials, COALESCE(SUM(connected), 0) connected, COALESCE(SUM(engaged), 0) engaged "
            "FROM cell_agg WHERE workflow_id = ?1 AND day * 24 + hour >= ?2 AND day * 24 + hour < ?3",
            (workflow_id, first_hour, before_hour)).fetchone()

    def option_counts(self, workflow_id: str, first_hour: int, last_hour: int) -> list[sqlite3.Row]:
        """Per welcome message, voice, caller ID and feature cell, call counters between two hours since D0 (inclusive)."""
        return self.conn.execute(
            "SELECT v.welcome_message welcome_message, v.voice voice, v.caller_id caller_id, lang, city_tier, age_band, "
            "value_band, SUM(a.dials) dials, SUM(a.connected) connected, SUM(a.engaged) engaged "
            "FROM cell_agg a JOIN variants v USING (variant_id) JOIN feature_cells USING (cell_id) "
            "WHERE a.workflow_id = ?1 AND a.day BETWEEN ?2 AND ?3 AND a.day * 24 + a.hour BETWEEN ?4 AND ?5 "
            "GROUP BY v.welcome_message, v.voice, v.caller_id, a.cell_id",
            (workflow_id, first_hour // 24, last_hour // 24, first_hour, last_hour)).fetchall()

    def active_leads_by_cell(self, workflow_id: str, first_active_day: int) -> list[sqlite3.Row]:
        """Per feature cell, the leads of campaigns created since that day."""
        return self.conn.execute(
            "SELECT lang, city_tier, age_band, value_band, COUNT(*) leads "
            "FROM leads l JOIN campaigns c USING (campaign_id) JOIN feature_cells USING (cell_id) "
            "WHERE c.workflow_id = ?1 AND c.created_day >= ?2 "
            "GROUP BY l.cell_id", (workflow_id, first_active_day)).fetchall()
