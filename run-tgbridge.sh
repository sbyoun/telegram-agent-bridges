#!/usr/bin/env bash
# run-tgbridge.sh <instance> — launch one unified bridge instance.
# Loads instances/<instance>/.env, ensures a shared repo-root venv, runs the
# tgbridge package. Provider is chosen via BRIDGE_PROVIDER in the instance .env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTANCE="${1:?usage: run-tgbridge.sh <instance>}"
INST_DIR="$SCRIPT_DIR/instances/$INSTANCE"
ENV_FILE="$INST_DIR/.env"

[ -f "$ENV_FILE" ] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }

set -a
source "$ENV_FILE"
set +a
export BRIDGE_STATE_DIR="${BRIDGE_STATE_DIR:-$INST_DIR/state}"
mkdir -p "$BRIDGE_STATE_DIR"

VENV="${TGBRIDGE_VENV:-$SCRIPT_DIR/.venv}"
REQ="$SCRIPT_DIR/requirements.txt"
STAMP="$VENV/.requirements-installed"
if [[ ! -x "$VENV/bin/python" ]]; then
  "${PYTHON_BOOTSTRAP:-python3}" -m venv "$VENV"
fi
if [[ "$REQ" -nt "$STAMP" ]]; then
  "$VENV/bin/python" -m pip install -r "$REQ"
  touch "$STAMP"
fi

cd "$SCRIPT_DIR"
exec "$VENV/bin/python" -m tgbridge >> "$INST_DIR/bridge.log" 2>&1
