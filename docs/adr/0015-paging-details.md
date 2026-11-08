# Paging details

The spec fixes the four conditions that page. It leaves open what keeps a page active, how a loop can report lag it would otherwise die of, and what counts as unfixed or as a stale loop. We settled them as follows.

- **Alerts are re-judged at the end of every tick and stored.** `controller health` only reads, so a page must not depend on someone running it. Each alert is raised once, logged at ERROR with `PAGE`, and cleared when its condition stops. A restart keeps the record, so it does not page again.
- **A failed catch-up mid-run no longer ends the loop.** The clock keeps moving, as it would in production, and nothing new is decided until the store is current. Reconciling goes on, because Proposals follow the platform and not the store, so an approval doesn't lapse waiting for ingest. Otherwise the lag could never exceed one tick and the condition could never fire. A failure during `start()` still raises: with no history there is nothing to run on.
- **"Unfixed" starts counting at detection, not onset.** Onset is an estimate that can sit a day back, which would page on the tick of detection.
- **An escalated incident stays unfixed until the prompt changes.** Escalating hands it to a person, and paging is how the person hears. The exception is an incident closed because the live prompt passed validation: the controller treats that spike as answered, so it is a note. An incident in `verifying` has a fix in force and does not page; if the fix fails it escalates.
- **A stale-base loop is three Proposals of one workflow and kind going stale in a row because the base moved.** One or two are a supervisor editing, which is normal. A Proposal that went stale for another reason (request hours ended, the platform lost or refused it) breaks the streak. Proposals still in flight don't break the streak, or each rebuild would clear the page and raise it again.
- **An apply failure stays active for a simulated day at most.** The controller rebuilds these on its own, so nothing else may end one. A lapse also clears when a later Proposal of its kind is applied, and a stale loop when one of its kind ends any other way.
- **Decisions per hour is per simulated hour.** It counts what the decide steps recorded and leaves out the loop's own errors and retries. Decisions not to act are recorded only when they change, so a quiet loop shows a low number.
- **`_guarded` catches every `Exception`, not only `SimError`.** The rule is that one workflow's error never stops the others, and a hidden scenario can break a decide step in ways the simulator's errors don't cover.

- **Calls that aren't safe to repeat stay at one attempt.** Apply, auto-apply, LLM suggestions and validation runs could apply or pay twice if retried blindly. A failed apply is looked up in config history and tried again next tick. A failed suggestion or validation is an `error` note, and the incident tries again next tick.

## Consequences

- If ingest is down at the scenario's last tick, the run ends with the page raised and the last chunks unread, where it used to crash.
- A programming error in a decide step becomes an `error` note on every tick instead of a crash, so it is only as visible as `controller health` and the log make it.
- An apply that keeps failing with 5xx stays `applying` and appears as repeated notes. It isn't one of the two apply failures the ticket names.
- If the clock itself can't be moved after all its retries, the loop stops and nothing pages. There is no tick to page from, and a restart resumes.
- The credit page can trail a spend by one tick, because the balance is read from the state at the start of the tick.
