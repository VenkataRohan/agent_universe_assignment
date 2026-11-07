# Spec: Formi outcome-first controller

Status: ready-for-agent

Design decisions behind this spec: `GLOSSARY.md` and `docs/adr/0001`–`0008`. Use the glossary's terms throughout.

## Problem Statement

Formi's outbound voice agents run on a configuration a human sets once and rarely revisits: welcome message, voice, caller ID, prompt version, and which hours each kind of person gets called. Nobody notices when a caller ID goes bad or a supervisor ships a prompt that hallucinates. Nobody tests whether another voice would keep more people on the line, and nobody notices when yesterday's improvement stops working. Clients are regulated insurers and lenders. They need every change to pass a named human approval or a policy-permitted auto-apply, they need to know why any customer heard what they heard, and they won't accept a change that hurts any group of their customers. Platform SRE needs this to run unattended across hundreds of workflows, survive being killed at any moment, and never apply a change twice.

## Solution

An always-on **controller** that drives the simulator over its HTTP API only, and for every workflow continuously:

1. **Ingests** every event, idempotently and fast enough for the load scenario, into feature-cell aggregates and an audit-only exposure log.
2. **Detects** population-level problems: hallucination spikes and degraded caller IDs. Each is backed by a statistical test, never a single call.
3. **Experiments** on customer-facing dimensions that can move the workflow's primary KPI. It screens all challengers, then confirms one per dimension on fresh data with a deterministic test that has a stated false-winner rate, a guardrail, and a cohort harm check.
4. **Proposes** every change through governance with a summary, diff, affected-lead count, dry run, conservative impact score and rationale. It batches changes into as few requests as possible, times them to approver hours, and respects each tenant's preference overlay.
5. **Applies and learns**: it applies approvals promptly, promotes winners with a holdback, rolls back decayed winners, tunes call time per cohort, and never silently overwrites a supervisor edit.
6. **Explains**: from its own store, a CLI answers "why did workflow X change at T, who approved it, what happened next", "what did this customer hear and why", "what's going on in this workflow right now", and "is the loop itself healthy".

It ships as `./run.sh <SIM_URL>`. It resumes cleanly after `kill -9`, and comes with a crash-test script, grade reports for public_01 and public_02, load numbers for public_03, and a complete `SUBMISSION.md`.

## User Stories

### Running the loop
1. As a grader, I want `./run.sh <SIM_URL>` to start the controller and run until the scenario finishes with no manual steps, so that I can run it against hidden scenarios unattended.
2. As a grader, I want the controller to learn about the world only through the HTTP API, so that it performs on hidden scenarios as it does on public ones.
3. As a grader, I want the controller to work for any workflow, tenant, option set and policy the API describes, so that hidden scenarios with different policies are handled correctly.
4. As an SRE, I want the controller to advance the simulated clock one 15-minute tick at a time, so that incidents are caught and approvals applied within a tick.
5. As an SRE, I want one simulated day of the load scenario processed in under 10 wall-clock minutes on 4 vCPU / 8 GB, so that the loop keeps up with production volume.
6. As an SRE, I want the controller to keep only active campaigns' leads in memory, so that memory stays bounded next to the simulator on an 8 GB box.

### Ingestion
7. As an SRE, I want each event chunk ingested in one transaction that also advances the cursor, so that a crash mid-chunk never double-counts or drops events.
8. As an analyst, I want every call folded into a feature-cell aggregate (workflow, day, hour, config version, feature cell, variant), so that any cohort's KPIs can be computed by summing cells.
9. As a compliance head, I want every call recorded in an exposure log with the exact variant, config version and cohort, so that I can tell an auditor what a specific customer heard on a given day.
10. As an engineer, I want `config_changed`, `approval_decided` and `campaign_created` events stored verbatim, so that the audit trail has the world's own record of each change.
11. As an engineer, I want history chunks before D0 ingested the same way as live chunks, so that baselines come from real pre-D0 behaviour.

### Bootstrapping from history
12. As a data scientist, I want each workflow's baseline pickup, engagement and hallucination-flag rates computed from history, so that detection thresholds are relative to that workflow's normal.
13. As a data scientist, I want each workflow's analysis partition chosen deterministically from history (the single feature whose groups differ most on the primary KPI, with groups under 10% of traffic merged), so that cohorts are sized to the traffic we actually have.
14. As a data scientist, I want per-cohort, per-hour primary-KPI rates from history, adjusted for attempt number, so that call-time tuning starts from real data.

### Cohorts
15. As a product manager, I want one cohort object with a stable ID used by detection, experiments, call time and the supervisor view, so that "seniors" means the same thing everywhere.
16. As an engineer, I want a workflow's cohorts to be disjoint, so that the simulator's first-match ordering can never put a lead in a different cohort from our analysis.
17. As an engineer, I want cohorts written into a workflow's config only when a cohort-specific allocation or hour table needs them, so that config stays minimal and readable.
18. As a supervisor, I want cohorts named in plain language ("age 60+"), so that I understand who a change affects.

### Experiments
19. As a product manager, I want experiments only on dimensions that can move the workflow's primary KPI (welcome message and voice for engagement-primary workflows), so that scarce approvals go where they can change the outcome.
20. As a compliance head, I want control to keep at least half the traffic on every dimension during a screen, so that customers' exposure to untested variants is limited.
21. As a data scientist, I want a screen to run every challenger at once in a factorial split of welcome × voice, so that every call informs both dimensions.
22. As a data scientist, I want the screen to drop challengers that are clearly worse than control and pick the best remaining one per dimension, so that only one candidate per dimension goes forward.
23. As a data scientist, I want nothing promoted from screen data, so that picking the best arm doesn't inflate the false-winner rate.
24. As a data scientist, I want the confirm stage to test one candidate against control at 50/50 on fresh data only, with a one-sided z-test (MDE 10% relative, power 0.8, α 0.05 spread over at most 3 daily looks), so that each promotion has at most a 5% false-winner rate.
25. As a product manager, I want a candidate that improves the primary KPI but fails non-inferiority on the guardrail (lower bound of the guardrail difference below −max_drop) never to be promoted, so that the guardrail is respected.
26. As a CTO, I want a promotion blocked if any cohort of the analysis partition shows significant harm (one-sided α 0.10, uncorrected), so that we never ship an improvement that hurts a group of customers.
27. As a data scientist, I want experiment calls attributed by the `config_version` recorded on each call, so that workflows with `next_campaign_only` are analysed correctly while arms phase in.
28. As a data scientist, I want an experiment that hasn't confirmed by its last look to end inconclusive and revert to control, so that experiments don't run forever.
29. As a product manager, I want experiment arms that never ran (because approval was refused or lapsed) to contribute nothing to learning, so that only what actually happened counts as evidence.

### Promotion, decay and rollback
30. As a data scientist, I want a promoted winner to keep a 20% holdback on control, so that "is it still winning?" remains a randomised comparison.
31. As a supervisor, I want a winner rolled back when a rolling 3-day one-sided test shows it worse than the holdback (α 0.05), so that a variant that stops working is backed out quickly.
32. As a product manager, I want the holdback released after 7 days without a decay signal, batched into the next proposal, so that the full lift is eventually taken.
33. As a product manager, I want a winner to change only its own workflow's config, so that promoting to other clients stays a separate decision.

### Call time
34. As a product manager, I want per-cohort `hour_weights` proposed when a history replay predicts at least 3% relative gain on the primary KPI with the guardrail holding, so that the right people are called at the hours they're most likely to answer.
35. As an engineer, I want every hour of the dialling window present in every hour table, weights clipped to [0.5, 2], so that no capacity is wasted on all-zero hours.
36. As a supervisor, I want a schedule change rolled back if the primary KPI drops significantly (α 0.05) against the 3 days before it, so that a bad schedule doesn't persist.

### Hallucination incidents
37. As a compliance head, I want a hallucination incident raised when a rolling 2-hour window of at least 150 connects shows a flag rate at least 2× the workflow's baseline with a one-sided binomial p < 0.001, so that we act on populations, not anecdotes.
38. As a compliance head, I want the last known-good prompt validated and proposed first when a prompt change preceded the incident, so that the fix is the one with the most evidence and the least cost.
39. As a CTO, I want `/llm/suggest-fixes` called only when there is no known-good prompt, so that credits are spent only where they change a decision.
40. As a compliance head, I want every prompt-change proposal to carry a validation run for that workflow and prompt that meets the policy's minimum pass rate, so that "the model thinks it's better" is never the evidence.
41. As an approver, I want a proposal that undoes a supervisor edit to say so explicitly ("reverts the D3 14:00 supervisor edit P1→P2 because…"), so that nothing a supervisor did is overwritten silently.
42. As a compliance head, I want a fix verified after apply (flag rate back to baseline within the next 300 connects), and the incident escalated if not, so that we know the fix worked.
43. As a CTO, I want at least 30 credits always reserved for incident response, so that we never run out mid-incident.
44. As a supervisor, I want incident proposals allowed to fall back to the policy's own limits when my preference overlay would delay them, with the reason in the summary, so that customer harm outranks my scheduling preference.

### Caller-ID health
45. As an SRE, I want a caller ID flagged degraded when its rolling-day pickup is at least 15% below the workflow's other caller IDs with a one-sided z p < 0.001 on at least 300 dials, so that bad numbers are caught without false alarms.
46. As a supervisor, I want weight moved off a degraded caller ID onto healthy ones (auto-applied where policy allows, within max_shift, otherwise proposed), so that pickup recovers quickly.
47. As an SRE, I want a caller ID with no recent evidence capped at 10% of traffic until it has 300 dials, then given an equal share or dropped, so that we don't rely on an untested number.

### Governance
48. As a compliance head, I want welcome message, voice, prompt version and cohorts never auto-applied, even where policy would allow it, so that every customer-facing change has a named human approval.
49. As Kestrel's supervisor, I want no auto-apply at all on my workflows except moving off a degraded caller ID, so that I'm asked before anything changes on a running campaign.
50. As Kestrel's supervisor, I want at most 2 requests a day and none before 11:00, so that I'm not flooded during standups.
51. As an engineer, I want the preference overlay able only to tighten a policy, never loosen it, so that client wishes can't break the governance boundary.
52. As a supervisor, I want at most one in-flight proposal per workflow (incident fixes excepted), with other changes merged into it, so that I get one request with five things in it, not five requests.
53. As an approver, I want requests sent only within my approver hours and not in the last 2 hours of them, so that requests don't sit overnight and expire.
54. As an engineer, I want an approved proposal applied on the first tick after approval, so that it never lapses.
55. As a product manager, I want an expired proposal treated as a rejection and rebuilt from fresh data as a new proposal, so that stale requests are never resubmitted.
56. As an engineer, I want a pending proposal marked stale when a supervisor edit changes its base, and re-proposed on the new base if still worthwhile, so that supervisor edits are always preserved.
57. As a supervisor, I want an experiment's data closed at my edit when I change a dimension under experiment, and a new config version treated as a separate stratum otherwise, so that my edits don't corrupt the experiment.
58. As a product manager, I want every proposal's impact score to be a conservative expected change in conclusion rate (lower 80% bound × affected share ÷ 5 percentage points, clipped to [0,1]), with incident fixes floored at 0.9, so that ranking the queue by impact means something.
59. As an approver, I want `affected_leads` to count only leads the change will actually reach (active campaigns of affected cohorts, honouring `next_campaign_only`, plus the next day's expected batch), so that I know the blast radius.
60. As an approver, I want a dry run that replays the last 3 days' traffic mix under the proposed allocation and reports predicted primary and guardrail KPIs with intervals and cohorts at risk, so that I can judge the change.
61. As an approver, I want screen-start proposals labelled as exploration with their exposure-weighted expected value, so that I know they're an experiment, not a claimed win.
62. As a compliance head, I want every auto-applied operational change on my workflows listed in a weekly summary with what changed and why, so that we know how the latitude in our signed policy was used.

### Crash safety and reliability
63. As an SRE, I want every proposal recorded with a deterministic ID before it's sent, so that a restart never sends it twice.
64. As an SRE, I want a `409 duplicate_proposal_id` treated as "already sent" and reconciled, so that retries are safe.
65. As an SRE, I want every non-terminal proposal reconciled against the approvals API on startup, so that no pending approval is lost.
66. As an SRE, I want apply intent recorded before applying and confirmed through config history, so that no approval is ever applied twice.
67. As an SRE, I want the controller to resume only if the DB's scenario matches the simulator's and the simulator's clock hasn't gone backwards, and otherwise refuse with a clear message, so that a restarted simulator isn't mistaken for the same world.
68. As an SRE, I want 5xx responses and timeouts retried with backoff, 4xx responses recorded as decision outcomes, and one workflow's error never stopping the loop for the others, so that the controller degrades gracefully.
69. As an SRE, I want a script that SIGKILLs the controller at 3 random points during public_02, restarts it each time, and checks for no duplicate proposals, no double applies, no lost pending proposals, and a grade within noise of an uninterrupted run, so that crash safety is demonstrated, not promised.

### Audit and explanation
70. As a reviewer, I want `controller why <workflow> --at "D3 14:00"` to show the change in force, its proposal, its evidence, who approved it, and what happened afterwards, so that any decision can be explained live from the store.
71. As a compliance head, I want `controller heard <lead_id>` to show every call's exact variant and why that config was live, so that I can answer an IRDAI auditor within 24 hours.
72. As an engineer, I want every decision recorded with its inputs, including decisions not to act (experiment inconclusive, harm check blocked, incident below threshold), so that "why didn't it change" is answerable too.

### Supervisor view and loop health
73. As a supervisor, I want `controller status <workflow>` to show what's being tested, what's winning, what needs my approval and what changed today, in one place, so that I'm not reading fifty alerts.
74. As an SRE, I want `controller health` to show ingest lag, decisions per hour, credit burn and active alerts, so that I can see the loop's health without reading logs.
75. As an on-call engineer, I want to be paged only for ingest lag above 2 ticks, credits below the reserve, an incident unfixed after 4 hours, or apply failures, each message saying what to look at, so that every page is actionable.
76. As an on-call engineer, I want everything else (rejections, expirations, inconclusive experiments, single retries) shown as notes, not pages, so that I'm not woken for things that can wait.

### Deliverables
77. As a grader, I want `reports/` to contain `sim grade` output for public_01 and public_02 and measured load numbers for public_03 (ingest events/sec, decision lag, peak memory, wall-clock per simulated day), so that results are verifiable.
78. As a grader, I want `SUBMISSION.md` completed from the template, covering assumptions, scope, each stakeholder's reply, architecture, learning loop, governance, incidents, scale, reliability, audit query output, results, trade-offs, weaknesses and a production plan, so that the reasoning can be evaluated.
79. As a new engineer, I want a README section on running, debugging and extending the controller, so that I can own it when the author is on leave.

## Implementation Decisions

**Modules** (a deep module each: small interface, the behaviour behind it):

- **Sim client**: the only code that talks HTTP. It's constructed from an injected `httpx.Client`, so tests can pass FastAPI's `TestClient`. It classifies errors as retryable (5xx, timeout), decision outcome (4xx with code), or out of credits (402). It retries with backoff. All mutating calls carry idempotency keys.
- **Store**: one SQLite file in WAL mode (ADR-0001). It owns the schema and exposes transactions. Tables, at a high level: run identity (scenario, cursor), leads (lead → person, feature cell, campaign), cell aggregates, exposure log, world events (verbatim non-call events), proposals, experiments, incidents, decisions (the audit of every decide step, including no-ops), alerts and notes, loop metrics. No other module writes SQL against another module's tables.
- **Ingestor**: chunk → one transaction (aggregates, exposure log, leads, world events, cursor). It handles history and live chunks the same way. It caches active campaigns' leads in memory and warms that cache from the store on restart.
- **Workflow registry**: reads workflows from the API, then layers the tenant **preference overlay** over each governance policy and produces the *effective* policy. The overlay can only tighten the policy, and has an incident exception. Overlay entries are code-level data keyed by tenant ID, seeded with Kestrel (≤2 requests per 24 hours, none before 11:00, notify before change, so no auto-apply except degraded caller ID).
- **Cohorts**: feature-cell encoding (lang × city_tier × age_band × value_band, with the value lists discovered from lead events), disjoint cohort partitions, deterministic analysis-partition selection from history, materialising cohorts into config, and plain-language names.
- **Statistics (Seam 2)**: pure functions with no I/O. They cover sample size for a two-proportion test, the one-sided z-test, non-inferiority, the cohort harm check, the screen elimination and candidate choice, the binomial hallucination detector, the caller-ID degradation test, decay against the holdback, and confidence bounds for impact.
- **Experiment engine**: owns each experiment's state machine and its look and promotion decisions.
  ```
  planned → screen_requested → screening → confirm_requested → confirming
          → promoted (holdback) → released | rolled_back
  any → inconclusive | aborted (supervisor edit on an experimented dimension, rejection, lapse)
  ```
  Looks happen once a day at 09:00 simulated time. The confirm sample size per arm comes from the statistics module at α 0.05/3. Confirm looks start once each arm has at least half the planned sample, and the experiment ends inconclusive after the third look. If no screened challenger beats control on its point estimate, the confirm stage is skipped.
- **Schedule optimiser**: computes per-cohort hour tables from history, runs the replay check against the 3% gain threshold, and runs the before/after rollback check.
- **Incident responders**: one for hallucination (detect → known-good or LLM candidates → validate → propose → verify → escalate) and one for caller ID (degradation → shift, plus exploration caps). They respect the credit reserve of 30.
- **Config composer**: builds every proposed config as *the current config (including any supervisor edits) + the controller's intents* (experiment allocations, schedule tables, caller-ID weights, prompt fix). It computes `changed_dims` and the diff against the base, and checks the effective policy before anything is sent (allowed dimensions, `max_shift` total-variation distance, options, dialling window).
- **Governance client**: owns the Proposal state machine and its timing and batching rules.
  ```
  submitting → pending → approved → applying → applied
  pending → rejected | expired          approved → lapsed
  any non-terminal → stale (base changed) → replaced by a new Proposal
  ```
  - **Persistence:** the Proposal row is committed before `POST /approvals` or `PUT /config`. IDs are deterministic: `{workflow}-{kind}-{seq}`.
  - **Batching and timing:** one in-flight Proposal per workflow, with incident fixes exempt. Requests go out only within effective approver hours, and never in the last 2 hours of them. Approvals are applied on the first tick after they're approved.
  - **Recovery:** on startup, every non-terminal Proposal is reconciled through `GET /approvals?workflow_id=` and `config/history`.
  - **Proposal fields:** the governance client fills `impact_score`, `affected_leads` and `dry_run` as defined in user stories 58–61.
- **Loop**: one process, one thread. Each tick runs: advance 15 minutes → ingest new chunks → reconcile approvals and apply → detect incidents → (at 09:00) run experiment, schedule and caller-ID decisions → send any due proposals → record loop metrics → evaluate alerts. Every decision is written to the decisions table in the same transaction as the state change it causes.
- **Query interface and CLI**: the read-only queries `why`, `heard`, `status`, `health` and `report --weekly`, with the CLI as a thin wrapper over them. Tests assert through the same query interface.
- **Packaging**:
  - **`run.sh`:** runs `python3 -m controller --sim-url "$1" --db state/controller.db`, with no Docker and no new dependencies beyond `requirements.txt`.
  - **Restart identity:** the controller resumes from an existing DB only if its scenario name matches the simulator's and the simulator's `sim_minute` is at or beyond the stored cursor. Otherwise it refuses with a clear message.
  - **Scripts:** a crash-test script and a load-measurement script live in a scripts directory.

**API contract notes** (from `docs/SIMULATOR.md` and `sim/server.py`):
- `GET /workflows/{id}` returns `options`, `objective`, `governance`, `mid_flight`, `dialling_window`, `prompt_catalog` (summaries only) and `current_version`.
- Weights in an allocation are normalised.
- An hour table that omits an hour gives that hour weight 0.
- `base_version` must equal the current version.

## Testing Decisions

- **What a good test is:** it exercises behaviour through one of the two agreed seams and asserts on outcomes a user of the system would see: query-interface output, simulator API state, the proposal and approval records. It never asserts on internal tables, private functions or call order.
- **Seam 1 (primary): the simulator's HTTP API.** The controller is built from an injected `httpx.Client` and a temp DB, and driven with `step()` / `run()` against the **real simulator in-process** through FastAPI's `TestClient(sim.server.app)`. Scenarios are public scenarios, optionally with `days` / `history_days` shrunk as `tests/test_sim.py` does. Tests never author or edit hidden-truth sections. Behaviour tested here:
  - idempotent ingestion
  - restart and resume, simulated by stopping at an injected point and building a fresh controller on the same DB and world
  - the Proposal lifecycle, including stale, expired and duplicate
  - the governance boundary: no auto-apply on customer-facing dimensions; overlay timing and caps
  - supervisor-edit handling
  - query output
- **Incident behaviour** is tested by running **public_02** end to end, marked `slow`. Tests check that an incident is detected, that a validated fix is proposed with evidence, and that the fix is verified.
- **Seam 2: the statistics module.** Pure-function tests with synthetic counts. They cover:
  - sample size matching known values
  - the tests rejecting and accepting at their boundaries
  - simulated false-winner and false-positive rates over many synthetic null experiments, staying at or below their stated α
  - deterministic outputs for identical inputs
- **Prior art:** `tests/test_sim.py` (a scenario fixture with shrunk days, the approval and stale flow, auto-apply policy checks).
- **The crash test** is a script, not a pytest test, and its output log is a deliverable.
- **Load measurement** is a script whose output goes into `reports/`.

## Out of Scope

- Per-person call time (`lead_preferred_hour`). Declined despite Product marking it P0; reply drafted in `SUBMISSION.md`.
- Fair allocation of credits and approver attention across tenants (C15).
- Cohort-specific winners and promotion. Only workflow-wide promotion, guarded by a cohort harm check.
- Prompt versions as experiment arms. Prompts change only through incident response.
- Welcome and voice experiments on pickup-primary workflows (ADR-0007).
- Bandits and continuous re-weighting (ADR-0005).
- A UI or an HTTP status endpoint. The CLI only.
- Promoting winners across workflows or tenants.
- Postgres, Redis or Docker. Documented as the production path only.
- A single blended lift score (Sales memo). The KPIs stay separate.

## Further Notes

- **Measured from history over the API (public_01):**

  | | `nw_renewal` | `ks_cancel` | `nw_kyc` |
  |---|---|---|---|
  | connects/day | 943 | 1,175 | 502 (1,946 dials/day; pickup-primary) |

  The 60+ band is about 15% of connects. Pickup dips at 15–16h. Hallucination flags run at 1.4–1.6% of connects.
- **Simulator cost on load:** 71.7s for 9.74M events and 870 MB RSS. The controller has roughly 9.5 minutes of the 10-minute budget per simulated day.
- **Memo claims that are wrong against the policies.** These need to be called out in `SUBMISSION.md` §3:
  - Kestrel does not have auto-apply "for everything": prompt and cohorts are excluded, and `max_shift` is 0.2.
  - `nw_kyc` has auto-apply disabled.
  - P4 isn't an option on `nw_kyc`.
  - Changes can't go "live for everyone by evening" on `next_campaign_only` workflows.
  - Anita's 2/day cap is stricter than the policy's 4/day, and is honoured through the overlay.
  - Her "word-for-word compliance script" complaint describes the Strict prompt (P5).
- **Assumptions to list in `SUBMISSION.md` §1** include:
  - Welcome message and voice can't affect pickup.
  - Pre-D0 history ran on the default config.
  - Hallucination flags' false-positive rate is stable over time.
  - A tenant's preference overlay comes from its memo, keyed by tenant ID.
- **Commit history:** commit after each ticket. Small, honest commits.
