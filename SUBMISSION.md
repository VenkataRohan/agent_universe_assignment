# Outcome-First Agent Loop — Submission

**Name:** Venkata Rohan Kambhampati · **Date:** 2026-11-09 · **Hours actually spent (be honest):** 18 · **Language / storage:** Python 3 / one SQLite file (WAL mode)

---

## 0. TL;DR

- One process (`./run.sh <SIM_URL>`) ingests every event into SQLite, catches hallucination spikes and bad caller IDs, runs fixed-split welcome × voice experiments, and changes config only through approvals or policy-permitted auto-apply. `controller why` explains any change from its own store.
- public_01: conclusion rate **+9.3%** over the no-op baseline (0.406), 16 changes applied, 10 beneficial, 2 harmful, 0 expired or lapsed.
- public_02: conclusion rate **+24.8%** (0.413), hallucinated calls 204 against 431, 3 of 200 credits spent, 0 LLM calls. The supervisor's bad prompt was fixed 1,455 minutes after it shipped: it was detected after that day's request window closed and waited overnight.
- The honest part: most of the lift is caller ID and avoided harm. Engagement headroom captured is 7% and 25%, and no experiment winner went live on either scenario (one was confirmed, then an approver declined it).
- Not built: per-person call time, cross-tenant fairness (C15), merging unrelated changes into one request, a UI.

## 1. Assumptions

1. Welcome message and voice are heard after pickup, so they cannot move pickup. This is why `nw_kyc` (pickup-primary) gets no welcome or voice experiments (ADR-0007).
2. The `hallucination_flag` detector's error rate is stable over time, so a baseline from history stays valid during the run.
3. A caller ID's health is a property of the number across the workflow, not of a cohort.
4. A tenant's preference overlay is what its memo says, stored as code-level data keyed by tenant ID. Only `kestrel_finance` has one. A hidden scenario's other tenants run on their API policy alone.
5. Anita's "two requests a day" means a rolling 24 hours, the same way the platform counts `max_requests_per_24h`.
6. `cohorts` counts as customer-facing, because cohorts decide who hears which variant (ADR-0002).
7. "Named human approval" is met by the platform's approval path. The approval record carries an ID, status, time and reasons, and no approver name, so the store can't show one (see §3.02).
8. "Tell me before anything changes" is met by asking for approval, except when moving traffic off a degraded caller ID. There is no notification channel in the API, so that exception shows up afterwards in `controller status`.
9. The weekly summary Compliance asks for is a command (`controller report --weekly`), not a sent email, for the same reason.
10. A request in an approver's last 2 hours would likely sit overnight, so none are sent then. This is a judgement. `nw_kyc`'s latency reaches 480 minutes, so even this doesn't guarantee a same-day answer there.
11. A 10% relative lift in engagement is the smallest effect worth confirming at this traffic. A smaller minimum needs more days than the scenarios have.
12. Validation pass rate is a necessary check on a prompt, and says nothing about engagement. The API states this.
13. A prompt change within 24 hours before a spike is taken as its cause (ADR-0014). A wrong blame costs one validation run that still has to pass.
14. A simulator restart is a new world. The controller refuses to resume on a mismatched scenario or a clock that went backwards. `run.sh` moves the old store aside instead, so no manual step is needed.
15. Pre-D0 history ran on the default config, as `docs/SIMULATOR.md` says, so history can't compare variants. It is used for baselines, cohorts and hours only.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load scale | Built | Every decision is only as good as this. About 43k events/s, one transaction per chunk including the cursor. | Parallel ingest past one SQLite writer (§8). |
| C2 | Joins and one shared cohort object | Built | Product's definition, and needed by harm checks and call time. Disjoint partition with stable IDs (ADR-0004). | — |
| C3 | Bootstrapping from history | Built | Baselines, partition and hour rates come from pre-D0 data before any decision. | — |
| C4 | Call-time optimisation | Partial | Per-cohort only, with a held-out replay and before/after rollback. Per-person cut. On public_01/02 the replay found under 3% gain on every workflow, so no table was proposed. | Per-person needs a per-person store (ADR-0003 discards this) and a repeat-contact signal. |
| C5 | Caller-ID health | Built | Largest source of lift on both scenarios. Detection, capped exploration, shift. | — |
| C6 | Welcome × voice experiments | Built | The primary KPI for two of three workflows. Screen then confirm (ADR-0006). | — |
| C7 | Promotion, decay, rollback | Built, partly unproven | Holdback, rolling test and rollback exist and pass tests with injected decay. No public scenario reached a real rollback. | A scenario where a promoted winner decays. |
| C8 | Hallucination incident response | Built | The most direct customer harm. Detect, validate, propose, verify, escalate. | An engagement check after a fix (§13). |
| C9 | Governance client | Built, batching partial | Expiry, lapse, stale, re-proposal, overlay, approver timing and auto-apply all work. Batching is one in-flight Proposal per workflow, welcome and voice together, and holdback release riding along. Unrelated changes are still separate requests. | A general merge step in front of `propose`. |
| C10 | Honest impact, affected leads, dry runs | Built | Product's "must mean something". Screens score 0, since they claim nothing. | — |
| C11 | Supervisor view | Built (CLI) | Anita's "one screen". `controller status <workflow>`. | A web page over the same query. |
| C12 | Decision and audit store | Built | Mandatory, and what the review will test. | — |
| C13 | Crash recovery | Built | SRE's non-negotiable. `scripts/crash_recovery.py`. | — |
| C14 | Observability of the loop | Built | `controller health` and four pages. | Real pager integration. |
| C15 | Credit and approver fairness across tenants | Cut | Credits don't bind: 3 of 200 used on each public scenario. Per-tenant caps already protect approvers. | A coordinator that hands out credit allowances (§8). |

**The principle.** I built in the order a failure would hurt. First, what breaks a constraint or loses trust if wrong: governance, audit, crash safety. Second, what the primary KPI depends on: experiments and caller-ID health, which need ingestion, cohorts and history underneath. Third, anything that stops an active harm: incidents. I cut what couldn't change a decision on the traffic we have. Per-person time is the clearest case: per-cohort tuning already found no 3% gain, so finer tuning wasn't the next best use of time. Fairness solves a shortage that didn't appear.

## 3. Stakeholders

I checked each memo against `GET /workflows/{id}` on the public scenarios. Relevant policies:

| | `nw_renewal` | `nw_kyc` | `ks_cancel` |
|---|---|---|---|
| Primary KPI | engagement | pickup | engagement |
| Auto-apply | schedule, caller_id; max_shift 0.5 | **disabled** | schedule, caller_id, welcome, voice; max_shift 0.2 |
| Mid-flight | allowed | **next_campaign_only** | allowed |
| Approver hours | 10–19 | 10–18 | 10–19 |
| Requests per 24h | 3 | 2 | 4 |
| Prompts offered | P1, P3, P4, P5 | P1, P3 | P1, P3, P4 |

### 01 Sales (Nikhil Rao)

**Accepted.** Per-client numbers, reported per KPI (`reports/`). Few requests: one in-flight Proposal per workflow, and a peak of 3, 2 and 2 requests in 24 hours on public_01.

**Pushed back or declined.**
- A single blended score. It breaks a README constraint and Product's definition.
- "Winning by lunch, live by evening." A confirm needs its planned fresh sample (about 1,200–1,900 connects per arm), looks once a day, and then needs an approver. Early stopping is what produces false winners.
- Pushing winners straight through Kestrel. Declined by ADR-0002 and by Anita's own memo.
- Rolling out Persuasive (P4). Prompts change only through incident response, with validation for that workflow. A demo for another tenant isn't evidence.

**Wrong against the policy.**
- "Kestrel has turned on auto-apply for everything." `ks_cancel` excludes `prompt_version` and `cohorts`. Its `max_shift` is 0.2, and a 50/50 to 80/20 promotion moves 0.3, so a promotion couldn't auto-apply anyway.
- "Northwind is fine with auto-apply on KYC." `nw_kyc` has `auto_apply.enabled: false` and no allowed dimensions.
- "Live for everyone by evening." `nw_kyc` is `next_campaign_only`: allocations are locked per campaign, so running campaigns keep the old mix for up to 3 days.
- "Wherever it's available." P4 isn't an option on `nw_kyc`.

**Reply:**

> Nikhil, you'll have numbers for the summit: pickup and engagement per client, shown separately, because a blended score breaks a platform rule. I can't push winners straight through. Kestrel's auto-apply excludes prompts and caps any shift at 20%, and Northwind's KYC flow has auto-apply switched off, whatever was said at the QBR. A winner needs confirmation on fresh data and a client approver, so "by evening" isn't on offer. Persuasive won't roll out on another account's demo; prompts change only with validation for that workflow. The story I'd tell on stage: every change is explained and approved by the client. In simulation, a normal scenario raised pickup 6% and engagement 2%.

### 02 Client Compliance (Meera Iyer)

**Accepted.**
- Customer-facing dimensions are never auto-applied (`registry.py`, ADR-0002), even where policy would allow it.
- Every prompt Proposal carries a validation run ID. The platform enforces this too.
- `controller heard <lead_id>` answers the auditor's question from the exposure log, down to variant, config version, who set it and the Proposal behind it (§10). The reason printed is that version's latest change, so an option set by an earlier version takes a second lookup (§13.15).
- Auto-applied operational changes are listed by `controller report --weekly`.
- Requests go out only inside approver hours, less the last 2. Cohort harm blocks promotion.

**Pushed back or declined.**
- "Even briefly." Any experiment exposes some customers to a challenger that may be worse. I limit it (control keeps at least half of a screen), block on cohort harm (family-wise α 0.10 across cohorts), and require named approval to start. On a cohort of about 15% of traffic the check only sees large harm, so it can't prove there was none.
- "Named" approval is met only to the platform's level. See below.

**Wrong against the policy.**
- Operational settings being "covered by the policy we signed" holds on `nw_renewal` only. `nw_kyc` has auto-apply disabled, so every KYC caller-ID change went through approval (all `controller_approved` in the reports).
- Approver hours of 10–19 and 10–18 match the API exactly.
- Not wrong but worth saying: the platform's approval record has no approver name. The store shows the approval ID, decision time and reasons.

**Reply:**

> Meera, your conditions are built in. Anything a customer hears goes to your approver, and script changes carry a validation run. We can show what any customer heard, down to the variant, config version and approval behind it, within minutes. Requests go out only in your approvers' hours, less the final two. Auto-applied operational changes are listed weekly, and on KYC nothing auto-applies, since that policy has it switched off. Two limits, plainly. Any experiment exposes some customers to an untested variant: control keeps half the traffic and a harm check blocks promotion, but it can't prove no small group was hurt. And our records show the approval, not the approver's name; that must come from the platform.

### 03 Platform & SRE (Kabir Menon)

**Accepted.**
- The kill test (`scripts/crash_recovery.py`, three SIGKILLs, §9).
- State is committed before the action it announces, and Proposal IDs are deterministic.
- Four page conditions, each naming what to look at (ADR-0015).
- `controller health` for lag, decisions per hour and credits.
- No new infrastructure: an embedded SQLite file (ADR-0001).

**Pushed back or declined.**
- SQLite is still a store to back up. There's no service to run at 2 a.m., and production moves the same schema to his Postgres. Redis isn't needed, because working state is rebuilt from the store.
- "Idempotency everywhere" can't be literal: `/validation/runs` and `/llm/suggest-fixes` take no key, so a crash between the response and the commit pays once more (ADR-0014).

**Wrong against the policy.** Nothing in the memo contradicts the contract. Two things I couldn't confirm or should caveat: I can't verify from here that Postgres and Redis exist, and I measured on a 10-core Mac rather than his 4 vCPU box (§8). His 21:30 deploy falls after every dialling window and approver day in the public scenarios, so a restart then is the quiet case.

**Reply:**

> Kabir, the kill test is in reports/: three SIGKILLs during public_02, no duplicate proposals, no double applies, nothing lost, conclusion rate unchanged at 0.413. Everything commits before it acts. There are four page conditions, and each says where to look. `controller health` shows lag, decisions and credits. The store is one SQLite file, so no new service, and production moves the schema to your Postgres. The slowest load day took 103 s against your 600 s, measured on a 10-core laptop, so I'll re-measure on 4 vCPU. At 10× the decide step breaks first. One exception on idempotency: validation and LLM calls take no key, so a crash at that instant can pay twice.

### 04 Client Operations Supervisor (Anita Desai)

**Accepted.**
- Her overlay: at most 2 requests per 24 hours, none before 11:00, no auto-apply except moving off a degraded caller ID. Peak on `ks_cancel` was 2 on public_01. On public_02 it was 3, because the incident prompt fix went out under the policy's cap of 4 and says so in its summary.
- Her edits are preserved. Configs are built on the current config, and a base that moved makes a pending Proposal stale. Any Proposal revising her edit says so ("Reverts the D3 11:30 supervisor edit P1 -> P2 because…").
- One screen: `controller status ks_cancel`.

**Pushed back or declined.**
- A hallucination spike after her edit does get a request to revert it. It's never silent, and her approver decides. On public_02 they approved it.
- That one kind of request may use the policy's hours and cap rather than hers, and says so in its summary.
- A degraded caller ID is moved without asking first, one capped step per day. On public_02 that happened once (D1 14:45).
- If her approver rejects a move, auto-apply on caller ID stops for 24 hours, and the controller asks again instead. On public_02 that left D2 with its remaining traffic until an approver said yes at D3 17:00.
- "One request with five things in it" is only partly built (C9).

**Wrong against the policy.** Nothing she claims is false. Three facts are useful:
- Her 2/day is stricter than the platform's 4/day. It's honoured as an overlay, not a platform rule.
- Kestrel's signed policy allows auto-apply on welcome and voice. She asks for more care than the contract does, and she gets it.
- "Word for word" is Strict (P5). `ks_cancel` isn't offered P5, so it can't reach her line. It is offered on `nw_renewal`.

**Reply:**

> Anita, you'll get at most two requests a day, none before 11:00. Your edits stay: we build on your config, and any proposal changing one says so. Your one screen is `controller status ks_cancel`. Three exceptions. If a caller ID goes bad, we move traffic off it without asking, then show it on your screen. If your approver declines a move, we stop and ask again. If a hallucination spike follows your prompt change, we ask to revert it, naming your edit, possibly before 11:00 or past your two a day. And "five things in one request" is only partly built. The word-for-word prompt isn't offered on your line.

### 05 Product (Tara Kulkarni)

**Accepted.**
- Pickup and engagement stay separate in statistical checks, Proposals, reports and the CLI.
- One cohort object with a stable ID, partitioned (ADR-0004).
- Every Proposal has a diff, affected leads, dry run and impact score. `controller status` sorts pending requests by impact.
- Expired means rebuilt from fresh data, never resubmitted.
- Calls are attributed by the `config_version` they ran under, so an arm that never ran counts for nothing.
- A winner changes only its own workflow.
- Her open questions answered: cohorts at least 10% of traffic, one feature per workflow; a 200-connect screen and a confirm sized for a 10% relative lift. The cost is in §5.

**Pushed back or declined.**
- Per-person call time, despite P0. It needs per-person history we deliberately don't keep (ADR-0003). `lead_preferred_hour` also multiplies every other hour by 0.25, so a wrong guess costs dials. Per-cohort tuning already found no 3% gain on any of the three workflows.
- The other two P0s are built. Not all three could be.

**Wrong against the contract.** "Seniors in Tamil Nadu" can't be a cohort: leads carry `lang`, `city_tier`, `age_band` and `value_band`, with no state. The closest is age 60+ and `lang` = `ta`. Everything else matches the API, including the definition of engagement.

**Reply:**

> Tara, KPIs stay separate, cohorts are one shared object, and every proposal carries a diff, affected leads, a dry run and an impact score. Expired proposals are rebuilt from fresh data, and arms that never ran teach nothing. On your open question: cohorts of at least 10% of traffic, one feature per workflow. Confirming a 10% lift takes about 1,200–1,900 connects per arm. That keeps false winners at or below 5% per dimension, and it misses smaller lifts. The supervisor view and caller-ID health are built. I declined per-person call time: per-cohort tuning found under 3% gain on all three workflows, and a wrong personal guess costs dials.

### 06 CTO (Aryamann)

**Accepted.**
- Explainability over size: `controller why`, and no LLM in any decision about what won.
- A cohort harm check before any promotion.
- Compute spent where it changes a decision: known-good prompt first, 3 credits on each public scenario, 0 LLM calls.
- An honest account of what breaks first (§8), the choices above, and docs for whoever gets paged (README §11).

**Pushed back or declined.**
- "An improvement that hurts a cohort is worth less than nothing." Accepted as the rule, with the limit stated: the check has low power on a small cohort.
- "Hundreds without an engineer" isn't met yet. Measured to 12 workflows; the single-threaded decide step reaches the 10-minute budget near 120.

**Wrong against the contract.** Nothing. His warning is correct and §3.01, §3.02 and §3.05 show where. His claim that credits are cheap here also holds: 200 available, 3 used at most.

**Reply:**

> Aryamann, I chose. Built: ingestion, cohorts, experiments with a stated false-winner rate, incidents, governance, audit, crash recovery. Cut: per-person call time and cross-tenant fairness. I said no to Sales on speed and auto-apply, and to Product on one P0. Results: conclusion rate +9% on public_01 and +25% on public_02, mostly harm avoided, for 3 credits. Honest misses: the lift is mostly caller ID, no winner went live on either scenario, and rollback is proven only with induced decay. The decide step breaks first, near 120 workflows. Challenge this one: a prompt incident found after the request window closed waited overnight, 1,455 minutes to fix.

## 4. Architecture

```
                         SIMULATOR (HTTP only)
   /sim/advance  /events/chunks  /workflows  /approvals  /apply  PUT /config  /validation  /llm
        ▲              │              │            ▲       ▲          ▲            ▲
        │              ▼              ▼            │       │          │            │
 ┌──────┴──────────────────────────────────────────┴───────┴──────────┴────────────┴─────┐
 │  simclient.py  the only HTTP code. Retries 5xx/timeouts; 4xx = a recorded outcome      │
 └──────┬───────────────────────────────────────────────────────────────────┬─────────────┘
        │ chunks                                                            │ proposals
        ▼                                                                   │
 ┌─────────────┐  one transaction per chunk,      ┌───────────────┐         │
 │  ingest.py  │  including the cursor            │  registry.py  │         │
 └──────┬──────┘                                  │ policy+overlay│         │
        ▼                                         └───────┬───────┘         │
 ┌───────────────────────────────────────┐                │                 │
 │ store.py  state/controller.db (WAL)   │                ▼                 │
 │ cell_agg · flag_ticks · exposures     │        ┌───────────────┐         │
 │ leads · world_events · cohorts        │◀──────▶│ governance.py │─────────┘
 │ proposals · decisions · experiments   │        │ state machine,│
 │ incidents · alerts · run (cursor)     │        │ timing, apply │
 └───────┬───────────────────────────────┘        └───────▲───────┘
         │ read                                           │ Drafts
         ▼                                                │
 ┌────────────────────────────────────────────────────────┴──────────┐
 │ loop.py, each 15-minute tick, per workflow, in this order:         │
 │  reconcile → hallucination.py → callerid.py → experiments.py       │
 │            → schedule.py → callerid explore → alerts.py            │
 │ Pure helpers: stats.py · cohorts.py · composer.py · replay.py      │
 └───────────────────────────────────────────────────────────────────┘
         │
         ▼  read-only
 queries.py + __main__.py:  why · heard · status · health · report --weekly
```

**Where state lives.** Everything durable is in one SQLite file. In memory there is only the workflow list, the last simulator state and a cache of active campaigns' leads, all rebuilt on start.

**On restart.**
1. Check the store's scenario and clock against the simulator, or refuse.
2. Warm the lead cache from the store.
3. Ingest every chunk after the cursor.
4. Reconcile every in-flight Proposal against `/approvals` and config history before the next tick.

## 5. The learning loop

**Cohorts.**
- One feature per workflow is chosen from history only: the one whose values differ most on the primary KPI. On the public scenarios that is age band on the two engagement workflows (12–15 points) and city tier on `nw_kyc` (5–6 points).
- Cohorts under 10% of traffic are merged into their nearest neighbour. The largest is `default`. Cohorts are disjoint, so the simulator's first-match order can't disagree with our analysis.
- Cohorts reach a config only when an allocation or hour table needs them.
- *Why 10% and one feature.* At about 1,000 connects/day, a 10% cohort has about 100 connects/day. A confirm needs 1,200–1,900 per arm, so a cohort-specific winner would take over three weeks. Cohorts therefore check harm and tune hours; they don't get their own winners (ADR-0006).

**Experiment design.**
- Fixed split, no bandit: every reallocation needs an approval (ADR-0005).
- A **screen** runs all challengers at once with control on at least 50% of each dimension, checked daily from 09:00 until the smallest arm has 200 connects (about two days). Challengers clearly worse than control (one-sided α 0.05) are dropped. The best remaining with a positive point estimate goes forward. Nothing is promoted from a screen.
- A **confirm** tests one candidate per dimension at 50/50, on fresh data only.
- Exploitation is the promotion to 80%.
- *Two dimensions.* Welcome and voice are sampled independently, so one split gives a factorial design and each call informs both. I analyse main effects and don't estimate the interaction. Two winners promoted together could interact badly, and only the decay check would see it.

**Deciding a winner.**
- A one-sided two-proportion z-test, combined across config versions with Mantel-Haenszel weights, so a change elsewhere can't pass for a difference between arms.
- Stopping: at most 3 daily looks, each only once every arm has half the planned sample. α 0.05/3 per look, so Bonferroni over looks.
- *Harm check.* Holm's method across the workflow's cohorts at family-wise α 0.10 (ADR-0016), on every look. With no real effect it blocks about 10% of the time per look and about 20% over the 3 looks (simulated). Real harm stops a confirm at once.
- Screening leaves one comparison per dimension. Tests simulate null experiments and check the rate stays within sampling error of 5%.
- **False-winner rate: at most 5% per dimension**, about 10% for a Proposal promoting two (ADR-0011).
- *The cost Product asked about.* Minimum detectable effect is 10% relative at 80% power, so a real lift of a few percent usually ends inconclusive. That is the trade for a false-winner rate I can state.

**Decay and rollback.** A promoted winner keeps a 20% holdback on control. Daily from the third calendar day after promotion, a 72-hour one-sided test (α 0.05) compares winner with holdback. A signal sends a rollback Proposal. The fastest detection is about 3 days after promotion, plus up to a day for approver hours. No public scenario promoted a winner, so this has run only against injected decay in tests.

**Call time and caller ID.**
- *Call time:* per-cohort hour tables from attempt-adjusted history, clipped to [0.5, 2], accepted only if a replay on days the table wasn't fitted to predicts at least 3% gain with the guardrail holding (ADR-0013). It found `no_gain` on all three public workflows, because hours that are better for everyone can't be given to anyone in particular.
- *Per-person:* declined (§3.05).
- *Caller ID:* a number with under 300 dials in 7 days gets at most 10% of traffic until it has 300, then an equal share if it's at least as good, or none. A number is degraded when it's at least 15% below the workflow's others, one-sided p < 0.001, on at least 300 dials over 24 hours.

**Guardrail.** A candidate that wins the primary KPI must also pass non-inferiority on the guardrail: the 95% lower bound of the difference must not fall below `-max_drop` (0.02 here). It stays undecided until the third look, then blocks promotion, because a bound below the margin can simply mean too few dials. On public_01 `nw_renewal`'s W3 passed with a lower bound of −0.009.

## 6. Governance

**Proposal lifecycle.**

```
 approval route   submitting ──▶ pending ──▶ approved ──▶ applying ──▶ applied
                                  │  │                      │
                     rejected ◀───┘  └───▶ expired          └──▶ lapsed
 auto route                       applying ──▶ applied | refused ──▶ falls back to approval
 either route     any in-flight state ──▶ stale (the base version moved)

 expired | stale   ──▶ a NEW Proposal, rebuilt from fresh data, with `replaces` set
 rejected | lapsed ──▶ the same, except inside an experiment, which aborts and returns to control
```

`submitting` and `applying` may or may not have reached the platform, so neither is acted on without first asking it. That single rule gives the crash safety in §9. A rejection waits 24 hours before the same kind is asked again (2 hours for an incident), and it stops auto-apply on the dimensions it covered for 24 hours, so a person's no can't be routed around. An expiry has no cooldown, because nobody said no.

**Experiment lifecycle.**

```
 planned → screen_requested → screening → screened → confirm_requested → confirming
         → confirmed → promote_requested → promoted → released
                                              └─ decayed → rollback_requested → rolled_back
 any live state → inconclusive | aborted (supervisor edit on a tested dimension, rejection, lapse)
```

**Incident lifecycle.**

```
 open → verifying → verified
 open → escalated (live prompt passed validation, no candidate passed, or out of credits)
 verifying → escalated (flag rate did not recover)
 open | verifying → resolved (someone else changed the prompt first)
```

**Batching and timing.**
- One in-flight Proposal per workflow, with incident fixes exempt.
- Requests go only inside effective approver hours, less the last 2.
- The cap is counted on the platform's own timestamps, tightened by the overlay.
- An approval is applied on the first tick after it's seen, inside the 2-hour window.
- Results on both scenarios: **0 expired, 0 lapsed, 0 apply conflicts**. Peak requests stayed inside every platform cap. `ks_cancel` kept to Anita's 2, except on public_02 where the incident prompt fix used the policy's limits, as user story 44 allows, and reached 3.
- Batching beyond this is partial (C9).

**Auto-apply.** I use it only for operational dimensions and only where policy allows. I voluntarily don't use it on `ks_cancel`'s welcome and voice, even though the policy allows it within 0.2: a regulated client's customers hear those, so a person should say yes (ADR-0002). The cost is slower experiment starts. Kestrel additionally gets no auto-apply except moving off a degraded caller ID. Nothing auto-applies on a dimension for 24 hours after an approver rejects a change to it (ADR-0009).

**Impact, affected leads, dry run.**
- *Impact* is the lower 80% bound of the gain in concluded calls per dial × affected traffic share ÷ 5 points, clipped to [0, 1]. Incident fixes are floored at 0.9, because hallucination's cost isn't a change in conclusion rate. A first screen scores 0 since no one has heard the options. It understates conclusion-per-lead effects, the conservative direction.
- *Affected leads* counts active campaigns of affected cohorts, honouring `next_campaign_only`, plus the next campaign's expected size.
- *Dry run* replays the last 72 hours under both allocations, with 95% margins and cohorts at risk. It can only see changes of mix. It predicts nothing for a prompt change and says so.

**Supervisor edit mid-experiment.** On public_02, a supervisor changed `ks_cancel`'s prompt at D3 11:30 during a confirm. A prompt isn't under test, so the edit became a separate stratum and the confirm carried on. The caller-ID request pending at that moment went stale and was rebuilt on the new base (`ks_cancel-caller_id-3`, then `-4`). An edit to a dimension under test aborts the experiment, and only the untouched dimensions go back to control, so the edit stands (ADR-0010; covered by tests). A dimension a supervisor changed is left alone for 7 days.

## 7. Incidents

**Hallucination detection.** The last 2 hours, grown back up to 24 hours until it has at least 150 connects (ADR-0016), then a flag rate at least 2× baseline, and exact one-sided binomial p < 0.001, per live prompt, every tick.

**Expected false positives.** Overlapping windows are correlated, so 48 dialling ticks × 0.001 gives an upper bound of about 5% per workflow-day. Observed in these runs: **one** false incident in 54 workflow-days, on `nw_kyc` on public_01 (D6 15:15, 9 flags in 154 connects over 2.5 hours). The live prompt then passed validation at 98.3%, so it was handed to a person, nothing changed, and it cost 3 credits. Reaching back for 150 connects made that workflow testable at all; this is the price.

**Picking a fix.**
1. If a prompt changed in the last 24 hours, validate the last known-good prompt.
2. If nothing changed, validate the live prompt. If it passes, nothing says a change would help: escalate.
3. Only if those fail, call `/llm/suggest-fixes` and validate its candidates.

Only candidates meeting the policy's minimum pass rate qualify, and the highest wins. An LLM suggests; it never decides.

**Credits.** A reserve of 30 is kept for incidents. Validation at n=300 costs 3. public_01 and public_02 each used 3 credits, and neither called the LLM.

**Verifying.** After apply, wait for 300 connects on the fix. It's verified if the rate is under the raising level and not significantly above baseline (one-sided binomial p ≥ 0.05). If not, it's escalated and that prompt is ruled out.

**Timeline from public_02 (`ks_cancel`).** The controller's times are from its store. The two grader lines are from `reports/public_02.md`.

| Sim time | What happened | Evidence |
|---|---|---|
| D1 14:45 | Caller ID D2's pickup is low. One capped auto-applied step moves weight off it (incident exception to Kestrel's overlay). | 22.0% against 26.0%, 1,980 dials |
| D2 10:00 | Grader: D2 is flagged by the world. | `did_flagged` |
| D2 14:30 | Remaining move sent for approval, because one auto step per rolling day was used. | 18.8% against 24.5%, 607 dials |
| D2 16:45 | Approver rejects it (judgement call). Auto-apply on caller ID is now blocked for 24 hours, so D2 keeps its remaining share. | `ks_cancel-caller_id-2` |
| D3 11:15 | Asked again, in the first slot the request hours and Kestrel's cap of 2 allow. | 9.7% against 25.7%, 585 dials |
| D3 11:30 | Supervisor edits the prompt P1 → P2 (version 7). The pending caller-ID request goes stale. | `config_changed`, actor `supervisor` |
| D3 14:45 | Caller-ID request rebuilt on the new base. | `ks_cancel-caller_id-4` |
| D3 17:00 | Approved and applied (version 8). D2 at zero from here. | Grader: D2 share 12.8% on day 2, 8.3% on day 3 |
| D3 18:15 | Incident raised. P1 validated at 98.67% (n=300, 3 credits). Held: requests stop at 17:00 (policy hours, less the last 2). | 10 flags in 194 connects, 5.2% against 1.5%, p = 0.00088 |
| D4 10:00 | Revert sent, naming the edit, under the policy's hours rather than Kestrel's 11:00, and saying so. | `ks_cancel-hallucination-1` |
| D4 11:28 | Approver approves. | `apr_00012` |
| D4 11:45 | Applied at the next tick (version 9). **1,455 minutes after onset**. | Grader `mttr_minutes: 1455` |
| D4 14:30 | Verified. | 5 flags in 330 connects, p = 0.57 |

Hallucinated calls: 110 on `ks_cancel` against 348 for the baseline. Flags are sparse, so the tick that crosses p < 0.001 moves with chance. In an earlier run of the same detector it crossed at D3 14:30, inside the request window, and the fix landed in 285 minutes. The window is the sensitive part.

## 8. Scale

Measured by `scripts/measure_load.py` on public_03_load (12 workflows, about 650k dials/day, 5 days). Full method in `reports/public_03_load.md`. Re-measured after the last controller change.

| Measure | Result |
|---|---|
| Ingest throughput | about 43,100 events/s (4.2M live events) |
| End-to-end decision lag per tick | p50 0.79 s, p95 1.70 s, max 7.1 s, against a 15-simulated-minute tick |
| Peak memory (controller) | 424 MB; simulator 640–756 MB |
| Wall-clock per simulated day (controller) | 69–103 s; budget 600 s |
| `./run.sh` end to end | exit 0, 537 s for 5 days, including a one-off history catch-up of about 60 s |

Measured on a **10-core / 16 GB Mac, not 4 vCPU / 8 GB**. The controller uses about one core and the simulator is a separate process, so I expect the same result. It needs re-measuring before being quoted as met.

**What breaks first at 10×** (estimates):
1. **Decide**: 54–82 s/day today, serial per workflow per tick. About 540–820 s/day at 120 workflows, at or past the 10-minute budget.
2. **Ingest**: about 200 s/day. It shares one thread with decide and one SQLite writer.
3. **Memory**: active-lead cache, a few GB at most. The `exposures` table would dominate disk and need partitioning.

**Partitioning.** By tenant first: one process and one store per tenant, since tenants share no leads, cohorts or experiments. The only shared resource is credits, which a small coordinator would hand out. Within a big tenant, decide shards by workflow across a process pool without a schema change, and is the first thing I'd parallelise. Ingest has to stay a single ordered reader writing per-shard stores.

## 9. Reliability

**Crash-recovery test** (`scripts/crash_recovery.py`, log in `reports/crash_recovery_public_02.md`). An uninterrupted run sets the reference. Against a fresh simulator, the controller is SIGKILLed at three random points and restarted with the same `./run.sh`.

| Check | Result |
|---|---|
| Run completed after 3 SIGKILLs (two with a Proposal pending) | PASS |
| No duplicate Proposal IDs (store and platform) | PASS |
| No approval applied twice (config history) | PASS |
| No pending Proposal lost | PASS |
| Conclusion rate within noise | PASS (0.413 and 0.413) |

**What's lost.** Nothing committed. The tick in progress is redone from the cursor. Only a crash between a paid response and its commit costs anything: one validation or LLM call repeated.

**Idempotency keys.**

| Action | Key or mechanism |
|---|---|
| Ingesting a chunk | One transaction with the cursor |
| `POST /approvals` | `{workflow}-{kind}-{seq}`, committed first; a 409 duplicate is reconciled |
| Apply / auto-apply | Intent committed first; config history checked by `approval_id` or `proposal_id` before any attempt |
| `POST /sim/advance` | The clock is re-read and only the remaining distance requested |

**When the simulator returns an error.**

| Response | Behaviour |
|---|---|
| 5xx, timeout | Reads and `POST /approvals` retry 5 times with backoff. Apply, auto-apply, LLM and validation get one attempt, because a blind retry could apply or pay twice. A failed apply is checked in config history next tick. |
| 409 `stale_base_version` | Proposal marked stale and rebuilt on the new base |
| 409 `duplicate_proposal_id` | Reconciled as already sent |
| 410 | Lapsed, and pages |
| 403 `auto_apply_not_permitted` | Recorded as refused, falls back to approval |
| 402 | Incident escalated `out_of_credits` |
| A chunk fails | 3 attempts. Then nothing new is decided, reconciling continues, and the next tick retries. Over 2 ticks behind, it pages. |

One workflow's exception is recorded as an `error` note and doesn't stop the others.

## 10. Audit & observability

**"Why did `ks_cancel` change at 11:45 on D4, who approved it, and what happened after?"** Run against the public_02 store:

```
$ python -m controller why ks_cancel --at "D4 12:00" --db state/public_02.db
ks_cancel at D4 12:00: config version 9
  change      controller_approved at 2027-01-08T11:45:00+05:30; changed prompt_version
  proposal    ks_cancel-hallucination-1 (applied): [Incident response] Reverts the D3 11:30 supervisor edit P1 -> P2 because 5.2% of connects flagged against a 1.5% baseline (10 of 194 connects). Back to known-good P1 passed validation at 98.7% (n=300). Sent under the policy's own limits instead of the client's preference (outside request hours (11:00 to 19:00, less the last 2 hours)), because customer harm outranks it.
  rationale   The flag rate on P2 is at least 2x the workflow's baseline over 2 hours and 194 connects (one-sided binomial p = 0.00088), from about D3 11:30. Sent under the policy's own limits instead of the client's preference (outside request hours (11:00 to 19:00, less the last 2 hours)), because customer harm outranks it.
  impact      score 0.90, 6,000 leads affected
  diff        {"prompt_version": {"from": "P2", "to": "P1"}}
  dry run     {"engagement_rate": {"current": 0.4989, "margin": 0.0177, "proposed": 0.4989}, "pickup_rate": {"current": 0.2556, "margin": 0.0078, "proposed": 0.2556}}; cohorts at risk: none
  evidence    {"detection": {"baseline_flag_rate": 0.015242242787152967, "prompts": {"P1": {"connects": 176, "flags": 1, "p_value": 0.9330152011581303, "spike": false, "window_minutes": 510}, "P2": {"connects": 194, "flags": 10, "p_value": 0.0008808494823012243, "spike": true, "window_minutes": 120}}}, "detector": "hallucination_spike", "incident_id": "ks_cancel-hallucination-incident-1", "validation": {"n": 300, "pass_rate": 0.9867, "prompt": "P1", "source": "known_good"}, "validation_run_id": "val_00001"}
  approval    apr_00012: approved by the client's approver at 2027-01-08T11:28:00+05:30
  aftermath   this version: 10,100 dials, pickup 25.5%, engagement 49.7%; pickup by caller ID D1 24.0%, D3 27.1%
              version before: 2,700 dials, pickup 25.2%, engagement 47.1%; pickup by caller ID D1 24.4%, D3 26.0%
              next change: version 10 by controller_approved at 2027-01-10T13:00:00+05:30
  experiment  ks_cancel-exp-1: confirming (confirm_look at D4 09:00); voice V1 against V2, V3, V4; welcome_message W1 against W2, W3; candidates voice V2, welcome_message W2; confirm looks 1
  incident    ks_cancel-hallucination-incident-1: verified; P2 flagged from D3 11:30, detected D3 18:15; fix P1 applied D4 11:45, 1455 minutes after onset; ended D4 14:30: flag_rate_recovered
  decisions
    D0 00:15  experimentheld                    (outside request hours (11:00 to 19:00, less the last 2 hours))
    D0 11:00  experimentplanned               
    D0 11:15  experimentscreen_requested      
    D0 12:45  experimentscreening             
    D1 09:00  experimentwaiting_for_data      
    D2 09:00  experimentscreened              
    D2 09:00  experimentconfirm_held            (outside request hours (11:00 to 19:00, less the last 2 hours))
    D2 11:00  experimentconfirm_requested     
    D2 12:45  experimentconfirming            
    D3 09:00  experimentwaiting_for_data      
    D4 09:00  experimentconfirm_look          
    D4 10:00  proposal  recorded              ks_cancel-hallucination-1
    D4 10:00  proposal  sent                  ks_cancel-hallucination-1
    D4 10:00  hallucinationproposed              ks_cancel-hallucination-1
    D4 11:00  caller_id healthy               
    D4 11:15  caller_id healthy               
    D4 11:30  caller_id healthy               
    D4 11:45  proposal  approved              ks_cancel-hallucination-1
    D4 11:45  proposal  apply_intent          ks_cancel-hallucination-1
    D4 11:45  proposal  applied               ks_cancel-hallucination-1
    D4 11:45  hallucinationfix_applied           ks_cancel-hallucination-1
    D4 11:45  caller_id healthy               
    D4 12:00  hallucinationverifying             
    D4 12:00  caller_id healthy               
```

The platform's approval record carries no approver name (§3.02). "What did this lead hear, and why?" The `because` line gives that config version's latest change, which may not be the one that set the option heard (§13.15):

```
$ python -m controller heard ks_cancel:l00022513 --db state/public_02.db
ks_cancel:l00022513  (workflow ks_cancel, 6 calls)
  2027-01-05T09:40:00+05:30  attempt 1  voicemail
    heard   welcome_message W2, voice V2, caller_id D2, prompt_version P1 (cohort default)
    config  version 3, live since 2027-01-04T14:00:00+05:30, set by controller_approved, Proposal ks_cancel-caller_id_explore-1, Approval apr_00004
    because Try caller ID D3 on 10% of traffic until it has 300 dials: exploration, not a claimed win.
  2027-01-05T12:24:00+05:30  attempt 2  no_answer
    heard   welcome_message W1, voice V1, caller_id D1, prompt_version P1 (cohort default)
    config  version 3, live since 2027-01-04T14:00:00+05:30, set by controller_approved, Proposal ks_cancel-caller_id_explore-1, Approval apr_00004
    because Try caller ID D3 on 10% of traffic until it has 300 dials: exploration, not a claimed win.
  2027-01-06T13:15:00+05:30  attempt 3  no_answer
    heard   welcome_message W2, voice V1, caller_id D1, prompt_version P1 (cohort default)
    config  version 6, live since 2027-01-06T12:45:00+05:30, set by controller_approved, Proposal ks_cancel-experiment_confirm-1, Approval apr_00007
    because Confirm at 50/50 on fresh data: voice V2 against V1 (1,612 connected calls an arm planned); welcome_message W2 against W1 (1,735 connected calls an arm planned).
  2027-01-06T19:29:00+05:30  attempt 4  no_answer
    heard   welcome_message W2, voice V1, caller_id D3, prompt_version P1 (cohort default)
    config  version 6, live since 2027-01-06T12:45:00+05:30, set by controller_approved, Proposal ks_cancel-experiment_confirm-1, Approval apr_00007
    because Confirm at 50/50 on fresh data: voice V2 against V1 (1,612 connected calls an arm planned); welcome_message W2 against W1 (1,735 connected calls an arm planned).
  2027-01-07T13:44:00+05:30  attempt 5  voicemail
    heard   welcome_message W2, voice V2, caller_id D1, prompt_version P2 (cohort default)
    config  version 7, live since 2027-01-07T11:30:00+05:30, set by supervisor
  2027-01-07T17:13:00+05:30  attempt 6  voicemail
    heard   welcome_message W2, voice V2, caller_id D1, prompt_version P2 (cohort default)
    config  version 8, live since 2027-01-07T17:00:00+05:30, set by controller_approved, Proposal ks_cancel-caller_id-4, Approval apr_00011
    because [Incident response] Move traffic off caller ID D2: pickup 10.4% against 25.7% on the other numbers over the last 24 hours (604 dials). Replaces ks_cancel-caller_id-3, which went stale.
```

Loop health at the end of that run:

```
$ python -m controller health --db state/public_02.db
scenario          public_02_incidents
cursor            chunk 491, ingested through D10 00:00 (minute 14400)
events ingested   324319 (last event_id 324319)
ingest lag        0 ticks (simulator at D10 00:00); last ingest 696.0s ago
decisions/hour    18 in the last simulated hour
credits           197 left, 3 spent (0 in the last simulated day), reserve 30
alerts            none
notes             none
```

**Pages** (each says what to look at):

| Condition | First look |
|---|---|
| Ingest more than 2 ticks behind | Controller log and `GET /events/chunks` |
| Credits below the 30 reserve | The burn in `controller health` |
| Incident unfixed 4 hours after detection, or escalated with no fix | `controller why <workflow>` |
| Apply failure: lapsed approval, or 3 Proposals of one kind stale in a row | `controller why`, and who's editing |

**Not paged** (notes in `controller health`): rejections, expiries, inconclusive experiments, single retries, a live prompt that passed validation after a spike. They can all wait until morning. A programming error in a decide step is also only a note (ADR-0015), a known weak spot.

## 11. Results

**public_01_dev**

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.367 | +7.5% | 0.258 | +5.5% | 0.497 | +0.9% | 4% | 62 (57) |
| nw_kyc | 0.395 | +7.6% | 0.262 | +8.2% | 0.590 | -1.2% | 0% | 21 (21) |
| nw_renewal | 0.467 | +12.3% | 0.281 | +6.4% | 0.565 | +4.3% | 13% | 64 (54) |
| ALL | 0.406 | +9.3% | 0.267 | +6.3% | 0.539 | +1.8% | 7% | 147 (132) |

16 changes applied: 10 beneficial, 4 neutral, 2 harmful. 14 requests, 12 approved, 2 rejected, 0 expired, 0 lapsed. 3 credits (the false incident in §7).

**public_02_incidents**

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.356 | +51.1% | 0.242 | +33.8% | 0.500 | +6.9% | 34% | 110 (348) |
| nw_kyc | 0.432 | +15.7% | 0.274 | +16.8% | 0.597 | -1.8% | 0% | 26 (29) |
| nw_renewal | 0.477 | +10.2% | 0.291 | +11.0% | 0.546 | -1.5% | 19% | 68 (54) |
| ALL | 0.413 | +24.8% | 0.265 | +20.9% | 0.537 | +0.8% | 25% | 204 (431) |

20 changes applied: 11 beneficial, 8 neutral, 1 harmful. 18 requests: 15 approved, 2 rejected, 1 still pending at the end, 0 expired, 0 lapsed. 3 credits.

**Interpretation.**

Where it helped:
- **Caller ID** is the main source: 6 of the 10 beneficial changes on public_01 and 6 of 11 on public_02. Overall pickup is +6.3% and +20.9%, engagement +1.8% and +0.8%.
- **public_02's +51.1% on `ks_cancel` is mostly harm avoided.** The baseline keeps the degraded caller ID and the bad prompt to the end. That isn't the agent improving itself, and I wouldn't quote it in a sales deck.
- **Guardrails held.** `nw_kyc`'s engagement fell by about 0.011 at most, against an allowed 0.02.

Where it was weak:
- **Engagement gap captured: 7% and 25%**, and 0% on `nw_kyc` by design. On public_01 `nw_renewal`'s W3 was confirmed (p = 0.0082), then an approver declined the promotion (`nw_renewal-experiment_promote-1`), and a rejected promotion throws away the result. On public_02 both confirms ended `blocked_harm` with no candidate beating control overall (primary p of 0.69 and 0.74), each on one cohort at p ≈ 0.015. Holm's correction (ADR-0016) did what it should: it cleared `ks_cancel`'s two-cohort block at the first look, the confirm ran two more days, and real evidence still blocked it. It did not produce a winner, and the D6 return to control is the harmful change below.
- **No decay rollback ran.** The fall in W3's share in the decaying-variants section comes from experiments ending, not a detected decay.

Where it was harmful, 3 of 36 applied changes:
- *public_01 `nw_renewal` v5.* Auto-applied exploration added caller ID D4 at the 10% cap. D4 was poor. The cap limited the damage and D4 was moved off at D1 17:15. This is the price of exploring, and I'd keep the policy.
- *public_01 `ks_cancel` v6 and public_02 `ks_cancel` v10.* Both are the end of an inconclusive experiment: back to control, which the grader rated worse than the live split. The controller's own confirm data doesn't show that (public_01: V3 49.9% against 50.0%, W2 50.2% against 49.8%; public_02: V2 49.0% against 49.7%, W2 49.9% against 48.8%), so I can't explain the grader's −0.014 and −0.023 from the store.

What I'd change: let an incident fix use more of the approver's day, and validate a supervisor's prompt edit when it appears rather than waiting for the flag rate.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Postgres now | Closer to production, and SRE already runs it | More moving parts in `run.sh`. SQLite gives the same transactions on one file (ADR-0001). Same schema moves later. |
| Keep raw call events | Any later question stays answerable | 4.2M+ events; every KPI is a sum over cells. Aggregates plus an audit-only exposure log (ADR-0003). |
| Bandit allocation | Faster exploitation | Each reallocation needs an approval under a daily cap, and it gives no statable false-winner rate (ADR-0005). |
| One-stage test, Bonferroni over arms, cohorts and looks | Simplest correct design | 18–90+ days at this traffic against 8–10-day scenarios. Screen then confirm (ADR-0006). |
| Cohort-specific winners | Biggest potential lift, what Sales wants to show | A 15% cohort can't be confirmed in a scenario. Workflow-wide winner with a cohort harm check. |
| Auto-apply welcome and voice on `ks_cancel` | Policy allows it; faster starts | A customer hears them, and two memos ask for a human. Slower experiment start accepted (ADR-0002). |
| No holdback after promotion | About 20% more lift during the holdback | Without a counterfactual a decay is indistinguishable from a bad week (ADR-0008). |
| Keep auto-applying after an approver rejects | Stops a degraded number sooner; policy permits it | It routes around a person's no. Blocked for 24 hours; the cost is a degraded number keeping traffic longer (ADR-0009). |
| LLM picks the prompt fix | Would use the LLM's suggestions | README forbids an LLM deciding a winner. Known-good first; the LLM only supplies candidates (ADR-0014). |
| Harm check uncorrected across cohorts | Errs towards blocking | About 34% false alarms per look with 4 cohorts, so it stopped telling anyone about harm. Holm across cohorts (ADR-0016). |
| Fixed 2-hour incident window | Simplest, freshest | A workflow under 75 connects an hour could never raise an incident. It grows to 150 connects, up to 24 hours (ADR-0016). |
| Experiment on welcome and voice for pickup-primary workflows | Could raise the conclusion rate | Would judge winners on the guardrail, and the 2/day approval cap can't cover both (ADR-0007). |
| Web UI or HTTP status endpoint | Nicer for a supervisor | The CLI over one query interface covers it. Time went to governance. |

## 13. Known weaknesses

1. **Most lift is caller ID.** Engagement headroom captured is low, and no winner went live on the public scenarios. A real lift under 10% is expected to end inconclusive.
2. **An incident found late in the day waits overnight.** public_02's prompt incident was detected at D3 18:15, after the 17:00 request cut-off, so it took 1,455 minutes to fix. The cut-off exists because a request that sits overnight can expire (`ks_cancel`'s TTL is 12 hours). Respecting a rejection has a cost of the same kind: D2 kept its remaining traffic for another day.
3. **Batching is partial.** Unrelated changes (a screen, then caller-ID exploration) are separate requests. User story 52's merging exists only for welcome with voice and for holdback release.
4. **Rollback is unproven outside tests.** Decay rollback has run only against injected decay.
5. **Post-fix verification checks flags only.** A fix that stops hallucination but loses engagement (the near-verbatim Strict prompt, for example) would verify as healthy. Validation says nothing about engagement either.
6. **A very quiet workflow is still blind.** Under 150 connects in 24 hours, nothing can be tested. Widening the window also costs freshness on `nw_kyc`: 150 connects takes about 2.5 hours there, and it raised one false incident (§7).
7. **"Degraded" is relative.** On `nw_kyc` D1 was marked degraded at 24.8% pickup, close to the workflow's 26.2% history baseline: D2 is simply better. Calling that an incident and flooring its impact at 0.9 overstates it.
8. **The harm check is weak on a small cohort, and its looks aren't corrected.** Holm holds its false-alarm rate at 10% per look across cohorts, but over 3 looks it is about 20%. The guardrail is checked only across the workflow.
9. **Two winners promoted together aren't tested for interaction.**
10. **Call-time tuning has never fired on a public scenario,** so its live path is tested but not exercised at scale. Its before/after rollback is confounded by design.
11. **Overlays live in code.** A new client's preferences need a code change.
12. **Single thread.** One slow workflow delays the rest. Load numbers are from the wrong hardware (§8).
13. **Approver identity isn't recorded,** because the platform doesn't return it.
14. **An experiment still running at scenario end stays live** (`nw_renewal-exp-2` on public_01 was still screening).
15. **`controller heard` gives one reason per config version.** It prints that version's latest change, so a welcome or voice set by an earlier version shows an unrelated reason, as in the §10 output. Compliance's "the reason it was live" needs a per-dimension lookup, which isn't built.

## 14. If this went to production at Formi next month

In order, first two weeks:

**Week 1**
1. Run shadow-only on two workflows: real events in, decisions recorded, no writes. Compare with what approvers would have wanted.
2. Decide with clients whether an incident fix may go out in an approver's last 2 hours, given each workflow's TTL (§13.2).
3. Move the store to the platform's Postgres, keeping the schema. Run the crash test against it.
4. Get approver identity from the platform into the approval record, and move overlays into per-tenant config with their own audit.
5. Re-measure load on 4 vCPU / 8 GB, then move decide to a process pool.
6. Wire the four pages to the real pager and write a one-page runbook from README §11.

**Week 2**
7. Turn on caller-ID exploration and auto-apply for one Northwind renewal workflow, with the weekly summary going to Compliance.
8. Start approval-path experiments on that one workflow. Add an engagement check to incident verification before any prompt fix can go out.
9. Run a decay drill in staging to prove rollback end to end. Check real false-incident rates against the 5% bound and tune.
10. Only then widen to a second tenant, and review the 10% MDE with Product against what real lifts look like.
