#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

ROOT_DIR="$(pwd -P)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/trpg_engine.pid"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
APP_PID=""
START_PORT="${1:-${ZRIC_PORT:-8000}}"
START_HOST="${2:-${ZRIC_HOST:-0.0.0.0}}"

if [[ ! "$START_PORT" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] Invalid port: $START_PORT" >&2
    echo "Usage: ./start.sh [port] [host]" >&2
    exit 1
fi
if [[ "$START_PORT" -gt 65535 ]]; then
    echo "[ERROR] Invalid port: $START_PORT" >&2
    echo "Usage: ./start.sh [port] [host]" >&2
    exit 1
fi

mkdir -p "$RUNTIME_DIR"

pid_matches_launcher() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1

    local proc_cwd=""
    proc_cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    [[ "$proc_cwd" == "$ROOT_DIR" ]] || return 1

    local cmdline=""
    cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
    [[ "$cmdline" == *"$PYTHON_BIN"* && "$cmdline" == *"$ROOT_DIR/main.py"* ]]
}

stop_pid() {
    local pid="$1"
    local reason="$2"
    if pid_matches_launcher "$pid"; then
        echo "[cleanup] Stopping $reason Python process PID $pid..."
        kill "$pid" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 "$pid" 2>/dev/null || return 0
            sleep 0.25
        done
        kill -9 "$pid" 2>/dev/null || true
    fi
}

cleanup_stale_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local old_pid=""
        old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        stop_pid "$old_pid" "previous launcher-managed"
        rm -f "$PID_FILE"
    fi
}

cleanup_current_pid() {
    if [[ -n "${APP_PID:-}" ]]; then
        stop_pid "$APP_PID" "current launcher-managed"
    fi
    if [[ -f "$PID_FILE" && "$(cat "$PID_FILE" 2>/dev/null || true)" == "${APP_PID:-}" ]]; then
        rm -f "$PID_FILE"
    fi
}

cleanup_stale_pid
trap cleanup_current_pid EXIT INT TERM HUP

echo "====================================================="
echo " Z.R.I.C TRPG Engine - Linux one-click launcher"
echo "====================================================="
echo

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "[1/3] Creating local virtual environment..."
    if command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv
    elif command -v python >/dev/null 2>&1; then
        python -m venv .venv
    else
        echo "[ERROR] Python 3 was not found. Please install Python 3.10+ and retry." >&2
        exit 1
    fi
else
    echo "[1/3] Local virtual environment found."
fi

echo "[2/3] Installing or updating dependencies..."
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt

echo
echo "[3/3] Starting Z.R.I.C TRPG Engine..."
echo "     Bind: $START_HOST:$START_PORT"
echo "     GM console: http://<server-ip>:$START_PORT/"
echo "     Press Ctrl+C in this terminal to stop the server."
echo

"$PYTHON_BIN" "$ROOT_DIR/main.py" --host "$START_HOST" --port "$START_PORT" --no-browser &
APP_PID="$!"
printf '%s\n' "$APP_PID" >"$PID_FILE"
echo "[launcher] Server Python PID: $APP_PID"

set +e
wait "$APP_PID"
EXIT_CODE="$?"
set -e

exit "$EXIT_CODE"
