# Holm correction on the harm check, and an incident window that grows

Two corrections after reading our own graded runs. Neither was tuned to a public result, and neither produced a winner on a public scenario.

**The cohort harm check is now Holm-corrected across the workflow's cohorts, at family-wise α 0.10.** It was α 0.10 per cohort, uncorrected, on the reasoning that a harm check should err towards blocking. With four cohorts, that gave a candidate with no real effect about a 34% chance of blocking at every look, and a blocked confirm stops the whole experiment. A check that fires that often stops telling anyone about harm. Holm keeps the family-wise rate at any dependence between cohorts, and still names each harmed cohort.

**Hallucination detection takes the last 2 hours, or as far back as it needs for 150 connects, up to 24 hours.** A fixed 2-hour window meant a workflow under 75 connects an hour could never raise an incident. Per prompt, ticks are added newest first. The window's length depends on connects and never on flags, so the exact binomial test stays valid at any length.

## Consequences

- The harm check has less power on one small cohort: the most significant of four must now clear 0.025. Clear harm still blocks (ks_cancel's V2 at p = 0.015 on public_02).
- Harm is checked at up to 3 looks. Simulated with no real effect, four cohorts and cumulative looks, any block across the looks happens about 20% of the time, not 10%. We left the looks uncorrected so that real harm stops a confirm early.
- A quiet workflow can now raise false incidents. On public_01, `nw_kyc` raised one at D6 15:15 (9 flags in 154 connects). The live prompt passed validation, so it went to a person with no change, at a cost of 3 credits.
- The first morning ticks on a busy workflow now reach back into yesterday's calls, so a morning spike is seen sooner.
- A workflow under 150 connects in 24 hours remains undetectable.
