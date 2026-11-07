"""CLI: run the controller, or ask the store questions while it runs.

    python -m controller --sim-url http://127.0.0.1:8700 --db state/controller.db     # run the loop
    python -m controller health --db state/controller.db                            # read-only
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import httpx

from . import queries
from .loop import Controller, ResumeRefused


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="controller")
    ap.add_argument("--db", default="state/controller.db", help="the controller's SQLite store")
    ap.add_argument("--sim-url", help="simulator base URL (runs the loop)")
    ap.add_argument("--fresh-if-mismatch", action="store_true",
                    help="if the store belongs to another world, move it aside and start fresh "
                         "instead of refusing (used by run.sh so unattended runs need no manual step)")
    sub = ap.add_subparsers(dest="cmd")
    h = sub.add_parser("health", help="cursor, events ingested and ingest lag")
    h.add_argument("--db", default=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.cmd == "health":
        return _health(Path(a.db))
    if not a.sim_url:
        ap.error("--sim-url is required to run the loop")
    return _run(a.sim_url, Path(a.db), a.fresh_if_mismatch)


def _run(sim_url: str, db: Path, fresh_if_mismatch: bool) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("controller")
    with httpx.Client(base_url=sim_url, timeout=120) as http:
        c = Controller(http, db)
        try:
            c.start()
        except ResumeRefused as e:
            c.close()
            if not fresh_if_mismatch:
                print(f"controller: refusing to resume: {e}", file=sys.stderr)
                return 2
            aside = db.with_name(f"{db.name}.stale-{time.strftime('%Y%m%dT%H%M%S')}")
            for suffix in ("", "-wal", "-shm"):
                if Path(f"{db}{suffix}").exists():
                    Path(f"{db}{suffix}").rename(f"{aside}{suffix}")
            log.warning("store did not match this world (%s); moved it to %s and starting fresh", e, aside)
            c = Controller(http, db)
            c.start()
        try:
            c.run()
        finally:
            c.close()
    return 0


def _health(db: Path) -> int:
    if not db.exists():
        print(f"controller: no store at {db}", file=sys.stderr)
        return 1
    h = queries.health(db)
    if not h["started"]:
        print("controller: store exists but the controller has not started yet")
        return 0
    rows = [
        ("scenario", h["scenario"]),
        ("cursor", f"chunk {h['cursor_chunk']}, ingested through {_sim_time(h['ingested_through_minute'])}"
                   f" (minute {h['ingested_through_minute']})"),
        ("events ingested", f"{h['events_ingested']} (last event_id {h['last_event_id']})"),
        ("ingest lag", f"{h['ingest_lag_ticks']:g} ticks (simulator at {_sim_time(h['sim_minute_seen'])})"
                       + (f"; last ingest {h['seconds_since_last_ingest']}s ago"
                          if h["seconds_since_last_ingest"] is not None else "")),
    ]
    for k, v in rows:
        print(f"{k:<18}{v}")
    return 0


def _sim_time(minute: int) -> str:
    day, m = divmod(minute, 1440)
    return f"D{day} {m // 60:02d}:{m % 60:02d}"


if __name__ == "__main__":
    sys.exit(main())
