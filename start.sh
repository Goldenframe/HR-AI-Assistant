#!/usr/bin/env bash
# Cross-platform launcher (macOS/Linux), mirrors start.bat.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export PYTHONUTF8=1
mkdir -p data/tmp
export TMPDIR="$ROOT/data/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"

PYTHON=python3
if command -v python3.12 >/dev/null; then
  PYTHON=python3.12
elif command -v python3.11 >/dev/null; then
  PYTHON=python3.11
elif command -v python3.10 >/dev/null; then
  PYTHON=python3.10
fi

exec "$PYTHON" bot.py "$@"
