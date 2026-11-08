# Ticket map: Formi controller

Spec: `spec.md`. Tickets: `issues/NN-*.md`. Each ticket's `**Blocked by:**` line is the source of truth. This file is just the picture.

## Dependency graph

```mermaid
graph TD
  T01[01 Walking skeleton] --> T03[03 Cohorts & baselines]
  T01 --> T04[04 Governance client + caller ID]
  T02[02 Statistics module] --> T04
  T03 --> T04
  T04 --> T05[05 Auto-apply + exploration]
  T04 --> T06[06 Hallucination incidents]
  T03 --> T07[07 Experiments: screen]
  T04 --> T07
  T07 --> T08[08 Experiments: confirm + promotion]
  T08 --> T09[09 Decay rollback]
  T03 --> T10[10 Per-cohort call time]
  T05 --> T10
  T06 --> T11[11 Supervisor view]
  T08 --> T11
  T10 --> T11
  T06 --> T12[12 Loop health + paging]
  T04 --> T13[13 Crash-recovery evidence]
  T09 --> T14[14 Load performance]
  T10 --> T14
  T12 --> T14
  T11 --> T15[15 Submission + docs]
  T13 --> T15
  T14 --> T15
```

Critical path: **01 → 03 → 04 → 07 → 08 → 09 → 14 → 15**. Tickets 02, 05, 06, 10, 12 and 13 can be worked in parallel off that path.

## Status

When a ticket is finished, set its `**Status:**` line to `done` and tick its row here.

| # | Ticket | Blocked by | Done |
|---|---|---|---|
| 01 | Walking skeleton: loop, ingestion, restart | None | [x] |
| 02 | Statistics module | None | [ ] |
| 03 | Cohorts and history baselines | 01 | [x] |
| 04 | Governance client on the approval path, first used by degraded caller IDs | 01, 02, 03 | [x] |
| 05 | Auto-apply path and caller-ID exploration | 04 | [x] |
| 06 | Hallucination incident response | 04 | [ ] |
| 07 | Experiments: screen stage | 03, 04 | [x] |
| 08 | Experiments: confirm and promotion | 07 | [ ] |
| 09 | Decay rollback and holdback release | 08 | [ ] |
| 10 | Per-cohort call time | 03, 05 | [ ] |
| 11 | Consolidated supervisor view | 06, 08, 10 | [ ] |
| 12 | Loop health and paging | 06 | [ ] |
| 13 | Crash-recovery evidence | 04 | [ ] |
| 14 | Load performance on public_03 | 09, 10, 12 | [ ] |
| 15 | Grade reports, SUBMISSION.md and docs | 01–14 | [ ] |

**What can I start now?** It's any ticket whose blockers are all `done`. Quick check from the repo root:

```bash
grep -H -E '^\*\*(Blocked by|Status):' .scratch/formi-controller/issues/*.md
```
