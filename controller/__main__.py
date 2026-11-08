"""CLI: run the controller, or ask the store questions while it runs.

    python -m controller --sim-url http://127.0.0.1:8700 --db state/controller.db     # run the loop
    python -m controller health --db state/controller.db                            # read-only
    python -m controller status [workflow] --db state/controller.db                 # read-only
    python -m controller why <workflow> --at "D3 14:00" --db state/controller.db    # read-only
    python -m controller heard <lead_id> --db state/controller.db                   # read-only
    python -m controller report --weekly --db state/controller.db                   # read-only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import httpx

from . import queries, simtime
from .loop import Controller, ResumeRefused


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="controller")
    ap.add_argument("--db", default="state/controller.db", help="the controller's SQLite store")
    ap.add_argument("--sim-url", help="simulator base URL (runs the loop)")
    ap.add_argument("--fresh-if-mismatch", action="store_true",
                    help="if the store belongs to another world, move it aside and start fresh "
                         "instead of refusing (used by run.sh so unattended runs need no manual step)")
    sub = ap.add_subparsers(dest="cmd")
    h = sub.add_parser("health", help="cursor, events ingested and ingest lag")
    h.add_argument("--db", default=argparse.SUPPRESS)
    st = sub.add_parser("status", help="each workflow's analysis partition and history baselines")
    st.add_argument("workflow", nargs="?", help="just this workflow (default: all)")
    st.add_argument("--db", default=argparse.SUPPRESS)
    w = sub.add_parser("why", help="the change in force at a moment: Proposal, evidence, approval, aftermath, experiment")
    w.add_argument("workflow")
    w.add_argument("--at", required=True, help='a simulated time, e.g. "D3 14:00"')
    w.add_argument("--db", default=argparse.SUPPRESS)
    hd = sub.add_parser("heard", help="every call one lead received, and why that config was live")
    hd.add_argument("lead_id")
    hd.add_argument("--db", default=argparse.SUPPRESS)
    rp = sub.add_parser("report", help="the weekly summary: every auto-applied change, what changed and why")
    rp.add_argument("--weekly", action="store_true", required=True)
    rp.add_argument("--db", default=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.cmd == "health":
        return _health(Path(a.db))
    if a.cmd == "status":
        return _status(Path(a.db), a.workflow)
    if a.cmd == "why":
        return _why(Path(a.db), a.workflow, a.at)
    if a.cmd == "heard":
        return _heard(Path(a.db), a.lead_id)
    if a.cmd == "report":
        return _report(Path(a.db))
    if not a.sim_url:
        ap.error("--sim-url is required to run the loop")
    return _run(a.sim_url, Path(a.db), a.fresh_if_mismatch)


def _run(sim_url: str, db: Path, fresh_if_mismatch: bool) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("controller")
    with httpx.Client(base_url=sim_url, timeout=120) as http:
        c = Controller(http, db)
        try:
            c.start()
        except ResumeRefused as e:
            c.close()
            if not fresh_if_mismatch:
                print(f"controller: refusing to resume: {e}", file=sys.stderr)
                return 2
            aside = db.with_name(f"{db.name}.stale-{time.strftime('%Y%m%dT%H%M%S')}")
            for suffix in ("", "-wal", "-shm"):
                if Path(f"{db}{suffix}").exists():
                    Path(f"{db}{suffix}").rename(f"{aside}{suffix}")
            log.warning("store did not match this world (%s); moved it to %s and starting fresh", e, aside)
            c = Controller(http, db)
            c.start()
        try:
            c.run()
        finally:
            c.close()
    return 0


def _health(db: Path) -> int:
    if not db.exists():
        print(f"controller: no store at {db}", file=sys.stderr)
        return 1
    h = queries.health(db)
    if not h["started"]:
        print("controller: store exists but the controller has not started yet")
        return 0
    rows = [
        ("scenario", h["scenario"]),
        ("cursor", f"chunk {h['cursor_chunk']}, ingested through {simtime.label(h['ingested_through_minute'])}"
                   f" (minute {h['ingested_through_minute']})"),
        ("events ingested", f"{h['events_ingested']} (last event_id {h['last_event_id']})"),
        ("ingest lag", f"{h['ingest_lag_ticks']:g} ticks (simulator at {simtime.label(h['sim_minute_seen'])})"
                       + (f"; last ingest {h['seconds_since_last_ingest']}s ago"
                          if h["seconds_since_last_ingest"] is not None else "")),
    ]
    for k, v in rows:
        print(f"{k:<18}{v}")
    return 0


def _status(db: Path, workflow: str | None) -> int:
    if not db.exists():
        print(f"controller: no store at {db}", file=sys.stderr)
        return 1
    workflows = queries.status(db, workflow)["workflows"]
    if not workflows:
        print(f"controller: no analysis yet{f' for workflow {workflow}' if workflow else ''}", file=sys.stderr)
        return 1
    for i, w in enumerate(workflows):
        if i:
            print()
        _print_workflow_status(w)
    return 0


def _print_workflow_status(w: dict) -> None:
    b, p = w["baselines"], w["partition"]
    print(f"{w['workflow_id']}  (primary KPI: {w['primary_kpi']})")
    print(f"  baselines   pickup {_pct(b['pickup_rate'])}   engagement {_pct(b['engagement_rate'])}   "
          f"hallucination flags {_pct(b['hallucination_flag_rate'])} of connects   ({b['dials']:,} dials in history)")
    if p["feature"] is None:
        print("  partition   none: no feature splits this workflow's traffic")
        return
    print(f"  partition   by {p['feature'].replace('_', ' ')}: cohorts differ by {p['spread'] * 100:.1f} points "
          f"on {w['primary_kpi']}")
    for c in p["cohorts"]:
        h = w["cohort_history"][c["id"]]
        print(f"    {c['name']:<38}{c['id']:<28}{_pct(h['share']):>6} of traffic   "
              f"{w['primary_kpi']} {_pct(h['primary_kpi_rate'])}")


def _why(db: Path, workflow: str, at: str) -> int:
    if not db.exists():
        print(f"controller: no store at {db}", file=sys.stderr)
        return 1
    try:
        w = queries.why(db, workflow, queries.minute_at(db, at))
    except (KeyError, ValueError) as e:
        print(f"controller: {e.args[0]}", file=sys.stderr)
        return 1
    c = w["change"]
    print(f"{workflow} at {w['at']}: config version {w['config_version']}")
    print(f"  change      {c['actor']} at {c['applied_at'] or 'start'}"
          f"{'; changed ' + ', '.join(c['changed_dims']) if c['changed_dims'] else ''}")
    p = w["proposal"]
    if p is None:
        print("  proposal    none: the controller did not make this change")
    else:
        print(f"  proposal    {p['proposal_id']} ({p['status']}): {p['change_summary']}")
        print(f"  rationale   {p['rationale']}")
        print(f"  impact      score {p['impact_score']:.2f}, {p['affected_leads']:,} leads affected")
        print(f"  diff        {json.dumps(p['diff'], sort_keys=True)}")
        print(f"  dry run     {json.dumps(p['dry_run']['overall'], sort_keys=True)}; "
              f"cohorts at risk: {', '.join(p['dry_run']['cohorts_at_risk']) or 'none'}")
        print(f"  evidence    {json.dumps(w['evidence'], sort_keys=True)}")
    a = w["approval"]
    if a is not None:
        print(f"  approval    {a['approval_id']}: {a['status']} by the client's approver at {a['decided_at']}"
              f"{'; ' + '; '.join(a['reasons']) if a['reasons'] else ''}")
    after = w["aftermath"]
    print(f"  aftermath   this version: {_version_line(after['under_this_version'])}")
    if after["under_previous_version"] is not None:
        print(f"              version before: {_version_line(after['under_previous_version'])}")
    n = after["next_change"]
    print(f"              next change: {'none' if n is None else 'version ' + str(n['version']) + ' by ' + n['actor'] + ' at ' + n['applied_at']}")
    if w["experiment"] is not None:
        print(f"  experiment  {_experiment_line(w['experiment'])}")
    if w["incident"] is not None:
        print(f"  incident    {_incident_line(w['incident'])}")
    print("  decisions")
    for d in w["decisions"]:
        print(f"    {simtime.label(d['minute'])}  {d['kind']:<10}{d['outcome']:<22}{d['proposal_id'] or ''}"
              f"{_decision_reason(d['detail'])}")
    return 0


def _incident_line(i: dict) -> str:
    line = (f"{i['incident_id']}: {i['status']}; {i['bad_prompt']} flagged from {simtime.label(i['onset_minute'])}, "
            f"detected {simtime.label(i['detected_minute'])}")
    if i["applied_minute"] is not None:
        line += (f"; fix {i['fix_prompt']} applied {simtime.label(i['applied_minute'])}, "
                 f"{i['minutes_onset_to_applied']} minutes after onset")
    return line + (f"; ended {simtime.label(i['end_minute'])}: {i['end_reason']}" if i["end_minute"] is not None else "")


def _experiment_line(e: dict) -> str:
    plans = e["design"]["dimensions"]
    line = (f"{e['experiment_id']}: {e['status']} ({e['last_decision']} at {e['decided_at']}); "
            + "; ".join(f"{d} {p['control']} against {', '.join(p['challengers'])}" for d, p in sorted(plans.items())))
    if e["candidates"]:
        line += "; candidates " + ", ".join(f"{d} {o}" for d, o in sorted(e["candidates"].items()))
    eliminated = [o for view in (e["dimensions"] or {}).values() for o in view.get("eliminated", [])]
    line += f"; eliminated {', '.join(eliminated)}" if eliminated else ""
    if e["confirm_version"] is not None:
        line += f"; confirm looks {e['confirm_looks']}"
    for d, v in sorted(e["verdicts"].items()):
        if "candidate" in v:
            line += f"; {d} {v['candidate']}: {v['status']}"
    for d, h in sorted(e["holdbacks"].items()):
        line += f"; {d} holdback on {h['control']}"
    return line


def _decision_reason(detail: dict) -> str:
    """Why a decision came out as it did, for the kinds of decision that have a reason to give."""
    for key in ("hold", "why", "reasons", "error", "violations"):
        if detail.get(key):
            return f"  ({detail[key]})"
    return ""


def _version_line(k: dict) -> str:
    return (f"{k['dials']:,} dials, pickup {_pct(k['pickup_rate'])}, engagement {_pct(k['engagement_rate'])}; pickup by "
            f"caller ID {', '.join(f'{c} {_pct(r)}' for c, r in sorted(k['pickup_by_caller_id'].items())) or 'none'}")


def _heard(db: Path, lead_id: str) -> int:
    if not db.exists():
        print(f"controller: no store at {db}", file=sys.stderr)
        return 1
    h = queries.heard(db, lead_id)
    if not h["calls"]:
        print(f"controller: no calls recorded for lead {lead_id}", file=sys.stderr)
        return 1
    print(f"{lead_id}  (workflow {h['workflow_id']}, {len(h['calls'])} calls)")
    for call in h["calls"]:
        v, cfg = call["variant"], call["config"]
        print(f"  {call['ts']}  attempt {call['attempt_no']}  {call['disposition']}")
        print(f"    heard   welcome_message {v['welcome_message']}, voice {v['voice']}, caller_id {v['caller_id']}, "
              f"prompt_version {v['prompt_version']} (cohort {call['cohort_id']})")
        print(f"    config  version {cfg['version']}, live since {cfg['applied_at'] or 'start'}, set by {cfg['actor']}"
              f"{', Proposal ' + cfg['proposal_id'] if cfg['proposal_id'] else ''}"
              f"{', Approval ' + cfg['approval_id'] if cfg['approval_id'] else ''}")
        if cfg["change_summary"]:
            print(f"    because {cfg['change_summary']}")
    return 0


def _report(db: Path) -> int:
    if not db.exists():
        print(f"controller: no store at {db}", file=sys.stderr)
        return 1
    weeks = queries.weekly_report(db)["weeks"]
    if not weeks:
        print("No auto-applied changes.")
    for week in weeks:
        print(f"{week['label']}: {len(week['changes'])} auto-applied change(s)")
        for c in week["changes"]:
            print(f"  {c['at']}  {c['workflow_id']}  version {c['version']}  {c['proposal_id']}"
                  f"{'  [incident response]' if c['incident_response'] else ''}")
            print(f"    what  {'; '.join(c['what'])}")
            print(f"    why   {c['why'] or 'not recorded'}")
    return 0


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.1%}"


if __name__ == "__main__":
    sys.exit(main())
