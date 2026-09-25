"""The Layer 2 -> Layer 3 shared data contract (meeting directory).

Schema "plaud-harness/meeting/1". Everything here is harness convention
(HARNESS_POLICY): the file layout, the RTTM/STM dialects and the time grid
are chosen by the harness, not recovered from any Plaud artifact. The only
device fact this module carries is the sample rate, which is imported from
the emulator's evidence-backed constant rather than restated.

Time grid (HARNESS_POLICY, load-bearing for V4): every segment boundary the
generator emits lies on a 1/128 s grid, i.e. a multiple of 125 samples at
16 kHz. k/128 is a dyadic rational, so it is exactly representable as a
double, RTTM `start + duration` sums are exact, and the text form written
with 7 decimals parses back to the identical double. That is what lets
pyannote.metrics report a DER of exactly 0.0 between the RTTM and an
annotation rebuilt from the sample-level activity mask, independent of any
tolerance inside pyannote. Word timings are NOT on this grid; they are
sample-exact and only appear in meeting.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from generator._evidence import plaud_audio

SCHEMA_MEETING = "plaud-harness/meeting/1"
SCHEMA_HYPOTHESIS = "plaud-harness/hypothesis/1"

#: 16 kHz. DIRECT evidence: OggUtils passes sipush 16000 to init()
#: (emulator/plaudsim/audio.py SAMPLE_RATE_HZ, R6-S1).
SAMPLE_RATE = plaud_audio.SAMPLE_RATE_HZ

#: HARNESS_POLICY: 1/128 s boundary grid == 125 samples at 16 kHz.
TIME_GRID_HZ = 128
GRID_SAMPLES = SAMPLE_RATE // TIME_GRID_HZ
assert GRID_SAMPLES * TIME_GRID_HZ == SAMPLE_RATE, "grid must divide the sample rate"

MEETING_FILES = ("meeting.json", "ref.rttm", "ref.stm")


def fmt_time(seconds: float) -> str:
    """Shortest fixed-point text (<= 7 decimals) that round-trips the double
    for any k/128 value; always keeps one decimal so RTTM/STM columns parse
    as floats rather than ints."""
    text = format(float(seconds), ".7f").rstrip("0")
    if text.endswith("."):
        text += "0"
    return text


def samples_to_seconds(n: int) -> float:
    """Exact for multiples of GRID_SAMPLES; correctly rounded otherwise."""
    return n / SAMPLE_RATE


def quantise_down(n_samples: int) -> int:
    return (int(n_samples) // GRID_SAMPLES) * GRID_SAMPLES


def quantise_up(n_samples: int) -> int:
    n = int(n_samples)
    return ((n + GRID_SAMPLES - 1) // GRID_SAMPLES) * GRID_SAMPLES


def format_rttm(meeting_id: str, segments: list[dict[str, Any]]) -> str:
    """Standard RTTM: SPEAKER <id> 1 <start> <dur> <NA> <NA> <speaker> <NA> <NA>."""
    lines = []
    for seg in segments:
        start = float(seg["start"])
        dur = float(seg["end"]) - start
        lines.append(
            f"SPEAKER {meeting_id} 1 {fmt_time(start)} {fmt_time(dur)} <NA> <NA> {seg['speaker']} <NA> <NA>"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def format_stm(meeting_id: str, segments: list[dict[str, Any]]) -> str:
    """meeteval STM: <id> 1 <speaker> <start> <end> <text>, one line per segment."""
    lines = []
    for seg in segments:
        lines.append(
            f"{meeting_id} 1 {seg['speaker']} {fmt_time(seg['start'])} {fmt_time(seg['end'])} {seg['text']}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def parse_rttm(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if parts[0] != "SPEAKER" or len(parts) < 9:
            raise ValueError(f"not an RTTM SPEAKER line: {line!r}")
        start = float(parts[3])
        dur = float(parts[4])
        out.append(
            {"meeting_id": parts[1], "speaker": parts[7], "start": start, "end": start + dur}
        )
    return out


def parse_stm(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=5)
        if len(parts) < 5:
            raise ValueError(f"not an STM line: {line!r}")
        out.append(
            {
                "meeting_id": parts[0],
                "speaker": parts[2],
                "start": float(parts[3]),
                "end": float(parts[4]),
                "text": parts[5] if len(parts) > 5 else "",
            }
        )
    return out


def validate_meeting(meeting: dict[str, Any]) -> list[str]:
    """Return a list of contract violations (empty == conforming)."""
    errors: list[str] = []
    if meeting.get("schema") != SCHEMA_MEETING:
        errors.append(f"schema must be {SCHEMA_MEETING!r}")
    for key in ("meeting_id", "sample_rate", "duration_s", "channels", "speakers", "segments", "audio", "generator"):
        if key not in meeting:
            errors.append(f"missing top-level key {key!r}")
    if errors:
        return errors
    if meeting["sample_rate"] != SAMPLE_RATE:
        errors.append(f"sample_rate must be {SAMPLE_RATE}")
    ids = [s.get("id") for s in meeting["speakers"]]
    if len(set(ids)) != len(ids):
        errors.append("duplicate speaker ids")
    for spk in meeting["speakers"]:
        for key in ("id", "voice", "position_m"):
            if key not in spk:
                errors.append(f"speaker entry missing {key!r}")
    last_start = -1.0
    duration = float(meeting["duration_s"])
    for i, seg in enumerate(meeting["segments"]):
        for key in ("speaker", "start", "end", "text", "words"):
            if key not in seg:
                errors.append(f"segment {i} missing {key!r}")
        if errors:
            break
        if seg["speaker"] not in ids:
            errors.append(f"segment {i} references unknown speaker {seg['speaker']!r}")
        if not (0.0 <= seg["start"] < seg["end"] <= duration + 1e-9):
            errors.append(f"segment {i} outside [0, duration] or empty: {seg['start']}..{seg['end']}")
        if seg["start"] < last_start:
            errors.append(f"segment {i} is not sorted by start")
        last_start = seg["start"]
        if seg["text"] != " ".join(seg["text"].split()) or seg["text"] != seg["text"].lower():
            errors.append(f"segment {i} text is not lowercase single-space tokens")
        words = seg["words"]
        if [w["w"] for w in words] != seg["text"].split():
            errors.append(f"segment {i} words do not match text")
        prev_end = seg["start"] - 1e-9
        for w in words:
            if not (seg["start"] - 1e-9 <= w["start"] <= w["end"] <= seg["end"] + 1e-9):
                errors.append(f"segment {i} word {w['w']!r} lies outside its segment")
            if w["start"] < prev_end:
                errors.append(f"segment {i} word {w['w']!r} is not monotonic")
            prev_end = w["end"]
    audio = meeting["audio"]
    for key in ("mix_wav", "stems", "device"):
        if key not in audio:
            errors.append(f"audio entry missing {key!r}")
    gen = meeting["generator"]
    for key in ("name", "version", "seed", "scenario"):
        if key not in gen:
            errors.append(f"generator entry missing {key!r}")
    return errors


def load_meeting(directory: str | Path) -> dict[str, Any]:
    directory = Path(directory)
    meeting = json.loads((directory / "meeting.json").read_text())
    errors = validate_meeting(meeting)
    if errors:
        raise ValueError("meeting.json violates the contract: " + "; ".join(errors))
    return meeting


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Deterministic JSON: sorted keys, fixed indent, trailing newline."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


__all__ = [
    "GRID_SAMPLES",
    "MEETING_FILES",
    "SAMPLE_RATE",
    "SCHEMA_HYPOTHESIS",
    "SCHEMA_MEETING",
    "TIME_GRID_HZ",
    "fmt_time",
    "format_rttm",
    "format_stm",
    "load_meeting",
    "parse_rttm",
    "parse_stm",
    "quantise_down",
    "quantise_up",
    "samples_to_seconds",
    "validate_meeting",
    "write_json",
]
