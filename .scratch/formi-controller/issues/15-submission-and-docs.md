# 15: Grade reports, SUBMISSION.md and docs

**What to build:** This ticket produces the final deliverables:
- `sim grade` reports for public_01 and public_02 against their baselines
- the crash test re-run against the full controller
- a complete `SUBMISSION.md` from the template:
  - assumptions, and scope with its choosing principle
  - each stakeholder's accepted, declined and wrong claims, plus a ready-to-paste reply
  - architecture diagram, the learning loop, governance state machines, the incident timeline from public_02
  - scale numbers and reliability
  - a real `controller why` query with its output
  - interpreted results, trade-offs, known weaknesses, and the production plan
- a README section a new engineer can run, debug and extend the controller from

Spec: user stories 77–79.

**Blocked by:** 01–14

**Status:** done

- [x] `reports/` holds public_01 and public_02 grade reports, the load report and the crash-test log
- [x] Every SUBMISSION.md template heading is filled; none deleted
- [x] Each of the six memos has accepted / declined / wrong-claim notes and a ≤120-word reply
- [x] SUBMISSION.md §10 contains a real audit query and its real output
- [x] A teammate can run, debug and extend from the README alone

## Comments

Grade reports were regenerated from HEAD and came out identical to the earlier runs. The crash test was re-run on HEAD. The load report stands: since it was measured, the controller has changed only in the read-only CLI.

Two things left for the author: the hours-spent field at the top of `SUBMISSION.md`, and ticket 09's last box, which stays open because no public scenario reached a real rollback (`SUBMISSION.md` §13 says so).
