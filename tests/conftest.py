"""Make `routes` importable from the plugin's tests/ directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
