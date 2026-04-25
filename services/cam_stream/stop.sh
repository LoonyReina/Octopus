#!/bin/bash
PIDFILE="/tmp/mmclaw_cam_stream.pid"
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" || true
    sleep 1
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" || true
    echo "stopped pid=$PID"
  fi
  rm -f "$PIDFILE"
fi
