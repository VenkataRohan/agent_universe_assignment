# 05: Auto-apply path and caller-ID exploration

**What to build:** Operational changes take the auto-apply path where the effective policy allows it: every changed dimension is in `allowed_dims` and within `max_shift`. Customer-facing dimensions (welcome message, voice, prompt version, cohorts) are never auto-applied, even where policy would allow it. A tenant whose overlay says "notify before change" (Kestrel) gets no auto-apply at all, except moving weight off a degraded caller ID, which is labelled as incident response. A caller ID with no recent evidence is capped at 10% of traffic until it has 300 dials, then given an equal share or dropped. A `403 auto_apply_not_permitted` is recorded as a decision outcome and falls back to the approval path. `controller report --weekly` lists every auto-applied operational change, with what changed and why.

Spec: user stories 46–49, 51, 62; ADR-0002.

**Blocked by:** 04

**Status:** ready-for-agent

- [ ] On nw_renewal, caller-ID changes auto-apply within max_shift
- [ ] No customer-facing dimension is ever auto-applied on any workflow (verified by config history actors)
- [ ] On ks_cancel, no auto-apply occurs except a degraded-caller-ID shift
- [ ] An untested caller ID never exceeds 10% of traffic before it has 300 dials of evidence
- [ ] A 403 from auto-apply leads to an approval-path Proposal, and both are visible in the audit
- [ ] `controller report --weekly` lists auto-applied changes with rationale
