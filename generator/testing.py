"""Test-support helpers that live inside the generator package.

The harness's tests/conftest.py is shared and not owned by this track, so
the session-wide fixture cache lives here: the first test module to ask for
a given (preset, seed, overrides) meeting generates it once, later modules
reuse the same directory. Not imported by production code paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CACHE: dict[str, Path] = {}


def fixture_meeting(base_dir: Path, preset: str = "smoke", seed: int = 3, overrides: dict[str, Any] | None = None) -> tuple[Path, dict[str, Any]]:
    """Generate (once per process) and return (directory, meeting.json dict)."""
    from generator.export import write_meeting
    from generator.meeting import generate_meeting
    from generator.scenario import load_scenario

    ov = dict(overrides or {})
    ov["seed"] = seed
    key = json.dumps({"preset": preset, "overrides": ov}, sort_keys=True)
    path = _CACHE.get(key)
    if path is None or not (path / "meeting.json").is_file():
        path = Path(base_dir) / f"{preset}-s{seed}-{abs(hash(key)) % 10_000_000}"
        write_meeting(generate_meeting(load_scenario(preset, ov)), path)
        _CACHE[key] = path
    return path, json.loads((path / "meeting.json").read_text())


__all__ = ["fixture_meeting"]
