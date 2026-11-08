# Screen details: enough data, ending, and supervisor edits

The spec says a screen runs until it "has enough data", that other edits "start a separate stratum", and that an experiment ends without a candidate if nothing beats control. It leaves the details open. We settled them as follows.

- **Enough data is 200 connects on the smallest arm.** Every arm of every experimented dimension must have them at the 09:00 look. At the traffic we measured (about 1,000 connects a day, a voice challenger on one sixth of it) that is about two days, as ADR-0006 planned. A power calculation on the smallest arm would ask for about 1,000 connects, six or more days of an 8-day scenario, so this is a judgement: the screen only has to drop the clearly worse. A screen still short of that after 4 days ends with no candidate.
- **The look is once a day from 09:00, not only at 09:00.** A restart that misses the 09:00 tick still gets that day's look.
- **Every config version is its own stratum.** Arms are compared inside a version and combined with Mantel-Haenszel weights, so a prompt or caller-ID change between strata can't pass for a difference between arms. With one version this is the plain pooled test.
- **The screen writes `default` only.** A config cohort with its own welcome or voice table isn't in the split, and its calls are left out of the analysis.
- **Control is what most traffic ran before.** When a workflow already splits a dimension, the largest option is control. Returning to control restores the whole earlier distribution.
- **Screens are planned at the moment they can be requested.** A design made hours before a request could go out would be stale after a supervisor edit. A plan found without a Proposal after a crash is aborted and made afresh.
- **Experiments are asked for before caller-ID exploration, after incident detection.** Welcome and voice can move an engagement-primary workflow's primary KPI, and caller ID can't (ADR-0007).
- **An ended screen with nothing to carry forward asks to go back to control.** It restores only the dimensions still carrying its split, so a supervisor's edit stands, and the workflow waits for that before planning anything else.
- **A supervisor edit aborts a screen that has already chosen candidates too.** Its split is still live, and candidates chosen against the old control mean nothing.
- **A dimension a supervisor changed is left alone for 7 days**, as caller-ID exploration does (ADR-0009). Dimensions they didn't touch can still be screened.
- **A screen's expected value counts only options someone has heard.** No proposal claims a lift for an option without calls, so a first screen's value is zero and its impact score is 0. Counts run up to the latest supervisor edit to welcome or voice. It is a rough figure for the approver, never an input to a decision.

## Consequences

- Between a screen choosing candidates and the confirm stage starting, the screen's split stays live. Changing it would cost an approval that the confirm Proposal spends anyway.
- After a supervisor edit aborts a screen, the untouched dimension can take two approvals to return to the same place: one to go back to control and one to start again.
