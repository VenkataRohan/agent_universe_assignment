# 04: Governance client on the approval path, first used by degraded caller IDs

**What to build:** When a caller ID degrades, the controller notices and asks for approval to move weight off it. This is the first full trip through the governance boundary.
- **Detection:** the caller-ID degradation test runs every tick over a rolling day.
- **Config composer:** builds the full proposed config from the *current* config (supervisor edits included) plus the controller's intent, with diff and `changed_dims`, and checks it against the effective policy before sending.
- **Effective policy:** the API's governance policy with the tenant preference overlay layered on top. The overlay can only tighten. Kestrel's entry: at most 2 requests per 24 hours and none before 11:00.
- **Proposal lifecycle:** `submitting → pending → approved → applying → applied`, with branches `rejected | expired | lapsed | stale`.
  - The Proposal row is committed before sending, with a deterministic ID.
  - A `409 duplicate_proposal_id` means "already sent", so the controller reconciles.
  - Non-terminal Proposals are reconciled on startup via the approvals API and config history.
  - Approvals are applied on the first tick after approval.
  - An expired Proposal is treated as a rejection and rebuilt as a new Proposal from fresh data.
  - A supervisor edit marks a pending Proposal stale, and it is rebuilt on the new base if still worthwhile.
- **Timing and batching:** requests are sent only within effective approver hours, and not in the last 2 hours. One in-flight Proposal per workflow.
- **Proposal fields:** impact score, affected leads and dry run, for this kind of change.
- **Audit:** every decision (including "not degraded, did nothing") is written in the same transaction as its state change. `controller why <workflow> --at <time>` and `controller heard <lead_id>` answer from the store.

Spec: user stories 41, 45–46, 50–56, 58–60, 63–66, 70–72; ADR-0002.

**Blocked by:** 01, 02, 03

**Status:** ready-for-agent

- [ ] On public_02, a degraded caller ID is detected and an approval-path Proposal shifting weight off it is approved and applied
- [ ] Killing the controller between recording and sending a Proposal, then restarting, sends it exactly once
- [ ] Killing between approval and apply, then restarting, applies it exactly once (confirmed via config history)
- [ ] No request is ever sent outside effective approver hours, nor to Kestrel before 11:00 or beyond 2 per 24 hours
- [ ] An expired Proposal produces a new Proposal (new ID) built from fresh data, never a resubmission
- [ ] A supervisor edit makes a pending Proposal stale and any rebuilt Proposal preserves the supervisor's change
- [ ] `controller why` shows change, Proposal, evidence, approval and aftermath; `controller heard` shows each call's exact variant and why that config was live
