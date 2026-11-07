"""Seam-1 helpers: recount events straight from the API, and inject faults into its transport."""
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


def faulty_client(client: httpx.Client, **kw) -> tuple[httpx.Client, ChunkFaults]:
    faults = ChunkFaults(client._transport, **kw)  # type: ignore[attr-defined]
    return httpx.Client(transport=faults, base_url=client.base_url), faults
