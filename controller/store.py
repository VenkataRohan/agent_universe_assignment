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

Written once per workflow, after history is in (see `analysis`):
- workflow_analysis  the workflow's primary KPI, its partition's feature and its history baselines
- cohorts            the cohorts of each workflow's analysis partition (`default` included)
- cohort_hour_rates  per cohort and hour, the primary KPI from history, adjusted for attempt number
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .cohorts import Cohort, Partition

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

    @staticmethod
    def history_call_counts(conn: sqlite3.Connection, workflow_id: str) -> list[sqlite3.Row]:
        """Call counters from before D0, per feature cell, hour and attempt band."""
        return conn.execute(
            "SELECT lang, city_tier, age_band, value_band, a.hour hour, a.attempt_band band, SUM(a.dials) dials, "
            "SUM(a.connected) connected, SUM(a.engaged) engaged, SUM(a.hallucination_flags) flags "
            "FROM cell_agg a JOIN feature_cells USING (cell_id) "
            "WHERE a.workflow_id = ? AND a.day < 0 GROUP BY a.cell_id, a.hour, a.attempt_band", (workflow_id,)).fetchall()

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
