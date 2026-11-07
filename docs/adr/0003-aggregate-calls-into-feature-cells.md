# Calls are aggregated into feature cells; raw calls are not kept

Each call event is folded into a counter keyed by workflow, day, hour, config version, feature cell (lang × city_tier × age_band × value_band) and variant, and then discarded. Leads are kept as a map from lead to person and feature cell. Every cohort's KPIs are a sum over cells, so detection, experiments and the supervisor view all read one table, and ingestion stays fast and small at about 850k events/day. A chunk is ingested in one transaction that also advances the cursor, which makes re-ingestion after a crash idempotent without per-event dedup.

## Consequences

- Per-person questions (e.g. a person's best hour) can't be answered after the fact. This is consistent with cutting per-person call time.
- Learning never reads raw calls. But a slim per-call **exposure log** (lead, time, config version, cohort, variant, outcome) is written in the same chunk transaction, **for audit only**: Northwind's auditors need "which variant did this customer hear, and why was it live" (≈40 MB/day on load).
