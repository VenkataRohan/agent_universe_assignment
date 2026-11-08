# A workflow's cohorts are a disjoint partition

Within a workflow, cohorts never overlap: each is a filter over lead features, and together with `default` they cover every lead exactly once. The simulator assigns a lead to the *first* matching cohort in config order. With overlapping cohorts, a lead's membership in config could differ from its membership in our analysis, and then detection, experiments and the supervisor view would disagree about who is in "60+". A partition makes order irrelevant. The cost: two overlapping ideas ("60+", "Tamil speakers") must be expressed as their intersections.

## How the analysis partition is chosen

These apply to days before D0 only, so the partition can't change with live data or a restart.

- **"Differ most"** is the spread: the highest group's primary-KPI rate minus the lowest group's, after merging. It reads directly as "60+ engage 14 points more than 18-30". Ties go to the feature name.
- **Traffic** is the primary KPI's denominator: connects for engagement, dials for pickup.
- **Merging** folds the smallest group under 10% into the group with the nearest KPI rate, repeating until every group clears 10%. Nearest rate needs no assumption about which values are adjacent (feature values are discovered), and it loses the least of the difference being measured. A feature that collapses into one group can't be chosen; if none remain, the workflow has only `default`.
- **The largest group is `default`.** It never has to be written into a config, and a feature value history never saw falls into the same cohort in the controller and in the simulator.
- **Hourly rates** are indirectly standardised for attempt number: the hour's successes over those expected from its attempt mix at the cohort's per-band rates, scaled to the cohort's overall rate. Direct standardisation would be undefined for an hour missing an attempt band. The result is capped at 100%.

## A call's `cohort_id` is not its analysis cohort

Cohorts are written into a config only when a cohort-specific allocation or hour table needs them (spec story 17), so a lead in an unwritten cohort is dialled as `default`, and its call event, `cell_agg.cohort_id` and exposure carry `default`. That field says which config branch dialled the call. **Analysis membership always comes from the lead's feature cell**, via `Partition.cohort_of`; nothing that reports on an analysis cohort may read `cohort_id` for it.
