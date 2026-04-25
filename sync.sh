#!/usr/bin/env bash
# mmclaw incremental sync wrapper (bash / git-bash / WSL).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python "$SCRIPT_DIR/sync.py" "$@"
