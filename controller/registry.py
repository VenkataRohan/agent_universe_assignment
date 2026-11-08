"""Workflow registry: the effective governance policy of a workflow.

The API states a workflow's policy. A tenant's preference overlay is layered on top and can only
tighten it: whatever an overlay says, the effective policy never allows more than the API's.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .simtime import MINUTES_PER_DAY

REQUEST_MARGIN_HOURS = 2  # a request in an approver's last 2 hours would sit overnight
CUSTOMER_FACING_DIMS = frozenset({"welcome_message", "voice", "prompt_version", "cohorts"})  # ADR-0002
INCIDENT_RESPONSE_DIMS = frozenset({"caller_id"})  # what "notify before change" still lets an incident move


@dataclass(frozen=True)
class Overlay:
    """A tenant's stated wishes, from its memo."""
    max_requests_per_24h: int | None = None
    not_before_hour: int | None = None
    notify_before_change: bool = False  # no auto-apply, except moving weight off a degraded caller ID


OVERLAYS: Mapping[str, Overlay] = {
    "kestrel_finance": Overlay(max_requests_per_24h=2, not_before_hour=11, notify_before_change=True),
}


@dataclass(frozen=True)
class EffectivePolicy:
    approver_start_hour: int
    approver_end_hour: int  # exclusive
    max_requests_per_24h: int
    auto_apply_dims: frozenset[str] = frozenset()
    incident_auto_apply_dims: frozenset[str] = frozenset()
    max_shift: float = 0.0

    def may_request_at(self, minute: int) -> bool:
        """Is this simulated minute inside the hours a request may be sent?"""
        minute_of_day = minute % MINUTES_PER_DAY
        return (self.approver_start_hour * 60 <= minute_of_day
                < (self.approver_end_hour - REQUEST_MARGIN_HOURS) * 60)


def effective_policy(workflow: Mapping, overlays: Mapping[str, Overlay] = OVERLAYS) -> EffectivePolicy:
    governance = workflow["governance"]
    start, end = governance["approver_hours"]
    cap = governance["max_requests_per_24h"]
    overlay = overlays.get(workflow["tenant_id"])
    if overlay is not None:
        if overlay.not_before_hour is not None:
            start = max(start, overlay.not_before_hour)
        if overlay.max_requests_per_24h is not None:
            cap = min(cap, overlay.max_requests_per_24h)
    auto_apply = governance["auto_apply"]
    allowed = frozenset(auto_apply["allowed_dims"]) - CUSTOMER_FACING_DIMS if auto_apply["enabled"] else frozenset()
    notify = overlay is not None and overlay.notify_before_change
    return EffectivePolicy(start, end, cap, frozenset() if notify else allowed,
                           allowed & INCIDENT_RESPONSE_DIMS if notify else allowed, auto_apply["max_shift"])
