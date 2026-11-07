#!/usr/bin/env bash
# Starts the controller and runs it until the scenario finishes. No Docker; only requirements.txt.
#   ./run.sh <SIM_URL>
# The store is state/controller.db (override with CONTROLLER_DB). If that store belongs to a
# different world (other scenario, or a restarted simulator), it is moved aside and the run starts
# fresh, so unattended runs need no manual step. Same world: the controller resumes from its cursor.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
fi
exec "$PY" -m controller --sim-url "$SIM_URL" --db "${CONTROLLER_DB:-state/controller.db}" --fresh-if-mismatch
