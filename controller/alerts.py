"""Alerts: the four conditions that page, re-judged at the end of every tick (ADR-0015).

Exactly these page, and every message says what to look at. An alert is raised once, stays active
while its condition holds, and is logged when raised. What else is worth seeing is a note (`queries.health`).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from itertools import groupby

from .credits import RESERVE
from .governance import APPLIED, CONFIG_CHANGED_BEFORE_SENT, IN_FLIGHT, LAPSED, STALE
from .hallucination import ESCALATED
from .simtime import MINUTES_PER_DAY, label
from .store import Store

log = logging.getLogger("controller")

CREDITS, INGEST_LAG, INCIDENT, APPLY_FAILED = "credits_below_reserve", "ingest_lag", "incident_unfixed", "apply_failed"
MAX_INGEST_LAG_TICKS = 2
INCIDENT_FIX_MINUTES = 240
STALE_LOOP_PROPOSALS = 3  # in a row, one workflow and kind: one or two are a supervisor editing, and are expected
APPLY_FAILURE_ACTIVE_MINUTES = MINUTES_PER_DAY  # the controller rebuilds on its own, so nothing else may end one of these


@dataclass(frozen=True)
class Page:
    key: str        # one occurrence of the condition: the same key on the next tick is the same page
    condition: str
    message: str


def sync(store: Store, now: int, credits_left: float) -> None:
    """Raise what newly holds, clear what no longer does, and keep the active messages current."""
    pages = {p.key: p for p in _ingest_lag(store) + _credits(credits_left) + _incidents(store, now) + _apply_failures(store, now)}
    with store.transaction() as conn:
        active = Store.active_alert_keys(conn)
        for key in active - pages.keys():
            Store.clear_alert(conn, key, now)
        for key, page in pages.items():
            if key in active:
                Store.update_alert(conn, key, page.message)
            else:
                Store.raise_alert(conn, key, page.condition, page.message, now)
                log.error("PAGE at %s: %s", label(now), page.message)


def _ingest_lag(store: Store) -> list[Page]:
    run = store.run_identity()
    assert run is not None  # alerts are judged at the end of a tick, after the run was created
    lag = (run["sim_minute_seen"] - run["ingested_through_minute"]) / run["tick_minutes"]
    if lag <= MAX_INGEST_LAG_TICKS:
        return []
    return [Page(INGEST_LAG, INGEST_LAG, f"Ingest is {lag:g} ticks behind: the store is complete through {label(run['ingested_through_minute'])} "
                                         f"and the simulator is at {label(run['sim_minute_seen'])}, and nothing is being decided until it catches up. "
                                         f"Look at the controller log for ingest failures and at the simulator's GET /events/chunks.")]


def _credits(left: float) -> list[Page]:
    if left >= RESERVE:
        return []
    return [Page(CREDITS, CREDITS, f"{left:g} credits left, below the reserve of {RESERVE}: the next incident may not be able to "
                                   f"validate a fix. Look at the credit burn in `controller health` and at what spent it.")]


def _incidents(store: Store, now: int) -> list[Page]:
    """Open past the deadline, or handed to a person and still on the same prompt. The clock starts at detection."""
    return [_incident_page(i, now) for i in store.unfixed_incidents()
            if now - i["detected_minute"] >= INCIDENT_FIX_MINUTES and not (i["status"] == ESCALATED and store.prompt_changed_since(i["workflow_id"], i["end_minute"]))]


def _incident_page(i: sqlite3.Row, now: int) -> Page:
    state = "still open" if i["status"] != ESCALATED else f"escalated ({i['end_reason']}) with no fix in place"
    hours = (now - i["detected_minute"]) / 60
    return Page(f"{INCIDENT}:{i['incident_id']}", INCIDENT,
                f"Incident {i['incident_id']} on prompt {i['bad_prompt']} is {state}, {hours:g} hours after it was detected at "
                f"{label(i['detected_minute'])}. People may still be hearing it. Look at `controller why {i['workflow_id']} "
                f'--at "{label(now)}"`, then approve a fix or change the prompt by hand.')


def _apply_failures(store: Store, now: int) -> list[Page]:
    pages: list[Page] = []
    for (wf, kind), rows in groupby(store.proposal_endings(), key=lambda p: (p["workflow_id"], p["kind"])):
        ended = [p for p in rows if p["status"] not in IN_FLIGHT]
        pages += [_lapsed_page(p, wf) for i, p in enumerate(ended)
                  if p["status"] == LAPSED and _recent(p, now) and not any(q["status"] == APPLIED for q in ended[i + 1:])]
        streak = _trailing_stale(ended)
        if len(streak) >= STALE_LOOP_PROPOSALS and _recent(streak[-1], now):
            pages.append(_stale_loop_page(streak, wf, kind))
    return pages


def _recent(proposal: sqlite3.Row, now: int) -> bool:
    return now - proposal["ended_minute"] < APPLY_FAILURE_ACTIVE_MINUTES


def _trailing_stale(ended: list[sqlite3.Row]) -> list[sqlite3.Row]:
    streak: list[sqlite3.Row] = []
    for p in reversed(ended):
        if not _base_moved(p):
            break
        streak.insert(0, p)
    return streak


def _base_moved(p: sqlite3.Row) -> bool:
    """Stale also covers a request hours ran out on, one the platform forgot and one it refused: none of those is a loop."""
    return p["status"] == STALE and (p["last_outcome"] == STALE or (p["reasons"] is not None and CONFIG_CHANGED_BEFORE_SENT in p["reasons"]))


def _lapsed_page(p: sqlite3.Row, wf: str) -> Page:
    return Page(f"{APPLY_FAILED}:lapsed:{p['proposal_id']}", APPLY_FAILED,
                f"The approval for {p['proposal_id']} lapsed at {label(p['ended_minute'])} before it could be applied, so an approved change "
                f"is not live. Look at `controller why {wf} --at \"{label(p['ended_minute'])}\"` and at why the apply window was missed.")


def _stale_loop_page(streak: list[sqlite3.Row], wf: str, kind: str) -> Page:
    ids = ", ".join(p["proposal_id"] for p in streak)
    return Page(f"{APPLY_FAILED}:stale_loop:{wf}:{kind}", APPLY_FAILED,
                f"{len(streak)} {kind} Proposals in a row on {wf} went stale ({ids}): the base keeps moving underneath them. Look at who is "
                f"changing this workflow's config with `controller why {wf} --at \"{label(streak[-1]['ended_minute'])}\"`.")
