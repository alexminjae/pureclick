#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c "import webview" 2>/dev/null; then
  echo "Installing browser dependency (pywebview)..."
  python3 -m pip install -r requirements.txt
fi

exec python3 pureclick.py
