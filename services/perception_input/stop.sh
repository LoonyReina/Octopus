#!/bin/bash
set -e
PIDFILE="/tmp/mmclaw_perception_input.pid"
if [ ! -f "$PIDFILE" ]; then
  echo "perception_input not running (no pidfile)"
  exit 0
fi
PID="$(cat "$PIDFILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  for _ in 1 2 3 4 5; do
    sleep 0.5
    if ! kill -0 "$PID" 2>/dev/null; then break; fi
  done
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID" 2>/dev/null || true
  fi
  echo "perception_input stopped (pid=$PID)"
else
  echo "perception_input pid=$PID already gone"
fi
rm -f "$PIDFILE"
