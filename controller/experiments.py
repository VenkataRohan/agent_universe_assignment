"""Experiment engine: each experiment's state machine and its daily look (ADR-0005, 0006, 0007, 0010).

    planned -> screen_requested -> screening -> screened
    any -> inconclusive | aborted

Every state change is committed with its decision, and each decision carries the experiment as it
stood, so the decisions table reads as the experiment's history.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from . import stats
from .composer import Composed, compose, effective_dist, normalised, set_distribution, violations
from .governance import APPLIED, IN_FLIGHT, Draft, GovernanceClient
from .simclient import SimClient
from .simtime import MINUTES_PER_DAY, label, minute_of
from .store import Store, experiment_view

KIND = "experiment"
SCREEN_KIND = "experiment_screen"
END_KIND = "experiment_end"
PLANNED, SCREEN_REQUESTED, SCREENING, SCREENED = "planned", "screen_requested", "screening", "screened"
INCONCLUSIVE, ABORTED = "inconclusive", "aborted"

# Which dimensions can move each primary KPI. Welcome and voice are heard after pickup (ADR-0007).
DIMENSIONS_FOR_KPI: Mapping[str, tuple[str, ...]] = {"engagement_rate": ("welcome_message", "voice")}
CONTROL_SHARE = 0.5
SUPERVISOR_QUIET_MINUTES = 7 * MINUTES_PER_DAY  # a supervisor's own choice is left to run this long
LOOK_MINUTE_OF_DAY = 9 * 60


def respond(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, now: int) -> None:
    wf = workflow["workflow_id"]
    dimensions = DIMENSIONS_FOR_KPI.get(workflow["objective"]["primary"], ())
    if not dimensions:
        return store.note_decision(now, wf, KIND, "not_applicable",
                                   {"why": "welcome message and voice can't move this workflow's primary KPI"})
    exp = store.latest_experiment(wf)
    if exp is not None and exp["status"] in (PLANNED, SCREEN_REQUESTED):
        exp = _follow_request(store, exp, now)
    if exp is not None and exp["status"] in (SCREENING, SCREENED):  # a screened split is still live
        exp = _abort_if_edited(store, exp, now)
    if exp is not None and exp["status"] == SCREENING and _look_due(exp, now):
        exp = _look(store, exp, now)
    if exp is not None and exp["split_live"]:
        return _return_to_control(store, sim, governance, workflow, exp, now)
    if exp is None or exp["status"] == ABORTED:
        _start(store, sim, governance, workflow, dimensions, now)


# -- starting -------------------------------------------------------------------------------------
def _start(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping,
           dimensions: Iterable[str], now: int) -> None:
    """Design the experiment against the config as it is now, and ask for it in the same breath."""
    wf = workflow["workflow_id"]
    hold = governance.hold_reason(wf, SCREEN_KIND, now)
    if hold is not None:
        return store.note_decision(now, wf, KIND, "held", {"hold": hold})
    current = sim.config(wf)
    design = _design(store, workflow, current["config"], dimensions, now)
    if not design["dimensions"]:
        return store.note_decision(now, wf, KIND, "nothing_to_test", {"why": "no dimension has an untried option free to test"})
    composed = compose(current, *(set_distribution(d, plan["split"]) for d, plan in design["dimensions"].items()))
    problems = violations(workflow, composed)
    if problems:
        return store.note_decision(now, wf, KIND, "blocked", {"violations": problems})
    with store.transaction() as conn:
        seq = Store.next_experiment_seq(conn, wf)
        experiment_id = f"{wf}-exp-{seq}"
        Store.insert_experiment(conn, experiment_id=experiment_id, workflow_id=wf, seq=seq, status=PLANNED,
                                design=json.dumps(design, sort_keys=True), planned_minute=now)
        _record(store, conn, now, "planned", experiment_id, {})
    governance.propose(_screen_draft(store, workflow, store.experiment(experiment_id), composed), now)


def _design(store: Store, workflow: Mapping, config: Mapping, dimensions: Iterable[str], now: int) -> dict:
    """Control on half the traffic, the other half shared equally among every other option on offer."""
    planned = {}
    for dim in dimensions:
        edit = store.last_supervisor_edit(workflow["workflow_id"], dim)
        before = effective_dist(config, "default", dim)
        if not before or (edit is not None and now - edit < SUPERVISOR_QUIET_MINUTES):
            continue
        control = max(sorted(before), key=lambda o: before[o])  # what most of the traffic ran before the experiment
        challengers = sorted(set(workflow["options"][dim]) - {control})
        if not challengers:
            continue
        # Rounded down, so that normalising can only leave control at or above its share.
        each = math.floor((1 - CONTROL_SHARE) / len(challengers) * 1e6) / 1e6
        planned[dim] = {"control": control, "before": before, "challengers": challengers,
                        "split": {control: CONTROL_SHARE, **{c: each for c in challengers}}}
    excluded = sorted(c for c, dims in config["allocations"].items() if c != "default" and set(dims) & set(planned))
    return {"dimensions": planned, "excluded_cohorts": excluded}


def _screen_draft(store: Store, workflow: Mapping, exp: sqlite3.Row, composed: Composed) -> Draft:
    design = json.loads(exp["design"])
    plans = design["dimensions"]
    exposed = round(1 - math.prod(plan["split"][plan["control"]] / sum(plan["split"].values()) for plan in plans.values()), 4)
    value = _expected_value(store, workflow, design)
    arms = "; ".join(f"{dim} {plan['control']} (control, half the traffic) against {', '.join(plan['challengers'])}"
                     for dim, plan in sorted(plans.items()))
    return Draft(
        workflow_id=workflow["workflow_id"], kind=SCREEN_KIND, composed=composed,
        change_summary=(f"Exploration, not a claimed win: screen {arms}. Exposure-weighted expected value "
                        f"{value * 100:+.1f} points of engagement rate; {exposed:.0%} of calls hear at least one challenger."),
        rationale=("Every option on offer runs at once in a factorial split, only to choose one candidate per dimension "
                   "to confirm on fresh data. Nothing is promoted from a screen. The expected value counts only options "
                   "already heard by callers; an option nobody has heard adds nothing."),
        evidence={"experiment_id": exp["experiment_id"], "stage": "screen", "label": "exploration", "design": design,
                  "exposure_weighted_expected_value": value, "exposed_share": exposed},
        gain_lower_bound=0.0, affected_share=exposed)


def _expected_value(store: Store, workflow: Mapping, design: Mapping) -> float:
    """Engagement-rate change from the split: each challenger's share x its lift so far. No calls, no lift.

    Data closes at a supervisor's latest edit to an experimented dimension, as an aborted experiment's did.
    """
    wf = workflow["workflow_id"]
    edits = [e for d in DIMENSIONS_FOR_KPI[workflow["objective"]["primary"]] if (e := store.last_supervisor_edit(wf, d)) is not None]
    rows = store.arm_counts(wf, 0, before_hour=max(edits) // 60 if edits else None, excluded_cohorts=design["excluded_cohorts"])
    value = 0.0
    for dim, plan in design["dimensions"].items():
        totals = _totals(rows, dim)
        control = totals.get(plan["control"])
        total_weight = sum(plan["split"].values())
        for c in plan["challengers"]:
            if control is not None and c in totals and control.trials and totals[c].trials:
                value += plan["split"][c] / total_weight * (totals[c].rate - control.rate)
    return round(value, 4)


def _totals(rows: Iterable[sqlite3.Row], dim: str) -> dict[str, stats.Counts]:
    out: dict[str, stats.Counts] = {}
    for r in rows:
        c = out.get(r[dim], stats.Counts(0, 0))
        out[r[dim]] = stats.Counts(c.successes + r["engaged"], c.trials + r["connected"])
    return out


# -- following the request ------------------------------------------------------------------------
def _screen_proposal(store: Store, exp: sqlite3.Row) -> sqlite3.Row | None:
    p = store.latest_proposal(exp["workflow_id"], SCREEN_KIND)
    return p if p is not None and json.loads(p["body"])["evidence"]["experiment_id"] == exp["experiment_id"] else None


def _follow_request(store: Store, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    p = _screen_proposal(store, exp)
    if p is None:  # a crash between planning and asking: the design may be stale, so it is planned afresh
        return _move(store, exp, ABORTED, now, "aborted", {"why": "planned but never requested"},
                     end_minute=now, end_reason="never_requested")
    if p["status"] == APPLIED:
        return _move(store, exp, SCREENING, now, "screening", {"proposal_id": p["proposal_id"]},
                     screen_version=p["applied_version"], screen_minute=p["applied_minute"],
                     last_look_day=p["applied_minute"] // MINUTES_PER_DAY)
    if p["status"] in IN_FLIGHT:
        return exp if exp["status"] == SCREEN_REQUESTED else _move(store, exp, SCREEN_REQUESTED, now, "screen_requested",
                                                                   {"proposal_id": p["proposal_id"]})
    # Refused, lapsed or stale: the arms never ran, so there is nothing to learn from it.
    return _move(store, exp, ABORTED, now, "aborted", {"why": f"the screen request {p['proposal_id']} ended {p['status']}",
                                                       "reasons": json.loads(p["reasons"]) if p["reasons"] else []},
                 end_minute=now, end_reason=f"request_{p['status']}")


# -- supervisor edits ------------------------------------------------------------------------------
def _abort_if_edited(store: Store, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    """A supervisor edit to an experimented dimension closes the experiment's data at the edit.

    Any other change is a new config version, and each version is its own stratum at the look.
    """
    design = json.loads(exp["design"])
    plans = design["dimensions"]
    edit = next((e for e in store.config_changes_after(exp["workflow_id"], exp["screen_version"])
                 if e["actor"] == "supervisor" and set(plans) & set(e["changed_dims"])), None)
    if edit is None:
        return exp
    minute = minute_of(edit["ts"], store.d0())
    # The whole hour of the edit goes too: counters are hourly, and a campaign locked to an earlier version
    # would otherwise carry calls dialled after the edit under a version still inside the experiment.
    rows = store.arm_counts(exp["workflow_id"], exp["screen_version"], edit["version"] - 1, minute // 60, design["excluded_cohorts"])
    views = {dim: {"arms": _arm_view(_totals(rows, dim), plan)} for dim, plan in plans.items()}
    return _move(store, exp, ABORTED, now, "aborted",
                 {"why": f"a supervisor changed {', '.join(sorted(set(plans) & set(edit['changed_dims'])))} at {label(minute)}",
                  "edit": {"version": edit["version"], "at": label(minute)}, "dimensions": views},
                 end_minute=minute, end_reason="supervisor_edit", split_live=1, candidates=None)


# -- the daily look --------------------------------------------------------------------------------
def _look_due(exp: sqlite3.Row, now: int) -> bool:
    """Once a day from 09:00, so a restart that misses the 09:00 tick still gets that day's look."""
    day, minute = divmod(now, MINUTES_PER_DAY)
    return minute >= LOOK_MINUTE_OF_DAY and exp["last_look_day"] != day


def _look(store: Store, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    design = json.loads(exp["design"])
    plans = design["dimensions"]
    rows = store.arm_counts(exp["workflow_id"], exp["screen_version"], excluded_cohorts=design["excluded_cohorts"])
    arms = {dim: _totals(rows, dim) for dim in plans}
    views: dict[str, dict[str, Any]] = {dim: {"arms": _arm_view(arms[dim], plan)} for dim, plan in plans.items()}
    day = now // MINUTES_PER_DAY
    if min(arm["connected"] for view in views.values() for arm in view["arms"].values()) < stats.SCREEN_MIN_TRAFFIC_PER_ARM:
        if now - exp["screen_minute"] < stats.SCREEN_MAX_DAYS * MINUTES_PER_DAY:
            return _move(store, exp, SCREENING, now, "waiting_for_data", {"dimensions": views}, last_look_day=day)
        return _move(store, exp, INCONCLUSIVE, now, "no_candidate", {"dimensions": views, "why": "too little traffic to judge"},
                     last_look_day=day, end_minute=now, end_reason="insufficient_data", split_live=1)
    versions = sorted({r["config_version"] for r in rows})
    candidates = {}
    for dim, plan in plans.items():
        strata = [_stratum([r for r in rows if r["config_version"] == v], dim, plan) for v in versions]
        result = stats.screen_strata(strata)
        views[dim].update(candidate=result.candidate, eliminated=list(result.eliminated))
        if result.candidate is not None:
            candidates[dim] = result.candidate
    detail = {"dimensions": views, "strata": versions}
    if candidates:
        return _move(store, exp, SCREENED, now, "screened", detail, last_look_day=day, candidates=json.dumps(candidates, sort_keys=True))
    return _move(store, exp, INCONCLUSIVE, now, "no_candidate", {**detail, "why": "no challenger beat control on its point estimate"},
                 last_look_day=day, end_minute=now, end_reason="no_candidate", split_live=1)


def _stratum(rows: list[sqlite3.Row], dim: str, plan: Mapping) -> tuple[stats.Counts, dict[str, stats.Counts]]:
    totals = _totals(rows, dim)
    empty = stats.Counts(0, 0)
    return totals.get(plan["control"], empty), {c: totals.get(c, empty) for c in plan["challengers"]}


def _arm_view(totals: Mapping[str, stats.Counts], plan: Mapping) -> dict[str, dict[str, int]]:
    empty = stats.Counts(0, 0)
    return {o: {"connected": totals.get(o, empty).trials, "engaged": totals.get(o, empty).successes}
            for o in [plan["control"], *plan["challengers"]]}


# -- returning to control --------------------------------------------------------------------------
def _return_to_control(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, exp: sqlite3.Row, now: int) -> None:
    """Ask for the dimensions still carrying this experiment's split back. Anything else is left alone."""
    wf, experiment_id, status = workflow["workflow_id"], exp["experiment_id"], exp["status"]
    current = sim.config(wf)
    plans = json.loads(exp["design"])["dimensions"]
    live = sorted(d for d, plan in plans.items() if effective_dist(current["config"], "default", d) == normalised(plan["split"]))
    if not live:
        _move(store, exp, status, now, "split_removed", {}, split_live=0)
        return None
    hold = governance.hold_reason(wf, END_KIND, now)
    if hold is not None:
        return store.note_decision(now, wf, KIND, "return_held", {"experiment_id": experiment_id, "hold": hold})
    composed = compose(current, *(set_distribution(d, plans[d]["before"]) for d in live))
    problems = violations(workflow, composed)
    if problems:
        return store.note_decision(now, wf, KIND, "return_blocked", {"experiment_id": experiment_id, "violations": problems})
    governance.propose(Draft(
        workflow_id=wf, kind=END_KIND, composed=composed,
        change_summary=f"End experiment {experiment_id}: return {' and '.join(live)} to what ran before it ({exp['end_reason']}).",
        rationale="The experiment has ended without anything to carry forward, so nobody should keep hearing its challengers.",
        evidence={"experiment_id": experiment_id, "stage": "return_to_control", "end_reason": exp["end_reason"], "dimensions": live},
        gain_lower_bound=0.0, affected_share=1 - CONTROL_SHARE), now)
    return None


# -- state changes, each committed with its decision -----------------------------------------------
def _move(store: Store, exp: sqlite3.Row, status: str, now: int, outcome: str, detail: dict, **fields: object) -> sqlite3.Row:
    with store.transaction() as conn:
        Store.update_experiment(conn, exp["experiment_id"], status=status, **fields)
        _record(store, conn, now, outcome, exp["experiment_id"], detail)
    return store.experiment(exp["experiment_id"])


def _record(store: Store, conn: sqlite3.Connection, now: int, outcome: str, experiment_id: str, detail: dict) -> None:
    """Call inside the transaction of the state change: the row read here is the one about to commit."""
    row = store.experiment(experiment_id)
    Store.record_decision(conn, now, row["workflow_id"], KIND, outcome, None, {"experiment": experiment_view(row), **detail})
