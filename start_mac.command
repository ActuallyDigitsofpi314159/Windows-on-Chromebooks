#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ ! -x .venv/bin/python ]]; then
    echo 'Run bash setup_mac.sh first.' >&2
    exit 1
fi
./.venv/bin/python remote_host.py
