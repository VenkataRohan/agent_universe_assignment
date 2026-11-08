# Confirm details: looks, blocking and ending

The spec fixes the confirm stage's tests and says it ends after at most 3 looks. It leaves open what a look does when the tests disagree, what happens with two dimensions in one experiment, and how an approval that doesn't happen ends. We settled them as follows.

- **Only a look with half the planned sample counts as a look.** Every arm of every undecided dimension must have at least half its planned connects. A shorter day records `waiting_for_data` and uses up nothing. A confirm still short after 7 days ends undecided, so a thin workflow can't hold a split forever.
- **Planned sample comes from control's rate in the screen.** It is the freshest measure of that arm. A rate outside 1% to 90% is clamped for sizing, since there is no 10% lift to detect above that.
- **Each dimension is decided on its own and frozen.** A dimension that wins is settled at that look and isn't tested again, as a sequential test requires. The experiment ends when all are settled or at the third look.
- **Failing the guardrail isn't final until the last look.** A lower bound below `-max_drop` can mean "too few dials to rule it out": at the planned sample a 2-point margin is roughly a coin's worth of noise away. So a passing primary with an unproven guardrail stays undecided, and only the third look blocks it. The guardrail's alpha is per look and not corrected, so three chances to pass make it a little easier to clear than its stated 5%.
- **Harm is final at once, and stops the whole confirm.** A cohort significantly worse off is evidence of harm, unlike an unproven guardrail, so the 50/50 doesn't run another day for the sake of another dimension. Winners already settled are still promoted. This errs towards blocking, as ADR-0006's uncorrected alpha does, and costs some wins when a false alarm stops an undecided dimension.
- **The harm check runs on the primary KPI.** The guardrail is checked across the workflow only, not per cohort.
- **The 5% false-winner rate is per dimension.** Each candidate gets its own 0.05/3 per look. A Proposal promoting two winners has about a 10% chance of containing at least one false one.
- **Winners are promoted together, once all are settled.** One approval, as batching asks. A candidate that didn't win goes back to what ran before in the same Proposal. A dimension with no candidate went back at confirm start, so promotion leaves it alone and any supervisor edit to it stands.
- **Every version is its own stratum**, as in the screen (ADR-0010), for the primary test, the guardrail and each cohort's harm check.
- **A Proposal that expired or went stale is rebuilt from fresh data; a refusal or a lapse ends the experiment.** Nobody said no to the first kind. A rejected or lapsed confirm or promotion aborts and goes back to control, as the spec says.
- **A confirm's data opens at its own config version.** A supervisor edit counts from there too, and only to a dimension still in the experiment.
- **Impact is in concluded calls per dial.** A lift in engaged-per-connect is worth the pickup rate, the same unit caller-ID Proposals use, which understates the effect on conclusion rate per lead. That is the conservative direction. Each dimension's bound is scaled by the share of traffic it moves. A confirm's score uses the screen's lower bound, which picking the best arm can flatter, and its rationale says so.
- **The dry run replays welcome and voice as well as caller ID.** A change of mix moves the prediction by the difference between those options' own rates over the last 3 days. An option nobody heard in that window adds nothing.

## Consequences

- At the planned sample, the guardrail often needs a second or third look, so a true winner can take longer to promote than its primary test alone would.
- A rejected promotion throws away a confirmed result. A new experiment starts after the split has gone back, and has to earn it again.
