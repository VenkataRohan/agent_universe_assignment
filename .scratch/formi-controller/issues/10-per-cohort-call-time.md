# 10: Per-cohort call time

**What to build:** From the history baselines, the controller builds per-cohort hour tables over the analysis partition. Every hour of the dialling window is present, with weights clipped to [0.5, 2]. A table is proposed only if a history replay predicts at least 3% relative gain on the primary KPI with the guardrail holding. It auto-applies where `schedule` is allowed, and goes through approval otherwise. After apply, the before/after check rolls the schedule back if the primary KPI drops significantly (α 0.05) against the 3 days before. The confounding in before/after evaluation is disclosed in the Proposal rationale.

Spec: user stories 14, 34–36.

**Blocked by:** 03, 05

**Status:** ready-for-agent

- [ ] Every proposed hour table covers every dialling-window hour and is accepted by the simulator
- [ ] No schedule Proposal is made when the replay predicts < 3% gain
- [ ] nw_renewal schedule changes auto-apply; nw_kyc's go through approval
- [ ] A significant post-apply drop triggers a rollback Proposal
