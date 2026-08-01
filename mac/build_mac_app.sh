#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip pyinstaller
pyinstaller --windowed --name "PureClick for Mac" \
  --paths .. \
  --hidden-import pureclick_core \
  --hidden-import pureclick_seat_core \
  --add-data "../browser:browser" \
  pureclick.py

echo ""
echo "Built: dist/PureClick for Mac.app"
