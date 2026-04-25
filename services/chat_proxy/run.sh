#!/bin/bash
# Start the chat proxy in the background. Idempotent.
set -e
cd "$(dirname "$0")"
LOG="/tmp/mmclaw_chat_proxy.log"
PIDFILE="/tmp/mmclaw_chat_proxy.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "chat_proxy already running (pid=$(cat "$PIDFILE"))"
  exit 0
fi

nohup python3 -u chat_proxy.py >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"
sleep 1
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "chat_proxy started (pid=$(cat "$PIDFILE")) -> log: $LOG"
else
  echo "chat_proxy failed to start; tail of log:"
  tail -n 30 "$LOG" || true
  exit 1
fi
