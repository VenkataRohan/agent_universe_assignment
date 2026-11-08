"""Experiment engine: each experiment's state machine and its daily look (ADR-0005, 0006, 0007, 0008, 0010, 0011).

    planned -> screen_requested -> screening -> screened -> confirm_requested -> confirming
            -> confirmed -> promote_requested -> promoted
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
from .cohorts import FEATURES, KPI_COUNTERS
from .composer import Composed, compose, effective_dist, normalised, set_distribution, violations
from .governance import APPLIED, EXPIRED, IN_FLIGHT, STALE, Draft, GovernanceClient
from .simclient import SimClient
from .simtime import MINUTES_PER_DAY, label, minute_of
from .store import Store, experiment_view, load_partition

KIND = "experiment"
SCREEN_KIND = "experiment_screen"
CONFIRM_KIND = "experiment_confirm"
PROMOTE_KIND = "experiment_promote"
END_KIND = "experiment_end"
PLANNED, SCREEN_REQUESTED, SCREENING, SCREENED = "planned", "screen_requested", "screening", "screened"
CONFIRM_REQUESTED, CONFIRMING, CONFIRMED = "confirm_requested", "confirming", "confirmed"
PROMOTE_REQUESTED, PROMOTED = "promote_requested", "promoted"
INCONCLUSIVE, ABORTED = "inconclusive", "aborted"
RUNNING = (SCREENING, SCREENED, CONFIRM_REQUESTED, CONFIRMING, CONFIRMED, PROMOTE_REQUESTED)  # its split is live
ASKED_AGAIN = (EXPIRED, STALE)  # nobody said no to these, so the experiment goes back a step and asks afresh
WINNER, UNDECIDED, NO_WINNER = "winner", "undecided", "no_winner"  # what a confirm look can say of a dimension
BLOCKED_GUARDRAIL, BLOCKED_HARM, TOO_LITTLE_DATA = "blocked_guardrail", "blocked_harm", "insufficient_data"

# Which dimensions can move each primary KPI. Welcome and voice are heard after pickup (ADR-0007).
DIMENSIONS_FOR_KPI: Mapping[str, tuple[str, ...]] = {"engagement_rate": ("welcome_message", "voice")}
CONTROL_SHARE = 0.5
CONFIRM_SHARE = 0.5  # of the traffic on control, and so on the candidate
HOLDBACK_SHARE = 0.2  # stays on control after a promotion (ADR-0008)
MIN_SIZING_RATE, MAX_SIZING_RATE = 0.01, 0.90  # a rate outside these leaves no room to size a 10% lift
SUPERVISOR_QUIET_MINUTES = 7 * MINUTES_PER_DAY  # a supervisor's own choice is left to run this long
LOOK_MINUTE_OF_DAY = 9 * 60


def respond(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, now: int) -> None:
    wf = workflow["workflow_id"]
    dimensions = DIMENSIONS_FOR_KPI.get(workflow["objective"]["primary"], ())
    if not dimensions:
        return store.note_decision(now, wf, KIND, "not_applicable",
                                   {"why": "welcome message and voice can't move this workflow's primary KPI"})
    exp = store.latest_experiment(wf)
    if exp is not None:
        exp = _advance(store, sim, governance, workflow, exp, now)
        if exp["split_live"]:
            return _return_to_control(store, sim, governance, workflow, exp, now)
    if exp is None or exp["status"] == ABORTED:
        _start(store, sim, governance, workflow, dimensions, now)


def _advance(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    """Whatever this tick can do for a running experiment, in the order the states follow each other."""
    if exp["status"] in (PLANNED, SCREEN_REQUESTED):
        exp = _follow_request(store, exp, now)
    if exp["status"] == CONFIRM_REQUESTED:
        exp = _follow_confirm(store, exp, now)
    if exp["status"] == PROMOTE_REQUESTED:
        exp = _follow_promotion(store, exp, now)
    if exp["status"] in RUNNING:
        exp = _abort_if_edited(store, exp, now)
    if exp["status"] == SCREENING and _look_due(exp, now):
        exp = _look(store, exp, now)
    if exp["status"] == CONFIRMING and _look_due(exp, now):
        exp = _confirm_look(store, workflow, exp, now)
    if exp["status"] == SCREENED:
        exp = _request_confirm(store, sim, governance, workflow, exp, now)
    if exp["status"] == CONFIRMED:
        exp = _request_promotion(store, sim, governance, workflow, exp, now)
    return exp


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
def _stage_proposal(store: Store, exp: sqlite3.Row, kind: str) -> sqlite3.Row | None:
    p = store.latest_proposal(exp["workflow_id"], kind)
    return p if p is not None and json.loads(p["body"])["evidence"]["experiment_id"] == exp["experiment_id"] else None


def _follow_request(store: Store, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    p = _stage_proposal(store, exp, SCREEN_KIND)
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
    plans, first_version = _running_plans(exp, design)
    edit = next((e for e in store.config_changes_after(exp["workflow_id"], first_version)
                 if e["actor"] == "supervisor" and set(plans) & set(e["changed_dims"])), None)
    if edit is None:
        return exp
    minute = minute_of(edit["ts"], store.d0())
    # The whole hour of the edit goes too: counters are hourly, and a campaign locked to an earlier version
    # would otherwise carry calls dialled after the edit under a version still inside the experiment.
    rows = store.arm_counts(exp["workflow_id"], first_version, edit["version"] - 1, minute // 60, design["excluded_cohorts"])
    views = {dim: {"arms": _arm_view(_totals(rows, dim), plan)} for dim, plan in plans.items()}
    return _move(store, exp, ABORTED, now, "aborted",
                 {"why": f"a supervisor changed {', '.join(sorted(set(plans) & set(edit['changed_dims'])))} at {label(minute)}",
                  "edit": {"version": edit["version"], "at": label(minute)}, "dimensions": views},
                 end_minute=minute, end_reason="supervisor_edit", split_live=1, candidates=None)


def _running_plans(exp: sqlite3.Row, design: Mapping) -> tuple[dict, int]:
    """The dimensions still carrying the split, and the config version their data starts at."""
    if exp["confirm_version"] is None:
        return design["dimensions"], exp["screen_version"]
    confirming = json.loads(exp["confirm_plan"])
    return {d: {**design["dimensions"][d], "challengers": [c["candidate"]]} for d, c in confirming.items()}, exp["confirm_version"]


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


# -- the confirm stage ------------------------------------------------------------------------------
def _request_confirm(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    """Ask for each candidate against control at 50/50. Dimensions with no candidate go back to what ran before."""
    wf = workflow["workflow_id"]
    sent = _stage_proposal(store, exp, CONFIRM_KIND)
    if sent is not None and sent["status"] not in ASKED_AGAIN:  # a crash between proposing and recording it
        return _move(store, exp, CONFIRM_REQUESTED, now, "confirm_requested", {"proposal_id": sent["proposal_id"]})
    hold = governance.hold_reason(wf, CONFIRM_KIND, now)
    if hold is not None:
        store.note_decision(now, wf, KIND, "confirm_held", {"experiment_id": exp["experiment_id"], "hold": hold})
        return exp
    design, candidates = json.loads(exp["design"]), json.loads(exp["candidates"])
    plan = _confirm_plan(store, workflow, exp, design, candidates)
    composed = compose(sim.config(wf), *(set_distribution(d, plan[d]["split"] if d in plan else p["before"])
                                         for d, p in design["dimensions"].items()))
    problems = violations(workflow, composed)
    if problems:
        store.note_decision(now, wf, KIND, "confirm_blocked", {"experiment_id": exp["experiment_id"], "violations": problems})
        return exp
    pid = governance.propose(_confirm_draft(workflow, exp, plan, composed), now)
    return _move(store, exp, CONFIRM_REQUESTED, now, "confirm_requested", {"proposal_id": pid})


def _confirm_plan(store: Store, workflow: Mapping, exp: sqlite3.Row, design: Mapping, candidates: Mapping[str, str]) -> dict:
    """Per candidate: its split, the sample it needs, and what the screen measured for it."""
    rows = store.arm_cell_counts(workflow["workflow_id"], exp["screen_version"], design["excluded_cohorts"])
    plan = {}
    for dim, candidate in candidates.items():
        dimension = design["dimensions"][dim]
        control = dimension["control"]
        arms = _arm_totals(rows, dim, (control, candidate))
        rate = arms[control]["engaged"] / arms[control]["connected"] if arms[control]["connected"] else MIN_SIZING_RATE
        moved = max(CONFIRM_SHARE - dimension["split"][candidate] / sum(dimension["split"].values()), 0.0)
        plan[dim] = {"control": control, "candidate": candidate, "split": {control: CONFIRM_SHARE, candidate: CONFIRM_SHARE},
                     "baseline_rate": rate, "arms": arms, "moved_share": moved,
                     "planned_per_arm": stats.confirm_sample_size_per_arm(min(max(rate, MIN_SIZING_RATE), MAX_SIZING_RATE)),
                     "gain_lower_bound": _conclusion_gain(rows, dim, candidate, control, workflow["objective"]["primary"])}
    return plan


def _confirm_draft(workflow: Mapping, exp: sqlite3.Row, plan: Mapping, composed: Composed) -> Draft:
    arms = "; ".join(f"{dim} {c['candidate']} against {c['control']} ({c['planned_per_arm']:,} connected calls an arm planned)"
                     for dim, c in sorted(plan.items()))
    return Draft(
        workflow_id=workflow["workflow_id"], kind=CONFIRM_KIND, composed=composed,
        change_summary=f"Confirm at 50/50 on fresh data: {arms}.",
        rationale=("The screen chose each candidate, and a screen's best arm can flatter itself, so nothing is decided on its "
                   f"data. Only calls dialled under this config count. Up to {stats.CONFIRM_MAX_LOOKS} daily looks each test at "
                   f"alpha {stats.CONFIRM_ALPHA_PER_LOOK:.4f}, with a guardrail and a harm check on every cohort. The impact score "
                   "uses the screen's lower bound, which that same selection can flatter."),
        evidence={"experiment_id": exp["experiment_id"], "stage": "confirm", "confirm": plan},
        # Dimensions move different shares of traffic, so each is scaled by its own here and the share below is 1.
        gain_lower_bound=sum(c["gain_lower_bound"] * c["moved_share"] for c in plan.values()), affected_share=1.0)


def _follow_confirm(store: Store, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    p = _stage_proposal(store, exp, CONFIRM_KIND)
    assert p is not None  # the move to confirm_requested is made after the Proposal exists
    if p["status"] == APPLIED:
        body = json.loads(p["body"])
        return _move(store, exp, CONFIRMING, now, "confirming", {"proposal_id": p["proposal_id"]}, confirm_version=p["applied_version"],
                     confirm_minute=p["applied_minute"], last_look_day=p["applied_minute"] // MINUTES_PER_DAY,
                     confirm_plan=json.dumps(body["evidence"]["confirm"], sort_keys=True))
    return exp if p["status"] in IN_FLIGHT else _proposal_ended(store, exp, p, now, SCREENED)


def _proposal_ended(store: Store, exp: sqlite3.Row, p: sqlite3.Row, now: int, retry_from: str) -> sqlite3.Row:
    """Expired or overtaken, nobody said no, so it is rebuilt from fresh data. A refusal ends the experiment."""
    detail = {"proposal_id": p["proposal_id"], "ended": p["status"], "reasons": json.loads(p["reasons"]) if p["reasons"] else []}
    if p["status"] in ASKED_AGAIN:
        return _move(store, exp, retry_from, now, "proposal_retry", detail)
    return _move(store, exp, ABORTED, now, "aborted", {**detail, "why": f"the Proposal {p['proposal_id']} ended {p['status']}"},
                 end_minute=now, end_reason=f"request_{p['status']}", split_live=1)


def _confirm_look(store: Store, workflow: Mapping, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    """Judge every undecided dimension on calls dialled under the confirm config. Only a full look uses up one of the three."""
    design, plan = json.loads(exp["design"]), json.loads(exp["confirm_plan"])
    settled: dict[str, dict] = json.loads(exp["verdicts"]) if exp["verdicts"] else {}
    rows = store.arm_cell_counts(exp["workflow_id"], exp["confirm_version"], design["excluded_cohorts"])
    undecided = {d: c for d, c in plan.items() if d not in settled}
    arms = {d: _arm_totals(rows, d, (c["control"], c["candidate"])) for d, c in undecided.items()}
    day = now // MINUTES_PER_DAY
    if any(a["connected"] * 2 < undecided[d]["planned_per_arm"] for d in undecided for a in arms[d].values()):
        if now - exp["confirm_minute"] < stats.CONFIRM_MAX_DAYS * MINUTES_PER_DAY:
            return _move(store, exp, CONFIRMING, now, "waiting_for_data", {"dimensions": {d: {"arms": a} for d, a in arms.items()}}, last_look_day=day)
        short = {d: {"status": TOO_LITTLE_DATA, "arms": a} for d, a in arms.items()}
        return _conclude_confirm(store, exp, now, {**settled, **short}, exp["confirm_looks"], day)
    looks = exp["confirm_looks"] + 1
    last = looks >= stats.CONFIRM_MAX_LOOKS
    partition = load_partition(store.conn, exp["workflow_id"])
    assert partition is not None  # bootstrapped from history before the loop decides anything
    by_cohort: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_cohort.setdefault(partition.cohort_of({f: r[f] for f in FEATURES}), []).append(r)
    judged = {d: _judge(rows, by_cohort, workflow, d, c["candidate"], c["control"], arms[d]) for d, c in undecided.items()}
    for verdict in judged.values():
        verdict["status"] = _look_status(verdict, last)
    settled.update({d: v for d, v in judged.items() if v["status"] != UNDECIDED})
    harmed = any(v["status"] == BLOCKED_HARM for v in judged.values())
    if len(settled) < len(plan) and not harmed:
        return _move(store, exp, CONFIRMING, now, "confirm_look", {"dimensions": judged, "look": looks}, last_look_day=day,
                     confirm_looks=looks, verdicts=json.dumps(settled, sort_keys=True))
    # A cohort is being harmed, so the 50/50 stops today, for any dimension that hadn't been settled too.
    settled.update({d: {**v, "status": NO_WINNER} for d, v in judged.items() if v["status"] == UNDECIDED})
    return _conclude_confirm(store, exp, now, settled, looks, day)


def _judge(rows: list[sqlite3.Row], by_cohort: Mapping[str, list[sqlite3.Row]], workflow: Mapping, dim: str, candidate: str, control: str,
           arms: Mapping) -> dict:
    """The primary test, the guardrail and the harm check, each with the figures behind it."""
    objective = workflow["objective"]
    kpi = objective["primary"]
    guardrail_key, max_drop = next(iter(objective["guardrail"].items()))
    guardrail_kpi = guardrail_key.removesuffix("_max_drop")
    primary = stats.beats_control_strata(_strata(rows, dim, candidate, control, kpi))
    guardrail = stats.guardrail_non_inferior_strata(_strata(rows, dim, candidate, control, guardrail_kpi), max_drop)
    harm = stats.cohort_harm_strata({c: _strata(cells, dim, candidate, control, kpi) for c, cells in by_cohort.items()})
    return {"control": control, "candidate": candidate, "arms": arms,
            "primary": {"kpi": kpi, "passed": primary.holds, "p_value": primary.p_value, "alpha": stats.CONFIRM_ALPHA_PER_LOOK},
            "guardrail": {"kpi": guardrail_kpi, "passed": guardrail.passed, "lower_bound": guardrail.lower_bound, "max_drop": max_drop},
            "harm": {"harmed_cohorts": sorted(c for c, v in harm.items() if v.holds), "p_values": {c: v.p_value for c, v in harm.items()}},
            "gain_lower_bound": _conclusion_gain(rows, dim, candidate, control, kpi),
            "return_gain_lower_bound": _conclusion_gain(rows, dim, control, candidate, kpi)}


def _look_status(verdict: Mapping, last: bool) -> str:
    """A guardrail that can't yet rule out the drop may just need more calls; only the last look is final for it."""
    primary, guardrail = verdict["primary"]["passed"], verdict["guardrail"]["passed"]
    if verdict["harm"]["harmed_cohorts"]:
        return BLOCKED_HARM
    if primary and guardrail:
        return WINNER
    if not last:
        return UNDECIDED
    return BLOCKED_GUARDRAIL if primary else NO_WINNER


def _conclude_confirm(store: Store, exp: sqlite3.Row, now: int, settled: Mapping[str, dict], looks: int, day: int) -> sqlite3.Row:
    """Winners go on to promotion; with none, the experiment ends and asks to go back to control."""
    statuses = [v["status"] for v in settled.values()]
    why = "; ".join(_why_not(dim, v) for dim, v in sorted(settled.items()) if v["status"] != WINNER)
    detail = {"dimensions": settled, "look": looks, **({"why": why} if why else {})}
    fields: dict[str, object] = {"last_look_day": day, "confirm_looks": looks, "verdicts": json.dumps(settled, sort_keys=True)}
    if WINNER in statuses:
        return _move(store, exp, CONFIRMED, now, "winner", detail, **fields)
    blocked = BLOCKED_HARM in statuses or BLOCKED_GUARDRAIL in statuses
    reason = next((s for s in (BLOCKED_HARM, BLOCKED_GUARDRAIL, TOO_LITTLE_DATA) if s in statuses), NO_WINNER)
    return _move(store, exp, INCONCLUSIVE, now, "blocked" if blocked else "inconclusive", detail, end_minute=now, end_reason=reason, split_live=1, **fields)


def _why_not(dim: str, verdict: Mapping) -> str:
    status = verdict["status"]
    if status == TOO_LITTLE_DATA:
        return f"{dim}: too little traffic for half the planned sample"
    candidate = f"{dim} {verdict['candidate']}"
    if status == BLOCKED_HARM:
        return f"{candidate} is significantly worse for {', '.join(verdict['harm']['harmed_cohorts'])}"
    if status == BLOCKED_GUARDRAIL:
        g = verdict["guardrail"]
        return f"{candidate}: {g['kpi']} lower bound {g['lower_bound']:+.3f} is below the allowed drop of {-g['max_drop']:+.3f}"
    return f"{candidate} did not beat control before the confirm ended"


# -- promotion --------------------------------------------------------------------------------------
def _request_promotion(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    """Winners take most of the traffic and control keeps the holdback. A candidate that didn't win goes back in the same Proposal."""
    wf = workflow["workflow_id"]
    sent = _stage_proposal(store, exp, PROMOTE_KIND)
    if sent is not None and sent["status"] not in ASKED_AGAIN:  # a crash between proposing and recording it
        return _move(store, exp, PROMOTE_REQUESTED, now, "promote_requested", {"proposal_id": sent["proposal_id"]})
    hold = governance.hold_reason(wf, PROMOTE_KIND, now)
    if hold is not None:
        store.note_decision(now, wf, KIND, "promotion_held", {"experiment_id": exp["experiment_id"], "hold": hold})
        return exp
    design, settled = json.loads(exp["design"]), json.loads(exp["verdicts"])
    winners = {d: v for d, v in settled.items() if v["status"] == WINNER}
    tables = {d: ({v["candidate"]: 1 - HOLDBACK_SHARE, v["control"]: HOLDBACK_SHARE} if d in winners else design["dimensions"][d]["before"])
              for d, v in settled.items()}
    composed = compose(sim.config(wf), *(set_distribution(d, t) for d, t in tables.items()))
    problems = violations(workflow, composed)
    if problems:
        store.note_decision(now, wf, KIND, "promotion_blocked", {"experiment_id": exp["experiment_id"], "violations": problems})
        return exp
    pid = governance.propose(_promotion_draft(workflow, exp, winners, composed), now)
    return _move(store, exp, PROMOTE_REQUESTED, now, "promote_requested", {"proposal_id": pid})


def _promotion_draft(workflow: Mapping, exp: sqlite3.Row, winners: Mapping[str, dict], composed: Composed) -> Draft:
    moved = 1 - HOLDBACK_SHARE - CONFIRM_SHARE
    named = "; ".join(f"{dim} {v['candidate']} over {v['control']} (p = {v['primary']['p_value']:.4f})" for dim, v in sorted(winners.items()))
    return Draft(
        workflow_id=workflow["workflow_id"], kind=PROMOTE_KIND, composed=composed,
        change_summary=f"Promote {named}, keeping {HOLDBACK_SHARE:.0%} of traffic on control as a holdback.",
        rationale=("Each winner beat control on fresh data at the per-look alpha, passed the guardrail and harmed no cohort. Control "
                   "keeps a holdback so that \"is it still winning?\" stays a randomised comparison."),
        evidence={"experiment_id": exp["experiment_id"], "stage": "promotion", "holdback_share": HOLDBACK_SHARE, "winners": winners},
        # Each winner's own bound x the traffic it gains, so the share below is 1.
        gain_lower_bound=sum(v["gain_lower_bound"] * moved for v in winners.values()), affected_share=1.0)


def _follow_promotion(store: Store, exp: sqlite3.Row, now: int) -> sqlite3.Row:
    p = _stage_proposal(store, exp, PROMOTE_KIND)
    assert p is not None  # the move to promote_requested is made after the Proposal exists
    if p["status"] == APPLIED:
        return _move(store, exp, PROMOTED, now, "promoted", {"proposal_id": p["proposal_id"], "version": p["applied_version"]})
    return exp if p["status"] in IN_FLIGHT else _proposal_ended(store, exp, p, now, CONFIRMED)


def _arm_totals(rows: Iterable[sqlite3.Row], dim: str, options: Iterable[str]) -> dict[str, dict[str, int]]:
    out = {o: {"dials": 0, "connected": 0, "engaged": 0} for o in options}
    for r in rows:
        if r[dim] in out:
            for k in out[r[dim]]:
                out[r[dim]][k] += r[k]
    return out


def _strata(rows: Iterable[sqlite3.Row], dim: str, candidate: str, control: str, kpi: str) -> list[tuple[stats.Counts, stats.Counts]]:
    """(candidate, control) on this KPI, per config version."""
    trials, successes = KPI_COUNTERS[kpi]
    versions: dict[int, dict[str, stats.Counts]] = {}
    for r in rows:
        if r[dim] in (candidate, control):
            arms = versions.setdefault(r["config_version"], {})
            c = arms.get(r[dim], stats.Counts(0, 0))
            arms[r[dim]] = stats.Counts(c.successes + r[successes], c.trials + r[trials])
    empty = stats.Counts(0, 0)
    return [(arms.get(candidate, empty), arms.get(control, empty)) for _, arms in sorted(versions.items())]


def _conclusion_gain(rows: list[sqlite3.Row], dim: str, candidate: str, control: str, kpi: str) -> float:
    """80% lower bound on the KPI gain, as concluded calls per dial: a lift per connect is worth the pickup rate."""
    arms = _arm_totals(rows, dim, (candidate, control))
    dials = sum(a["dials"] for a in arms.values())
    pickup = sum(a["connected"] for a in arms.values()) / dials if dials else 0.0
    return stats.difference_lower_bound_strata(_strata(rows, dim, candidate, control, kpi)) * pickup


# -- returning to control --------------------------------------------------------------------------
def _return_to_control(store: Store, sim: SimClient, governance: GovernanceClient, workflow: Mapping, exp: sqlite3.Row, now: int) -> None:
    """Ask for the dimensions still carrying this experiment's split back. Anything else is left alone."""
    wf, experiment_id, status = workflow["workflow_id"], exp["experiment_id"], exp["status"]
    current = sim.config(wf)
    plans = json.loads(exp["design"])["dimensions"]
    confirming = json.loads(exp["confirm_plan"]) if exp["confirm_plan"] else {}
    splits = {d: [normalised(plan["split"]), normalised(confirming.get(d, {}).get("split"))] for d, plan in plans.items()}
    live = sorted(d for d in plans if effective_dist(current["config"], "default", d) in splits[d])
    if not live:
        _move(store, exp, status, now, "split_removed", {}, split_live=0)
        return None
    hold = governance.hold_reason(wf, END_KIND, now)
    if hold is not None:
        return store.note_decision(now, wf, KIND, "return_held", {"experiment_id": experiment_id, "hold": hold})
    composed = compose(current, *(set_distribution(d, plans[d]["before"]) for d in live))
    verdicts = json.loads(exp["verdicts"]) if exp["verdicts"] else {}
    problems = violations(workflow, composed)
    if problems:
        return store.note_decision(now, wf, KIND, "return_blocked", {"experiment_id": experiment_id, "violations": problems})
    governance.propose(Draft(
        workflow_id=wf, kind=END_KIND, composed=composed,
        change_summary=f"End experiment {experiment_id}: return {' and '.join(live)} to what ran before it ({exp['end_reason']}).",
        rationale="The experiment has ended without anything to carry forward, so nobody should keep hearing its challengers.",
        evidence={"experiment_id": experiment_id, "stage": "return_to_control", "end_reason": exp["end_reason"], "dimensions": live},
        # What going back recovers, where a confirm measured it: a candidate that merely tied claims nothing.
        gain_lower_bound=sum(verdicts.get(d, {}).get("return_gain_lower_bound", 0.0) for d in live),
        affected_share=1 - CONTROL_SHARE), now)
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
