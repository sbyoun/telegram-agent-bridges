#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
set -a
source "$SCRIPT_DIR/.env"
set +a

exec python3 "$SCRIPT_DIR/bridge.py" >> "$SCRIPT_DIR/bridge.log" 2>&1
