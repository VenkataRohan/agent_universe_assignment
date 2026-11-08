# Formi Controller

The always-on loop that watches every workflow's outcomes, experiments with how the agent shows up, and changes its config only through the governance boundary.

## World

**Tenant**:
A client (an insurer or lender) that owns one or more workflows.
_Avoid_: Customer, account

**Workflow**:
One outbound use case of a tenant, with its own dialling window, options, objective and governance policy.
_Avoid_: Campaign, flow

**Campaign**:
One daily batch of leads uploaded to a workflow.
_Avoid_: Batch, upload

**Lead**:
One person enrolled in one campaign.
_Avoid_: Contact, record

**Person**:
The human behind leads; the same person can return in later campaigns.
_Avoid_: Customer, user

## Outcomes

**Pickup rate**:
Connected dials ÷ dials.

**Engagement rate**:
Engaged calls ÷ connected calls, where engaged means the person stayed to a conclusion, interested or not.

**Conclusion rate**:
Leads concluded ÷ leads uploaded; the business outcome above both KPIs.
_Avoid_: Conversion, blended score

**Primary KPI**:
The one KPI a workflow's objective says to optimise.

**Guardrail**:
The maximum drop a workflow's objective tolerates on the KPI that isn't primary.

## Config and governance

**Dimension**:
One independently changeable part of a workflow's config: cohorts, welcome_message, voice, caller_id, prompt_version or schedule.

**Customer-facing dimension**:
A dimension a customer hears or experiences: welcome_message, voice, prompt_version, or cohorts (which decide who hears which variant).
_Avoid_: Agent-facing, content dimension

**Operational dimension**:
A dimension that changes who is called when, or from which number, without changing what they hear: schedule and caller_id.

**Governance policy**:
A workflow's rules for changing config, as the platform states them: auto-apply limits, approver hours, request caps, TTLs.

**Preference overlay**:
A tenant's stated wishes that tighten its governance policy (fewer requests, later hours); it can never loosen it.
_Avoid_: Override, custom policy

**Proposal**:
The controller's own record of a config change it wants, from intent through approval to applied or abandoned.
_Avoid_: Request, change request

**Approval**:
The platform's record of a human approver's decision on a Proposal.
_Avoid_: Proposal (they are not the same thing)

**Supervisor edit**:
A config change made by a client supervisor directly, outside governance.

## Learning

**Feature cell**:
One combination of a lead's features (lang, city tier, age band, value band); the smallest population the controller reasons about.

**Cohort**:
A named, stable-ID set of feature cells within one workflow. A workflow's cohorts never overlap, and whatever they leave out is `default`.
_Avoid_: Segment, bucket, group

**Experiment**:
A fixed traffic split on one or more customer-facing dimensions of a workflow, run until a deterministic test decides it.
_Avoid_: Test (ambiguous), trial

**Control**:
The option an experiment compares against: what the workflow ran before the experiment.

**Challenger**:
An option an experiment gives traffic to in place of the control.
_Avoid_: Variant (a variant is the full set of options one call was dialled with)

**Exposure**:
One call's record of exactly which variant one lead heard, under which config version and cohort. Kept for audit, never used for learning.

**Variant**:
The combination of welcome message, voice, caller ID and prompt version one call was dialled with.

**Screen**:
The first stage of an experiment: every challenger runs at once, only to choose one candidate per dimension. Nothing is promoted from it.
A screen's Proposal is labelled "exploration" to say it claims no win. That is a label for the approver; it is not caller-ID Exploration.

**Arm**:
One option of an experimented dimension, with the calls that heard it. Control is an arm; so is each challenger.

**Stratum**:
The calls of one config version within an experiment. Arms are compared inside a stratum, so a change that isn't to an experimented dimension can't pass for a difference between arms.

**Candidate**:
The one challenger per dimension a screen sends forward to confirm. Not a winner: nothing is promoted from a screen.

**Confirm**:
The second stage: one candidate against control, judged on fresh data only.

**Look**:
One daily judgement of a confirm, made once every arm has at least half its planned sample. A confirm gets at most three.

**Analysis partition**:
The one set of cohorts per workflow, chosen from history, over which promotions are checked for harm and call time is tuned.

**Winner**:
A challenger that beat control on the primary KPI, passed the guardrail and harmed no cohort of the analysis partition, under the experiment's test.

**Promotion**:
Moving a winner from experiment share to most of the workflow's traffic.

**Holdback**:
The share of traffic that stays on control after a promotion, so the winner can still be compared against it.

**Decay**:
A promoted winner becoming worse than its holdback.
_Avoid_: Fatigue, drift (drift is a change in a KPI with no variant change)

**Rollback**:
A Proposal that undoes one of the controller's own changes after it went wrong: a decayed winner's traffic back to control, or a schedule back to what ran before.
_Avoid_: Revert (reserved for undoing a supervisor edit)

**Holdback release**:
Moving the holdback's traffic to the winner after 7 days without a decay signal. It only ever goes out inside another Proposal.

**Hour table**:
One cohort's weights for every hour of the dialling window. It decides who the dialler picks in an hour, not how many calls it makes.

**History replay**:
A prediction of what a schedule would have done on history days it wasn't fitted to, with each hour's and each cohort's dials held as they were.

**Before/after check**:
The single comparison of a workflow's primary KPI in the 3 days after a schedule change with the 3 days before. Confounded by design, so it can only trigger a rollback.

## Incidents

**Incident**:
A population-level, statistically confirmed degradation on a workflow (hallucination spike, degraded caller ID) that the controller must respond to. Never triggered by a single call.
_Avoid_: Alert (an alert is a message about the loop's own health)

**Known-good prompt**:
The prompt version a workflow ran most recently that no prompt-change incident was blamed on.

**Onset**:
When an incident's flag rate most likely began to rise. A prompt change in the day before detection is taken as the onset; otherwise it is estimated from the per-tick flag counts.

**Escalated**:
An incident the controller has handed to a person, because the live prompt passed validation despite the spike, no candidate passed, credits ran out, or the applied fix did not bring the flag rate back down.

**Degraded caller ID**:
A caller ID whose pickup is significantly and materially below the workflow's other caller IDs over the same period.
_Avoid_: Spam-flagged (that is a cause we can't observe)

**Exploration**:
Giving a caller ID with no recent evidence a capped share of traffic until it has enough dials to be kept at an equal share or dropped. Not an experiment: it is operational, and nothing is claimed as a win.

**Unproven caller ID**:
A caller ID with fewer than 300 dials in the last 7 days. Moving traffic never raises its share.

**Credit reserve**:
The credits held back for incident response that nothing else may spend.

## Loop health

**Alert**:
A message about the loop's own health. In this controller every alert is a page.
_Avoid_: Incident (an incident is about a workflow's calls)

**Page**:
An alert that needs a person now, saying what to look at. Only four conditions page: ingest lag above 2 ticks, credits below the reserve, an incident unfixed after 4 hours, and an apply failure.

**Apply failure**:
A lapsed approval, or a stale-base loop: three Proposals of one kind on one workflow going stale in a row.

**Note**:
Something shown by `controller health` that can wait: a rejection, an expiry, an inconclusive experiment, a retry.
_Avoid_: Page, warning
