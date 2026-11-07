"""Ingestor: one event chunk -> one transaction.

Each chunk writes the lead map, the feature-cell aggregates, the audit-only exposure log, the
verbatim world events and the cursor together, so a crash mid-chunk leaves no trace and the
chunk is simply ingested again (ADR-0003). History and live chunks take the same path.

Only active campaigns' leads are cached in memory. A campaign is active until its cadence runs
out: created on day d with cadence [2,3,3], its leads can be dialled on days d..d+2. The cache
is an optimisation only; a call for an uncached lead falls back to the store.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from .store import Store

FLUSH_ROWS = 20_000
WORLD_EVENT_TYPES = frozenset({"config_changed", "approval_decided", "campaign_created"})


class IngestError(Exception):
    """A chunk could not be ingested; its transaction was rolled back."""


@dataclass
class _Staged:
    """Changes to in-memory state that become visible only once the chunk commits."""
    cells: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    variants: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    campaigns: dict[str, tuple[str, int]] = field(default_factory=dict)        # id -> (wf, day)
    campaign_leads: dict[str, dict[str, int]] = field(default_factory=dict)    # id -> lead -> cell


class Ingestor:
    def __init__(self, store: Store, d0: date, campaign_days: dict[str, int]):
        """`campaign_days`: per workflow, how many days a campaign's leads stay dialable."""
        self._store = store
        self._d0 = d0
        self._campaign_days = campaign_days
        self._default_days = max(campaign_days.values(), default=1)
        self._day_of: dict[str, int] = {}
        conn = store.conn
        self._cells = {(r[1], r[2], r[3], r[4]): r[0] for r in conn.execute(
            "SELECT cell_id, lang, city_tier, age_band, value_band FROM feature_cells")}
        self._variants = {(r[1], r[2], r[3], r[4]): r[0] for r in conn.execute(
            "SELECT variant_id, welcome_message, voice, caller_id, prompt_version FROM variants")}
        self._campaigns: dict[str, tuple[str, int]] = {}
        self._campaign_leads: dict[str, dict[str, int]] = {}
        self._lead_cell: dict[str, int] = {}

    # -- the active-lead cache -----------------------------------------------------------------
    def warm(self, current_day: int) -> None:
        """Rebuild the cache from the store: every lead of a campaign still active on `current_day`."""
        self._campaigns.clear(); self._campaign_leads.clear(); self._lead_cell.clear()
        conn = self._store.conn
        for cid, wf, day in conn.execute("SELECT campaign_id, workflow_id, created_day FROM campaigns"):
            if self._active(wf, day, current_day):
                self._campaigns[cid] = (wf, day)
                leads = {lid: cell for lid, cell in conn.execute(
                    "SELECT lead_id, cell_id FROM leads WHERE campaign_id = ?", (cid,))}
                self._campaign_leads[cid] = leads
                self._lead_cell.update(leads)

    def cached_campaigns(self) -> set[str]:
        return set(self._campaigns)

    def cached_leads(self) -> int:
        return len(self._lead_cell)

    def _active(self, wf: str, created_day: int, current_day: int) -> bool:
        return created_day + self._campaign_days.get(wf, self._default_days) > current_day

    def _evict(self, current_day: int) -> None:
        """Drop committed campaigns whose cadence has run out. Called only after a commit."""
        for cid, (wf, day) in list(self._campaigns.items()):
            if not self._active(wf, day, current_day):
                del self._campaigns[cid]
                for lid in self._campaign_leads.pop(cid, {}):
                    self._lead_cell.pop(lid, None)

    def _evict_staged(self, current_day: int, staged: _Staged) -> None:
        """Mid-chunk: drop staged campaigns that have ended, so a long history chunk stays small."""
        for cid, (wf, day) in list(staged.campaigns.items()):
            if not self._active(wf, day, current_day):
                del staged.campaigns[cid]
                staged.campaign_leads.pop(cid, None)
        for cid in list(staged.campaign_leads):  # late leads of already-committed campaigns
            if cid not in staged.campaigns and cid in self._campaigns:
                wf, day = self._campaigns[cid]
                if not self._active(wf, day, current_day):
                    del staged.campaign_leads[cid]

    # -- ingestion -----------------------------------------------------------------------------
    def day(self, ts: str) -> int:
        """Day index relative to D0 of a local (IST) timestamp."""
        d = self._day_of.get(ts[:10])
        if d is None:
            d = self._day_of[ts[:10]] = (date.fromisoformat(ts[:10]) - self._d0).days
        return d

    def ingest_chunk(self, meta: dict, lines: Iterable[str]) -> int:
        """Ingest one chunk atomically, advancing the cursor to it. Returns events ingested."""
        staged = _Staged()
        try:
            with self._store.transaction() as conn:
                n = self._write(conn, meta, lines, staged)
        except (ValueError, KeyError, TypeError) as e:
            raise IngestError(f"chunk {meta['chunk']}: {e!r}") from e
        # Committed: publish staged state.
        self._cells.update(staged.cells)
        self._variants.update(staged.variants)
        self._campaigns.update(staged.campaigns)
        for cid, leads in staged.campaign_leads.items():
            self._campaign_leads.setdefault(cid, {}).update(leads)
            self._lead_cell.update(leads)
        self._evict(self.day(meta["until_ts"]))
        return n

    def _write(self, conn: sqlite3.Connection, meta: dict, lines: Iterable[str], st: _Staged) -> int:
        # cell_agg key -> counters, in cell_agg column order: dials, connected, engaged, interested,
        # hallucination_flags, flow_adherence_sum, duration_s_sum
        agg: dict[tuple, list] = {}
        leads: list[tuple] = []
        exposures: list[tuple] = []
        world: list[tuple] = []
        n, first_id, last_id = 0, None, None

        def flush() -> None:
            conn.executemany("INSERT INTO leads VALUES (?,?,?,?,?)", leads)
            conn.executemany("INSERT INTO exposures VALUES (?,?,?,?,?,?,?,?,?,?,?)", exposures)
            leads.clear(); exposures.clear()

        def lead_cell(lead_id: str) -> int:
            cell = self._lead_cell.get(lead_id)
            if cell is not None:
                return cell
            for staged_leads in st.campaign_leads.values():
                if lead_id in staged_leads:
                    return staged_leads[lead_id]
            flush()
            row = conn.execute("SELECT cell_id FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if row is None:
                raise IngestError(f"call for unknown lead {lead_id}")
            return row[0]

        for line in lines:
            if not line:
                continue
            ev = json.loads(line)
            eid = ev["event_id"]
            if last_id is not None and eid <= last_id:
                raise IngestError(f"event_id not increasing at {eid}")
            first_id = eid if first_id is None else first_id
            last_id = eid
            n += 1
            t = ev["type"]
            if t == "call":
                v = ev["variant"]
                vkey = (v["welcome_message"], v["voice"], v["caller_id"], v["prompt_version"])
                vid = self._variants.get(vkey) or st.variants.get(vkey)
                if vid is None:
                    vid = st.variants[vkey] = len(self._variants) + len(st.variants) + 1
                    conn.execute("INSERT INTO variants VALUES (?,?,?,?,?)", (vid, *vkey))
                ts = ev["ts"]
                cell = lead_cell(ev["lead_id"])
                qa = ev["qa"]
                flag = None if qa is None else int(bool(qa["hallucination_flag"]))
                key = (ev["workflow_id"], self.day(ts), int(ts[11:13]), ev["config_version"],
                       ev["cohort_id"], cell, vid, min(ev["attempt_no"], 3))
                c = agg.get(key)
                if c is None:
                    c = agg[key] = [0, 0, 0, 0, 0, 0.0, 0]
                c[0] += 1
                if ev["connected"]:
                    c[1] += 1
                    c[4] += flag or 0
                    c[5] += qa["flow_adherence"] if qa else 0.0
                if ev["engaged"]:
                    c[2] += 1
                if ev["disposition"] == "concluded_interested":
                    c[3] += 1
                c[6] += ev["duration_s"]
                exposures.append((eid, ev["lead_id"], ts, ev["attempt_no"], ev["config_version"],
                                  ev["cohort_id"], vid, int(ev["connected"]), int(ev["engaged"]),
                                  ev["disposition"], flag))
            elif t == "lead":
                f = ev["features"]
                ckey = (f["lang"], f["city_tier"], f["age_band"], f["value_band"])
                new_cell = self._cells.get(ckey) or st.cells.get(ckey)
                if new_cell is None:
                    new_cell = st.cells[ckey] = len(self._cells) + len(st.cells) + 1
                    conn.execute("INSERT INTO feature_cells VALUES (?,?,?,?,?)", (new_cell, *ckey))
                cid = ev["campaign_id"]
                leads.append((ev["lead_id"], ev["person_id"], ev["workflow_id"], cid, new_cell))
                if cid in st.campaign_leads or cid in self._campaigns:
                    st.campaign_leads.setdefault(cid, {})[ev["lead_id"]] = new_cell
            if t in WORLD_EVENT_TYPES:
                world.append((eid, t, ev.get("workflow_id"), ev["ts"], line))
                if t == "campaign_created":
                    day = self.day(ev["ts"])
                    conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?)",
                                 (ev["campaign_id"], ev["workflow_id"], day, ev["leads"],
                                  ev["config_version_at_creation"]))
                    self._evict_staged(day, st)
                    st.campaigns[ev["campaign_id"]] = (ev["workflow_id"], day)
                    st.campaign_leads[ev["campaign_id"]] = {}
            if len(leads) + len(exposures) >= FLUSH_ROWS:
                flush()

        if n != meta["events"] or (n and (first_id != meta["first_event_id"] or last_id != meta["last_event_id"])):
            raise IngestError(f"chunk {meta['chunk']} incomplete: got {n} events "
                              f"({first_id}..{last_id}), expected {meta['events']} "
                              f"({meta['first_event_id']}..{meta['last_event_id']})")
        flush()
        conn.executemany("INSERT INTO world_events VALUES (?,?,?,?,?)", world)
        conn.executemany(
            "INSERT INTO cell_agg VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO UPDATE SET dials = dials + excluded.dials, connected = connected + excluded.connected, "
            "engaged = engaged + excluded.engaged, interested = interested + excluded.interested, "
            "hallucination_flags = hallucination_flags + excluded.hallucination_flags, "
            "flow_adherence_sum = flow_adherence_sum + excluded.flow_adherence_sum, "
            "duration_s_sum = duration_s_sum + excluded.duration_s_sum",
            [(*k, *c) for k, c in agg.items()])
        Store.advance_cursor(conn, meta["chunk"], meta["until_minute"], n, meta["last_event_id"] if n else 0)
        return n
