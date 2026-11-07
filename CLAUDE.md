# formi-controller

Outcome-first controller for the Formi simulator assignment. See `README.md` (the brief)
and `docs/SIMULATOR.md` (the HTTP contract).

## Ground rules (disqualifying if broken)

- The controller learns about the world **only through the simulator's HTTP API**.
- Never read anything below `# ---- hidden ground truth` in `scenarios/*.yaml`, and never
  read `runs/` (the simulator's own logs and `_truth/`). This applies to agents
  too, not just controller code: design decisions must not be informed by hidden truth.
- Never import `sim` from controller code, and never modify `sim/`.
- Agents may read only `sim/server.py` (the API surface) and `tests/test_sim.py` inside the
  simulator. Everything else in `sim/` encodes hidden world mechanics; don't read it.
- Tests may run the simulator (in-process via FastAPI's `TestClient`, or as a subprocess) and
  may shrink a public scenario's `days`, but must never author or edit hidden-truth sections.
- Reading `sim grade` reports in `reports/` is fine: they're the intended feedback.

## Agent skills

### Issue tracker

Local markdown: specs and tickets live under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
