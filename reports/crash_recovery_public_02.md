# Crash-recovery log — public_02_incidents

Run 2026-11-09 01:56, seed 11. The controller (`./run.sh`) was SIGKILLed at random points and restarted with the same command; each restart resumed from the store. An uninterrupted run took 55 s.

## Kills and restarts

| life | started | killed at | after (s) | simulated minute | in flight at the kill | restarted at | exit |
|---|---|---|---|---|---|---|---|
| 1 | 01:55:24 | 01:55:50 | 25.6 | 7050 | none | 01:55:50 | SIGKILL |
| 2 | 01:55:50 | 01:55:55 | 30.4 | 8175 | 1 pending | 01:55:55 | SIGKILL |
| 3 | 01:55:55 | 01:56:11 | 46.6 | 11760 | none | 01:56:11 | SIGKILL |
| 4 | 01:56:11 | — | — | — | — | — | 0 |

## Checks

| check | result |
|---|---|
| run completed after the restarts | PASS |
| no duplicate proposal IDs | PASS |
| no approval applied twice | PASS |
| no pending Proposal lost | PASS |
| grade within noise of the uninterrupted run | PASS |

Platform at the end: 18 approvals, 26 config versions; store: 24 Proposals (21 applied, 3 rejected).
Conclusion rate (ALL): 0.417 after the kills, 0.417 uninterrupted; tolerance 0.03.
