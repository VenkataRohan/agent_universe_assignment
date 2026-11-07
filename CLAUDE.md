# formi-controller

Outcome-first controller for the Formi simulator assignment. See `README.md` (the brief)
and `docs/SIMULATOR.md` (the HTTP contract).

## Ground rules (disqualifying if broken)

- The controller learns about the world **only through the simulator's HTTP API**.
- Never read anything below `# ---- hidden ground truth` in `scenarios/*.yaml`, and never
  read `runs/` (the simulator's own logs and `_truth/`). This applies to agents
  too, not just controller code: design decisions must not be informed by hidden truth.
- Never import `sim` from controller code, and never modify `sim/`.
- Reading `sim grade` reports in `reports/` is fine: they're the intended feedback.

## Agent skills

### Issue tracker

Local markdown: specs and tickets live under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
