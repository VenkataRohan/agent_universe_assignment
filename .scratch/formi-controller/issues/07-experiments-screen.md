# 07: Experiments: screen stage

**What to build:** On engagement-primary workflows, the controller plans an experiment on welcome message and voice and proposes a factorial screen split, labelled as exploration with its exposure-weighted expected value. Control keeps at least 50% on each dimension. Pickup-primary workflows get no welcome or voice experiments. Calls are attributed to arms by the `config_version` recorded on each call, so `next_campaign_only` phase-in is handled. A supervisor edit to an experimented dimension aborts the experiment at the edit. Other edits start a separate stratum. At the daily 09:00 look, once the screen has enough data, clearly worse challengers are eliminated and the best remaining one per dimension becomes the candidate. Nothing is promoted from the screen. If no challenger beats control on its point estimate, the experiment ends with no candidate. Arms that never ran contribute nothing to learning.

Spec: user stories 19–23, 27, 29, 57, 61; ADR-0005, ADR-0006, ADR-0007.

**Blocked by:** 03, 04

**Status:** ready-for-agent

- [ ] On public_01, nw_renewal and ks_cancel each get an approved screen; nw_kyc gets none
- [ ] Control share is ≥ 50% on every dimension during a screen
- [ ] Each screen ends with at most one candidate per dimension, or ends with no candidate
- [ ] A supervisor edit to an experimented dimension aborts the experiment; calls after the edit are excluded
- [ ] Experiment state and screen decisions are visible through `controller why` / the audit
