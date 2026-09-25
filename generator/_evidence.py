"""Locate the emulator's evidence-backed audio contracts without editing them.

The generator never re-derives a device fact that `emulator/plaudsim/audio.py`
already states with provenance; it imports that module. `emulator/` is not a
package root on sys.path when the generator is run as `python -m generator`,
so this shim adds it (read-only; nothing under emulator/ is modified).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMULATOR_DIR = ROOT / "emulator"

if str(EMULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(EMULATOR_DIR))

from plaudsim import audio as plaud_audio  # noqa: E402

__all__ = ["ROOT", "EMULATOR_DIR", "plaud_audio"]
