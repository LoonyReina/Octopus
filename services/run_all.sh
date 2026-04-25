#!/bin/bash
# Convenience: start both services from /root/mmclaw/services
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/cam_stream/run.sh"
"$HERE/chat_proxy/run.sh"
echo "---"
echo "cam_stream: http://127.0.0.1:8080/stream  (log: /tmp/mmclaw_cam_stream.log)"
echo "chat_proxy: http://127.0.0.1:18789/        (log: /tmp/mmclaw_chat_proxy.log)"
