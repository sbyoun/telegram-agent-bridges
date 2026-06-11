#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
set -a
source "$SCRIPT_DIR/.env"
set +a

# The mcp package needs Python >= 3.10. Pick the first suitable interpreter,
# preferring uv (handles standalone builds cleanly), then a 3.1x python3.
pick_bootstrap() {
  if [[ -n "${PYTHON_BOOTSTRAP:-}" ]]; then echo "$PYTHON_BOOTSTRAP"; return; fi
  for c in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
  done
  echo python3
}

if [[ -z "${PYTHON_BIN:-}" ]]; then
  VENV_DIR="${TELEGRAM_MCP_VENV:-$SCRIPT_DIR/.venv}"
  REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
  INSTALL_STAMP="$VENV_DIR/.requirements-installed"
  BOOTSTRAP="$(pick_bootstrap)"
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    if command -v uv >/dev/null 2>&1; then
      uv venv --python "$BOOTSTRAP" "$VENV_DIR"
    else
      "$BOOTSTRAP" -m venv "$VENV_DIR"
    fi
  fi
  if [[ "$REQUIREMENTS_FILE" -nt "$INSTALL_STAMP" ]]; then
    if command -v uv >/dev/null 2>&1; then
      uv pip install --python "$VENV_DIR/bin/python" -r "$REQUIREMENTS_FILE"
    else
      "$VENV_DIR/bin/python" -m pip install -r "$REQUIREMENTS_FILE"
    fi
    touch "$INSTALL_STAMP"
  fi
  PYTHON_BIN="$VENV_DIR/bin/python"
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/server.py" >> "$SCRIPT_DIR/server.log" 2>&1
