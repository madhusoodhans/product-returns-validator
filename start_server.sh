#!/usr/bin/env bash
# Starts (or restarts) the demo server on $HOST:$PORT (default 127.0.0.1:8602).
#   ./start_server.sh        in the foreground
#   ./start_server.sh --bg   in the background, log in logs/server.log
# Environment: HOST, PORT, ROUTER_URL, ROUTER_ADMIN_URL, GPU_USD_PER_HOUR (see README.md), PYTHON (default .venv/bin/python).
cd "$(dirname "$0")"
PY="${PYTHON:-.venv/bin/python}"
PORT="${PORT:-8602}"
[ -x "$PY" ] || { echo "$PY not found: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 1; }
mkdir -p logs
# replace a demo server that already listens on the port (only ours: app/server.py)
OLD=$(ss -ltnp 2>/dev/null | grep -E "[:.]$PORT " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
if [ -n "$OLD" ] && grep -q "app/server.py" "/proc/$OLD/cmdline" 2>/dev/null; then
  kill "$OLD"; sleep 0.5
fi
if [ "${1:-}" = "--bg" ]; then
  nohup "$PY" app/server.py > logs/server.log 2>&1 &
  echo "started pid $! on http://${HOST:-127.0.0.1}:$PORT (log: logs/server.log)"
else
  exec "$PY" app/server.py
fi
