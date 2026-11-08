# Load report: public_03 (12 workflows, ~650k dials/day, 5 days)

Reproduce: `.venv/bin/python scripts/measure_load.py` (instrumented, in-process) and
`.venv/bin/python scripts/measure_load.py --run-sh` (the real `./run.sh`). Raw output:
`public_03_load_inprocess.json`, `public_03_load_runsh.json`.

## Method

The simulator runs as its own `sim serve` process. In the instrumented run the controller is in
the script's process, and each 15-minute tick is split into **advance** (the simulator's work,
not ours), **ingest** and **decide**. Per-tick *decision lag* = ingest + decide, the wall time
between the simulator publishing a tick's events and the controller having acted on them.
*Controller wall-clock per simulated day* = the sum of ingest + decide over that day's 96 ticks;
the simulator's advance time is reported apart. Peak RSS is `ru_maxrss` of the controller process
(instrumented run) and a 0.5 s `ps` sampler of the `./run.sh` process (so it can undersample).
The `./run.sh` run supplies end-to-end wall clock, with the day taken from the simulator's clock.

**Caveat on the envelope.** Measured on a 10-core / 16 GB Mac, not on 4 vCPU / 8 GB. The controller
is one process, one thread (about 100% of one core while running), and the simulator is a separate
process, so 4 vCPUs leave each of them a core. Memory is measured directly and is far inside 8 GB.
The wall-clock figure should be re-taken on the real envelope before it is quoted as met there.

## Results

| | Value |
|---|---|
| Events ingested live (5 days) | 4,200,164 |
| Ingest throughput | ~42,000 events/s (99 s of ingest for 4.2 M events) |
| Startup (7-day history catch-up + bootstrap) | 64 s, once |
| Decision lag per tick | p50 0.81 s, p95 1.64 s, max 7.3 s (tick = 15 simulated minutes) |
| `./run.sh` end to end | exit 0, 546 s for 5 days, startup included |
| Peak RSS, controller | 450 MB (instrumented), 226 MB (`ps` sampled, `./run.sh`) |
| Peak RSS, simulator | 709–772 MB |

Controller wall-clock per simulated day (instrumented; budget 600 s):

| Day | Controller s | of which ingest | of which decide | Simulator advance s |
|---|---|---|---|---|
| D0 | 75.8 | 20.5 | 55.3 | 7.1 |
| D1 | 78.1 | 16.4 | 61.7 | 7.2 |
| D2 | 105.0 | 20.6 | 84.4 | 7.2 |
| D3 | 94.5 | 21.0 | 73.4 | 7.3 |
| D4 | 102.4 | 21.0 | 81.4 | 7.5 |

`./run.sh` per day, simulator included: 143 s (D0, with the 64 s startup), 87, 109, 100, 107 s.

**Verdict:** the slowest day is 105 s of controller time (112 s with the simulator), about 5.7×
under the 10-minute budget, and the worst tick's decision lag is 7 s against a 15-simulated-minute
tick. Memory is under 0.5 GB for the controller, so no optimisation was needed and none was made.

## What breaks first at 10× (about 6.5 M dials/day, 120 workflows)

Estimates from the measurements above, not measured runs.

1. **Decide (breaks first).** It is the larger share of every day (55–84 s) and it grows over the
   run (55 s on D0, 81 s on D4) as the aggregates and experiments accumulate. It runs serially,
   per workflow, per tick. At 10× workflows it is about 550–850 s per day, at or past the 10-minute
   budget, and the p95 tick lag moves from 1.6 s to roughly 16 s.
2. **Ingest.** Linear in events: about 200 s/day at 42 k events/s. Fine alone, but it shares the
   one thread with decide, so together they exceed the budget. A single SQLite writer is the ceiling
   (ADR-0001); its transaction per chunk is already the atomic unit.
3. **Memory.** The active-lead cache scales with leads in live campaigns (3-day cadence), so about
   10× of today's share of the 450 MB, which is a few GB at most. The cell-aggregate design (ADR-0003)
   keeps the analysis tables small; the audit-only `exposures` log is the one table that grows with
   every dial and would dominate disk (and need pruning or partitioning).
4. **Simulator HTTP.** Chunk download is a small share today (advance is 7 s/day); it grows linearly.

## How we would partition

- **By tenant first.** Tenants share no leads, cohorts or experiments, so one controller process
  and one SQLite file per tenant, each handling only that tenant's workflows. The one shared
  resource is the credit budget; it needs a small coordinator that holds the reserve and hands each
  controller a spend allowance (the "credits below the reserve" page then fires from the coordinator).
- **Then by workflow within a big tenant.** A workflow's decide step reads only its own aggregates,
  so decide shards cleanly across processes. Ingest is the awkward part: the event stream is global
  and ordered by `event_id`. Either every shard fetches and filters every chunk (cheap relative to
  decide, but duplicated), or one ingest process writes per-shard stores and the shards only decide.
  The second is better past a few shards.
- **Parallelise decide before ingest.** Moving decide to a process pool, one workflow group per
  worker, takes the dominant cost off the single thread with no change to the store schema.
- Governance, paging and `controller health` stay per shard; the paging alert for ingest lag should
  be evaluated per shard so one slow shard is named rather than averaged away.
