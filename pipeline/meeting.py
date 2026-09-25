"""Meeting-directory reader/writer for the shared data contract.

A meeting directory (produced by the Layer 2 generator, or by the
``import-stm`` CLI here for real corpora) holds::

    meeting.json   schema "plaud-harness/meeting/1"
    ref.rttm       SPEAKER <id> 1 <start> <dur> <NA> <NA> <speaker> <NA> <NA>
    ref.stm        <id> 1 <speaker> <start> <end> <text>
    mix.wav, stems/<spk>.wav, device/recording.ogg ...   (as meeting.json says)

This module only *reads* what the generator wrote and, for tests and the
importer, writes the same shape.  It is not the generator.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .base import MEETING_SCHEMA, PipelineError, Segment, make_segment, sort_segments
from .formats import segments_to_rttm_lines, segments_to_stm_lines


class MeetingFormatError(PipelineError):
    """meeting.json violates the shared contract."""


MEETING_FILENAME = "meeting.json"


def find_meeting_json(audio_path: str | Path, meeting_dir: str | Path | None = None) -> Path | None:
    """Locate meeting.json for an audio file.

    HARNESS_POLICY search order: ``meeting_dir`` if given (a directory or the
    file itself), then the audio file's directory, then one and two levels up
    (audio lives in ``<dir>/mix.wav``, ``<dir>/stems/x.wav`` or
    ``<dir>/device/recording.ogg``).  Returns None when nothing is found.
    """
    if meeting_dir is not None:
        md = Path(meeting_dir)
        candidate = md if md.is_file() else md / MEETING_FILENAME
        return candidate if candidate.is_file() else None
    p = Path(audio_path).resolve()
    for parent in (p.parent, p.parent.parent, p.parent.parent.parent):
        candidate = parent / MEETING_FILENAME
        if candidate.is_file():
            return candidate
    return None


def read_meeting(path: str | Path) -> dict[str, Any]:
    """Read and validate meeting.json (``path`` may be the dir or the file)."""
    p = Path(path)
    if p.is_dir():
        p = p / MEETING_FILENAME
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise MeetingFormatError(f"{p}: not JSON: {exc}") from exc
    validate_meeting(data)
    return data


def validate_meeting(m: dict[str, Any]) -> dict[str, Any]:
    if m.get("schema") != MEETING_SCHEMA:
        raise MeetingFormatError(f"schema must be {MEETING_SCHEMA!r}, got {m.get('schema')!r}")
    for k in ("meeting_id", "sample_rate", "duration_s", "channels", "speakers", "segments", "audio"):
        if k not in m:
            raise MeetingFormatError(f"meeting.json lacks {k!r}")
    if not isinstance(m["meeting_id"], str) or not m["meeting_id"] or any(c.isspace() for c in m["meeting_id"]):
        raise MeetingFormatError("meeting_id must be a non-empty string without whitespace")
    ids = {s.get("id") for s in m["speakers"]}
    if len(ids) != len(m["speakers"]) or None in ids:
        raise MeetingFormatError("speakers need unique non-null ids")
    last = -math.inf
    for i, s in enumerate(m["segments"]):
        for k in ("speaker", "start", "end", "text"):
            if k not in s:
                raise MeetingFormatError(f"segment {i} lacks {k!r}")
        if s["speaker"] not in ids:
            raise MeetingFormatError(f"segment {i} speaker {s['speaker']!r} not in speakers")
        if float(s["start"]) < last:
            raise MeetingFormatError(f"segment {i} breaks the sorted-by-start contract")
        if float(s["end"]) < float(s["start"]):
            raise MeetingFormatError(f"segment {i} ends before it starts")
        last = float(s["start"])
        for j, w in enumerate(s.get("words", []) or []):
            for k in ("w", "start", "end"):
                if k not in w:
                    raise MeetingFormatError(f"segment {i} word {j} lacks {k!r}")
    return m


def resolve_audio(meeting_dir: str | Path, meeting: dict[str, Any], key: str = "mix") -> Path:
    """Pick an audio file from ``meeting["audio"]``.

    ``key`` is ``"mix"`` (audio.mix_wav), ``"device"`` (the first entry under
    audio.device, or ``device.<name>``), ``"stem:<speaker>"`` or a literal
    relative path.  HARNESS_POLICY: paths are relative to the meeting dir.
    """
    md = Path(meeting_dir)
    audio = meeting.get("audio", {})
    if key == "mix":
        rel = audio.get("mix_wav")
    elif key == "device":
        dev = audio.get("device") or {}
        rel = next(iter(dev.values()), None)
    elif key.startswith("device."):
        rel = (audio.get("device") or {}).get(key.split(".", 1)[1])
    elif key.startswith("stem:"):
        rel = (audio.get("stems") or {}).get(key.split(":", 1)[1])
    else:
        rel = key
    if not rel:
        raise MeetingFormatError(f"meeting {meeting.get('meeting_id')!r} has no audio for key {key!r}")
    p = md / rel
    if not p.is_file():
        raise FileNotFoundError(p)
    return p


def reference_segments(meeting: dict[str, Any]) -> list[Segment]:
    """Deep-copied, contract-shaped ground-truth segments."""
    out = []
    for s in meeting["segments"]:
        out.append(
            make_segment(
                str(s["speaker"]),
                float(s["start"]),
                float(s["end"]),
                str(s["text"]),
                [{"w": str(w["w"]), "start": float(w["start"]), "end": float(w["end"])} for w in (s.get("words") or [])],
            )
        )
    return sort_segments(out)


# --- writer (tests + the import-stm CLI) --------------------------------------


def interpolate_words(text: str, start: float, end: float) -> list[dict[str, Any]]:
    """HARNESS_POLICY: when a source has no word times, spread the tokens
    uniformly over the segment.  Marked in the meeting's generator block."""
    toks = text.split()
    if not toks:
        return []
    step = (end - start) / len(toks)
    return [
        {"w": t, "start": round(start + i * step, 4), "end": round(start + (i + 1) * step, 4)}
        for i, t in enumerate(toks)
    ]


def meeting_from_stm(
    stm_text: str,
    meeting_id: str | None = None,
    *,
    sample_rate: int = 16000,
    duration_s: float | None = None,
    channels: int = 1,
    audio: dict[str, Any] | None = None,
    generator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a meeting dict from an STM reference (word times interpolated)."""
    from .formats import parse_stm

    rows = parse_stm(stm_text)
    if not rows:
        raise MeetingFormatError("STM has no lines")
    ids = {r["meeting_id"] for r in rows}
    if meeting_id is None:
        if len(ids) != 1:
            raise MeetingFormatError(f"STM covers several meetings {sorted(ids)}; pass meeting_id")
        meeting_id = rows[0]["meeting_id"]
    rows = [r for r in rows if r["meeting_id"] == meeting_id]
    speakers = []
    for r in rows:
        if r["speaker"] not in speakers:
            speakers.append(r["speaker"])
    segments = [
        make_segment(r["speaker"], r["start"], r["end"], r["text"], interpolate_words(r["text"], r["start"], r["end"]))
        for r in rows
    ]
    segments = sort_segments(segments)
    if duration_s is None:
        duration_s = max((s["end"] for s in segments), default=0.0)
    return {
        "schema": MEETING_SCHEMA,
        "meeting_id": meeting_id,
        "sample_rate": int(sample_rate),
        "duration_s": float(duration_s),
        "channels": int(channels),
        # HARNESS_POLICY: imported corpora have no known voice/position.
        "speakers": [{"id": s, "voice": "unknown", "position_m": [0.0, 0.0, 0.0]} for s in speakers],
        "segments": segments,
        "audio": audio or {"mix_wav": "mix.wav", "stems": {}, "device": {}},
        "generator": generator
        or {"name": "pipeline.meeting.meeting_from_stm", "version": "1", "seed": 0,
            "scenario": {"source": "stm", "word_times": "interpolated (HARNESS_POLICY)"}},
    }


def write_meeting_dir(
    out_dir: str | Path,
    meeting: dict[str, Any],
    mix_pcm: np.ndarray | None = None,
    sample_rate: int | None = None,
) -> Path:
    """Write meeting.json + ref.rttm + ref.stm (+ mix.wav when PCM is given)."""
    import soundfile as sf

    validate_meeting(meeting)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / MEETING_FILENAME).write_text(json.dumps(meeting, indent=2) + "\n")
    segs = reference_segments(meeting)
    mid = meeting["meeting_id"]
    (out / "ref.rttm").write_text("".join(l + "\n" for l in segments_to_rttm_lines(mid, segs)))
    (out / "ref.stm").write_text("".join(l + "\n" for l in segments_to_stm_lines(mid, segs)))
    if mix_pcm is not None:
        sr = int(sample_rate or meeting["sample_rate"])
        rel = meeting["audio"].get("mix_wav", "mix.wav")
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(target), np.asarray(mix_pcm, dtype=np.float32), sr, subtype="PCM_16")
    return out
