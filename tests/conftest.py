"""Shared test setup: put ``src`` and this directory on ``sys.path``."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

for candidate in (_ROOT / "src", _HERE):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
