# Caller-ID exploration and capped incident steps

The spec says a degraded caller ID is moved "within max_shift, otherwise proposed", and an untested one is capped at 10% "then given an equal share or dropped". It leaves the details open. We settled them as follows.

- **Capped incident step.** A move larger than `max_shift` auto-applies one step of exactly `max_shift`, and the remainder goes to an approver. Without this, Kestrel's exception in ADR-0002 would never fire on a 50/50 split with `max_shift` 0.2. Each number gets at most one auto-applied move per rolling day, because repeated steps would add up to more than the policy allows.
- **An unproven number is never raised.** Weight leaving a degraded number goes only to numbers with 300 dials in the last 7 days. A number being explored also stays out of the degradation test's comparison pool until it is concluded, so a lucky newcomer can't get a proven number flagged.
- **Keep or drop reuses the screen rule** (`stats.screen`): not clearly worse, and above the numbers in use on its point estimate. A number at parity may be dropped. That loses nothing in expectation and keeps customers off a number that hasn't earned traffic.
- **Dropped means dropped.** A number that was dropped, or moved off as degraded, is not explored again in this run, even after its evidence ages out.
- **The cap binds the controller, not supervisors.** Only numbers the controller starts are capped and concluded. A supervisor's own choice is left alone, and exploration starts nothing for 7 days after a supervisor changes a workflow's caller IDs.
- **Exploration decides every tick, not at 09:00.** Every approver's hours start after 09:00, so a 09:00-only decision could never reach approval-only workflows. Its decisions not to act are recorded when they change, not on every tick.

## Consequences

- On approval-only workflows exploration uses standalone requests until batching exists, one in flight at a time.
- A number that recovers after being dropped stays unused for the rest of the run.
