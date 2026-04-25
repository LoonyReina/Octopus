#!/bin/bash
# Start the perception_input watcher in the background. Idempotent.
#
# Polls /root/mmclaw/services/captures/, sends the newest jpg to chat_proxy,
# and (if DASHSCOPE_API_KEY is set) writes a 1024-d embedding json to
# /root/mmclaw/services/embeddings/.
set -e
cd "$(dirname "$0")"
LOG="/tmp/mmclaw_perception_input.log"
PIDFILE="/tmp/mmclaw_perception_input.pid"

export PERCEPTION_CAPTURES_DIR="${PERCEPTION_CAPTURES_DIR:-/root/mmclaw/services/captures}"
export PERCEPTION_EMBEDDINGS_DIR="${PERCEPTION_EMBEDDINGS_DIR:-/root/mmclaw/services/embeddings}"
export PERCEPTION_CHAT_URL="${PERCEPTION_CHAT_URL:-http://127.0.0.1:18790/chat}"
export PERCEPTION_INTERVAL_SECONDS="${PERCEPTION_INTERVAL_SECONDS:-15}"
export PERCEPTION_INJECT_CHAT="${PERCEPTION_INJECT_CHAT:-1}"
export PERCEPTION_EMBED="${PERCEPTION_EMBED:-1}"
export PERCEPTION_PROMPT="${PERCEPTION_PROMPT:-[perception] new camera frame}"

mkdir -p "$PERCEPTION_EMBEDDINGS_DIR" || true

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "perception_input already running (pid=$(cat "$PIDFILE"))"
  exit 0
fi

nohup python3 -u watcher.py >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"
sleep 1
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "perception_input started (pid=$(cat "$PIDFILE")) -> log: $LOG"
  echo "  captures = $PERCEPTION_CAPTURES_DIR"
  echo "  embeddings = $PERCEPTION_EMBEDDINGS_DIR"
  echo "  chat = $PERCEPTION_CHAT_URL  interval=${PERCEPTION_INTERVAL_SECONDS}s"
else
  echo "perception_input failed to start; tail of log:"
  tail -n 30 "$LOG" || true
  exit 1
fi
