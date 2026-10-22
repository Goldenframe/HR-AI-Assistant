#!/usr/bin/env bash
# Check local Teleport proxies and required env before starting the bot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-demo}" # demo | corporate

port_open() {
  local port=$1
  if command -v nc >/dev/null; then
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1
  else
    (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1
  fi
}

check_port() {
  local name=$1 port=$2
  if port_open "$port"; then
    echo "OK  $name  127.0.0.1:$port"
    return 0
  fi
  echo "FAIL $name  127.0.0.1:$port недоступен" >&2
  return 1
}

fail=0

case "$MODE" in
  demo)
    check_port ai 62432 || fail=1
    ;;
  corporate|corp|atlassian)
    check_port ai 62432 || fail=1
    check_port confluence 62451 || fail=1
    check_port jira 62452 || fail=1
    ;;
  *)
    echo "Usage: $(basename "$0") {demo|corporate}" >&2
    exit 1
    ;;
esac

env_has() {
  local key=$1
  [[ -n "${!key:-}" ]] && return 0
  [[ -f "$ROOT/.env" ]] || return 1
  grep -E "^[[:space:]]*${key}=" "$ROOT/.env" | grep -vqE "^[[:space:]]*${key}=[[:space:]]*$"
}

if [[ -f "$ROOT/.env" ]]; then
  echo "OK  .env найден"
else
  echo "WARN .env нет — токен спросит при старте; для AI задайте AI_BASE_URL и AI_API_KEY или Hermes"
fi

if env_has AI_BASE_URL && env_has AI_API_KEY; then
  echo "OK  AI_BASE_URL и AI_API_KEY заданы"
elif [[ -n "${HERMES_CONFIG:-}" && -f "${HERMES_CONFIG}" ]]; then
  echo "OK  HERMES_CONFIG=$HERMES_CONFIG"
elif [[ -n "${LOCALAPPDATA:-}" && -f "${LOCALAPPDATA}/hermes/config.yaml" ]]; then
  echo "OK  Hermes (LOCALAPPDATA)"
elif [[ -f "$HOME/Library/Application Support/hermes/config.yaml" ]]; then
  echo "OK  Hermes (macOS Application Support)"
else
  echo "WARN AI-креды не найдены: задайте AI_BASE_URL+AI_API_KEY в .env"
fi

if [[ "$fail" -ne 0 ]]; then
  echo >&2
  echo "Поднимите туннели:  $ROOT/scripts/tunnels.sh up" >&2
  exit 1
fi

echo "preflight ($MODE): готово"
