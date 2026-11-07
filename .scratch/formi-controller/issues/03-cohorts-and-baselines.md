# 03: Cohorts and history baselines

**What to build:** After history ingestion, every workflow has:
- **Feature cells**, encoded from feature values discovered on lead events.
- **An analysis partition**, chosen deterministically. It's the single feature whose groups differ most on the workflow's primary KPI, with groups under 10% of traffic merged. It's expressed as disjoint cohorts with stable IDs and plain-language names.
- **Baselines** for pickup rate, engagement rate and hallucination-flag rate.
- **Per-cohort, per-hour primary-KPI rates**, adjusted for attempt number.

Cohorts can be written into a config, and a cohort's KPIs can be computed by summing its cells. `controller status` shows each workflow's partition and baselines.

Spec: user stories 12–18; ADR-0004.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] Analysis partition on each public_01 workflow is chosen deterministically and is identical across two runs
- [ ] Partition cohorts are disjoint and, with default, cover every feature cell exactly once
- [ ] A cohort's KPIs computed from cells equal those computed by recounting the exposure log for that cohort
- [ ] Materialised config cohorts are accepted by the simulator and call events carry the same cohort IDs
- [ ] `controller status` shows partition, cohort names and baselines per workflow
