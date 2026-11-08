# Outcome-First Agent Loop — Senior Engineer Assignment

**Formi · Agentic Universe** · Greenfield · Suggested effort 12–16 hours across 7 calendar days

---

## 1. Context

Formi runs outbound voice agents for insurers and lenders across India: renewal reminders, KYC follow-ups,
cancellation reconfirmations. Each **workflow** dials leads within a business-set window, following a retry
cadence the business configures. Today a human sets the agent's configuration once and rarely revisits it.
That configuration covers which welcome message it opens with, which voice it uses, which caller ID it dials
from, which prompt version runs, and which hours each kind of person gets called in.

Two numbers decide whether a workflow is healthy, and we never blend them:

- **Pickup rate**: of the dials, how many connected.
- **Engagement rate**: of the people who picked up, how many stayed to a conclusion (interested *or* not).

The business outcome sits above both: **leads concluded ÷ leads uploaded**.

We are moving from agents that humans tune to agents that **earn autonomy**. They notice what's working, try
better options on the right people, ask for permission in a form a busy supervisor can say yes to, apply
changes safely, and back out when something stops working. A client supervisor can see why every change was
made. Nothing changes on a live client's agent without passing the governance boundary.

**Your job is to build that loop.**

## 2. The problem

Build a **controller**: an always-on service that, for every workflow on the platform, continuously:

1. **Detects**: pickup or engagement drifting, a caller ID going bad, hallucinations spiking. It works on
   thresholds and populations, never on single calls.
2. **Hypothesises and experiments**: it forms cohorts, splits traffic between variants, and decides how much
   evidence is enough.
3. **Proposes**: it packages a change with a summary, a diff, the affected leads, a dry run, an impact score
   and a rationale, then routes it through governance.
4. **Applies and learns**: it applies what's approved, keeps measuring, promotes winners and rolls back
   variants that degrade.
5. **Explains**: from your system's own records, anyone can answer *"why did workflow X change at 14:00 on
   D3, who approved it, and what happened next?"*

You won't connect to our production. `sim/` contains a **closed-loop simulator** of the world: tenants,
workflows, leads, a dialler, people who pick up or don't, client approvers, client supervisors who edit things
mid-day, and an LLM that suggests prompt fixes. Your controller drives it over HTTP. The simulator holds
hidden ground truth: which variants actually work, for whom, for how long, and what happens when you change
things. Read **[docs/SIMULATOR.md](docs/SIMULATOR.md)** carefully. It is the contract.

## 3. Scale envelope

Design for the platform we're building toward, not the dev scenario:

- **~2 crore dials/month**: about 6.5 lakh/day inside a 12-hour window, peaking above 50k dials/hour.
- Tens of tenants and hundreds of workflows, each with its own policy, objective and approver.
- Events arrive continuously. Your loop's decisions are only as fresh as its ingestion.

`scenarios/public_03_load.yaml` (12 workflows, ~650k dials/day, ~850k events/day) proves your system keeps up.
We will ask for numbers: ingest throughput, decision lag, memory, and where it would break at 10×.

## 4. What's in the box

```
sim/                      the world (don't modify — we run our own copy)
scenarios/
  public_01_dev.yaml      3 workflows, 8 days, no incidents — start here
  public_02_incidents.yaml  caller ID flagged, a supervisor ships a bad prompt, a supervisor edits mid-experiment
  public_03_load.yaml     12 workflows at full scale, 5 days
stakeholders/             six memos from people who want things from you. They disagree.
examples/naive_controller.py   shows the API round-trip. It is deliberately bad.
docs/SIMULATOR.md         API, config, governance, events
SUBMISSION_TEMPLATE.md    copy to SUBMISSION.md
```

```bash
pip install -r requirements.txt
python -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/base01
python -m sim serve    --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01     # :8700
./run.sh http://127.0.0.1:8700                                                        # your controller
python -m sim grade --run runs/mine01 --baseline runs/base01 --out reports/public_01.md
```

**Ground rules for the simulator**
- Your controller may only know what the HTTP API tells it. Reading `runs/`, importing `sim`, or parsing
  scenario files (including fields like `base_pickup` or `did_base_health`, which the API never returns) is
  disqualifying. Our hidden scenarios share the schema and
  differ in truth, incidents and policy. A controller tuned to the public truth will do badly on them.
- `./run.sh <SIM_URL>` must start your controller and run until the scenario finishes, with no manual steps.
  We will run it against hidden scenarios on a 4-vCPU / 8 GB Linux box. Docker is fine. Say so in `run.sh`.
- Any language and any storage. Every dependency you add is something you'll have to defend
  (see `stakeholders/03`).

## 5. Constraints that are not up for negotiation

These come from the platform and our regulated clients. They don't move, whatever the memos say.

1. **The governance boundary is absolute.** Every change your controller makes goes through `/approvals` or a
   policy-permitted auto-apply. Supervisor edits are a fact of life. You don't overwrite them silently.
2. **The business owns whether and when to call.** Cadence, dialling windows and capacity are out of scope.
   You decide *who* gets called in a given hour and *how* the agent shows up.
3. **Pickup and engagement stay separate KPIs.** Optimise a workflow's `primary` KPI and respect its
   `guardrail`.
4. **Winner selection is statistical and deterministic.** An LLM may propose what to try. It never decides
   what won.
5. **Act on populations, not anecdotes.** Detection is threshold-based. No change is triggered by one call.
6. **Every decision is auditable** from your own store. The simulator's logs don't count.
7. **Credits are finite.** LLM and validation calls cost credits. Running out mid-incident is a failure.
8. **Crash safety.** Kill your controller at any point, restart it, and it resumes. It must not double-apply,
   lose a pending approval, or forget what it learned.

## 6. Scope: you can't do all of it

Here is everything someone has asked for. Done well, it's **2–3× what fits in the time budget.** Choosing is
part of the assignment. Don't spread yourself thin. We'd rather see five capabilities done properly than
fifteen sketched.

| # | Capability |
|---|---|
| C1 | Event ingestion at load-scenario scale: backlog-safe, idempotent, restartable |
| C2 | Lead/feature joins and **one shared cohort object** used by detection, experiments and the supervisor view |
| C3 | Bootstrapping from history before D0 |
| C4 | Call-time optimisation: per cohort and/or per person |
| C5 | Caller-ID health: detect degradation, rotate, explore safely |
| C6 | Variant experimentation on welcome message and voice: design, minimum sample sizes, multiple comparisons |
| C7 | Winner promotion, **decay detection and rollback** |
| C8 | Hallucination incident response: detect → LLM suggest → validate → propose with evidence → verify after apply |
| C9 | Governance client: proposal lifecycle, batching under approver fatigue, timing against approver hours, expiry/lapse, stale-base re-proposal, auto-apply within policy |
| C10 | Honest impact scores, affected-lead counts, and dry runs against historical cohorts |
| C11 | A consolidated supervisor view (API or minimal UI): one place, not fifty alerts |
| C12 | A queryable decision/audit store: *"why did X change"* |
| C13 | Crash recovery, demonstrated: kill mid-run, restart, show the run completes correctly |
| C14 | Observability of the loop itself: ingest lag, decisions/hour, credit burn, alerts an on-call engineer can act on |
| C15 | Fair allocation of credits and approver attention across tenants |

**Floor (mandatory):** the controller runs public_01 and public_02 end to end with no manual steps, keeps an
audit trail (C12, even a minimal one), never breaks the constraints above, and `SUBMISSION.md` is complete.
Everything beyond that is your call. Write down what you cut and why.

## 7. Stakeholders

`stakeholders/` holds six memos: Sales, a client's Compliance head, Platform/SRE, a client operations
supervisor, Product, and the CTO. Each is reasonable on its own. Together they contradict each other, and
some assume things about the system that aren't true. **Check their claims against the actual workflow
policies.** In `SUBMISSION.md`, cover each one: what you accepted, what you pushed back on, and the reply
you'd actually send them.

## 8. Deliverables

A git repository (private is fine; share access with the email on the form) containing:

1. **Your controller**, with `run.sh`.
2. **`SUBMISSION.md`**, from the template. This is where most of the evaluation happens.
3. **`reports/`**: `sim grade` output for public_01 and public_02, plus your load-test numbers for public_03.
4. **Crash-recovery evidence**: a script or log showing a kill/restart mid-run and a correct finish.
5. **Commit history** that shows how your thinking progressed. Small, honest commits beat one squash.
6. *Optional:* a walkthrough video of 5 minutes or less.

## 9. How we evaluate

| Weight | What | How |
|---|---|---|
| 25% | **Outcomes on hidden scenarios** | We run `run.sh` against 2 hidden scenarios. We look at lift versus baseline on the primary KPI (with the guardrail respected), harmful changes applied, incident time-to-fix, decay rollback, and governance hygiene. No points for lift bought by breaking a constraint. |
| 25% | **Design and reasoning** | Is the learning loop statistically sound? Are cohorts sized sensibly? Are false positives handled? Does the design hold at the scale envelope? |
| 20% | **Scope and stakeholder judgement** | Did you choose what mattered, say no clearly, and catch the memos' wrong assumptions? |
| 20% | **Engineering** | Correctness, crash safety, idempotency, throughput on the load scenario, tests where they matter, and code a teammate could own. |
| 10% | **Communication** | Could a new engineer run, debug and extend this from your documents alone? |

A 60-minute review follows. We'll pick one decision your controller made on a hidden scenario and ask you to
explain it from your audit store, live. We'll change a constraint and ask what breaks. We'll ask about the
thing you cut.

## 10. Rules

- **AI tools are allowed, and we expect you to use them.** You must be able to defend every line and every
  decision without them in the review. Generic, unadapted output is easy to spot and scores poorly.
- If something is ambiguous, **state your assumption in `SUBMISSION.md` and proceed**. Don't wait for an
  answer. Questions are welcome but won't block you.
- Don't share the assignment or your solution publicly.

---

## 11. Running, debugging and extending the controller

For whoever owns this when the author is away. Sections 1–10 above are the brief. The reasoning is in
`SUBMISSION.md`, the design decisions in `docs/adr/`, and the vocabulary in `GLOSSARY.md`. Use the
glossary's words: a *Proposal* and an *Approval* are different things.

### Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# terminal 1: the simulator. --force wipes that run directory
.venv/bin/python -m sim serve --scenario scenarios/public_02_incidents.yaml --run-dir runs/mine02 --force

# terminal 2: the controller, until the scenario finishes (exit 0)
./run.sh http://127.0.0.1:8700
```

`run.sh` writes to `state/controller.db`, or to `CONTROLLER_DB` if set. Logs go to stderr.

Kill the controller at any point and run the same command: it resumes from the store. If the store
belongs to another world (a different scenario, or a restarted simulator), `run.sh` moves it aside as
`controller.db.stale-<timestamp>` and starts fresh. `python -m controller` without
`--fresh-if-mismatch` refuses instead, with exit code 2.

To grade a run, stop the simulator and compare against a no-op baseline. `baseline` has no `--force`,
so give it a run directory that doesn't exist:

```bash
.venv/bin/python -m sim baseline --scenario scenarios/public_02_incidents.yaml --run-dir runs/base02
.venv/bin/python -m sim grade --run runs/mine02 --baseline runs/base02 --out reports/public_02.md
```

Evidence scripts, which write to `reports/`:

```bash
.venv/bin/python scripts/crash_recovery.py          # 3 SIGKILLs on public_02, about 3 minutes
.venv/bin/python scripts/measure_load.py            # public_03, instrumented, about 10 minutes
.venv/bin/python scripts/measure_load.py --run-sh   # public_03 through the real ./run.sh
```

Tests and types:

```bash
.venv/bin/python -m pytest -q tests                 # everything; -m "not slow" skips full scenario runs
.venv/bin/pip install mypy && .venv/bin/python -m mypy controller    # mypy isn't in requirements.txt
```

**Ground rules (breaking one is disqualifying).** Learn about the world only through the simulator's
HTTP API. Controller code never imports `sim` or parses `scenarios/*.yaml`, and nobody modifies `sim/`.
People and agents alike never read `runs/` (`sim grade` does that) or anything below
`# ---- hidden ground truth` in a scenario file, and inside `sim/` read only `server.py` and
`tests/test_sim.py`. Tests may run the simulator and shrink a public scenario's `days`, but never author
or edit hidden-truth sections.

### Ask it questions

All of these only read the store, so they are safe while the loop runs. Run them with the virtualenv
active (`source .venv/bin/activate`) or as `.venv/bin/python -m controller …`. Add `--db <path>` for a
store other than `state/controller.db`.

| Question | Command |
|---|---|
| Why did this workflow change at this time, who approved it, what happened next? | `python -m controller why ks_cancel --at "D3 17:00"` |
| What did this lead hear, and what config was live? | `python -m controller heard ks_cancel:l00022549` |
| What's being tested, what needs approval, what changed today, what did we decide not to do? | `python -m controller status ks_cancel` |
| Partitions and baselines for every workflow | `python -m controller status` |
| Is the loop healthy? | `python -m controller health` |
| What was auto-applied this week, and why? | `python -m controller report --weekly` |

### When something is wrong

Start with `controller health`. Every decide step is recorded in the `decisions` table, including
the decisions not to act.

| You see | It means | Look at |
|---|---|---|
| `PAGE … Ingest is N ticks behind` | Chunks are failing. Nothing new is decided, reconciling continues, the next tick retries. | Log lines `ingest attempt`, then the simulator's `GET /events/chunks` |
| `PAGE … credits left, below the reserve` | The next incident may not be able to validate a fix. | The credit burn in `health`, then `validated`/`suggested` decisions |
| `PAGE … Incident … hours after it was detected` | A person has to approve a fix or change the prompt. | The `controller why` command in the page itself |
| `PAGE … approval … lapsed` or `Proposals in a row … went stale` | An approved change isn't live, or a supervisor keeps moving the base. | `controller why`, then who is changing the config |
| `error` notes, log line `decide step failed` | A programming error in one workflow's decide step. It doesn't page and repeats every tick. | The traceback in the log |
| "refusing to resume" (exit 2) | The store is from another world. | Point `--db` at a new file |
| Port 8700 in use | Another simulator is running. | `sim serve --port <n>`, then `./run.sh http://127.0.0.1:<n>` |
| A change you expected didn't happen | Usually `held`: approver hours, the cap or a cooldown. | The `chose not to act` list in `controller status <workflow>` |

Raw queries, for when the CLI doesn't answer it:

```bash
sqlite3 state/controller.db \
  "SELECT minute, kind, outcome, proposal_id FROM decisions WHERE workflow_id='ks_cancel' ORDER BY decision_id DESC LIMIT 20"
sqlite3 state/controller.db \
  "SELECT minute, workflow_id, outcome, detail FROM decisions WHERE outcome IN ('error','ingest_failed','retried')"
sqlite3 state/controller.db \
  "SELECT proposal_id, status, reasons FROM proposals WHERE status <> 'applied'"
```

`minute` counts simulated minutes from D0 00:00, so 5010 is D3 11:30.

### How the code is laid out

One process, one thread, one 15-minute tick at a time. Each tick (`loop.py`): advance the clock → ingest
every new chunk → for each workflow, reconcile Proposals, then hallucination, caller ID, experiments,
call time, caller-ID exploration → re-judge the alerts.

| Module | Owns |
|---|---|
| `simclient.py` | The only HTTP code. Retries, error classes, which calls are safe to repeat |
| `store.py` | The SQLite schema, transactions and most of the SQL. `ingest.py` writes its own bulk inserts |
| `ingest.py` | Chunk → one transaction (aggregates, exposures, leads, cursor) |
| `registry.py` | A workflow's effective policy: API policy plus tenant overlay, which can only tighten |
| `analysis.py`, `cohorts.py` | Baselines and the analysis partition, from history only |
| `stats.py` | Every statistical test. Pure functions, no I/O, thresholds at the top of the file |
| `composer.py` | Builds a proposed config on top of the current one, and checks it against policy |
| `governance.py` | The Proposal state machine: sending, timing, reconciling, applying |
| `replay.py` | A Proposal's dry run and affected leads |
| `hallucination.py`, `callerid.py`, `experiments.py`, `schedule.py` | The four deciders. Each builds a `Draft` and hands it to governance |
| `alerts.py` | The four page conditions |
| `credits.py` | The only route to paid endpoints, so the reserve can't be bypassed |
| `queries.py`, `__main__.py` | Read-only answers, and the CLI over them |

### Extending it

Rules that keep it safe, which every change must keep:

1. **Commit before you act.** A state is written before the call it announces, and `submitting` or
   `applying` is never acted on without first asking the platform which it was.
2. **Every config change goes through `GovernanceClient`** (`propose` or `auto_apply`). Nothing else
   calls `/approvals`, `/apply` or `PUT /config`.
3. **Every decision is recorded,** including a decision not to act, in the same transaction as its
   state change (`Store.record_decision`, or `store.note_decision` for a no-op).
4. **A new statistical rule goes in `stats.py`,** with a test on synthetic counts and a simulated
   false-positive rate if it can fire repeatedly.
5. **An LLM may suggest. It never decides.**
6. **Customer-facing dimensions are never auto-applied** (`CUSTOMER_FACING_DIMS`, ADR-0002), and nothing
   auto-applies on a dimension for 24 hours after an approver rejects a change to it (ADR-0009).

| To add | Do this |
|---|---|
| A tenant's preference | Add an `Overlay` to `OVERLAYS` in `registry.py`. It can only tighten a policy. Test it in `test_governance.py` |
| A new detector or responder | A new module with a `respond(...)` that builds a `Draft`, called from `Controller._decide_workflow` in priority order. Incidents go first |
| A dimension to experiment on | `DIMENSIONS_FOR_KPI` in `experiments.py`. Read ADR-0007 first |
| A change to a test's threshold | The constant at the top of `stats.py`. Then re-run public_01/02 and read the `harmful` lines |
| A new question for the CLI | A function in `queries.py`, a thin printer in `__main__.py`. Tests assert through the query, not the print |

Tests go through one of two seams: the simulator's HTTP API (`tests/harness.py`; `Trouble` wraps the
transport to inject a degraded number, an expiry or a kill, and `ScriptedWorld` answers the read API
where no public scenario produces a case), or the pure functions in `stats.py`. Assert on what a user
would see, never on private functions or internal tables. Record a decision that a future reader would
otherwise question as a new file in `docs/adr/`.
