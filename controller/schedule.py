"""Schedule optimiser: per-cohort hour tables from history, the replay that justifies one, and the check after it.

The dialler runs at capacity, so a table can only decide which cohort takes an hour's dials. The replay holds each
hour's dials and each cohort's dials as they were and moves only who is dialled when. A table's forecast comes from
days it wasn't fitted to, because fitted and replayed on the same days it would be credited with its own noise.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import NamedTuple

from . import stats
from .analysis import hour_rates
from .cohorts import DEFAULT, FEATURES, KPI_COUNTERS
from .composer import compose, set_hour_tables, violations, write_cohorts
from .governance import Draft, GovernanceClient
from .replay import at_risk
from .simclient import SimClient
from .simtime import MINUTES_PER_DAY, label
from .store import Store, load_partition

KIND = "schedule"
COHORTS_KIND = "schedule_cohorts"
ROLLBACK_KIND = "schedule_rollback"
PLAN_KIND = "schedule_plan"
CHECK_KIND = "schedule_check"
PROPOSE, NO_GAIN, GUARDRAIL_FAILS, TOO_LITTLE_HISTORY = "propose", "no_gain", "guardrail_fails", "too_little_history"
NO_DROP = "no_drop"

MIN_RELATIVE_GAIN = 0.03
MIN_WEIGHT, MAX_WEIGHT = 0.5, 2.0
BEFORE_AFTER_HOURS = 72
_WINDOW_DAYS = BEFORE_AFTER_HOURS // 24
SUPERVISOR_QUIET_MINUTES = 7 * MINUTES_PER_DAY  # a supervisor's own schedule is left to run this long
_RAKING_ROUNDS = 60
_CONFOUNDING = ("Before/after evaluation is confounded: the days compared differ in their leads and in anything else "
                "that changed, so it can only flag a significant drop and is not proof that a table worked.")


class _Row(NamedTuple):
    day: int
    cohort: str
    hour: int
    band: int
    dials: int
    connected: int
    engaged: int


_CohortHour = tuple[str, int]


# -- deciding -------------------------------------------------------------------------------------
def respond(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, now: int) -> None:
    applied = store.latest_applied(workflow["workflow_id"], KIND)
    if applied is not None:
        return _check_after_apply(store, sim, governance, workflow, applied, now)
    plan = _plan(store, workflow, now)
    return None if plan is None else _propose(store, sim, governance, workflow, plan, now)


def _plan(store: Store, workflow: Mapping, now: int) -> dict | None:
    """Worked out once: it reads only history, so the answer can't change. None if no table is worth proposing."""
    wf = workflow["workflow_id"]
    if store.last_decision(wf, PLAN_KIND) is None:
        store.note_decision(now, wf, PLAN_KIND, *_evaluate(store, workflow))
    decided = store.last_decision(wf, PLAN_KIND)
    assert decided is not None  # noted just above
    return json.loads(decided["detail"]) if decided["outcome"] == PROPOSE else None


def _evaluate(store: Store, workflow: Mapping) -> tuple[str, dict]:
    objective = workflow["objective"]
    primary = objective["primary"]
    guardrail_key, max_drop = next(iter(objective["guardrail"].items()))
    guardrail = guardrail_key.removesuffix("_max_drop")
    partition = load_partition(store.conn, workflow["workflow_id"])
    assert partition is not None  # bootstrapped from history before the loop decides anything
    rows = [_Row(r["day"], partition.cohort_of({f: r[f] for f in FEATURES}), r["hour"], r["band"], r["dials"], r["connected"], r["engaged"])
            for r in Store.history_call_counts(store.conn, workflow["workflow_id"])]
    days = sorted({r.day for r in rows})
    if len(days) < 2:
        return TOO_LITTLE_HISTORY, {"history_days": len(days)}
    cohorts, window = [c.id for c in partition.cohorts], workflow["dialling_window"]
    folds = [_replay([r for r in rows if r.day in test], _tables([r for r in rows if r.day in fit], primary, cohorts, window))
             for fit, test in ((days[::2], days[1::2]), (days[1::2], days[::2]))]
    replay = _forecast(folds, primary, guardrail)
    gain, drop = replay["relative_gain"], replay["guardrail_drop"] + replay["overall"][guardrail]["margin"]  # the folds' disagreement counts against it
    detail = {"replay": replay, "min_relative_gain": MIN_RELATIVE_GAIN, "guardrail_max_drop": max_drop}
    if gain < MIN_RELATIVE_GAIN:
        return NO_GAIN, detail
    if drop > max_drop:
        return GUARDRAIL_FAILS, detail
    tables = _tables(rows, primary, cohorts, window)
    # A cohort with the same table as default needs neither a table nor to be written into the config.
    return PROPOSE, {**detail, "tables": {c: t for c, t in tables.items() if c == DEFAULT or t != tables[DEFAULT]}}


def _tables(rows: Sequence[_Row], primary: str, cohorts: Sequence[str], window: Sequence[int]) -> dict[str, dict[str, float]]:
    """Each hour of the window: the cohort's attempt-adjusted rate then over its overall rate, clipped."""
    adjusted, overall = _adjusted(rows, primary), _overall(rows, primary)
    def weight(cohort: str, hour: int) -> float:
        rate = adjusted.get((cohort, hour))
        return 1.0 if rate is None or not overall[cohort] else round(min(max(rate / overall[cohort], MIN_WEIGHT), MAX_WEIGHT), 3)
    start, end = window
    return {c: {str(h): weight(c, h) for h in range(start, end)} for c in cohorts}


def _adjusted(rows: Sequence[_Row], kpi: str) -> dict[_CohortHour, float]:
    traffic, successes = KPI_COUNTERS[kpi]
    return {(c, h): rate for c, h, _, _, rate in
            hour_rates([(r.cohort, r.hour, r.band, getattr(r, traffic), getattr(r, successes)) for r in rows])}


def _overall(rows: Sequence[_Row], kpi: str) -> dict[str, float]:
    traffic_col, successes_col = KPI_COUNTERS[kpi]
    totals: dict[str, list[int]] = {}
    for r in rows:
        t = totals.setdefault(r.cohort, [0, 0])
        t[0] += getattr(r, traffic_col)
        t[1] += getattr(r, successes_col)
    return {c: successes / traffic if traffic else 0.0 for c, (traffic, successes) in totals.items()}


# -- the replay -----------------------------------------------------------------------------------
def _replay(rows: Sequence[_Row], tables: Mapping[str, Mapping[str, float]]) -> dict:
    """These days' calls as they were and as the tables would have placed them."""
    dials: dict[_CohortHour, float] = {}
    for r in rows:
        dials[(r.cohort, r.hour)] = dials.get((r.cohort, r.hour), 0.0) + r.dials
    placed = _rake({k: d * tables[k[0]].get(str(k[1]), 1.0) for k, d in dials.items()}, dials)
    pickup, engagement = _adjusted(rows, "pickup_rate"), _adjusted(rows, "engagement_rate")
    pickup_of, engagement_of = _overall(rows, "pickup_rate"), _overall(rows, "engagement_rate")

    def kpis(mix: Mapping[_CohortHour, float], cohort: str | None = None) -> dict[str, float]:
        cells = [k for k in mix if cohort is None or k[0] == cohort]
        connects = {k: mix[k] * pickup.get(k, pickup_of[k[0]]) for k in cells}
        engaged = sum(connects[k] * engagement.get(k, engagement_of[k[0]]) for k in cells)
        total_dials, total_connects = sum(mix[k] for k in cells), sum(connects.values())
        return {"pickup_rate": total_connects / total_dials if total_dials else 0.0,
                "engagement_rate": engaged / total_connects if total_connects else 0.0,
                "concluded_per_dial": engaged / total_dials if total_dials else 0.0}

    return {"dials": sum(dials.values()), "current": kpis(dials), "proposed": kpis(placed),
            "cohorts": {c: {"current": kpis(dials, c), "proposed": kpis(placed, c)} for c in sorted(tables)}}


def _rake(seed: Mapping[_CohortHour, float], actual: Mapping[_CohortHour, float]) -> dict[_CohortHour, float]:
    """Scale `seed` until every cohort and every hour has the dials it actually had."""
    placed = dict(seed)
    for _ in range(_RAKING_ROUNDS):
        for side in (0, 1):
            want: dict[object, float] = {}
            have: dict[object, float] = {}
            for k, d in actual.items():
                want[k[side]] = want.get(k[side], 0.0) + d
                have[k[side]] = have.get(k[side], 0.0) + placed[k]
            for k in placed:
                if have[k[side]]:
                    placed[k] *= want[k[side]] / have[k[side]]
    return placed


def _forecast(folds: Sequence[dict], primary: str, guardrail: str) -> dict:
    """The two folds' predictions together. Each margin is how far apart the folds came out."""
    def combined(path: Sequence[str], kpi: str) -> dict[str, float]:
        def at(fold: dict, side: str) -> float:
            node = fold
            for key in path:
                node = node[key]
            return node[side][kpi]
        current, proposed = (sum(at(f, side) for f in folds) / len(folds) for side in ("current", "proposed"))
        return {"current": current, "proposed": proposed, "margin": abs(at(folds[0], "proposed") - at(folds[1], "proposed")) / 2}

    kpis = (primary, guardrail)
    cohorts = {c: {k: combined(("cohorts", c), k) for k in kpis} for c in sorted(folds[0]["cohorts"])}
    overall = {k: combined((), k) for k in kpis}
    base = overall[primary]["current"]
    per_dial_gain = [f["proposed"]["concluded_per_dial"] - f["current"]["concluded_per_dial"] for f in folds]
    return {"primary_kpi": primary, "guardrail_kpi": guardrail, "overall": overall, "cohorts": cohorts,
            "cohorts_at_risk": sorted(c for c, v in cohorts.items() if any(at_risk(v[k]) for k in kpis)),
            "dials_replayed": round(sum(f["dials"] for f in folds)),
            "relative_gain": (overall[primary]["proposed"] - base) / base if base else 0.0,
            "guardrail_drop": overall[guardrail]["current"] - overall[guardrail]["proposed"],
            "worst_fold_gain_per_dial": min(per_dial_gain)}


# -- proposing ------------------------------------------------------------------------------------
def _propose(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, plan: Mapping, now: int) -> None:
    wf = workflow["workflow_id"]
    edit = store.last_supervisor_edit(wf, "schedule")
    if edit is not None and now - edit < SUPERVISOR_QUIET_MINUTES:
        return store.note_decision(now, wf, KIND, "held", {"hold": f"a supervisor changed this workflow's schedule in the last {SUPERVISOR_QUIET_MINUTES // MINUTES_PER_DAY} days"})
    current = sim.config(wf)
    partition = load_partition(store.conn, wf)
    assert partition is not None  # bootstrapped from history before the loop decides anything
    known = {c.id for c in partition.cohorts}
    foreign = sorted(c["id"] for c in current["config"]["cohorts"] if c["id"] not in known)
    if foreign:  # first match wins in the config, so these could take leads from the tables' cohorts
        return store.note_decision(now, wf, KIND, "blocked", {"cohorts_outside_the_partition": foreign})
    tables, needed = plan["tables"], write_cohorts(partition.config_cohorts(set(plan["tables"])))
    composed = compose(current, needed, set_hour_tables(tables))
    if not composed.changed_dims:
        return store.note_decision(now, wf, KIND, "already_in_place", {})
    if "cohorts" in composed.changed_dims and governance.auto_apply_covers(wf, "schedule"):
        # Cohorts always need an approver (ADR-0002) and the table needn't, so the table waits for them and then auto-applies.
        return _submit(store, governance, workflow, Draft(
            workflow_id=wf, kind=COHORTS_KIND, composed=compose(current, needed),
            change_summary="Define this workflow's cohorts so that each can have its own call hours. Nobody's calls change yet.",
            rationale=("A per-cohort hour table can only name cohorts the config defines, and cohorts need a named approval. "
                       "These cohorts carry no allocation or table of their own, so every lead is dialled exactly as today until "
                       "the table follows."),
            evidence={"detector": "schedule_replay", "stage": "cohorts", "replay": plan["replay"]},
            gain_lower_bound=0.0, affected_share=0.0), now)
    replay = plan["replay"]
    return _submit(store, governance, workflow, Draft(
        workflow_id=wf, kind=KIND, composed=composed,
        change_summary=(f"Call each cohort at its better hours: a replay of history predicts {replay['primary_kpi']} "
                        f"{replay['overall'][replay['primary_kpi']]['current']:.1%} -> {replay['overall'][replay['primary_kpi']]['proposed']:.1%}."),
        rationale=(f"On history days the table wasn't fitted to, with each hour's and each cohort's dials held as they were, the "
                   f"replay predicts a {replay['relative_gain']:.1%} relative gain on {replay['primary_kpi']}, at least the "
                   f"{MIN_RELATIVE_GAIN:.0%} required, with the guardrail holding. Every dialling-window hour is present and weights "
                   f"stay within [{MIN_WEIGHT}, {MAX_WEIGHT}]. After applying, it is rolled back if the primary KPI drops significantly "
                   f"against the {_WINDOW_DAYS} days before. {_CONFOUNDING}"),
        evidence={"detector": "schedule_replay", "stage": "table", "replay": replay, "tables": tables},
        # The whole workflow's gain, so the share below is 1.
        gain_lower_bound=replay["worst_fold_gain_per_dial"], affected_share=1.0,
        dry_run={"method": "history_replay", "held_out_days": True, **{k: replay[k] for k in (
            "dials_replayed", "primary_kpi", "guardrail_kpi", "overall", "cohorts", "cohorts_at_risk")}}), now)


def _submit(store: Store, governance: GovernanceClient, workflow: Mapping, draft: Draft, now: int) -> None:
    wf = workflow["workflow_id"]
    problems = violations(workflow, draft.composed)
    if problems:
        return store.note_decision(now, wf, KIND, "blocked", {"violations": problems, "kind": draft.kind})
    auto = governance.auto_apply_refusal(wf, draft.kind, draft.composed, now) is None
    hold = governance.hold_reason(wf, draft.kind, now, auto=auto)
    if hold is not None:
        return store.note_decision(now, wf, KIND, "held", {"hold": hold, "kind": draft.kind})
    (governance.auto_apply if auto else governance.propose)(draft, now)
    return None


# -- after applying -------------------------------------------------------------------------------
def _check_after_apply(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping,
                       applied: sqlite3.Row, now: int) -> None:
    """One comparison, once 72 complete hours have followed the change: a table is judged on its first three days only."""
    wf = workflow["workflow_id"]
    last = store.last_decision(wf, CHECK_KIND)
    if last is not None and last["outcome"] == NO_DROP:
        return None
    rolled_back = store.latest_applied(wf, ROLLBACK_KIND)
    if rolled_back is not None:
        return None
    if any(e["actor"] == "supervisor" and "schedule" in e["changed_dims"] for e in store.config_changes_after(wf, applied["applied_version"])):
        return store.note_decision(now, wf, CHECK_KIND, NO_DROP, {"why": "a supervisor changed the schedule, and their choice stands"})
    hour = applied["applied_minute"] // 60
    after_start = hour + 1  # the hour of the change is mixed, so it is on neither side
    after_end = after_start + BEFORE_AFTER_HOURS
    if now // 60 < after_end:
        return None
    kpi = workflow["objective"]["primary"]
    before_calls, after_calls = store.kpi_totals(wf, hour - BEFORE_AFTER_HOURS, hour), store.kpi_totals(wf, after_start, after_end)
    before, after = _kpi_counts(before_calls, kpi), _kpi_counts(after_calls, kpi)
    verdict = stats.schedule_dropped(after, before)
    detail = {"proposal_id": applied["proposal_id"], "kpi": kpi, "alpha": stats.SCHEDULE_ROLLBACK_ALPHA, "p_value": verdict.p_value,
              "before": {"successes": before.successes, "trials": before.trials, "from": label((hour - BEFORE_AFTER_HOURS) * 60), "to": label(hour * 60)},
              "after": {"successes": after.successes, "trials": after.trials, "from": label(after_start * 60), "to": label(after_end * 60)}}
    if not verdict.holds:
        return store.note_decision(now, wf, CHECK_KIND, NO_DROP, detail)
    store.note_decision(now, wf, CHECK_KIND, "dropped", detail)
    return _roll_back(store, sim, governance, workflow, applied, before, after, _concluded_per_trial(before_calls, kpi), detail, now)


def _kpi_counts(calls: sqlite3.Row, kpi: str) -> stats.Counts:
    traffic, successes = KPI_COUNTERS[kpi]
    return stats.Counts(calls[successes], calls[traffic])


def _concluded_per_trial(calls: sqlite3.Row, kpi: str) -> float:
    """What one more success on this KPI is worth in concluded calls per dial."""
    numerator, denominator = ("connected", "dials") if kpi == "engagement_rate" else ("engaged", "connected")
    return calls[numerator] / calls[denominator] if calls[denominator] else 0.0


def _roll_back(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, applied: sqlite3.Row,
               before: stats.Counts, after: stats.Counts, concluded_per_trial: float, detail: dict, now: int) -> None:
    wf, kpi = workflow["workflow_id"], detail["kpi"]
    previous = json.loads(applied["body"])["diff"]["schedule"]["from"]["hour_weights"]
    margin = before.rate - after.rate - stats.difference_lower_bound(before, after, 0.975)
    return _submit(store, governance, workflow, Draft(
        workflow_id=wf, kind=ROLLBACK_KIND, composed=compose(sim.config(wf), set_hour_tables(previous)),
        change_summary=(f"Roll back the call-hour table: {kpi} fell from {before.rate:.1%} to {after.rate:.1%} "
                        f"in the {_WINDOW_DAYS} days after it (p = {detail['p_value']:.4f})."),
        rationale=(f"The primary KPI dropped significantly (one-sided alpha {stats.SCHEDULE_ROLLBACK_ALPHA}) against the {_WINDOW_DAYS} days before "
                   f"the table went in, so it goes back to the schedule that ran before. {_CONFOUNDING} Going back is the safe direction."),
        evidence={"detector": "schedule_before_after", "stage": "rollback", **detail},
        # What going back recovers, over the whole workflow, so the share below is 1.
        gain_lower_bound=stats.difference_lower_bound(before, after) * concluded_per_trial, affected_share=1.0,
        # The allocation replay can't see hours: going back is predicted to return the KPI to where it was.
        dry_run={"method": "before_after", "primary_kpi": kpi, "cohorts_at_risk": [],
                 "overall": {kpi: {"current": after.rate, "proposed": before.rate, "margin": margin}}}), now)
