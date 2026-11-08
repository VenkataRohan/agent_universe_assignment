"""Governance client: owns the Proposal state machine and its timing and batching rules.

Approval route:  submitting -> pending -> approved -> applying -> applied
                 pending -> rejected | expired       approved/applying -> lapsed
Auto route:      applying -> applied | refused (the platform said no, so it falls back to approval)
Either route:    any in-flight state -> stale (the base changed underneath it)

Crash safety comes from one rule: a state is committed before the action it announces. A row in
`submitting` may or may not have reached the platform, and a row in `applying` may or may not have
been applied, so neither is ever acted on without first asking the platform which it was. On
startup and on every tick, `reconcile` does that asking.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from . import replay, stats
from .composer import Composed, describe_change, largest_shift
from .registry import OVERLAYS, EffectivePolicy, Overlay, effective_policy
from .simclient import SimClient, SimError
from .simtime import MINUTES_PER_DAY, label, minute_of
from .store import Store

log = logging.getLogger("controller")

SUBMITTING, PENDING, APPROVED, APPLYING, APPLIED = "submitting", "pending", "approved", "applying", "applied"
REJECTED, EXPIRED, LAPSED, STALE, REFUSED = "rejected", "expired", "lapsed", "stale", "refused"
IN_FLIGHT = (SUBMITTING, PENDING, APPROVED, APPLYING)
# A successor says what became of the Proposal it replaces.
REBUILT_FROM = {REJECTED: "was rejected", EXPIRED: "expired", LAPSED: "lapsed", STALE: "went stale",
                REFUSED: "was refused auto-apply"}
APPROVAL_ROUTE, AUTO_ROUTE = "approval", "auto"
INCIDENT_LABEL = "[Incident response] "

WINDOW_24H = MINUTES_PER_DAY
# A human said no, or the platform refused the request as invalid: asking again at once can't help.
# An expired Proposal has no cooldown: nobody said no, it just wasn't looked at in time.
REJECTION_COOLDOWN_MINUTES = MINUTES_PER_DAY
PROPOSAL_DECISION = "proposal"


@dataclass(frozen=True)
class Draft:
    """What the deciding code knows. `propose` fills in the affected leads, dry run and impact score."""
    workflow_id: str
    kind: str
    composed: Composed
    change_summary: str
    rationale: str
    evidence: dict
    gain_lower_bound: float  # conservative gain in conclusion rate on the traffic this moves
    affected_share: float    # the share of the workflow's traffic this moves
    incident: bool = False


class GovernanceClient:
    def __init__(self, store: Store, sim: SimClient, workflows: list[dict],
                 overlays: Mapping[str, Overlay] = OVERLAYS):
        self._store, self._sim, self._overlays = store, sim, overlays
        self._workflows = {w["workflow_id"]: w for w in workflows}

    def _policy(self, workflow_id: str) -> EffectivePolicy:
        return effective_policy(self._workflows[workflow_id], self._overlays)

    def max_shift(self, workflow_id: str) -> float:
        return self._policy(workflow_id).max_shift

    # -- may we change it ourselves? -----------------------------------------------------------
    def auto_apply_refusal(self, workflow_id: str, kind: str, composed: Composed, now: int,
                           incident: bool = False) -> str | None:
        """Why this change must go through approval instead, or None if it may be auto-applied."""
        policy = self._policy(workflow_id)
        permitted = policy.incident_auto_apply_dims if incident else policy.auto_apply_dims
        outside = sorted(set(composed.changed_dims) - permitted)
        if outside:
            return f"auto-apply does not cover {', '.join(outside)} on this workflow"
        shift = largest_shift(composed)
        if shift > policy.max_shift:
            return f"it moves {shift:.0%} of a cohort's traffic and auto-apply allows at most {policy.max_shift:.0%}"
        if self._store.auto_refused_since(workflow_id, kind, now - WINDOW_24H):
            return "the platform refused an auto-apply of this kind in the last 24 hours"
        return None

    # -- may we ask? ---------------------------------------------------------------------------
    def hold_reason(self, workflow_id: str, kind: str, now: int, incident: bool = False, auto: bool = False) -> str | None:
        """Why a new Proposal can't go out at this minute, or None if it can.

        Incident fixes wait only for their own kind. An auto-apply asks no approver, so the approver's hours,
        request cap and cooldown don't apply to it.
        """
        in_flight = self._store.open_proposals(workflow_id, IN_FLIGHT)
        if any(p["kind"] == kind for p in in_flight) if incident else in_flight:
            return "a Proposal is already in flight on this workflow"
        if auto:
            return None
        policy = self._policy(workflow_id)
        if not policy.may_request_at(now):
            return (f"outside request hours ({policy.approver_start_hour}:00 to "
                    f"{policy.approver_end_hour}:00, less the last 2 hours)")
        sent = self._store.requests_since(workflow_id, now - WINDOW_24H)
        if sent >= policy.max_requests_per_24h:
            return f"{sent} of {policy.max_requests_per_24h} allowed requests sent in the last 24 hours"
        last = self._store.latest_proposal(workflow_id, kind)
        if last is not None and last["status"] == REJECTED and now - last["decided_minute"] < REJECTION_COOLDOWN_MINUTES:
            return f"{last['proposal_id']} was rejected less than 24 hours ago"
        return None

    # -- proposing -----------------------------------------------------------------------------
    def propose(self, draft: Draft, now: int) -> str:
        """Commit the Proposal, then send it to an approver. Returns its ID."""
        pid = self._record(draft, now, APPROVAL_ROUTE, SUBMITTING, now)
        self._send(self._store.proposal(pid), now)
        return pid

    def auto_apply(self, draft: Draft, now: int) -> str:
        """Commit the Proposal, then apply it. If the platform refuses, ask an approver instead. Returns the last ID."""
        pid = self._record(draft, now, AUTO_ROUTE, APPLYING, None)
        self._put_config(self._store.proposal(pid), draft.composed.base_version, now)
        if self._store.proposal(pid)["status"] == REFUSED and self.hold_reason(draft.workflow_id, draft.kind, now, draft.incident) is None:
            return self.propose(draft, now)
        return pid

    def _record(self, draft: Draft, now: int, route: str, status: str, sent_minute: int | None) -> str:
        wf, kind, composed = draft.workflow_id, draft.kind, draft.composed
        workflow = self._workflows[wf]
        affected_leads = replay.affected_leads(self._store, workflow, composed, now)
        dry_run = replay.dry_run(self._store, workflow, composed, now)
        impact = stats.impact_score(draft.gain_lower_bound, draft.affected_share, draft.incident)
        note = self._supervisor_edit_note(wf, composed)
        rationale = f"{draft.rationale} {note}" if note else draft.rationale
        change_summary = (INCIDENT_LABEL if draft.incident else "") + (f"{draft.change_summary} {note}" if note else draft.change_summary)
        with self._store.transaction() as conn:
            seq = Store.next_proposal_seq(conn, wf, kind)
            pid = f"{wf}-{kind}-{seq}"
            prev = self._store.latest_proposal(wf, kind)
            replaces = prev["proposal_id"] if prev is not None and prev["status"] in REBUILT_FROM else None
            summary = change_summary + (f" Replaces {replaces}, which {REBUILT_FROM[prev['status']]}." if replaces and prev else "")
            body = {"proposal_id": pid, "workflow_id": wf, "base_version": composed.base_version,
                    "proposed_config": composed.config, "change_summary": summary, "diff": composed.diff,
                    "affected_leads": affected_leads, "dry_run": dry_run, "impact_score": impact,
                    "rationale": rationale, "evidence": draft.evidence}
            Store.insert_proposal(conn, proposal_id=pid, workflow_id=wf, kind=kind, seq=seq, route=route,
                                  incident=int(draft.incident), status=status, base_version=composed.base_version,
                                  body=json.dumps(body, sort_keys=True), replaces=replaces, created_minute=now,
                                  sent_minute=sent_minute)
            Store.record_decision(conn, now, wf, PROPOSAL_DECISION, "recorded", pid,
                                  {"changed_dims": list(composed.changed_dims), "summary": summary, "route": route})
        return pid

    def _supervisor_edit_note(self, workflow_id: str, composed: Composed) -> str:
        """Say so when this changes a dimension a supervisor set last: nothing is overwritten silently."""
        last: dict[str, dict] = {}
        for entry in self._sim.config_history(workflow_id):
            for dim in entry["changed_dims"]:
                last[dim] = entry
        edited = sorted(d for d in composed.changed_dims if d in last and last[d]["actor"] == "supervisor")
        d0 = self._store.d0()
        return " ".join(f"Revises the {label(minute_of(last[d]['applied_at'], d0))} supervisor edit "
                        f"(version {last[d]['version']}, {d}): {describe_change(d, composed.diff[d])}." for d in edited)

    def _send(self, p: sqlite3.Row, now: int) -> None:
        try:
            approval = self._sim.request_approval(json.loads(p["body"]))
        except SimError as e:
            if e.code == "duplicate_proposal_id":  # an earlier attempt got through
                sent = {a["proposal_id"]: a for a in self._sim.approvals(p["workflow_id"])}.get(p["proposal_id"])
                if sent is not None:
                    self._mark_pending(p, sent, now, "duplicate_reconciled")
            elif e.code == "stale_base_version":
                self._abandon(p, now, "the config changed before this was sent")
            elif e.retryable:  # may or may not have arrived; reconcile will find out
                self._note(p, now, "send_failed", {"error": str(e)})
            else:
                self._transition(p, REJECTED, now, "refused", {"error": str(e)}, sent_minute=None, decided_minute=now,
                         reasons=json.dumps([e.code]))
            return
        self._mark_pending(p, approval, now, "sent")

    def _mark_pending(self, p: sqlite3.Row, approval: Mapping, now: int, outcome: str) -> sqlite3.Row:
        # The platform's own timestamp: what its request cap counts, however many attempts this took.
        return self._transition(p, PENDING, now, outcome, {"approval_id": approval["approval_id"]},
                                approval_id=approval["approval_id"], expires_at=approval["expires_at"],
                                sent_minute=minute_of(approval["created_at"], self._store.d0()))

    def _abandon(self, p: sqlite3.Row, now: int, why: str) -> None:
        """It never reached the platform, so it doesn't count against the request cap."""
        self._transition(p, STALE, now, "abandoned", {"why": why}, sent_minute=None, reasons=json.dumps([why]))

    # -- reconciling ---------------------------------------------------------------------------
    def reconcile(self, workflow_id: str, now: int) -> None:
        """Bring every in-flight Proposal of this workflow into line with the platform."""
        rows = self._store.open_proposals(workflow_id, IN_FLIGHT)
        if not rows:
            return
        approvals = {a["proposal_id"]: a for a in self._sim.approvals(workflow_id)}
        version = self._sim.config(workflow_id)["version"]
        for p in rows:
            if p["route"] == AUTO_ROUTE:
                self._put_config(p, version, now)
            else:
                self._reconcile_one(p, approvals.get(p["proposal_id"]), version, now)

    def _reconcile_one(self, p: sqlite3.Row, approval: Mapping | None, version: int, now: int) -> None:
        if approval is None:
            if p["status"] == SUBMITTING:
                self._send_or_abandon(p, version, now)
            else:
                self._transition(p, STALE, now, "lost", {"why": "the platform has no record of this request"},
                         reasons=json.dumps(["the platform has no record of this request"]))
            return
        if p["status"] == SUBMITTING:
            p = self._mark_pending(p, approval, now, "reconciled_as_sent")
        world = approval["status"]
        if world in (REJECTED, EXPIRED):
            self._transition(p, world, now, world, {"reasons": approval.get("reasons", [])}, decided_minute=now,
                     reasons=json.dumps(approval.get("reasons", [])))
        elif world == LAPSED:
            self._transition(p, LAPSED, now, LAPSED, {"apply_by": approval.get("apply_by")}, decided_minute=now)
        elif world == PENDING:
            if version != p["base_version"]:
                self._mark_stale(p, version, now)
        else:  # approved, or already applied by an earlier life of this process
            if p["status"] == PENDING:
                p = self._transition(p, APPROVED, now, APPROVED, {"approval_id": p["approval_id"]}, decided_minute=now,
                             apply_by=approval.get("apply_by"))
            self._apply(p, version, now)

    def _send_or_abandon(self, p: sqlite3.Row, version: int, now: int) -> None:
        """A recorded Proposal the platform never received. It goes out only if it could go out now."""
        policy = self._policy(p["workflow_id"])
        others = self._store.requests_since(p["workflow_id"], now - WINDOW_24H, excluding=p["proposal_id"])
        if version != p["base_version"]:
            self._abandon(p, now, "the config changed before this was sent")
        elif not policy.may_request_at(now):
            self._abandon(p, now, "request hours ended before this was sent")
        elif others >= policy.max_requests_per_24h:
            self._abandon(p, now, "the request cap was reached before this was sent")
        else:
            self._send(p, now)

    def _mark_stale(self, p: sqlite3.Row, version: int, now: int) -> None:
        why = f"the config moved from version {p['base_version']} to {version} underneath this Proposal"
        self._transition(p, STALE, now, STALE, {"why": why, "current_version": version}, reasons=json.dumps([why]))

    # -- applying ------------------------------------------------------------------------------
    def _apply(self, p: sqlite3.Row, version: int, now: int) -> None:
        if p["status"] == APPROVED:
            p = self._transition(p, APPLYING, now, "apply_intent", {"approval_id": p["approval_id"]})
        wf = p["workflow_id"]
        done = next((h for h in self._sim.config_history(wf) if h["approval_id"] == p["approval_id"]), None)
        if done is not None:
            self._transition(p, APPLIED, now, "confirmed_applied", {"version": done["version"]},
                     applied_minute=now, applied_version=done["version"])
            return
        if version != p["base_version"]:
            self._mark_stale(p, version, now)
            return
        try:
            result = self._sim.apply_approval(wf, p["approval_id"])
        except SimError as e:
            if e.code == "stale_base_version":
                self._mark_stale(p, version, now)
            elif e.status == 410:
                self._transition(p, LAPSED, now, LAPSED, {"error": str(e)})
            elif e.retryable:  # stays `applying`: the next tick looks in config history before trying again
                log.warning("apply of %s failed: %s", p["proposal_id"], e)
                self._note(p, now, "apply_failed", {"error": str(e)})
            else:  # the platform will never take it, so it must not block this workflow's next Proposal
                log.warning("apply of %s refused: %s", p["proposal_id"], e)
                self._transition(p, STALE, now, "apply_refused", {"error": str(e)}, reasons=json.dumps([str(e)]))
            return
        self._transition(p, APPLIED, now, APPLIED, {"version": result["version"], "changed_dims": result["changed_dims"]},
                 applied_minute=now, applied_version=result["version"])

    def _put_config(self, p: sqlite3.Row, version: int, now: int) -> None:
        """Auto-apply, safe to repeat: a row in `applying` may already have reached the platform, so look first."""
        wf, body = p["workflow_id"], json.loads(p["body"])
        done = next((h for h in self._sim.config_history(wf) if h["proposal_id"] == p["proposal_id"]), None)
        if done is not None:
            self._transition(p, APPLIED, now, "confirmed_applied", {"version": done["version"]},
                             applied_minute=now, applied_version=done["version"])
            return
        if version != p["base_version"]:
            self._mark_stale(p, version, now)
            return
        try:
            result = self._sim.auto_apply(wf, {k: body[k] for k in ("proposal_id", "base_version", "proposed_config",
                                                                    "rationale", "evidence")})
        except SimError as e:
            if e.code == "stale_base_version":
                self._mark_stale(p, version, now)
            elif e.retryable:  # stays `applying`: the next tick looks in config history before trying again
                log.warning("auto-apply of %s failed: %s", p["proposal_id"], e)
                self._note(p, now, "apply_failed", {"error": str(e)})
            else:  # a 403 auto_apply_not_permitted, or any other answer that repeating won't change
                log.warning("auto-apply of %s refused: %s", p["proposal_id"], e)
                reasons = e.detail if isinstance(e.detail, list) else [str(e.detail or e.code)]
                self._transition(p, REFUSED, now, e.code, {"reasons": reasons}, decided_minute=now, reasons=json.dumps(reasons))
            return
        self._transition(p, APPLIED, now, APPLIED, {"version": result["version"], "changed_dims": result["changed_dims"]},
                         applied_minute=now, applied_version=result["version"])

    # -- state changes, each committed with its decision -----------------------------------------
    def _transition(self, p: sqlite3.Row, status: str, now: int, outcome: str, detail: dict, **fields: object) -> sqlite3.Row:
        with self._store.transaction() as conn:
            Store.update_proposal(conn, p["proposal_id"], status=status, **fields)
            Store.record_decision(conn, now, p["workflow_id"], PROPOSAL_DECISION, outcome, p["proposal_id"],
                                  {"from": p["status"], "to": status, **detail})
        return self._store.proposal(p["proposal_id"])

    def _note(self, p: sqlite3.Row, now: int, outcome: str, detail: dict) -> None:
        with self._store.transaction() as conn:
            Store.record_decision(conn, now, p["workflow_id"], PROPOSAL_DECISION, outcome, p["proposal_id"], detail)

