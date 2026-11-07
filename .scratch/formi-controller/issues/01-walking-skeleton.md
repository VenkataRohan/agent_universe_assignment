# 01: Walking skeleton: loop, ingestion, restart

**What to build:** `./run.sh <SIM_URL>` starts the controller and drives any scenario to its end, one 15-minute tick at a time. History chunks and live chunks are ingested the same way, each in one transaction that writes the lead map, the feature-cell aggregates, the audit-only exposure log, the verbatim world events (`config_changed`, `approval_decided`, `campaign_created`) and the chunk cursor. Only active campaigns' leads are cached in memory, and the cache is warmed from the store on restart. On startup, the controller resumes from an existing store only if the scenario name matches and the simulator's clock is at or beyond the stored cursor. Otherwise it refuses with a clear message. A minimal `controller health` shows the cursor, events ingested and ingest lag.

This ticket also sets up the Seam-1 test harness: the controller is built from an injected `httpx.Client` and a temp store, and tests drive it against the real simulator in-process through FastAPI's `TestClient`, with shrunk public scenarios. The controller never imports `sim`.

Spec: user stories 1–11, 67; ADR-0001, ADR-0003.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `./run.sh` against public_01 runs to `finished` with no manual steps
- [ ] Re-ingesting a chunk after an injected mid-chunk failure yields identical aggregates (no double count, no gap)
- [ ] A fresh controller on the same store and the same live world resumes from the cursor and finishes the run
- [ ] Restart against a different scenario, or a simulator whose clock is behind the cursor, refuses with a clear message
- [ ] Exposure log rows match call events one-to-one for a sample of leads
- [ ] In-memory lead cache holds only active campaigns' leads
- [ ] `controller health` prints cursor, events ingested and ingest lag while the controller runs (WAL read)
- [ ] Seam-1 harness exists and the tests above run through it; no test asserts on internal tables
