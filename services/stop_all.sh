#!/bin/bash
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/cam_stream/stop.sh" || true
"$HERE/chat_proxy/stop.sh" || true
