# Crash-recovery log — public_02_incidents

Run 2026-11-09 15:18, seed 17572. The controller (`./run.sh`) was SIGKILLed at random points and restarted with the same command; each restart resumed from the store. An uninterrupted run took 56 s.

## Kills and restarts

| life | started | killed at | after (s) | simulated minute | in flight at the kill | restarted at | exit |
|---|---|---|---|---|---|---|---|
| 1 | 15:17:16 | 15:17:44 | 28.3 | 7800 | 1 pending | 15:17:44 | SIGKILL |
| 2 | 15:17:44 | 15:17:47 | 31.3 | 8430 | 1 pending | 15:17:47 | SIGKILL |
| 3 | 15:17:47 | 15:18:04 | 48.7 | 12675 | none | 15:18:04 | SIGKILL |
| 4 | 15:18:04 | — | — | — | — | — | 0 |

## Checks

| check | result |
|---|---|
| run completed after the restarts | PASS |
| no duplicate proposal IDs | PASS |
| no approval applied twice | PASS |
| no pending Proposal lost | PASS |
| grade within noise of the uninterrupted run | PASS |

Platform at the end: 18 approvals, 25 config versions; store: 24 Proposals (20 applied, 1 pending, 2 rejected, 1 stale).
Conclusion rate (ALL): 0.413 after the kills, 0.413 uninterrupted; tolerance 0.03.
