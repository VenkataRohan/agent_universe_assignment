# Experiments screen, then confirm on fresh data

An experiment first **screens** every challenger (factorial split, control 50%) for about two days, only to choose one candidate per dimension. It then **confirms** that candidate against control at 50/50, judged on post-switch data only: a one-sided z-test on the primary KPI (MDE 10% relative, power 0.8, α 0.05 spread over at most 3 daily looks), plus a non-inferiority test on the guardrail. Measured traffic (≈940 connects/day on `nw_renewal`) makes a single-stage test with Bonferroni over all arms, cohorts and looks take 18–90+ days, against 8–10 day scenarios. A naive "pick the best arm" has an unknown false-winner rate. Confirming on fresh data keeps the per-promotion false-winner rate at ≤5% and costs one extra approval.

## Consequences

- Cohort-specific winners are not promoted: a 15% cohort can't be confirmed within a scenario. Instead, a promotion is blocked if any cohort of the workflow's analysis partition shows significant harm (one-sided, family-wise α 0.10 across cohorts by Holm's method, ADR-0016).
