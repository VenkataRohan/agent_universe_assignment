# Crash-recovery log — public_02_incidents

Run 2026-11-09 03:01, seed 15. The controller (`./run.sh`) was SIGKILLed at random points and restarted with the same command; each restart resumed from the store. An uninterrupted run took 54 s.

## Kills and restarts

| life | started | killed at | after (s) | simulated minute | in flight at the kill | restarted at | exit |
|---|---|---|---|---|---|---|---|
| 1 | 03:00:38 | 03:00:44 | 5.9 | 810 | 3 pending | 03:00:44 | SIGKILL |
| 2 | 03:00:44 | 03:01:15 | 37.2 | 9855 | none | 03:01:15 | SIGKILL |
| 3 | 03:01:15 | 03:01:25 | 47.0 | 12510 | none | 03:01:25 | SIGKILL |
| 4 | 03:01:25 | — | — | — | — | — | 0 |

## Checks

| check | result |
|---|---|
| run completed after the restarts | PASS |
| no duplicate proposal IDs | PASS |
| no approval applied twice | PASS |
| no pending Proposal lost | PASS |
| grade within noise of the uninterrupted run | PASS |

Platform at the end: 18 approvals, 24 config versions; store: 23 Proposals (19 applied, 1 pending, 2 rejected, 1 stale).
Conclusion rate (ALL): 0.416 after the kills, 0.416 uninterrupted; tolerance 0.03.
