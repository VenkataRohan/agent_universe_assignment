"""Loop: one process, one thread, one 15-minute tick at a time.

Each tick: advance the simulated clock -> ingest every chunk after the cursor -> for each workflow,
reconcile its Proposals (applying any approval) and run the caller-ID check. Later tickets add more
detection and decisions to `_decide`.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from . import analysis, callerid
from .governance import GovernanceClient
from .ingest import IngestError, Ingestor
from .registry import OVERLAYS, Overlay
from .simclient import SimClient, SimError
from .store import Store

log = logging.getLogger("controller")
CHUNK_ATTEMPTS = 3


class ResumeRefused(Exception):
    """The store belongs to a different world than the simulator we are talking to."""


class Controller:
    def __init__(self, http: httpx.Client, db_path: str | Path, overlays: Mapping[str, Overlay] = OVERLAYS):
        self.sim = SimClient(http)
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
        self._catch_up()
        analysis.bootstrap_from_history(self.store, workflows)
        self._workflows = workflows
        self._governance = GovernanceClient(self.store, self.sim, workflows, self._overlays)
        # A Proposal left half-done by an earlier life must not wait for the next tick.
        for w in workflows:
            self._guarded(w["workflow_id"], self._state["sim_minute"], self._governance.reconcile,
                          w["workflow_id"], self._state["sim_minute"])

    def step(self) -> bool:
        """Advance one tick and ingest what it produced. Returns True once the scenario is finished."""
        if self._ingestor is None:
            self.start()
        if self._state["finished"]:
            return True
        self._state = self.sim.advance_to(self._state["sim_minute"] + self._state["tick_minutes"])
        self.store.record_sim_minute(self._state["sim_minute"])
        self._catch_up()
        if not self._state["finished"]:
            self._decide(self._state["sim_minute"])
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

    def _decide_workflow(self, workflow: dict, now: int) -> None:
        assert self._governance is not None
        self._governance.reconcile(workflow["workflow_id"], now)
        callerid.respond(self.store, self.sim, self._governance, workflow, now)

    def _guarded(self, workflow_id: str, now: int, action: Callable[..., None], *args: object) -> None:
        """One workflow's trouble with the simulator is a recorded outcome, never the loop's end."""
        try:
            action(*args)
        except SimError as e:
            log.warning("%s: decide step failed: %s", workflow_id, e)
            with self.store.transaction() as conn:
                Store.record_decision(conn, now, workflow_id, "loop", "error", None, {"error": str(e)})

    # -- ingestion -----------------------------------------------------------------------------
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
