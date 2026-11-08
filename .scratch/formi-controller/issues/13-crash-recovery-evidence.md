# 13: Crash-recovery evidence

**What to build:** A script runs public_02, SIGKILLs the controller at 3 random points, restarts it each time, and then checks from the controller's store and the simulator API:
- no duplicate proposal IDs
- no approval applied twice
- no pending Proposal lost
- the grade is within noise of an uninterrupted run

It writes a log that is a deliverable. It is re-run against the full controller in ticket 15.

Spec: user story 69.

**Blocked by:** 04

**Status:** done

- [x] Script runs unattended and exits non-zero if any check fails
- [x] Log records kill times, restart times and each check's result
- [x] A run of the script passes against the current controller

## Comments

Script: `scripts/crash_recovery.py`; log: `reports/crash_recovery_public_02.md` (3 SIGKILLs, all checks pass; re-run in ticket 15). Grade tolerance is 0.03 absolute conclusion rate, a chosen figure rather than a measured noise floor.
