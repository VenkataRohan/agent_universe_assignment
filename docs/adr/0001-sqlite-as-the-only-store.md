# SQLite is the controller's only store

The controller keeps everything it knows (ingestion cursor, aggregates, proposals, experiments and the audit trail) in one SQLite file in WAL mode, and every "decide → record → act" step runs inside a transaction. The SRE memo asks for no new stateful infrastructure. An embedded file has nothing to operate at 2 a.m., survives `kill -9`, and can be queried live in the review. In production the same schema moves to the Postgres the platform already runs. Redis is not needed, because the loop's working state is rebuilt from the store on restart.

## Considered Options

- **Postgres in Docker**: closer to production, but more moving parts in `run.sh` on a 4 vCPU / 8 GB box, and nothing the simulator needs.
- **Append-only files / JSONL**: simple, but no transactions across "record intent" and "advance cursor", which crash safety depends on.
