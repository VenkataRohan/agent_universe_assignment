"""Seam-1 harness: the real simulator in-process behind FastAPI's TestClient.

Tests may import `sim` (the controller never does). Scenarios are public ones with only
`days` / `history_days` shrunk, as tests/test_sim.py does; hidden-truth sections are untouched.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sim.server as sim_server
from sim.scenario import load_scenario
from sim.world import World

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios"


@pytest.fixture()
def world(tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    """Start a fresh simulated world; returns an httpx client (TestClient) talking to it."""
    n = 0

    def start(name: str = "public_01_dev", days: int = 2, history_days: int = 2) -> TestClient:
        nonlocal n
        n += 1
        sc = load_scenario(str(SCENARIOS / f"{name}.yaml"))
        sc["days"], sc["history_days"] = days, history_days
        sim_server._world = World(sc, tmp_path / f"run{n}")
        return TestClient(sim_server.app)

    yield start
    sim_server._world = None


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "controller.db"
