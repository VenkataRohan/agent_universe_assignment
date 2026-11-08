"""Loop: one process, one thread, one 15-minute tick at a time.

Each tick: advance the simulated clock -> ingest every chunk after the cursor -> for each workflow,
reconcile its Proposals (applying any approval), respond to hallucination incidents, run the caller-ID check, then experiments, then
call-time tuning, then caller-ID exploration. Then, whatever happened, re-judge the alerts.

Ingest failing does not stop the clock: nothing is decided on a store that is behind, and the lag pages (ADR-0015).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from . import alerts, analysis, callerid, experiments, hallucination, schedule
from .credits import Credits
from .governance import GovernanceClient
from .ingest import IngestError, Ingestor
from .registry import OVERLAYS, Overlay
from .simclient import SimClient, SimError
from .store import Store

log = logging.getLogger("controller")
CHUNK_ATTEMPTS = 3
NO_WORKFLOW = "-"  # the workflow_id of a decision about the loop itself


class ResumeRefused(Exception):
    """The store belongs to a different world than the simulator we are talking to."""


class Controller:
    def __init__(self, http: httpx.Client, db_path: str | Path, overlays: Mapping[str, Overlay] = OVERLAYS,
                 sleep: Callable[[float], None] = time.sleep):
        self._retried: list[str] = []  # what had to be tried again since the last tick ended
        self.sim = SimClient(http, sleep=sleep, on_retry=self._retried.append)
        self._credits = Credits(self.sim)
        self.store = Store(db_path)
        self._overlays = overlays
        self._ingestor: Ingestor | None = None
        self._governance: GovernanceClient | None = None
        self._workflows: list[dict] = []
        self._state: dict = {}

    @property
    def ingestor(self) -> Ingestor:
        if self._ingestor is None:
            raise RuntimeError("controller not started")
        return self._ingestor

    # -- lifecycle -----------------------------------------------------------------------------
    def start(self) -> None:
        """Check the store belongs to this world, then catch up on every chunk after the cursor."""
        st = self.sim.state()
        run = self.store.run_identity()
        if run is None:
            d0 = (datetime.fromisoformat(st["ts"]) - timedelta(minutes=st["sim_minute"])).date()
            self.store.create_run(st["scenario"], d0.isoformat(), st["tick_minutes"], st["sim_minute"])
        else:
            _check_same_world(run, st)
            d0 = date.fromisoformat(run["d0_date"])
        workflows = self.sim.workflows()
        campaign_days = {w["workflow_id"]: len(w["cadence"]) for w in workflows}
        self._ingestor = Ingestor(self.store, d0, campaign_days)
        _, through = self.store.cursor()
        self._ingestor.warm(self._ingestor.day(_ts_at(d0, through)))
        self._state = st
        self.store.record_credits(st["sim_minute"], st["credits_remaining"])
        self._catch_up()
        analysis.bootstrap_from_history(self.store, workflows)
        self._workflows = workflows
        self._governance = GovernanceClient(self.store, self.sim, workflows, self._overlays, experiments.with_holdback_release)
        # A Proposal left half-done by an earlier life must not wait for the next tick.
        self._reconcile(self._state["sim_minute"])

    def step(self) -> bool:
        """Advance one tick and ingest what it produced. Returns True once the scenario is finished."""
        if self._ingestor is None:
            self.start()
        if self._state["finished"]:
            return True
        self._state = self.sim.advance_to(self._state["sim_minute"] + self._state["tick_minutes"])
        now = self._state["sim_minute"]
        self.store.record_sim_minute(now)
        self.store.record_credits(now, self._state["credits_remaining"])
        caught_up = self._try_catch_up(now)
        if not self._state["finished"]:
            self._decide(now) if caught_up else self._reconcile(now)
        self._note_retries(now)
        alerts.sync(self.store, now, self._state["credits_remaining"])
        return bool(self._state["finished"])

    def run(self) -> None:
        while not self.step():
            pass
        log.info("scenario finished at %s", self._state["ts"])

    def close(self) -> None:
        self.store.close()

    # -- deciding ------------------------------------------------------------------------------
    def _decide(self, now: int) -> None:
        for w in self._workflows:
            self._guarded(w["workflow_id"], now, self._decide_workflow, w, now)

    def _reconcile(self, now: int) -> None:
        """Proposals follow the platform, not the store, so an approval needn't wait for ingest and lapse."""
        assert self._governance is not None
        for w in self._workflows:
            self._guarded(w["workflow_id"], now, self._governance.reconcile, w["workflow_id"], now)

    def _decide_workflow(self, workflow: dict, now: int) -> None:
        assert self._governance is not None
        self._governance.reconcile(workflow["workflow_id"], now)
        # Incidents first, then the dimensions that can move the primary KPI, then exploration (ADR-0007).
        hallucination.respond(self.store, self.sim, self._credits, self._governance, workflow, now)
        flagged = callerid.check_degraded(self.store, self.sim, self._governance, workflow, now)
        experiments.respond(self.store, self.sim, self._governance, workflow, now)
        schedule.respond(self.store, self.sim, self._governance, workflow, now)
        callerid.explore(self.store, self.sim, self._governance, workflow, now, flagged)

    def _guarded(self, workflow_id: str, now: int, action: Callable[..., None], *args: object) -> None:
        """One workflow's trouble, whatever it is, is a recorded outcome, never the loop's end."""
        try:
            action(*args)
        except Exception as e:
            log.exception("%s: decide step failed", workflow_id)
            self._note(now, workflow_id, "error", {"error": f"{type(e).__name__}: {e}"})

    def _note_retries(self, now: int) -> None:
        if self._retried:
            self._note(now, NO_WORKFLOW, "retried", {"retries": self._retried})
            self._retried.clear()

    def _note(self, now: int, workflow_id: str, outcome: str, detail: dict) -> None:
        with self.store.transaction() as conn:
            Store.record_decision(conn, now, workflow_id, "loop", outcome, None, detail)

    # -- ingestion -----------------------------------------------------------------------------
    def _try_catch_up(self, now: int) -> bool:
        """Catch up, or say why not. Nothing new is decided on a store that is behind; the next tick tries again."""
        try:
            self._catch_up()
        except Exception as e:
            log.exception("ingest failed at minute %d", now)
            self._note(now, NO_WORKFLOW, "ingest_failed", {"error": f"{type(e).__name__}: {e}"})
            return False
        return True

    def _catch_up(self) -> None:
        """Ingest, in order, every chunk the simulator has published after the cursor."""
        cursor, _ = self.store.cursor()
        pending = self.sim.list_chunks("history", cursor) + self.sim.list_chunks("events", cursor)
        for meta in sorted(pending, key=lambda c: c["chunk"]):
            self._ingest_with_retry(meta)
        # Everything the simulator had published is in; the store is current to its clock.
        self.store.mark_ingested_through(self._state["sim_minute"])

    def _ingest_with_retry(self, meta: dict) -> None:
        for attempt in range(1, CHUNK_ATTEMPTS + 1):
            try:
                self.ingestor.ingest_chunk(meta, self.sim.chunk_lines(meta["chunk"]))
                return
            except (IngestError, SimError, httpx.TransportError) as e:
                log.warning("ingest attempt %d/%d failed: %s", attempt, CHUNK_ATTEMPTS, e)
                if attempt == CHUNK_ATTEMPTS:
                    raise
                self._retried.append(f"chunk {meta['chunk']}: {e}")
                self.sim.back_off(attempt)


def _check_same_world(run, st: dict) -> None:
    if run["scenario"] != st["scenario"]:
        raise ResumeRefused(
            f"store belongs to scenario {run['scenario']!r} but the simulator is running "
            f"{st['scenario']!r}. Point --db at a fresh file (or the store for this scenario).")
    if st["sim_minute"] < run["ingested_through_minute"]:
        raise ResumeRefused(
            f"the simulator's clock (minute {st['sim_minute']}) is behind the store's cursor "
            f"(minute {run['ingested_through_minute']}): the simulator was restarted, so this is a "
            f"new world. Point --db at a fresh file to start over.")


def _ts_at(d0: date, minute: int) -> str:
    return (datetime.combine(d0, datetime.min.time()) + timedelta(minutes=minute)).isoformat()
