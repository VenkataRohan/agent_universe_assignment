"""Credits: the only way to spend them, so nothing but an incident can touch the reserve (user story 43)."""
from __future__ import annotations

import math

from .simclient import SimClient

RESERVE = 30
SUGGEST_COST = 3


class ReserveHeld(Exception):
    """Spending this would take credits into the reserve, and it isn't for an incident."""


class Credits:
    def __init__(self, sim: SimClient):
        self._sim = sim

    def suggest_fixes(self, workflow_id: str, problem: str, incident: bool = False) -> dict:
        self._check(SUGGEST_COST, incident)
        return self._sim.suggest_fixes(workflow_id, problem)

    def validate_prompt(self, workflow_id: str, prompt_version: str, n: int, incident: bool = False) -> dict:
        self._check(math.ceil(n / 100), incident)
        return self._sim.validate_prompt(workflow_id, prompt_version, n)

    def _check(self, cost: int, incident: bool) -> None:
        if incident:
            return
        left = self._sim.state()["credits_remaining"]
        if left - cost < RESERVE:
            raise ReserveHeld(f"{cost} credits would leave {left - cost:g}, below the reserve of {RESERVE}")
