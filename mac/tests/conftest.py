from __future__ import annotations

import sys
from pathlib import Path

MAC_DIR = Path(__file__).resolve().parents[1]
if str(MAC_DIR) not in sys.path:
    sys.path.insert(0, str(MAC_DIR))
