# 11: Consolidated supervisor view

**What to build:** `controller status <workflow>` is the supervisor's single screen. It shows what's being tested, what's winning, what needs their approval, and what changed today, including auto-applied changes. Decisions not to act are visible too: experiment inconclusive, promotion blocked by harm, incident below threshold. Cohorts appear under their plain-language names. Without a workflow argument, it lists every workflow in a compact form.

Spec: user stories 18, 72–73.

**Blocked by:** 06, 08, 10

**Status:** ready-for-agent

- [ ] On a finished public_02 run, `controller status ks_cancel` shows the incident, its fix and today's changes on one screen
- [ ] Pending Proposals are listed with impact score, ordered by it
- [ ] Decisions not to act appear with their reason
- [ ] Output is readable by a non-engineer (plain-language cohort and change names)
- [ ] A change auto-applied as incident response is labelled as such (ADR-0002; the Proposal's `incident_response` field carries it)
