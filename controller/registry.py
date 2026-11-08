"""Workflow registry: the effective governance policy of a workflow.

The API states a workflow's policy. A tenant's preference overlay is layered on top and can only
tighten it: whatever an overlay says, the effective policy never allows more than the API's.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .simtime import MINUTES_PER_DAY

REQUEST_MARGIN_HOURS = 2  # a request in an approver's last 2 hours would sit overnight


@dataclass(frozen=True)
class Overlay:
    """A tenant's stated wishes, from its memo."""
    max_requests_per_24h: int | None = None
    not_before_hour: int | None = None


OVERLAYS: Mapping[str, Overlay] = {
    "kestrel_finance": Overlay(max_requests_per_24h=2, not_before_hour=11),
}


@dataclass(frozen=True)
class EffectivePolicy:
    approver_start_hour: int
    approver_end_hour: int  # exclusive
    max_requests_per_24h: int

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
    return EffectivePolicy(start, end, cap)
