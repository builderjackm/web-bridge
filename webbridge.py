#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# ///
"""Run the project CLI directly with ``uv run webbridge.py``."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from web_bridge.cli import main  # noqa: E402


if __name__ == "__main__":
    main()
