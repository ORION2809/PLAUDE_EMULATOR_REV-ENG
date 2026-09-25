"""Layer 2: synthetic meeting generator (ground truth by construction).

    from generator import generate_meeting, write_meeting, load_scenario
    meeting = generate_meeting(load_scenario("smoke", {"seed": 7}))
    write_meeting(meeting, "build/synthetic/demo")

Design and evidence: docs/generator.md. Device facts are imported from
emulator/plaudsim/audio.py; harness choices are marked HARNESS_POLICY.
"""

from generator.contract import (
    SAMPLE_RATE,
    SCHEMA_HYPOTHESIS,
    SCHEMA_MEETING,
    load_meeting,
    validate_meeting,
)
from generator.scenario import PRESETS, Scenario, load_scenario

__version__ = "0.1.0"


def generate_meeting(*args, **kwargs):  # lazy: keeps `import generator` light
    from generator.meeting import generate_meeting as _impl

    return _impl(*args, **kwargs)


def write_meeting(*args, **kwargs):
    from generator.export import write_meeting as _impl

    return _impl(*args, **kwargs)


__all__ = [
    "PRESETS",
    "SAMPLE_RATE",
    "SCHEMA_HYPOTHESIS",
    "SCHEMA_MEETING",
    "Scenario",
    "__version__",
    "generate_meeting",
    "load_meeting",
    "load_scenario",
    "validate_meeting",
    "write_meeting",
]
