# Hallucination incident details

The spec gives the detector's thresholds and the shape of the response. It leaves the following open.

- **Counted per tick and per prompt.** Hourly `cell_agg` can't give an exact rolling 2 hours, so ingestion also keeps connects and flags per workflow, 15-minute tick and prompt (`flag_ticks`). Detection tests each prompt in the window separately and acts only on the prompt still live, so calls on a prompt already replaced can't raise an incident against its successor, and old bad calls can't re-raise one after a fix.
- **A prompt change in the last 24 hours is taken to be the cause.** We tried dating the onset first and requiring the change to sit next to it, and that cleared the real culprit on public_02: flags are sparse enough that the estimate lands hours after the change. A false blame costs one validation run that still has to pass, while a false clear sends the fix to the LLM and may ship an unrelated prompt. When a change is blamed, the onset is the change itself; otherwise it is the likelihood-ratio estimate in `stats.spike_onset`.
- **Known-good means most recently used, never incident-marked, still offered.** A prompt an incident was raised on, or a fix that failed to bring the rate down, can't be known-good.
- **A known-good prompt that fails validation is not known-good,** so the LLM is asked after all. Otherwise a single noisy run would leave customers on a prompt we know is bad.
- **One fix per incident, chosen on validation pass rate.** Validation says nothing about engagement, so among candidates that pass there is no better basis. A rejected, expired or stale Proposal is rebuilt from the same validated candidate, without paying again.
- **"Back to baseline" means below the level that raises an incident and not significantly above baseline** (under 2x, and one-sided binomial p of at least 0.05, at 300 connects on the fix). We can't prove equality with baseline on 300 connects.
- **Escalated means a person takes over.** No new incident is raised on that prompt again in this run, so the controller can't spend credits on a loop. Paging is ticket 12.
- **The overlay fallback is for prompt fixes only.** A caller-ID incident already has auto-apply on its side. Policy limits still bind: the approver would reject a request beyond the platform's cap anyway.

## Consequences

- A workflow that can't reach 150 connects in 2 hours can never raise an incident: on public_02, `nw_kyc` runs about 55 connects an hour.
- At α 0.001, tested every tick on every workflow, a healthy workflow will sometimes raise a false incident. The spec accepts that rate; a false one still needs a validated fix and a human approval.
- A crash after a validation or an LLM suggestion returns, but before it is committed, pays for that one call again. Neither endpoint takes an idempotency key, so the "all mutating calls" rule can't cover them.
- A prompt change more than 24 hours old is not treated as the cause, so that incident goes to the LLM even though an earlier prompt exists.
