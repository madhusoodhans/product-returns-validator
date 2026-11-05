#!/usr/bin/env bash
# Starts (or restarts) the demo server on $HOST:$PORT (default 127.0.0.1:8602).
#   ./start_server.sh        in the foreground
#   ./start_server.sh --bg   in the background, log in logs/server.log
# Environment: VLLM_SR_API_KEY (required), HOST, PORT, VLLM_SR_API_BASE, D3_MODEL, GUARD_MODEL, LLM_MODEL, PLAY_ALL,
# LANDING_URL, MODEL_SLOTS, TRUST_PROXY (see README.md), PYTHON (default .venv/bin/python).
cd "$(dirname "$0")"
PY="${PYTHON:-.venv/bin/python}"
PORT="${PORT:-8602}"
[ -n "${VLLM_SR_API_KEY:-}" ] || { echo "VLLM_SR_API_KEY is not set (the hosted vllm-sr API key)" >&2; exit 1; }
[ -x "$PY" ] || { echo "$PY not found: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 1; }
mkdir -p logs
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"   # many threads: without it memory grows ~6x under load
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
