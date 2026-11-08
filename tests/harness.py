"""Seam-1 helpers: recount events straight from the API, inject faults into its transport, and
script a small world behind the same HTTP API for cases the real simulator never produces."""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
import httpx


def all_events(client: httpx.Client) -> Iterator[dict]:
    """Every event the world has published so far, read straight from the API (the test oracle)."""
    chunks = client.get("/events/chunks", params={"kind": "history"}).json()
    chunks += client.get("/events/chunks", params={"kind": "events"}).json()
    for ch in sorted(chunks, key=lambda c: c["chunk"]):
        for line in client.get(f"/events/chunks/{ch['chunk']}").text.splitlines():
            yield json.loads(line)


def recount_calls(client: httpx.Client) -> dict[tuple[str, str], Counter]:
    """Calls counted per (workflow, local date) straight from the API's events."""
    out: dict[tuple[str, str], Counter] = {}
    for ev in all_events(client):
        if ev["type"] != "call":
            continue
        c = out.setdefault((ev["workflow_id"], ev["ts"][:10]), Counter())
        c["dials"] += 1
        c["connected"] += ev["connected"]
        c["engaged"] += ev["engaged"]
        c["hallucination_flags"] += bool(ev["qa"] and ev["qa"]["hallucination_flag"])
    return out


class ChunkFaults(httpx.BaseTransport):
    """Wraps the simulator's transport and damages the body of chunk `chunk` when it is fetched.

    The first `times` fetches are damaged, later ones are served intact. The body keeps the first
    `keep` fraction of its lines; `cut_mid_line` also leaves half of the next line, otherwise the
    cut falls on a line boundary and looks like a valid, shorter chunk.
    """

    def __init__(self, inner: httpx.BaseTransport, chunk: int, times: int = 1, keep: float = 0.5,
                 cut_mid_line: bool = False):
        self._inner, self._path, self._left = inner, f"/events/chunks/{chunk}", times
        self._keep, self._mid = keep, cut_mid_line
        self.damaged = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self._inner.handle_request(request)
        if request.url.path != self._path or self._left <= 0:
            return resp
        resp.read()
        self._left -= 1
        self.damaged += 1
        lines = resp.text.splitlines()
        cut = int(len(lines) * self._keep)
        kept = lines[:cut] + ([lines[cut][:20]] if self._mid else [])
        return httpx.Response(200, content="\n".join(kept).encode(), headers=resp.headers)


# A call in a scripted world: (features, hour, attempt_no, connected, engaged, how many such calls).
ScriptedCalls = tuple[dict[str, str], int, int, bool, bool, int]


def features(lang: str = "hi", city_tier: str = "1", age_band: str = "31-45", value_band: str = "mid") -> dict[str, str]:
    return {"lang": lang, "city_tier": city_tier, "age_band": age_band, "value_band": value_band}


class ScriptedWorld(httpx.BaseTransport):
    """The simulator's read API over a hand-written history (one lead per call, all on D-1).

    Nothing here touches a scenario: it only lets a test state exact counts, so expected values
    can be worked out by hand.
    """

    def __init__(self, primary: str, calls: list[ScriptedCalls], workflow_id: str = "wf"):
        self.workflow = {"workflow_id": workflow_id, "cadence": [1], "objective": {"primary": primary}}
        events: list[dict] = []

        def emit(ev: dict) -> None:
            events.append({**ev, "workflow_id": workflow_id, "campaign_id": "c1", "event_id": len(events) + 1})

        emit({"type": "campaign_created", "ts": "2027-01-03T08:00:00", "tenant_id": "t", "leads": 0,
              "config_version_at_creation": 1})
        n = 0
        for feats, hour, attempt, connected, engaged, count in calls:
            for _ in range(count):
                n += 1
                lead, ts = f"l{n}", f"2027-01-03T{hour:02d}:00:00"
                emit({"type": "lead", "ts": "2027-01-03T08:00:00", "lead_id": lead, "person_id": f"p{n}", "features": feats})
                emit({"type": "call", "ts": ts, "tenant_id": "t", "lead_id": lead, "person_id": f"p{n}",
                      "attempt_no": attempt, "campaign_day_index": 0, "config_version": 1, "cohort_id": "default",
                      "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
                      "connected": connected, "engaged": engaged,
                      "disposition": "concluded_interested" if engaged else "short_hangup", "duration_s": 10, "turns": 1,
                      "qa": {"hallucination_flag": False, "flow_adherence": 1.0} if connected else None})
        self._events = events

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path, query = request.url.path, request.url.params
        if path == "/sim/state":
            return httpx.Response(200, json={"scenario": "scripted", "ts": "2027-01-04T00:00:00+05:30", "sim_minute": 0,
                                             "tick_minutes": 15, "finished": False})
        if path == "/workflows":
            return httpx.Response(200, json=[self.workflow])
        if path == "/events/chunks":
            meta = {"chunk": 0, "kind": "history", "events": len(self._events), "first_event_id": 1,
                    "last_event_id": len(self._events), "until_minute": 0, "until_ts": "2027-01-04T00:00:00"}
            return httpx.Response(200, json=[meta] if query.get("kind") == "history" and int(query.get("after", -1)) < 0 else [])
        if path == "/events/chunks/0":
            return httpx.Response(200, text="\n".join(json.dumps(e) for e in self._events))
        return httpx.Response(404, json={"error": "not_scripted", "detail": path})


def scripted_client(primary: str, calls: list[ScriptedCalls]) -> httpx.Client:
    return httpx.Client(transport=ScriptedWorld(primary, calls), base_url="http://scripted")


def faulty_client(client: httpx.Client, **kw) -> tuple[httpx.Client, ChunkFaults]:
    faults = ChunkFaults(client._transport, **kw)  # type: ignore[attr-defined]
    return httpx.Client(transport=faults, base_url=client.base_url), faults
