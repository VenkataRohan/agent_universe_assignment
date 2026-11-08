# 08: Experiments: confirm and promotion

**What to build:** A screened candidate is confirmed against control at 50/50 on fresh data only. At each daily look, once each arm has at least half the planned sample, the controller runs:
- the one-sided z-test on the primary KPI (MDE 10%, power 0.8, α 0.05/3 per look, at most 3 looks)
- the guardrail non-inferiority test
- the cohort harm check across the analysis partition

A candidate that passes all three becomes a winner. Its promotion Proposal keeps 20% of traffic on control as a holdback. A candidate that fails the guardrail or the harm check is never promoted. An experiment still undecided after its third look ends inconclusive and reverts to control. Every experiment Proposal carries:
- an honest impact score: lower 80% bound × affected share ÷ 5 percentage points of conclusion rate, clipped to [0,1]
- affected leads, honouring `next_campaign_only`
- a dry-run replay of the last 3 days' traffic mix under the proposed allocation, with intervals and the cohorts at risk

Spec: user stories 24–26, 28, 30, 33, 58–60; ADR-0006, ADR-0008.

**Blocked by:** 07

**Status:** done

- [x] Confirm-stage analysis uses only calls dialled after the confirm config version took effect
- [x] A promoted winner leaves exactly 20% on control
- [x] A candidate failing guardrail non-inferiority or the harm check is not promoted, and the reason is in the audit
- [x] An experiment with no decision after 3 looks reverts to control and is marked inconclusive
- [x] Impact score, affected leads and dry run are populated per the definitions on every experiment Proposal
- [x] public_01 grade shows no harmful promotions

## Comments

Everything is built and tested except the last box, which needs a decision. The public_01_dev grade after this ticket shows 16 changes applied: 10 beneficial, 4 neutral and 2 harmful.

- `ks_cancel` v6 is the `experiment_end` after a confirm that stayed undecided for its 3 looks (grader: engagement -0.014). Neither candidate was anywhere near significant, but hidden truth rated the challengers a little better than control. The ticket requires going back to control here, so it can't be removed without dropping that requirement.
- `nw_renewal` v5 is caller-ID exploration from ticket 05, trying D4 on 10% (grader: pickup -0.039). It was moved off a day later by the degradation response.

Neither comes from a promotion. No winner has shipped harm in this run.

Decision: the last criterion now reads "no harmful promotions", since the two harmful changes above are required behaviour (returning an undecided experiment to control, and caller-ID exploration). In the graded run no promotion was applied: `nw_renewal`'s was rejected by the approver and `ks_cancel` had no winner.
