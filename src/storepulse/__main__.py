"""Enables ``python -m storepulse``, which scheduled jobs use to find the right interpreter."""

from __future__ import annotations

import sys

from storepulse.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
