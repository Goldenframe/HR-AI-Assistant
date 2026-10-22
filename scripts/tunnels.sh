#!/usr/bin/env bash
# Manage local Teleport app proxies with fixed ports matching .env.example.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="$ROOT/data/tunnels"
# name:port — must stay in sync with .env.example
APPS=(ai:62432 confluence:62451 jira:62452)

usage() {
  echo "Usage: $(basename "$0") {up|down|status|logs}"
  echo "  up      start tsh proxy app for ai/confluence/jira"
  echo "  down    stop proxies started by this script"
  echo "  status  show pid / port / reachability"
  echo "  logs    tail proxy logs"
}

need_tsh() {
  command -v tsh >/dev/null || {
    echo "tsh не найден. Установите Teleport CLI и выполните tsh login." >&2
    exit 1
  }
}

# Read selected KEY=VALUE lines from .env without sourcing the whole file.
env_get() {
  local key=$1 file="$ROOT/.env"
  [[ -f "$file" ]] || return 0
  local line
  line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n1 || true)"
  [[ -n "$line" ]] || return 0
  line="${line#*=}"
  line="${line%$'\r'}"
  if [[ ${#line} -ge 2 && ( "${line:0:1}" == '"' && "${line: -1}" == '"' || "${line:0:1}" == "'" && "${line: -1}" == "'" ) ]]; then
    line="${line:1:${#line}-2}"
  fi
  printf '%s' "$line"
}

ensure_tsh_login() {
  need_tsh
  if tsh status >/dev/null 2>&1; then
    echo "OK  tsh: сессия уже активна"
    return 0
  fi

  local proxy user auth
  proxy="${TSH_PROXY:-$(env_get TSH_PROXY)}"
  user="${TSH_USER:-$(env_get TSH_USER)}"
  auth="${TSH_AUTH:-$(env_get TSH_AUTH)}"

  if [[ -z "$proxy" || -z "$user" || -z "$auth" ]]; then
    echo "Нет активной сессии tsh." >&2
    echo "Задайте TSH_PROXY, TSH_USER и TSH_AUTH в .env (см. .env.example)" >&2
    echo "или выполните вручную:" >&2
    echo "  tsh login --proxy=intranet.rdclr.ru --user=you@redcollar.ru --auth=hello.rdclr.ru" >&2
    exit 1
  fi

  echo "tsh login --proxy=$proxy --user=$user --auth=$auth"
  tsh login --proxy="$proxy" --user="$user" --auth="$auth"
}

port_open() {
  local port=$1
  if command -v nc >/dev/null; then
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1
  else
    (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1
  fi
}

pid_alive() {
  local pid=$1
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

cmd_up() {
  ensure_tsh_login
  mkdir -p "$RUN_DIR"
  local entry name port pidfile logfile
  for entry in "${APPS[@]}"; do
    name="${entry%%:*}"
    port="${entry##*:}"
    pidfile="$RUN_DIR/$name.pid"
    logfile="$RUN_DIR/$name.log"

    if [[ -f "$pidfile" ]] && pid_alive "$(cat "$pidfile")"; then
      echo "уже запущено: $name (pid $(cat "$pidfile"), порт $port)"
      continue
    fi
    rm -f "$pidfile"

    if port_open "$port"; then
      echo "порт $port уже открыт — пропускаю $name (чужой процесс или Connect)"
      continue
    fi

    nohup tsh proxy app "$name" -p "$port" >"$logfile" 2>&1 &
    echo $! >"$pidfile"
    echo "старт: $name → 127.0.0.1:$port (pid $!, лог $logfile)"
  done

  # Give proxies a moment, then report reachability.
  sleep 1
  cmd_status
}

cmd_down() {
  local entry name pidfile pid
  for entry in "${APPS[@]}"; do
    name="${entry%%:*}"
    pidfile="$RUN_DIR/$name.pid"
    if [[ ! -f "$pidfile" ]]; then
      echo "нет pid-файла: $name"
      continue
    fi
    pid="$(cat "$pidfile")"
    if pid_alive "$pid"; then
      kill "$pid" 2>/dev/null || true
      # tsh may spawn children; best-effort wait
      for _ in 1 2 3 4 5; do
        pid_alive "$pid" || break
        sleep 0.2
      done
      if pid_alive "$pid"; then
        kill -9 "$pid" 2>/dev/null || true
      fi
      echo "остановлен: $name (pid $pid)"
    else
      echo "уже не работает: $name (stale pid $pid)"
    fi
    rm -f "$pidfile"
  done
}

cmd_status() {
  local entry name port pidfile pid state
  printf '%-12s %-8s %-10s %s\n' NAME PORT PID PORT_OPEN
  for entry in "${APPS[@]}"; do
    name="${entry%%:*}"
    port="${entry##*:}"
    pidfile="$RUN_DIR/$name.pid"
    pid="-"
    if [[ -f "$pidfile" ]]; then
      pid="$(cat "$pidfile")"
      if ! pid_alive "$pid"; then
        pid="$pid (dead)"
      fi
    fi
    if port_open "$port"; then
      state=yes
    else
      state=no
    fi
    printf '%-12s %-8s %-10s %s\n' "$name" "$port" "$pid" "$state"
  done
}

cmd_logs() {
  mkdir -p "$RUN_DIR"
  local files=("$RUN_DIR"/*.log)
  if [[ ! -e "${files[0]}" ]]; then
    echo "Логов пока нет. Сначала: $(basename "$0") up"
    exit 1
  fi
  tail -n 50 -f "${files[@]}"
}

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  *) usage; exit 1 ;;
esac
