# Crash-recovery log — public_02_incidents

Run 2026-11-09 02:35, seed 15. The controller (`./run.sh`) was SIGKILLed at random points and restarted with the same command; each restart resumed from the store. An uninterrupted run took 55 s.

## Kills and restarts

| life | started | killed at | after (s) | simulated minute | in flight at the kill | restarted at | exit |
|---|---|---|---|---|---|---|---|
| 1 | 02:34:23 | 02:34:29 | 6.1 | 870 | 1 pending | 02:34:29 | SIGKILL |
| 2 | 02:34:29 | 02:35:01 | 38.2 | 10185 | none | 02:35:01 | SIGKILL |
| 3 | 02:35:01 | 02:35:11 | 48.4 | 12645 | none | 02:35:11 | SIGKILL |
| 4 | 02:35:11 | — | — | — | — | — | 0 |

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
