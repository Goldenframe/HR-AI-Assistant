#!/usr/bin/env bash
# One-shot local demo: Teleport AI tunnel + fictional team bot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONUTF8=1
mkdir -p data/tmp
export TMPDIR="$ROOT/data/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"

if ! command -v python3 >/dev/null; then
  echo "python3 не найден" >&2
  exit 1
fi

# Start AI (+ others if free); demo needs only AI for reports.
"$ROOT/scripts/tunnels.sh" up

if ! "$ROOT/scripts/preflight.sh" demo; then
  exit 1
fi

# Prefer Homebrew / pyenv Python 3.10+ when available.
PYTHON=python3
if command -v python3.12 >/dev/null; then
  PYTHON=python3.12
elif command -v python3.11 >/dev/null; then
  PYTHON=python3.11
elif command -v python3.10 >/dev/null; then
  PYTHON=python3.10
fi

echo
echo "Запуск демо: $PYTHON bot.py --demo-team"
echo "Остановка: Ctrl+C; туннели: scripts/tunnels.sh down"
echo
exec "$PYTHON" bot.py --demo-team
