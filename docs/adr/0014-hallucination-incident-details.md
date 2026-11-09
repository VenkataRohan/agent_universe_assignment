# Hallucination incident details

The spec gives the detector's thresholds and the shape of the response. It leaves the following open.

- **Counted per tick and per prompt.** Hourly `cell_agg` can't give an exact rolling 2 hours, so ingestion also keeps connects and flags per workflow, 15-minute tick and prompt (`flag_ticks`). Detection tests each prompt in the window separately and acts only on the prompt still live, so calls on a prompt already replaced can't raise an incident against its successor, and old bad calls can't re-raise one after a fix.
- **A prompt change in the last 24 hours is taken to be the cause.** We tried dating the onset first and requiring the change to sit next to it, and that cleared the real culprit on public_02: flags are sparse enough that the estimate lands hours after the change. A false blame costs one validation run that still has to pass, while a false clear sends the fix to the LLM and may ship an unrelated prompt. When a change is blamed, the onset is the change itself; otherwise it is the likelihood-ratio estimate in `stats.spike_onset`.
- **With no prompt change to blame, the live prompt is validated first.** At α 0.001 tested every tick, a healthy prompt sometimes fires the detector, and on public_02 that swapped a healthy prompt for one that cost 0.139 engagement. If the live prompt passes, nothing says a change would help: the incident escalates to a person and the prompt stays. That prompt raises no new incident for 24 hours, so one spike can't spend credits every tick. If it fails, the LLM is asked as the spec says.
- **Known-good means most recently used, not blamed through a prompt change, still offered.** A prompt that took an incident with no change to blame is still a valid target, since it was never shown to be the cause. A fix that failed to bring the rate down is ruled out as well.
- **A known-good prompt that fails validation is not known-good,** so the LLM is asked after all. Otherwise a single noisy run would leave customers on a prompt we know is bad.
- **One fix per incident, chosen on validation pass rate.** Validation says nothing about engagement, so among candidates that pass there is no better basis. A rejected, expired or stale Proposal is rebuilt from the same validated candidate, without paying again.
- **"Back to baseline" means below the level that raises an incident and not significantly above baseline** (under 2x, and one-sided binomial p of at least 0.05, at 300 connects on the fix). We can't prove equality with baseline on 300 connects.
- **Escalated means a person takes over.** Except where the live prompt passed validation, no new incident is raised on that prompt again in this run, so the controller can't spend credits on a loop. Paging is ticket 12.
- **A rejected incident fix may be asked again after 2 hours, not 24.** This applies to caller-ID incidents too. The platform's request cap still limits how often.
- **The overlay fallback is for prompt fixes only.** A caller-ID incident already has auto-apply on its side. Policy limits still bind: the approver would reject a request beyond the platform's cap anyway.

## Consequences

- A workflow that can't reach 150 connects in 24 hours can never raise an incident. The window grows to reach them (ADR-0016).
- At α 0.001, tested every tick on every workflow, a healthy workflow will sometimes raise a false incident. It costs one validation run and an escalation.
- A real spike on a prompt that still passes validation is left to a person, because validation is a noisy measure of safety.
- A crash after a validation or an LLM suggestion returns, but before it is committed, pays for that one call again. Neither endpoint takes an idempotency key, so the "all mutating calls" rule can't cover them.
- A prompt change more than 24 hours old is not treated as the cause, so that incident goes to the LLM even though an earlier prompt exists.
