#!/bin/bash
# Start the MJPEG camera streamer in the background. Idempotent.
#
# In addition to MJPEG streaming on http://127.0.0.1:8080/stream, this also
# runs the persistent capture mode by default. Frames land in:
#
#   /root/mmclaw/services/captures/cam_<ISO-timestamp>_<seq>.jpg
#
# Tunables (env vars):
#   CAPTURE_ENABLED            (default 1)
#   CAPTURE_INTERVAL_SECONDS   (default 5)
#   CAPTURE_RETENTION_COUNT    (default 100)
#   CAPTURE_DIR                (default /root/mmclaw/services/captures)
#   CAPTURE_JPEG_QUALITY       (default 85)
set -e
cd "$(dirname "$0")"
LOG="/tmp/mmclaw_cam_stream.log"
PIDFILE="/tmp/mmclaw_cam_stream.pid"

# Capture-mode defaults (override by exporting before calling run.sh).
export CAPTURE_ENABLED="${CAPTURE_ENABLED:-1}"
export CAPTURE_INTERVAL_SECONDS="${CAPTURE_INTERVAL_SECONDS:-5}"
export CAPTURE_RETENTION_COUNT="${CAPTURE_RETENTION_COUNT:-100}"
export CAPTURE_DIR="${CAPTURE_DIR:-/root/mmclaw/services/captures}"
export CAPTURE_JPEG_QUALITY="${CAPTURE_JPEG_QUALITY:-85}"

mkdir -p "$CAPTURE_DIR" || true

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "cam_stream already running (pid=$(cat "$PIDFILE"))"
  exit 0
fi

nohup python3 -u cam_stream.py >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"
sleep 1
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "cam_stream started (pid=$(cat "$PIDFILE")) -> log: $LOG"
  echo "  capture dir = $CAPTURE_DIR (interval=${CAPTURE_INTERVAL_SECONDS}s, keep=${CAPTURE_RETENTION_COUNT})"
else
  echo "cam_stream failed to start; tail of log:"
  tail -n 30 "$LOG" || true
  exit 1
fi
