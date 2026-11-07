# 13: Crash-recovery evidence

**What to build:** A script runs public_02, SIGKILLs the controller at 3 random points, restarts it each time, and then checks from the controller's store and the simulator API:
- no duplicate proposal IDs
- no approval applied twice
- no pending Proposal lost
- the grade is within noise of an uninterrupted run

It writes a log that is a deliverable. It is re-run against the full controller in ticket 15.

Spec: user story 69.

**Blocked by:** 04

**Status:** ready-for-agent

- [ ] Script runs unattended and exits non-zero if any check fails
- [ ] Log records kill times, restart times and each check's result
- [ ] A run of the script passes against the current controller
