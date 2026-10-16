"""Pytest bootstrap: make the repo root importable.

Layout:
  a11y/           <- service root (run pytest from here)
    a11y/         <- engine package
    common/       <- shared audit lib (bundled copy of ~/workspace/agent-api/common/)
    server.py
    tests/

server.py inserts the service root into sys.path itself; this conftest does
the same for the test session.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("X402_DISABLED", "1")  # tests run with the paywall off
