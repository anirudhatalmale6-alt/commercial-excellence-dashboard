#!/bin/sh
# Start / restart the MVP server in the test environment.
#   tools/run.sh [port]
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-8077}"
PIDFILE="$ROOT/logs/server.pid"
mkdir -p "$ROOT/logs"

if [ -f "$PIDFILE" ]; then
  OLD=$(cat "$PIDFILE")
  if kill -0 "$OLD" 2>/dev/null; then
    kill "$OLD"
    sleep 2
  fi
  rm -f "$PIDFILE"
fi

cd "$ROOT/backend"
nohup python3 -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" > "$ROOT/logs/server.log" 2>&1 &
echo $! > "$PIDFILE"
sleep 5
echo "started pid $(cat "$PIDFILE") on port $PORT"
curl -s "http://127.0.0.1:$PORT/api/healthz" || tail -20 "$ROOT/logs/server.log"
echo
