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


class Killed(Exception):
    """The controller process dying at an injected point. Nothing in the controller may catch it."""


class Trouble(httpx.BaseTransport):
    """The real simulator, with scripted trouble around it. No scenario file is involved.

    - `degrade`: workflow -> caller ID whose calls mostly go unanswered (live calls only, unless
      `degrade_history`). The simulator's own ground truth is untouched; only the events are rewritten.
    - `kill(...)`: die at the next matching request, before it reaches the simulator or after.
    - `lost_response(...)`: the next matching request reaches the simulator but answers 503.
    - `refuse(...)`: the next matching request never arrives and is answered with this 4xx.
    - `ttl_hours`: every approval request asks for this expiry, so it expires before an approver sees it.
    - `blank_summary`: every approval request goes out empty-summaried, so the approver must reject it.
    - Any config change whose proposal_id starts with `supervisor-` reads back as a supervisor edit.
      The simulator has no API for those, so a test makes one with PUT and this relabels it.
    - `lift` / `dampen`: workflow -> caller ID that looks better (one in five of its unanswered dials
      connects) or mildly worse (one in four of its answered calls never connects).
    - `approval_only`: the controller is shown policies without auto-apply and no spare caller IDs, so
      every change takes the approval path and nothing but the test's own trouble is in play.
    """

    def __init__(self, inner: httpx.BaseTransport, degrade: dict[str, str] | None = None, degrade_history: bool = False,
                 approval_only: bool = False):
        self._inner, self._degrade, self._degrade_history = inner, degrade or {}, degrade_history
        self._approval_only = approval_only
        self._kill: tuple[str, str, bool] | None = None
        self._lost: tuple[str, str] | None = None
        self._refuse: tuple[str, str, int, str] | None = None
        self.ttl_hours: float | None = None
        self.blank_summary = False
        self.lift: dict[str, str] = {}
        self.dampen: dict[str, str] = {}
        self._seen = 0
        self._lifted = self._dampened = 0

    def degrade(self, workflow: str, caller: str) -> None:
        self._degrade[workflow] = caller

    def kill(self, method: str, path_end: str, after_forward: bool = False) -> None:
        self._kill = (method, path_end, after_forward)

    def lost_response(self, method: str, path_end: str) -> None:
        self._lost = (method, path_end)

    def refuse(self, method: str, path_end: str, status: int, code: str) -> None:
        self._refuse = (method, path_end, status, code)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        request = self._rewrite_request(request)
        if self._refuse is not None and method == self._refuse[0] and path.endswith(self._refuse[1]):
            status, code = self._refuse[2:]
            self._refuse = None
            return httpx.Response(status, json={"error": code})
        armed = self._kill is not None and method == self._kill[0] and path.endswith(self._kill[1])
        if armed and not self._kill[2]:  # type: ignore[index]
            self._kill = None
            raise Killed(f"before {method} {path}")
        resp = self._inner.handle_request(request)
        if armed:
            self._kill = None
            resp.read()
            raise Killed(f"after {method} {path}")
        if self._lost is not None and method == self._lost[0] and path.endswith(self._lost[1]):
            self._lost = None
            resp.read()
            return httpx.Response(503, json={"error": "unavailable"})
        if path.startswith("/events/chunks/") and (self._degrade_history or not path.endswith("/0")):
            resp.read()
            lines = [self._rewrite_event(json.loads(l)) for l in resp.text.splitlines() if l]
            return httpx.Response(200, content="\n".join(json.dumps(l) for l in lines).encode())
        if path.endswith("/config/history"):
            resp.read()
            return httpx.Response(200, json=[self._relabel(e) for e in resp.json()])
        if path == "/workflows" and self._approval_only:
            resp.read()
            return httpx.Response(200, json=[self._approval_only_view(w, request) for w in resp.json()])
        return resp

    def _approval_only_view(self, workflow: dict, request: httpx.Request) -> dict:
        url = request.url.copy_with(path=f"/workflows/{workflow['workflow_id']}/config")
        config = self._inner.handle_request(httpx.Request("GET", url))
        config.read()
        in_use = sorted(c for dims in config.json()["config"]["allocations"].values() for c in dims.get("caller_id", {}))
        governance = {**workflow["governance"], "auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0}}
        return {**workflow, "governance": governance, "options": {**workflow["options"], "caller_id": in_use}}

    def _rewrite_request(self, request: httpx.Request) -> httpx.Request:
        if request.method != "POST" or request.url.path != "/approvals":
            return request
        body = json.loads(request.content)
        if self.ttl_hours is not None:
            body["expires_in_hours"] = self.ttl_hours
        if self.blank_summary:
            body["change_summary"] = ""
        return httpx.Request("POST", request.url, json=body)

    @staticmethod
    def _relabel(ev: dict) -> dict:
        if str(ev.get("proposal_id") or "").startswith("supervisor-"):
            return {**ev, "actor": "supervisor", "proposal_id": None}
        return ev

    def _rewrite_event(self, ev: dict) -> dict:
        if ev["type"] == "config_changed":
            return self._relabel(ev)
        if ev["type"] == "call" and ev["connected"] and ev["variant"]["caller_id"] == self._degrade.get(ev["workflow_id"]):
            self._seen += 1
            if self._seen % 5:  # four in five of its answered calls never connect
                return {**ev, "connected": False, "engaged": False, "disposition": "no_answer", "duration_s": 0,
                        "turns": 0, "qa": None}
        if ev["type"] == "call":
            return self._reshape_pickup(ev)
        return ev

    def _reshape_pickup(self, ev: dict) -> dict:
        caller, workflow = ev["variant"]["caller_id"], ev["workflow_id"]
        if not ev["connected"] and caller == self.lift.get(workflow):
            self._lifted += 1
            if self._lifted % 5 == 0:
                return {**ev, "connected": True, "engaged": False, "disposition": "short_hangup", "duration_s": 14, "turns": 2,
                        "qa": {"hallucination_flag": False, "flow_adherence": 1.0}}
        if ev["connected"] and caller == self.dampen.get(workflow):
            self._dampened += 1
            if self._dampened % 4 == 0:
                return {**ev, "connected": False, "engaged": False, "disposition": "no_answer", "duration_s": 0, "turns": 0, "qa": None}
        return ev


def troubled_client(client: httpx.Client, degrade: dict[str, str] | None = None, degrade_history: bool = False,
                    approval_only: bool = False) -> tuple[httpx.Client, Trouble]:
    trouble = Trouble(client._transport, degrade, degrade_history, approval_only)  # type: ignore[attr-defined]
    return httpx.Client(transport=trouble, base_url=client.base_url), trouble
