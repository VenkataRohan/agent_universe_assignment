# Call-time tuning details

The spec fixes the 3% threshold, the [0.5, 2] clip and the before/after rollback. It leaves open how a replay predicts anything, how a per-cohort table gets past the auto-apply limits, and when the check runs. We settled them as follows.

- **A table's weights are the cohort's attempt-adjusted hourly rate over its overall rate.** An hour with no history weighs 1. A cohort whose table equals `default`'s is not written into the config (spec story 17).
- **The replay holds each hour's dials and each cohort's dials as they were.** The dialler runs at capacity and the leads are the leads, so a table can only decide which cohort takes an hour. Without both totals held, a replay would credit a table for dialling more of whichever cohort converts best. A workflow with only `default` can't gain, and is correctly never proposed.
- **The forecast comes from days the table wasn't fitted to.** History is split into alternate days, each half's table is replayed on the other half, and the two predictions are averaged. Fitted and replayed on the same days, a table is credited with its own noise. The table finally proposed is fitted on all of history. A single day of history can't be split, so it never produces a table.
- **The plan is made once per workflow.** It reads only history, so the answer can't change. A "no" is never revisited.
- **Where `schedule` may auto-apply but its cohorts aren't in the config yet, the cohorts go to an approver first, alone.** The platform refuses a table naming a cohort the config doesn't define, and refuses to auto-apply `cohorts`, which ADR-0002 keeps with a human anyway. The cohorts carry no allocation or table, so nobody's calls change until the table follows, and the table then auto-applies. Where everything needs approval, cohorts and table go in one Proposal, because that costs the approver less.
- **A table is proposed on any tick, not only at 09:00.** Every approver's hours start after 09:00, so a 09:00-only decision could never reach an approver (the same reason as ADR-0009).
- **The before/after check is a single comparison, not a Look.** It compares the primary KPI over the 72 complete hours after the change with the 72 hours before it, at α 0.05 one-sided, as soon as those hours are in. The hour of the change is on neither side. Both windows span every hour of the day three times, so the hour mix can't favour either.
- **A rollback restores the schedule that ran before and leaves the cohorts defined.** Removing them would take an approver and change nothing. It uses the auto-apply path where the table did.
- **The guardrail must hold with the two folds' disagreement added to the predicted drop.** Two folds give no real interval, so this only leans the check towards caution. For the same reason a table's impact score uses its worse fold's gain in place of an 80% bound.
- **A supervisor's schedule from the last 7 days is left to run, as with caller IDs, and a config holding cohorts outside the partition gets no table.** Cohorts match first in config order, so those could take leads from the table's cohorts.
- **A table is proposed once.** After a rollback the same history would produce the same table, so none follows. A supervisor's change to the schedule ends the check: their choice stands.

## Consequences

- The before/after check is confounded: the days compared differ in their leads and in whatever else changed. Every schedule Proposal says so in its rationale. It is a safety net against a drop, and it cannot show that a table worked.
- On workflows with no cohort-by-hour interaction no table reaches 3%, even where hours differ a lot, because hours that are better for everyone can't be given to anyone in particular.
- A table on such a workflow as `nw_renewal` waits for one approval, so its first table is at the mercy of approver hours and the rejection cooldown.
- A real drop that begins after the single comparison is not caught here, a severe drop runs its full three days, and a table applied in a scenario's last three days is never checked.
- The forecast is always against history's traffic, so once a supervisor's older table is live, the 3% isn't measured against what is running.
- The windows anchor on when the controller saw the change applied. A retried apply can be a tick late, which puts at most that tick of the new schedule in the "before" window.
- A schedule reaches running campaigns even under `next_campaign_only`, so a table's `affected_leads` counts them.
