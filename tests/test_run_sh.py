"""`./run.sh <SIM_URL>` against a real `sim serve` subprocess on the full public_01 scenario."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from controller import queries

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.slow


@contextmanager
def sim_serving(scenario: str, run_dir: Path) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", scenario,
                             "--run-dir", str(run_dir), "--port", str(port), "--force"],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{url}/health").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


def _run_sh(url: str, db: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "CONTROLLER_DB": str(db), "PYTHON": sys.executable}
    return subprocess.run([str(ROOT / "run.sh"), url], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=600)


def test_run_sh_drives_public_01_to_finished_and_starts_fresh_on_a_restarted_simulator(tmp_path):
    db = tmp_path / "state" / "controller.db"
    scenario = "scenarios/public_01_dev.yaml"

    with sim_serving(scenario, tmp_path / "run1") as url:
        r = _run_sh(url, db)
        assert r.returncode == 0, r.stderr[-2000:]
        st = httpx.get(f"{url}/sim/state").json()
    assert st["finished"] is True
    assert queries.health(db)["events_ingested"] == st["last_event_id"]

    # A restarted simulator is a new world: run.sh moves the old store aside and runs fresh.
    with sim_serving(scenario, tmp_path / "run2") as url:
        r = _run_sh(url, db)
        assert r.returncode == 0, r.stderr[-2000:]
        assert "moved it to" in r.stderr
        st = httpx.get(f"{url}/sim/state").json()
    assert st["finished"] is True
    assert queries.health(db)["events_ingested"] == st["last_event_id"]
    assert list(db.parent.glob("controller.db.stale-*"))


def test_cli_without_fresh_flag_refuses_a_restarted_simulator_with_a_clear_message(tmp_path):
    db = tmp_path / "controller.db"
    with sim_serving("scenarios/public_01_dev.yaml", tmp_path / "run1") as url:
        httpx.post(f"{url}/sim/advance", json={"minutes": 600}, timeout=60)
        r = subprocess.run([sys.executable, "-m", "controller", "--sim-url", url, "--db", str(db)],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-2000:]  # ran to the end
    with sim_serving("scenarios/public_01_dev.yaml", tmp_path / "run2") as url:
        r = subprocess.run([sys.executable, "-m", "controller", "--sim-url", url, "--db", str(db)],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2
    assert "refusing to resume" in r.stderr and "behind the store's cursor" in r.stderr
