# 06: Hallucination incident response

**What to build:** When a workflow starts hallucinating, the controller raises an incident:
- **Detect:** over a rolling 2-hour window of at least 150 connects, the flag rate is at least 2× the workflow's baseline and the one-sided binomial test gives p < 0.001.
- **Candidate fix:** if a prompt change preceded onset, the candidate is the last known-good prompt. Otherwise call `/llm/suggest-fixes`.
- **Validate** each candidate (n=300) and propose only those meeting the policy's minimum pass rate, with the validation run attached as evidence.
- **Propose:** the fix gets an impact score of at least 0.9 and is exempt from the one-in-flight rule. If it undoes a supervisor edit, its summary says so explicitly. It may fall back to the policy's own limits when the preference overlay would delay it, and says why.
- **Verify** after apply: the flag rate returns to baseline within the next 300 connects, or the incident escalates.

At least 30 credits are always held in reserve for incidents.

Spec: user stories 37–44.

**Blocked by:** 04

**Status:** ready-for-agent

- [ ] On public_02 (slow test), the bad-prompt incident is detected, a validated fix with evidence is proposed, approved, applied and verified
- [ ] The fix summary names the supervisor edit it reverts
- [ ] No incident is raised on public_01 (no incidents in that scenario)
- [ ] LLM suggest is not called when a known-good prompt exists
- [ ] Credits never drop below the reserve through non-incident spending
- [ ] Time from onset to applied fix is recorded and shown in the audit
