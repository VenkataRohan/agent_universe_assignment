# 09: Decay rollback and holdback release

**What to build:** After promotion, the controller keeps comparing the winner against its holdback. When a rolling 3-day one-sided test shows the winner worse than the holdback (α 0.05), it sends a rollback Proposal that returns traffic to control, with the evidence attached. If 7 days pass without a decay signal, releasing the holdback is batched into the workflow's next Proposal.

Spec: user stories 31–32; ADR-0008.

**Blocked by:** 08

**Status:** ready-for-agent

- [x] A winner that becomes worse than holdback triggers a rollback Proposal with evidence
- [x] Rollback is applied through governance and recorded in the audit
- [x] Holdback release is never sent on its own; it is batched into the next Proposal after 7 clean days
- [ ] Grade report's decaying-variants section shows the controller moving off decayed variants

## Comments

Rollback, its audit and the batched release are built and tested end to end against the real simulator, with decay induced by `Trouble`. The last box is open: no public scenario exercised a real rollback.

- public_01 and public_02 promoted no winner, so their falling W3 share comes from experiments ending, not from a rollback.
- public_03 promoted W3 on three workflows and ends before a decay look could run. W3 was still ahead of its holdback then (about 60% against 54%), so an earlier first look would not have rolled it back either.

Open question for a later ticket: only caller-ID exploration on the approval route can carry a release today, so most workflows keep their holdback until ticket 10's schedule Proposals exist.
