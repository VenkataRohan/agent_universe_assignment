# 01: Walking skeleton: loop, ingestion, restart

**What to build:** `./run.sh <SIM_URL>` starts the controller and drives any scenario to its end, one 15-minute tick at a time. History chunks and live chunks are ingested the same way, each in one transaction that writes the lead map, the feature-cell aggregates, the audit-only exposure log, the verbatim world events (`config_changed`, `approval_decided`, `campaign_created`) and the chunk cursor. Only active campaigns' leads are cached in memory, and the cache is warmed from the store on restart. On startup, the controller resumes from an existing store only if the scenario name matches and the simulator's clock is at or beyond the stored cursor. Otherwise it refuses with a clear message. A minimal `controller health` shows the cursor, events ingested and ingest lag.

This ticket also sets up the Seam-1 test harness: the controller is built from an injected `httpx.Client` and a temp store, and tests drive it against the real simulator in-process through FastAPI's `TestClient`, with shrunk public scenarios. The controller never imports `sim`.

Spec: user stories 1–11, 67; ADR-0001, ADR-0003.

**Blocked by:** None (can start immediately)

**Status:** done

- [x] `./run.sh` against public_01 runs to `finished` with no manual steps
- [x] Re-ingesting a chunk after an injected mid-chunk failure yields identical aggregates (no double count, no gap)
- [x] A fresh controller on the same store and the same live world resumes from the cursor and finishes the run
- [x] Restart against a different scenario, or a simulator whose clock is behind the cursor, refuses with a clear message
- [x] Exposure log rows match call events one-to-one for a sample of leads
- [x] In-memory lead cache holds only active campaigns' leads
- [x] `controller health` prints cursor, events ingested and ingest lag while the controller runs (WAL read)
- [x] Seam-1 harness exists and the tests above run through it; no test asserts on internal tables

## Comments

**Implemented** (`controller/`: simclient, store, ingest, loop, queries, `__main__`; tests in `tests/test_walking_skeleton.py` and `tests/test_run_sh.py`, the latter marked `slow`).

- **Full public_01 through `./run.sh`:**
  - Wall time 8.9 s; controller peak RSS 108 MB.
  - Chunk bodies are streamed. History arrives as one 82 MB chunk, and reading it whole peaked at 560 MB.
- **Chunks are checked against their listed metadata** (event count, first and last `event_id`) before commit. A body cut at a line boundary would otherwise be ingested as a valid, shorter chunk, which is a silent gap.
- **`POST /sim/advance` is never retried blindly.** `advance_to(minute)` re-reads `/sim/state` after a failure, and the loop finds chunks by listing after the cursor rather than trusting `new_chunks`.
- **`run.sh` moves a mismatched store aside instead of refusing.** The plain CLI still refuses. Recorded in the spec under Packaging.
- **ADR-0003 is amended:** the aggregate key adds `cohort_id` and an attempt band.
- **One test reads controller memory rather than the query interface:** `cached_campaigns()` and `cached_leads()` on the ingestor. The cache is in-process state with no store representation, and the acceptance criterion is about that cache itself.
