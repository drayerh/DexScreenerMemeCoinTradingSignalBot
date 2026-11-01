"""Backwards-compatible entry point. Prefer: `dexsignal run`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from dexsignal.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["run"]))
