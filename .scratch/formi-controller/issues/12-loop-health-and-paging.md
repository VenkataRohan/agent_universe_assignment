# 12: Loop health and paging

**What to build:** `controller health` shows ingest lag, decisions per hour, credit burn and active alerts. Exactly four conditions page, each message saying what to look at:
1. ingest lag above 2 ticks
2. credits below the reserve
3. an incident unfixed after 4 hours
4. apply failures (a lapsed approval, or a repeated stale-base loop)

Everything else is a note: rejections, expirations, inconclusive experiments, single retries. 5xx responses and timeouts are retried with backoff. One workflow's error never stops the loop for the others.

Spec: user stories 68, 74–76.

**Blocked by:** 06

**Status:** done

- [x] `controller health` shows lag, decisions/hour, credit burn and alerts during a run
- [x] Each of the four page conditions raises a page with an actionable message when induced
- [x] Non-page conditions appear only as notes
- [x] An injected 5xx on one workflow's call is retried and does not stop other workflows
