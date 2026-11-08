# Decay and holdback release details

ADR-0008 fixes the test (a rolling 3-day one-sided comparison with the holdback at α 0.05) and the 7 clean days. It leaves open when the looks start, what a refused rollback does, and what "batched into the next Proposal" covers. We settled them as follows.

- **The first look waits for about three days of calls.** Looks run daily from 09:00, starting on the third calendar day after the promotion, so a promotion late in the day has a little under 72 hours at its first look. A shorter window would make the test noisier just when the winner has freshly passed a confirm, and a winner that decays at once is the unlikely case.
- **The window is the last 72 complete hours, on calls dialled under the promoted config or later.** Every config version is its own stratum, as in the screen and confirm (ADR-0010, 0011). Calls from a campaign locked to an earlier version are left out.
- **Each winner is tested on its own and rolled back on its own.** A Proposal returns only the dimensions that decayed, to what ran before the experiment. Any other winner stays on its holdback.
- **A rollback is an ordinary Proposal, not an incident.** It goes through approval, inside approver hours and the request cap, because welcome and voice are customer-facing (ADR-0002). A signal that can't go out yet waits and is sent when it can.
- **An expired or stale rollback is judged again on fresh data at once.** After a rejection or a lapse the winner keeps its traffic, and the next daily look can raise it again once the cooldown allows.
- **Every decay signal restarts the 7 clean days**, including a signal whose rollback was refused.
- **The release rides on the next non-incident Proposal that goes to an approver.** An incident fix must stand on its own, so that a doubt about the release can't delay it. Auto-applied changes can't carry it either, because they can't touch customer-facing dimensions.
- **A release is done when the Proposal carrying it is applied.** If that Proposal is rejected or expires, the release stays due and goes with the next one.
- **A supervisor edit to a promoted dimension ends that dimension's comparison.** Their choice stands and nothing is asked of it. When no held-back dimension is left, the experiment is aborted, as the spec has it for any supervisor edit.

## Consequences

- Daily looks at α 0.05 aren't corrected for repeats, so a winner with no real lift has a good chance of one false signal over its 7 days. A real winner rarely does. The cost of a false signal is one Proposal and going back to what ran before, which is the safe direction.
- A workflow that sends no other Proposal keeps its holdback indefinitely, at about 20% of the lift. That follows from never sending a release on its own.
- A release already in flight when a decay signal arrives can't be withdrawn, because the platform has no way to take back a request. If it is approved, the decaying winner briefly takes all the traffic and the rollback follows as soon as it may go out.
- A decayed winner can stay at 80% for up to a day longer than the signal when approver hours or the request cap hold the rollback.
