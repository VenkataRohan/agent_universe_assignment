# 09: Decay rollback and holdback release

**What to build:** After promotion, the controller keeps comparing the winner against its holdback. When a rolling 3-day one-sided test shows the winner worse than the holdback (α 0.05), it sends a rollback Proposal that returns traffic to control, with the evidence attached. If 7 days pass without a decay signal, releasing the holdback is batched into the workflow's next Proposal.

Spec: user stories 31–32; ADR-0008.

**Blocked by:** 08

**Status:** ready-for-agent

- [ ] A winner that becomes worse than holdback triggers a rollback Proposal with evidence
- [ ] Rollback is applied through governance and recorded in the audit
- [ ] Holdback release is never sent on its own; it is batched into the next Proposal after 7 clean days
- [ ] Grade report's decaying-variants section shows the controller moving off decayed variants
