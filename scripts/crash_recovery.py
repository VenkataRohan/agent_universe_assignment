"""Crash-recovery evidence: SIGKILL the controller at random points through public_02 and prove it recovers.

    python scripts/crash_recovery.py                       # 3 kills, seed from the clock; writes reports/crash_recovery_public_02.md
    python scripts/crash_recovery.py --kills 3 --seed 7 --days 4 --out /tmp/log.md

Method. An uninterrupted run (`./run.sh` against a fresh simulator) sets the reference grade and how long
a run takes. A second run, against another fresh simulator of the same scenario, has its controller killed
with SIGKILL at random wall-clock moments inside that duration and restarted with the same `./run.sh`
command, so every restart resumes from the store. When the last life finishes, four checks are made from
the controller's store (read-only) and the simulator's HTTP API:

  1. no duplicate proposal IDs        (store and platform each hold every id at most once)
  2. no approval applied twice        (config history applies each approval and proposal at most once)
  3. no pending Proposal lost         (store and platform agree on every Proposal; none stuck mid-flight)
  4. grade within noise               (conclusion rate of the killed run vs the uninterrupted run)

Exit code 0 only if every check passes and every life exited as expected.
"""
from __future__ import annotations

import argparse
import collections
import os
import random
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
IN_FLIGHT_MID_STEP = ("submitting", "applying")
SENT_STATUSES = ("pending", "approved", "applying", "applied", "rejected", "expired", "lapsed")
DEFAULT_TOLERANCE = 0.03  # absolute points of conclusion rate


# -- the checks: pure, each returns a list of problems (empty means it passed) ----------------------
def check_no_duplicate_proposals(store: list[dict], approvals: list[dict]) -> list[str]:
    out = []
    for where, ids in (("store", [r["proposal_id"] for r in store]), ("platform", [a["proposal_id"] for a in approvals])):
        out += [f"{where} holds proposal id {p} {n} times" for p, n in collections.Counter(ids).items() if n > 1]
    return out


def check_no_double_apply(history: list[dict]) -> list[str]:
    out = []
    for key in ("approval_id", "proposal_id"):
        counts = collections.Counter(h.get(key) for h in history if h.get(key))
        out += [f"{key} {v} was applied {n} times" for v, n in counts.items() if n > 1]
    return out


def check_no_lost_proposals(store: list[dict], approvals: list[dict], history: list[dict]) -> list[str]:
    out = []
    by_pid = {r["proposal_id"]: r for r in store}
    sim_by_pid = {a["proposal_id"]: a for a in approvals}
    applied_approvals = {h["approval_id"] for h in history if h.get("approval_id")}
    applied_proposals = {h["proposal_id"] for h in history if h.get("proposal_id")}
    for a in approvals:
        if a["proposal_id"] not in by_pid:
            out.append(f"platform approval {a['approval_id']} ({a['proposal_id']}, {a['status']}) has no Proposal in the store")
    for r in store:
        pid, status = r["proposal_id"], r["status"]
        if status in IN_FLIGHT_MID_STEP:
            out.append(f"{pid} is stuck in {status} at the end of the run")
        if r["route"] == "approval" and status in SENT_STATUSES and pid not in sim_by_pid:
            out.append(f"{pid} is {status} in the store but the platform has no record of it")
        applied_on_platform = (r.get("approval_id") in applied_approvals) or (pid in applied_proposals)
        if applied_on_platform and status != "applied":
            out.append(f"{pid} was applied on the platform but the store says {status}")
        if status == "applied" and not applied_on_platform:
            out.append(f"{pid} is applied in the store but not in the platform's config history")
    return out


def check_grade_within_noise(reference: float, killed: float, tolerance: float = DEFAULT_TOLERANCE) -> list[str]:
    if abs(reference - killed) > tolerance:
        return [f"conclusion rate {killed:.3f} after the kills vs {reference:.3f} uninterrupted (tolerance {tolerance})"]
    return []


def kill_points(duration_s: float, n: int, seed: int | None = None) -> list[float]:
    """n random moments, in seconds after launch, inside the middle 80% of a run that takes duration_s."""
    rng = random.Random(seed)
    return sorted(rng.uniform(0.1 * duration_s, 0.9 * duration_s) for _ in range(n))


# -- plumbing ---------------------------------------------------------------------------------------
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
    for _ in range(600):
        try:
            if httpx.get(f"{url}/health").status_code == 200:
                return proc, url
        except httpx.TransportError:
            time.sleep(0.1)
    proc.terminate()
    raise SystemExit("simulator did not come up")


def launch(url: str, db: Path, log: Path) -> subprocess.Popen:
    env = {**os.environ, "CONTROLLER_DB": str(db), "PYTHON": sys.executable}
    return subprocess.Popen([str(ROOT / "run.sh"), url], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=log.open("ab"))  # run.sh execs python: this pid is the controller


def grade(run_dir: Path, baseline_dir: Path, out: Path) -> float:
    subprocess.run([sys.executable, "-m", "sim", "grade", "--run", str(run_dir), "--baseline", str(baseline_dir),
                    "--out", str(out)], cwd=ROOT, check=True, capture_output=True)
    m = re.search(r"(?m)^\| ALL \| ([\d.]+)", out.read_text())
    if not m:
        raise SystemExit(f"no ALL row in {out}")
    return float(m.group(1))


def store_rows(db: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT proposal_id, workflow_id, status, route, approval_id FROM proposals")]
    finally:
        conn.close()


def in_flight_summary(db: Path) -> str:
    try:
        counts = collections.Counter(r["status"] for r in store_rows(db) if r["status"] in ("submitting", "pending", "approved", "applying"))
    except sqlite3.Error:
        return "store not readable"
    return ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"


def platform_view(url: str) -> tuple[list[dict], list[dict]]:
    with httpx.Client(base_url=url, timeout=60) as http:
        workflows = [w["workflow_id"] for w in http.get("/workflows").json()]
        approvals, history = [], []
        for w in workflows:
            approvals += http.get("/approvals", params={"workflow_id": w}).json()
            history += http.get(f"/workflows/{w}/config/history").json()
    return approvals, history


def sim_state(url: str) -> dict:
    return httpx.get(f"{url}/sim/state", timeout=30).json()


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def uninterrupted(scenario: Path, tmp: Path, baseline: Path) -> tuple[float, float]:
    sim, url = start_sim(scenario, tmp / "ref_run")
    try:
        t0 = time.perf_counter()
        p = launch(url, tmp / "ref.db", tmp / "ref.log")
        if p.wait() != 0:
            raise SystemExit(f"the uninterrupted run exited {p.returncode}; see {tmp / 'ref.log'}")
        duration = time.perf_counter() - t0
    finally:
        sim.terminate(); sim.wait(10)
    return duration, grade(tmp / "ref_run", baseline, tmp / "ref_grade.md")


def killed_run(scenario: Path, tmp: Path, baseline: Path, points: list[float]) -> dict:
    db, log = tmp / "kill.db", tmp / "kill.log"
    sim, url = start_sim(scenario, tmp / "kill_run")
    lives: list[dict] = []
    try:
        t_launch = time.perf_counter()
        proc = launch(url, db, log)
        started = stamp()
        for i, at in enumerate(points, 1):
            while time.perf_counter() - t_launch < at and proc.poll() is None:
                time.sleep(0.05)
            if proc.poll() is not None:  # finished before its kill point
                lives.append({"life": i, "started": started, "killed": False, "exit": proc.returncode})
                proc = None
                break
            st = sim_state(url)
            proc.send_signal(signal.SIGKILL); proc.wait()
            killed_at = stamp()
            lives.append({"life": i, "started": started, "killed": True, "killed_at": killed_at,
                          "after_s": round(time.perf_counter() - t_launch, 1), "sim_minute": st["sim_minute"],
                          "in_flight": in_flight_summary(db)})
            proc = launch(url, db, log)
            started = stamp()
            lives[-1]["restarted_at"] = started
        if proc is not None:
            code = proc.wait()
            lives.append({"life": len(lives) + 1, "started": started, "killed": False, "exit": code})
        approvals, history = platform_view(url)
        finished = sim_state(url)["finished"]
    finally:
        sim.terminate(); sim.wait(10)
    rate = grade(tmp / "kill_run", baseline, tmp / "kill_grade.md")
    return {"lives": lives, "approvals": approvals, "history": history, "finished": finished, "rate": rate,
            "store": store_rows(db)}


def render(scenario: str, seed: int, tolerance: float, ref_s: float, ref_rate: float, run: dict, results: list[tuple[str, list[str]]]) -> str:
    out = [f"# Crash-recovery log — {scenario}", "",
           f"Run {datetime.now():%Y-%m-%d %H:%M}, seed {seed}. The controller (`./run.sh`) was SIGKILLed at random points and "
           f"restarted with the same command; each restart resumed from the store. An uninterrupted run took {ref_s:.0f} s.", "",
           "## Kills and restarts", "",
           "| life | started | killed at | after (s) | simulated minute | in flight at the kill | restarted at | exit |", "|---|---|---|---|---|---|---|---|"]
    for l in run["lives"]:
        if l["killed"]:
            out.append(f"| {l['life']} | {l['started']} | {l['killed_at']} | {l['after_s']} | {l['sim_minute']} | {l['in_flight']} | {l['restarted_at']} | SIGKILL |")
        else:
            out.append(f"| {l['life']} | {l['started']} | — | — | — | — | — | {l['exit']} |")
    out += ["", "## Checks", "", "| check | result |", "|---|---|"]
    for name, problems in results:
        out.append(f"| {name} | {'PASS' if not problems else 'FAIL'} |")
    out += ["", f"Platform at the end: {len(run['approvals'])} approvals, {len(run['history'])} config versions; "
                f"store: {len(run['store'])} Proposals ({', '.join(f'{n} {s}' for s, n in sorted(collections.Counter(r['status'] for r in run['store']).items()))}).",
            f"Conclusion rate (ALL): {run['rate']:.3f} after the kills, {ref_rate:.3f} uninterrupted; tolerance {tolerance}."]
    for name, problems in results:
        if problems:
            out += ["", f"### {name}: failures", ""] + [f"- {p}" for p in problems]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=str(ROOT / "scenarios/public_02_incidents.yaml"))
    ap.add_argument("--kills", type=int, default=3)
    ap.add_argument("--seed", type=int, help="seeds the kill points (default: from the clock, and printed)")
    ap.add_argument("--days", type=int, help="shrink the scenario to this many days")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    ap.add_argument("--out", default=str(ROOT / "reports/crash_recovery_public_02.md"))
    a = ap.parse_args(argv)
    seed = a.seed if a.seed is not None else int(time.time()) % 100000
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        scenario = shrink(Path(a.scenario), a.days, tmp) if a.days else Path(a.scenario)
        print(f"[{stamp()}] baseline + uninterrupted run (seed {seed})", flush=True)
        subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", str(scenario), "--run-dir", str(tmp / "base")],
                       cwd=ROOT, check=True, capture_output=True)
        ref_s, ref_rate = uninterrupted(scenario, tmp, tmp / "base")
        points = kill_points(ref_s, a.kills, seed)
        print(f"[{stamp()}] uninterrupted: {ref_s:.0f} s, conclusion rate {ref_rate:.3f}; killing at {[round(p) for p in points]} s", flush=True)
        run = killed_run(scenario, tmp, tmp / "base", points)
    problems_lives = [f"life {l['life']} exited {l['exit']}" for l in run["lives"] if not l["killed"] and l["exit"] != 0]
    problems_lives += [] if run["finished"] else ["the simulator never reached the end of the scenario"]
    made = sum(1 for l in run["lives"] if l["killed"])
    problems_lives += [f"only {made} of {a.kills} kills happened: the controller finished first"] if made < a.kills else []
    results = [
        ("run completed after the restarts", problems_lives),
        ("no duplicate proposal IDs", check_no_duplicate_proposals(run["store"], run["approvals"])),
        ("no approval applied twice", check_no_double_apply(run["history"])),
        ("no pending Proposal lost", check_no_lost_proposals(run["store"], run["approvals"], run["history"])),
        ("grade within noise of the uninterrupted run", check_grade_within_noise(ref_rate, run["rate"], a.tolerance)),
    ]
    text = render(Path(a.scenario).stem, seed, a.tolerance, ref_s, ref_rate, run, results)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text)
    print(text)
    return 0 if all(not p for _, p in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
