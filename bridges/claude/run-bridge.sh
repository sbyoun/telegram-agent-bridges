#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
set -a
source "$SCRIPT_DIR/.env"
set +a

if [[ -z "${PYTHON_BIN:-}" ]]; then
  VENV_DIR="${CLAUDE_BRIDGE_VENV:-$SCRIPT_DIR/.venv}"
  REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
  INSTALL_STAMP="$VENV_DIR/.requirements-installed"
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    "${PYTHON_BOOTSTRAP:-python3}" -m venv "$VENV_DIR"
  fi
  if [[ "$REQUIREMENTS_FILE" -nt "$INSTALL_STAMP" ]]; then
    "$VENV_DIR/bin/python" -m pip install -r "$REQUIREMENTS_FILE"
    touch "$INSTALL_STAMP"
  fi
  PYTHON_BIN="$VENV_DIR/bin/python"
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/bridge.py" >> "$SCRIPT_DIR/bridge.log" 2>&1
