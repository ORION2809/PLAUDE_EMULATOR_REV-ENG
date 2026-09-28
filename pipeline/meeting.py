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
import re
from pathlib import Path
from typing import Any

import numpy as np

from .base import MEETING_SCHEMA, PipelineError, Segment, make_segment, sort_segments
from .formats import segments_to_rttm_lines, segments_to_stm_lines


class MeetingFormatError(PipelineError):
    """meeting.json violates the shared contract."""


MEETING_FILENAME = "meeting.json"

#: HARNESS_POLICY: a meeting_id is used as a directory name (``batch`` writes
#: ``<out>/<meeting_id>/``), so it must be one safe path component: word
#: characters (Unicode letters, digits, ``_``), ``.`` and ``-``, not starting
#: with ``.`` or ``-`` (so no ``..``, no ``/``, no whitespace), at most 200
#: characters.  The generator's ids (``synth-<name>-s0003``, built with
#: ``str.isalnum``) and AMI ids (``ES2002a``) fit.
MEETING_ID_RE = re.compile(r"^\w[\w.-]{0,199}$")


def check_meeting_id(meeting_id: Any) -> str:
    if not isinstance(meeting_id, str) or not MEETING_ID_RE.match(meeting_id):
        raise MeetingFormatError(
            f"meeting_id {meeting_id!r} must match {MEETING_ID_RE.pattern} "
            "(one safe path component: letters, digits, '.', '_', '-')"
        )
    return meeting_id


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
    check_meeting_id(m["meeting_id"])
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

    ``key`` is ``"mix"`` (audio.mix_wav), ``"device"`` (the primary device
    recording, see below), ``device.<name>``, ``"stem:<speaker>"`` or a literal
    relative path.  HARNESS_POLICY: paths are relative to the meeting dir.

    ``"device"`` resolves like ``generator.contract.device_primary_path``: the
    entry named by audio.device_primary (generator >= 0.2.0), else
    audio.device["ogg_opus"], else -- only for older or hand-written meetings
    with neither -- the first entry under audio.device.  Never blindly the
    first entry: the generator writes keys sorted, so on a 0.2.0 meeting that
    is ``e2ee_ogg`` (device/recording_e2ee.bin, ciphertext).  A device_primary
    naming no audio.device entry is a MeetingFormatError.

    A path taken from meeting.json (every key but the literal) must be
    relative and must stay inside the meeting dir after resolving symlinks
    and ``..``; a literal ``key`` is the operator's own choice and is not
    constrained.
    """
    md = Path(meeting_dir)
    audio = meeting.get("audio", {})
    from_meeting_json = True
    if key == "mix":
        rel = audio.get("mix_wav")
    elif key == "device":
        dev = audio.get("device") or {}
        primary = audio.get("device_primary")
        if primary:
            if not isinstance(primary, str) or primary not in dev:
                raise MeetingFormatError(
                    f"meeting {meeting.get('meeting_id')!r}: audio.device_primary {primary!r} "
                    "names no audio.device entry"
                )
            rel = dev[primary]
        elif "ogg_opus" in dev:
            rel = dev["ogg_opus"]
        else:
            rel = next(iter(dev.values()), None)
    elif key.startswith("device."):
        rel = (audio.get("device") or {}).get(key.split(".", 1)[1])
    elif key.startswith("stem:"):
        rel = (audio.get("stems") or {}).get(key.split(":", 1)[1])
    else:
        rel, from_meeting_json = key, False
    if not rel:
        raise MeetingFormatError(f"meeting {meeting.get('meeting_id')!r} has no audio for key {key!r}")
    if not isinstance(rel, str):
        raise MeetingFormatError(f"meeting {meeting.get('meeting_id')!r}: audio path for {key!r} is not a string")
    p = md / rel
    if from_meeting_json:
        if Path(rel).is_absolute():
            raise MeetingFormatError(f"meeting.json audio path {rel!r} (key {key!r}) must be relative")
        root = md.resolve()
        target = p.resolve()
        if target != root and root not in target.parents:
            raise MeetingFormatError(
                f"meeting.json audio path {rel!r} (key {key!r}) resolves outside the meeting dir {md}"
            )
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
    """Build a meeting dict from an STM reference (word times interpolated).

    ``meeting_id`` selects that file id's rows from a multi-meeting STM.  For
    a single-meeting STM whose file id differs, it RENAMES the import (the
    original id is recorded as ``generator.scenario.stm_file_id``).  An id
    that matches no rows of a multi-meeting STM, or an STM with no usable
    rows, raises MeetingFormatError -- an empty reference would make every
    downstream score meaningless.  The optional NIST ``<label>`` field is
    never part of the words; NIST pseudo-speaker rows are dropped and
    counted (``excluded_region``/``ignore_time_segment_in_scoring`` spans
    are kept in ``generator.scenario.stm_excluded_regions``).
    """
    from .formats import STM_PSEUDO_SPEAKERS, parse_stm

    rows = parse_stm(stm_text)
    if not rows:
        raise MeetingFormatError("STM has no lines")
    ids = sorted({r["meeting_id"] for r in rows})
    stm_file_id = None
    if meeting_id is None:
        if len(ids) != 1:
            raise MeetingFormatError(f"STM covers several meetings {ids}; pass meeting_id")
        meeting_id = ids[0]
    elif meeting_id not in ids:
        if len(ids) != 1:
            raise MeetingFormatError(
                f"meeting_id {meeting_id!r} matches no STM rows; the STM covers {ids}"
            )
        stm_file_id = ids[0]  # single-meeting STM: rename the import
    check_meeting_id(meeting_id)
    wanted = stm_file_id or meeting_id
    rows = [r for r in rows if r["meeting_id"] == wanted]
    pseudo = [r for r in rows if r["speaker"] in STM_PSEUDO_SPEAKERS]
    rows = [r for r in rows if r["speaker"] not in STM_PSEUDO_SPEAKERS]
    if not rows:
        raise MeetingFormatError(f"STM has no speaker rows for {wanted!r}")
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
    stm_notes: dict[str, Any] = {}
    if stm_file_id is not None:
        stm_notes["stm_file_id"] = stm_file_id
    if pseudo:
        stm_notes["stm_pseudo_speaker_rows"] = len(pseudo)
        excluded = [[r["start"], r["end"]] for r in pseudo if r["speaker"] != "inter_segment_gap"]
        if excluded:
            stm_notes["stm_excluded_regions"] = excluded
    labelled = sum(1 for r in rows if r.get("label"))
    if labelled:
        stm_notes["stm_label_fields"] = labelled
    if generator is not None and stm_notes:
        generator = dict(generator)
        generator["scenario"] = {**dict(generator.get("scenario") or {}), **stm_notes}
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
            "scenario": {"source": "stm", "word_times": "interpolated (HARNESS_POLICY)", **stm_notes}},
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
