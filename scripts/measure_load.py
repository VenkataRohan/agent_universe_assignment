"""Load measurement: run a scenario through the controller and record what it cost.

    python scripts/measure_load.py                          # public_03, in-process, instrumented
    python scripts/measure_load.py --run-sh                 # the same, but through ./run.sh as a subprocess
    python scripts/measure_load.py --days 1 --out /tmp/m.json

Method. The simulator runs as its own `sim serve` subprocess. In the default mode the controller
runs in this process, so each tick can be split into its three costs: the simulator advancing
(not the controller's), ingest, and decide. "Decision lag" for a tick is ingest + decide: the wall
time between the simulator publishing a tick's events and the controller having acted on them.
Controller cost per simulated day is the sum of those over the day's ticks; simulator time is
reported apart. Peak RSS is the process high-water mark (ru_maxrss), sampled at each day boundary.
With --run-sh the controller is the real `./run.sh` process, sampled with `ps` every 0.5 s, and only
whole-run wall clock, per-day wall clock (from the simulator clock) and RSS are available.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import resource
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MINUTES_PER_DAY = 1440


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out else 0.0


def self_peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


def shrink(scenario: Path, days: int, into: Path) -> Path:
    text = re.sub(r"(?m)^days: \d+", f"days: {days}", scenario.read_text(), count=1)
    out = into / scenario.name
    out.write_text(text)
    return out


def start_sim(scenario: Path, run_dir: Path) -> tuple[subprocess.Popen, str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", str(scenario), "--run-dir",
                             str(run_dir), "--port", str(port), "--force"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(6000):
        try:
            if httpx.get(f"{url}/health").status_code == 200:
                return proc, url
        except httpx.TransportError:
            time.sleep(0.1)
    proc.terminate()
    raise SystemExit("simulator did not come up")


class PeakSampler(threading.Thread):
    """Samples another process's RSS with `ps` until stopped."""
    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self.pid, self.peak, self._stop_evt = pid, 0.0, threading.Event()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            self.peak = max(self.peak, rss_mb(self.pid))
            self._stop_evt.wait(0.5)

    def stop(self) -> float:
        self._stop_evt.set(); self.join()
        return self.peak


def measure_in_process(url: str, db: Path, sim_pid: int) -> dict:
    from controller.loop import Controller
    sim_sampler = PeakSampler(sim_pid); sim_sampler.start()
    ticks: list[dict] = []
    cur: dict = {}

    def timed(obj, name: str, key: str):
        fn = getattr(obj, name)
        def wrapper(*a, **kw):
            t = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                cur[key] = cur.get(key, 0.0) + time.perf_counter() - t
        setattr(obj, name, wrapper)

    with httpx.Client(base_url=url, timeout=120) as http:
        c = Controller(http, db)
        t_start = time.perf_counter()
        c.start()
        startup_s = time.perf_counter() - t_start
        timed(c.sim, "advance_to", "advance"); timed(c, "_try_catch_up", "ingest"); timed(c, "_decide", "decide")
        days: dict[int, dict] = {}
        t_run = time.perf_counter()
        events_before = _events_ingested(c)
        done = False
        while not done:
            cur.clear()
            t0 = time.perf_counter()
            done = c.step()
            now = c._state["sim_minute"]
            day = (now - 1) // MINUTES_PER_DAY
            tick = {"minute": now, "day": day, "total": time.perf_counter() - t0, **{k: cur.get(k, 0.0) for k in ("advance", "ingest", "decide")}}
            ticks.append(tick)
            d = days.setdefault(day, {"day": day, "wall_s": 0.0, "controller_s": 0.0, "sim_s": 0.0, "ingest_s": 0.0, "decide_s": 0.0})
            d["wall_s"] += tick["total"]; d["sim_s"] += tick["advance"]
            d["ingest_s"] += tick["ingest"]; d["decide_s"] += tick["decide"]
            d["controller_s"] += tick["total"] - tick["advance"]
            d["peak_rss_mb"] = self_peak_rss_mb()
        wall_s = time.perf_counter() - t_run
        events = _events_ingested(c) - events_before
        c.close()
    ingest_s = sum(t["ingest"] for t in ticks)
    lag = [t["ingest"] + t["decide"] for t in ticks]
    return {
        "mode": "in-process", "startup_s": round(startup_s, 1), "wall_s": round(wall_s, 1),
        "events_ingested_live": events, "ingest_s": round(ingest_s, 1),
        "ingest_events_per_s": round(events / ingest_s) if ingest_s else None,
        "decision_lag_s": {"p50": round(statistics.median(lag), 3), "p95": round(pct(lag, .95), 3), "max": round(max(lag), 3)},
        "days": [{k: round(v, 1) if isinstance(v, float) else v for k, v in d.items()} for d in days.values()],
        "controller_peak_rss_mb": round(self_peak_rss_mb()), "simulator_peak_rss_mb": round(sim_sampler.stop()),
    }


def _events_ingested(c) -> int:
    return int(c.store.conn.execute("SELECT events_ingested FROM run").fetchone()[0])


def measure_run_sh(url: str, db: Path, sim_pid: int) -> dict:
    env = {**os.environ, "CONTROLLER_DB": str(db), "PYTHON": sys.executable}
    t0 = time.perf_counter()
    proc = subprocess.Popen([str(ROOT / "run.sh"), url], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctrl, sim = PeakSampler(proc.pid), PeakSampler(sim_pid)
    ctrl.start(); sim.start()
    day_marks: dict[int, float] = {}
    with httpx.Client(base_url=url, timeout=60) as http:
        while proc.poll() is None:
            try:
                m = http.get("/sim/state").json()["sim_minute"]
                day_marks.setdefault(m // MINUTES_PER_DAY, time.perf_counter() - t0)
            except Exception:
                pass
            time.sleep(1)
    wall = time.perf_counter() - t0
    marks = sorted(day_marks.items())
    per_day = [{"day": d, "wall_s": round((marks[i + 1][1] if i + 1 < len(marks) else wall) - t, 1)} for i, (d, t) in enumerate(marks)]
    # run.sh execs python, so proc.pid is the controller itself.
    return {"mode": "run.sh", "exit_code": proc.returncode, "wall_s": round(wall, 1), "days": per_day,
            "controller_peak_rss_mb": round(ctrl.stop()), "simulator_peak_rss_mb": round(sim.stop())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=str(ROOT / "scenarios/public_03_load.yaml"))
    ap.add_argument("--days", type=int, help="shrink the scenario to this many days")
    ap.add_argument("--run-sh", action="store_true", help="drive ./run.sh instead of the in-process controller")
    ap.add_argument("--out", help="write the result as JSON here")
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        scenario = shrink(Path(a.scenario), a.days, tmp_p) if a.days else Path(a.scenario)
        proc, url = start_sim(scenario, tmp_p / "sim_run")
        try:
            fn = measure_run_sh if a.run_sh else measure_in_process
            result = fn(url, tmp_p / "controller.db", proc.pid)
        finally:
            proc.terminate(); proc.wait(10)
    result["scenario"] = scenario.name
    result["host"] = {"cpus": os.cpu_count(), "platform": sys.platform}
    text = json.dumps(result, indent=2)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0 if result.get("exit_code", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
