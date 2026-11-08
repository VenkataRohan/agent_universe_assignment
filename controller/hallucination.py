"""Hallucination responder: detect -> candidates -> validate -> propose -> verify -> escalate (ADR-0014).

An incident:  open -> verifying -> verified
              open -> escalated (no candidate passed validation, or credits ran out)
              verifying -> escalated (the flag rate did not come back down)
              open | verifying -> resolved (someone else changed the prompt first)

Each validation is committed as soon as it comes back, so a restart spends nothing it has already spent.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from . import replay, stats
from .composer import Composed, compose, set_prompt, violations
from .credits import Credits
from .governance import APPLIED, IN_FLIGHT, Draft, GovernanceClient
from .simclient import SimClient, SimError
from .simtime import MINUTES_PER_DAY, label, minute_of
from .store import Store

KIND = "hallucination"
WINDOW_MINUTES = 120
ONSET_LOOKBACK_MINUTES = MINUTES_PER_DAY
PROMPT_CHANGE_LOOKBACK_MINUTES = MINUTES_PER_DAY
VALIDATION_RUN_SIZE = 300
OPEN, VERIFYING, VERIFIED, ESCALATED, RESOLVED = "open", "verifying", "verified", "escalated", "resolved"
KNOWN_GOOD, SUGGESTED = "known_good", "llm"


def respond(store: Store, sim: SimClient, credits: Credits, governance: GovernanceClient, workflow: Mapping, now: int) -> None:
    wf = workflow["workflow_id"]
    incident = store.live_incident(wf) or _detect(store, sim, workflow, now)
    if incident is None:
        return
    if incident["status"] == VERIFYING:
        _verify(store, wf, incident, now)
    else:
        _fix(store, sim, credits, governance, workflow, incident, now)


# -- detect ---------------------------------------------------------------------------------------
def _detect(store: Store, sim: SimClient, workflow: Mapping, now: int) -> sqlite3.Row | None:
    """One prompt at a time: calls on a prompt that has already gone can't be the live prompt's fault."""
    wf = workflow["workflow_id"]
    baseline = store.baseline_flag_rate(wf)
    windows = _windows(store.flag_ticks(wf, now - WINDOW_MINUTES, now))
    tested = {p: c for p, c in windows.items() if c.trials >= stats.HALLUCINATION_MIN_CONNECTS}
    if not tested:
        store.note_decision(now, wf, KIND, "too_few_connects", {})
        return None
    verdicts = {p: stats.hallucination_spike(c, baseline) for p, c in tested.items()}
    evidence = {"window_minutes": WINDOW_MINUTES, "baseline_flag_rate": baseline,
                "prompts": {p: {"connects": c.trials, "flags": c.successes, "p_value": verdicts[p].p_value,
                                "spike": verdicts[p].holds} for p, c in sorted(tested.items())}}
    if not any(v.holds for v in verdicts.values()):
        _decided(store, now, wf, "below_threshold", evidence)
        return None
    live = sim.config(wf)["config"]["prompt_version"]
    if not (live in verdicts and verdicts[live].holds):
        _decided(store, now, wf, "prompt_no_longer_live", {**evidence, "live_prompt": live})
        return None
    if store.escalated_on(wf, live):
        store.note_decision(now, wf, KIND, "already_escalated", {"prompt": live})
        return None
    return _raise(store, workflow, live, evidence, baseline, now)


def _windows(rows: list[sqlite3.Row]) -> dict[str, stats.Counts]:
    totals: dict[str, list[int]] = {}
    for r in rows:
        t = totals.setdefault(r["prompt_version"], [0, 0])
        t[0] += r["flags"]
        t[1] += r["connects"]
    return {p: stats.Counts(flags, connects) for p, (flags, connects) in totals.items()}


def _raise(store: Store, workflow: Mapping, live: str, evidence: dict, baseline: float, now: int) -> sqlite3.Row:
    wf = workflow["workflow_id"]
    cause = _cause(store, workflow, live, now)
    if cause["prompt_change"] is not None:
        onset = cause["prompt_change"]["minute"]
    else:
        ticks = [r for r in store.flag_ticks(wf, now - ONSET_LOOKBACK_MINUTES, now) if r["prompt_version"] == live]
        onset = ticks[stats.spike_onset([stats.Counts(r["flags"], r["connects"]) for r in ticks], baseline)]["minute"]
    with store.transaction() as conn:
        seq = Store.next_incident_seq(conn, wf)
        incident_id = f"{wf}-{KIND}-incident-{seq}"
        Store.insert_incident(conn, incident_id=incident_id, workflow_id=wf, seq=seq, status=OPEN, bad_prompt=live,
                              onset_minute=onset, detected_minute=now, detection=json.dumps(evidence, sort_keys=True),
                              cause=json.dumps(cause, sort_keys=True))
        Store.record_decision(conn, now, wf, KIND, "raised", None,
                              {**evidence, "incident_id": incident_id, "cause": cause, "onset_minute": onset})
    return store.incident(incident_id)


def _cause(store: Store, workflow: Mapping, live: str, now: int) -> dict:
    """The prompt change this incident followed, if it did, and the prompt to go back to.

    A change in the last day is taken to be the cause (ADR-0014). Flags are too sparse to date an onset to within
    a few hours, and wrongly blaming a change costs one validation run, which then has to pass.
    """
    wf = workflow["workflow_id"]
    changes = [e for e in store.config_changes_after(wf, 0) if "prompt_version" in e["changed_dims"]]
    minute = minute_of(changes[-1]["ts"], store.d0()) if changes else None
    if minute is None or now - minute > PROMPT_CHANGE_LOOKBACK_MINUTES:
        return {"prompt_change": None, "known_good": None}
    last = changes[-1]
    earlier = store.prompts_before(wf, minute)
    spoiled = store.prompts_with_incidents(wf) | {live}
    known_good = next((p for p in earlier if p not in spoiled and p in workflow["options"]["prompt_version"]), None)
    previous = next((p for p in earlier if p != live), None)
    return {"prompt_change": {"version": last["version"], "actor": last["actor"], "minute": minute, "from": previous, "to": live},
            "known_good": known_good}


# -- fix ------------------------------------------------------------------------------------------
def _fix(store: Store, sim: SimClient, credits: Credits, governance: GovernanceClient, workflow: Mapping,
         incident: sqlite3.Row, now: int) -> None:
    wf, incident_id = workflow["workflow_id"], incident["incident_id"]
    latest = store.latest_proposal(wf, KIND)
    mine = latest if latest is not None and json.loads(latest["body"])["evidence"].get("incident_id") == incident_id else None
    if mine is not None and mine["status"] in IN_FLIGHT:
        return _record_proposed(store, wf, incident, mine, now)
    if mine is not None and mine["status"] == APPLIED:
        return _start_verifying(store, wf, incident, mine, now)
    current = sim.config(wf)
    if current["config"]["prompt_version"] != incident["bad_prompt"]:
        return _end(store, wf, incident, RESOLVED, "prompt_changed_elsewhere", now, {"live_prompt": current["config"]["prompt_version"]})
    try:
        candidates = _validated_candidates(store, credits, workflow, incident, now)
    except SimError as e:
        if not e.out_of_credits:
            raise
        return _end(store, wf, incident, ESCALATED, "out_of_credits", now, {"error": str(e)})
    chosen = _best(candidates, workflow)
    if chosen is None:
        return _end(store, wf, incident, ESCALATED, "no_valid_fix", now, {"candidates": candidates})
    _propose(store, governance, workflow, store.incident(incident_id), compose(current, set_prompt(chosen["prompt"])), chosen, now)


def _validated_candidates(store: Store, credits: Credits, workflow: Mapping, incident: sqlite3.Row, now: int) -> list[dict]:
    """Every candidate that can be tried, validated. The known-good prompt alone, unless it fails or there is none."""
    wf, incident_id = workflow["workflow_id"], incident["incident_id"]
    candidates: list[dict] = json.loads(incident["candidates"])
    known_good = json.loads(incident["cause"])["known_good"]
    if known_good is not None and not candidates:
        candidates = [{"prompt": known_good, "source": KNOWN_GOOD}]
    candidates = _validate_all(store, credits, wf, incident_id, candidates, now)
    if _best(candidates, workflow) is not None or incident["suggested"]:
        return candidates
    candidates = _ask_llm(store, credits, workflow, incident, candidates, now)
    return _validate_all(store, credits, wf, incident_id, candidates, now)


def _validate_all(store: Store, credits: Credits, wf: str, incident_id: str, candidates: list[dict], now: int) -> list[dict]:
    out = list(candidates)
    for i, c in enumerate(out):
        if "validation_run_id" in c:
            continue
        run = credits.validate_prompt(wf, c["prompt"], VALIDATION_RUN_SIZE, incident=True)
        out[i] = {**c, "validation_run_id": run["validation_run_id"], "pass_rate": run["pass_rate"], "n": run["n"]}
        with store.transaction() as conn:
            Store.update_incident(conn, incident_id, candidates=json.dumps(out, sort_keys=True))
            Store.record_decision(conn, now, wf, KIND, "validated", None, {"incident_id": incident_id, **out[i]})
    return out


def _ask_llm(store: Store, credits: Credits, workflow: Mapping, incident: sqlite3.Row, candidates: list[dict], now: int) -> list[dict]:
    wf, incident_id = workflow["workflow_id"], incident["incident_id"]
    detection = json.loads(incident["detection"])["prompts"][incident["bad_prompt"]]
    problem = (f"Prompt {incident['bad_prompt']} is hallucinating: {detection['flags']} of {detection['connects']} connected "
               f"calls flagged in the last {WINDOW_MINUTES // 60} hours.")
    suggestions = credits.suggest_fixes(wf, problem, incident=True)["suggestions"]
    tried = {c["prompt"] for c in candidates}
    usable = set(workflow["options"]["prompt_version"]) - store.prompts_with_incidents(wf) - tried
    out = candidates + [{"prompt": s["prompt_version"], "source": SUGGESTED, "summary": s["summary"]}
                        for s in suggestions if s["prompt_version"] in usable]
    with store.transaction() as conn:
        Store.update_incident(conn, incident_id, candidates=json.dumps(out, sort_keys=True), suggested=1)
        Store.record_decision(conn, now, wf, KIND, "suggested", None, {"incident_id": incident_id, "suggestions": suggestions})
    return out


def _best(candidates: list[dict], workflow: Mapping) -> dict | None:
    """The candidate that passed with the highest pass rate. Listing order settles ties: known-good comes first."""
    floor = workflow["governance"]["min_validation_pass_rate"]
    passed = [c for c in candidates if c.get("pass_rate", -1.0) >= floor]
    return max(passed, key=lambda c: c["pass_rate"], default=None)


def _propose(store: Store, governance: GovernanceClient, workflow: Mapping, incident: sqlite3.Row, composed: Composed,
             chosen: dict, now: int) -> None:
    wf = workflow["workflow_id"]
    problems = violations(workflow, composed)
    if problems:
        return store.note_decision(now, wf, KIND, "blocked", {"incident_id": incident["incident_id"], "violations": problems})
    hold = governance.hold_reason(wf, KIND, now, incident=True, policy_limits_only=True)
    if hold is not None:
        return store.note_decision(now, wf, KIND, "held", {"incident_id": incident["incident_id"], "hold": hold})
    overlay_hold = governance.hold_reason(wf, KIND, now, incident=True)
    pid = governance.propose(_draft(store, workflow, incident, composed, chosen, overlay_hold, now), now)
    _record_proposed(store, wf, incident, store.proposal(pid), now)


def _record_proposed(store: Store, wf: str, incident: sqlite3.Row, proposal: sqlite3.Row, now: int) -> None:
    """Tie the incident to its fix. Also runs while the Proposal is in flight, so a crash just after recording it loses nothing."""
    body = json.loads(proposal["body"])
    prompt = body["proposed_config"]["prompt_version"]
    if incident["fix_prompt"] == prompt:
        return
    with store.transaction() as conn:
        Store.update_incident(conn, incident["incident_id"], fix_prompt=prompt)
        Store.record_decision(conn, now, wf, KIND, "proposed", proposal["proposal_id"],
                              {"incident_id": incident["incident_id"], "fix_prompt": prompt, "validation": body["evidence"]["validation"]})


def _draft(store: Store, workflow: Mapping, incident: sqlite3.Row, composed: Composed, chosen: dict, overlay_hold: str | None,
           now: int) -> Draft:
    wf = workflow["workflow_id"]
    detection = json.loads(incident["detection"])
    seen = detection["prompts"][incident["bad_prompt"]]
    change = json.loads(incident["cause"])["prompt_change"]
    rate, baseline = seen["flags"] / seen["connects"], detection["baseline_flag_rate"]
    observed = f"{rate:.1%} of connects flagged against a {baseline:.1%} baseline ({seen['flags']} of {seen['connects']} connects)"
    validated = f"{chosen['prompt']} passed validation at {chosen['pass_rate']:.1%} (n={chosen['n']})"
    reverts = chosen["source"] == KNOWN_GOOD and change is not None and change["actor"] == "supervisor"
    if reverts:
        summary = (f"Reverts the {label(change['minute'])} supervisor edit {change['from']} -> {change['to']} because {observed}. "
                   f"Back to known-good {validated}.")
    elif chosen["source"] == KNOWN_GOOD:
        summary = f"Return the prompt from {incident['bad_prompt']} to known-good {chosen['prompt']}: {observed}. {validated}."
    else:
        summary = f"Change the prompt from {incident['bad_prompt']} to {chosen['prompt']}: {observed}. {validated}."
    rationale = (f"The flag rate on {incident['bad_prompt']} is at least {stats.HALLUCINATION_MIN_RATIO}x the workflow's baseline over "
                 f"{WINDOW_MINUTES // 60} hours and {seen['connects']} connects (one-sided binomial p = {seen['p_value']:.2g}), from "
                 f"about {label(incident['onset_minute'])}.")
    if overlay_hold is not None:
        why = f" Sent under the policy's own limits instead of the client's preference ({overlay_hold}), because customer harm outranks it."
        summary, rationale = summary + why, rationale + why
    evidence = {"detector": "hallucination_spike", "incident_id": incident["incident_id"], "validation_run_id": chosen["validation_run_id"],
                "validation": {k: chosen[k] for k in ("prompt", "pass_rate", "n", "source")}, "detection": detection}
    dry_run = {**replay.dry_run(store, workflow, composed, now),
               "prompt": {"from": incident["bad_prompt"], "to": chosen["prompt"], "flag_rate_now": rate, "baseline_flag_rate": baseline,
                          "validation_pass_rate": chosen["pass_rate"],
                          "note": "A prompt change moves no traffic, so the replay above cannot predict its effect on KPIs."}}
    # The floor of 0.9 sets an incident's impact: hallucination's cost isn't a change in conclusion rate.
    return Draft(wf, KIND, composed, summary, rationale, evidence, gain_lower_bound=0.0, affected_share=1.0, incident=True,
                 dry_run=dry_run, names_supervisor_edit=reverts)


# -- verify ---------------------------------------------------------------------------------------
def _start_verifying(store: Store, wf: str, incident: sqlite3.Row, proposal: sqlite3.Row, now: int) -> None:
    applied = proposal["applied_minute"]
    prompt = json.loads(proposal["body"])["proposed_config"]["prompt_version"]
    with store.transaction() as conn:
        Store.update_incident(conn, incident["incident_id"], status=VERIFYING, fix_prompt=prompt, applied_minute=applied,
                              applied_version=proposal["applied_version"])
        Store.record_decision(conn, now, wf, KIND, "fix_applied", proposal["proposal_id"],
                              {"incident_id": incident["incident_id"], "minutes_onset_to_applied": applied - incident["onset_minute"]})


def _verify(store: Store, wf: str, incident: sqlite3.Row, now: int) -> None:
    prompt, applied = incident["fix_prompt"], incident["applied_minute"]
    if any("prompt_version" in e["changed_dims"] for e in store.config_changes_after(wf, incident["applied_version"])):
        return _end(store, wf, incident, RESOLVED, "prompt_changed_elsewhere", now, {})
    window = _windows([r for r in store.flag_ticks(wf, applied, now) if r["prompt_version"] == prompt]).get(prompt, stats.Counts(0, 0))
    if window.trials < stats.HALLUCINATION_VERIFY_CONNECTS:
        return store.note_decision(now, wf, KIND, "verifying", {"incident_id": incident["incident_id"]})
    verdict = stats.flag_rate_recovered(window, store.baseline_flag_rate(wf))
    detail = {"connects": window.trials, "flags": window.successes, "p_value": verdict.p_value}
    if verdict.holds:
        _end(store, wf, incident, VERIFIED, "flag_rate_recovered", now, detail)
    else:
        _end(store, wf, incident, ESCALATED, "not_recovered", now, detail)


# -- shared ---------------------------------------------------------------------------------------
def _end(store: Store, wf: str, incident: sqlite3.Row, status: str, reason: str, now: int, detail: dict) -> None:
    with store.transaction() as conn:
        Store.update_incident(conn, incident["incident_id"], status=status, end_minute=now, end_reason=reason)
        Store.record_decision(conn, now, wf, KIND, status, None, {"incident_id": incident["incident_id"], "reason": reason, **detail})


def _decided(store: Store, now: int, wf: str, outcome: str, detail: dict) -> None:
    with store.transaction() as conn:
        Store.record_decision(conn, now, wf, KIND, outcome, None, detail)
