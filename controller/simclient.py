"""Sim client: the only code that talks HTTP to the simulator.

Built from an injected `httpx.Client` (tests pass FastAPI's TestClient). 5xx responses and
transport errors are retried with backoff; 4xx responses surface as `SimError` carrying the
simulator's error code, for the caller to record as a decision outcome.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx


class SimError(Exception):
    """A non-retryable answer from the simulator (4xx), or retries exhausted."""

    def __init__(self, status: int, code: str, detail: Any = None):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail

    @property
    def retryable(self) -> bool:
        """It may succeed if tried again: a server error, or the request may never have arrived."""
        return self.status == 0 or self.status >= 500


class SimClient:
    def __init__(self, http: httpx.Client, retries: int = 5, backoff_s: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep):
        self._http = http
        self._retries = retries
        self._backoff_s = backoff_s
        self._sleep = sleep

    # -- reads ---------------------------------------------------------------------------------
    def state(self) -> dict:
        return self._json("GET", "/sim/state")

    def workflows(self) -> list[dict]:
        return self._json("GET", "/workflows")

    def config(self, workflow_id: str) -> dict:
        return self._json("GET", f"/workflows/{workflow_id}/config")

    def config_history(self, workflow_id: str) -> list[dict]:
        return self._json("GET", f"/workflows/{workflow_id}/config/history")

    def approvals(self, workflow_id: str) -> list[dict]:
        return self._json("GET", "/approvals", params={"workflow_id": workflow_id})

    def list_chunks(self, kind: str, after: int = -1) -> list[dict]:
        return self._json("GET", "/events/chunks", params={"kind": kind, "after": after})

    def chunk_lines(self, n: int) -> Iterator[str]:
        """Stream chunk `n` as raw NDJSON lines (kept verbatim for the audit trail).

        Streaming keeps memory flat however large the chunk (history arrives as one chunk). A
        failure mid-stream raises from the iterator; the caller's transaction then rolls back.
        """
        with self._http.stream("GET", f"/events/chunks/{n}") as r:
            if r.status_code >= 400:
                r.read()
                raise _as_error(r)
            yield from r.iter_lines()

    # -- governance ----------------------------------------------------------------------------
    def request_approval(self, body: dict) -> dict:
        """Safe to retry: `proposal_id` is the idempotency key, a repeat answers 409 duplicate_proposal_id."""
        return self._json("POST", "/approvals", json=body)

    def apply_approval(self, workflow_id: str, approval_id: str) -> dict:
        """One attempt only. After a failure the caller must look in config history before trying again."""
        return self._once("POST", f"/workflows/{workflow_id}/apply", {"approval_id": approval_id})

    def auto_apply(self, workflow_id: str, body: dict) -> dict:
        """One attempt only, for the same reason as `apply_approval`."""
        return self._once("PUT", f"/workflows/{workflow_id}/config", body)

    # -- time ----------------------------------------------------------------------------------
    def advance_to(self, target_minute: int) -> dict:
        """Move the simulated clock to `target_minute`; safe to retry.

        `POST /sim/advance` is not idempotent, so it is never retried blindly: after a failure
        the clock is re-read and only the remaining distance is requested. Returns the state.
        """
        delay = self._backoff_s
        for attempt in range(self._retries + 1):
            st = self.state()
            if st["finished"] or st["sim_minute"] >= target_minute:
                return st
            try:
                r = self._http.request("POST", "/sim/advance",
                                       json={"minutes": target_minute - st["sim_minute"]})
            except httpx.TransportError:
                pass
            else:
                if r.status_code == 409:  # finished
                    return self.state()
                if r.status_code < 400:
                    continue
                if r.status_code < 500:
                    raise _as_error(r)
            if attempt < self._retries:
                self._sleep(delay)
                delay *= 2
        raise SimError(0, "advance_failed", f"could not reach minute {target_minute}")

    # -- plumbing ------------------------------------------------------------------------------
    def _once(self, method: str, path: str, body: dict) -> Any:
        try:
            r = self._http.request(method, path, json=body)
        except httpx.TransportError as e:
            raise SimError(0, "transport_error", str(e)) from e
        if r.status_code >= 400:
            raise _as_error(r)
        return r.json()

    def _json(self, method: str, path: str, **kw: Any) -> Any:
        return self._request(method, path, **kw).json()

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        delay = self._backoff_s
        for attempt in range(self._retries + 1):
            try:
                r = self._http.request(method, path, **kw)
            except httpx.TransportError as e:
                if attempt == self._retries:
                    raise SimError(0, "transport_error", str(e)) from e
            else:
                if r.status_code < 400:
                    return r
                if r.status_code < 500:
                    raise _as_error(r)
                if attempt == self._retries:
                    raise _as_error(r)
            self._sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")


def _as_error(r: httpx.Response) -> SimError:
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text}
    if isinstance(body, dict):
        return SimError(r.status_code, str(body.get("error") or r.reason_phrase), body.get("detail"))
    return SimError(r.status_code, r.reason_phrase, body)
