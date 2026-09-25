"""Entry point for ``python -m evals``."""

from __future__ import annotations

import sys

from evals.cli import main

if __name__ == "__main__":
    sys.exit(main())
