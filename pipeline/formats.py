"""RTTM / STM / hyp.json serialisation for the shared data contract.

Formats (all HARNESS_POLICY choices within two public standards):

  RTTM  ``SPEAKER <id> 1 <start> <dur> <NA> <NA> <speaker> <NA> <NA>``
        (NIST RTTM as read by pyannote.database.util.load_rttm and
        meeteval.io.RTTM; channel fixed at 1)
  STM   ``<id> 1 <speaker> <start> <end> <text>``
        (meeteval.io.STM; an empty transcript is legal and is what a
        diarization-only system writes)

Times are written with 3 decimals (millisecond precision) in the text
formats; hyp.json keeps the full float.  Speakers are opaque strings and
must not contain whitespace (both formats are whitespace-delimited).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base import Hypothesis, PipelineError, Segment, sort_segments

TIME_DECIMALS = 3  # HARNESS_POLICY


def _fmt(x: float) -> str:
    return f"{float(x):.{TIME_DECIMALS}f}"


def _check_token(kind: str, value: str) -> str:
    if not value or any(c.isspace() for c in value):
        raise PipelineError(f"{kind} {value!r} must be non-empty and contain no whitespace")
    return value


def segments_to_rttm_lines(meeting_id: str, segments: list[Segment]) -> list[str]:
    _check_token("meeting_id", meeting_id)
    lines = []
    for s in sort_segments(segments):
        start, end = float(s["start"]), float(s["end"])
        spk = _check_token("speaker", str(s["speaker"]))
        lines.append(
            f"SPEAKER {meeting_id} 1 {_fmt(start)} {_fmt(end - start)} <NA> <NA> {spk} <NA> <NA>"
        )
    return lines


def segments_to_stm_lines(meeting_id: str, segments: list[Segment]) -> list[str]:
    _check_token("meeting_id", meeting_id)
    lines = []
    for s in sort_segments(segments):
        spk = _check_token("speaker", str(s["speaker"]))
        text = " ".join(str(s.get("text", "")).split())
        line = f"{meeting_id} 1 {spk} {_fmt(s['start'])} {_fmt(s['end'])} {text}"
        lines.append(line.rstrip())
    return lines


def parse_rttm(text: str) -> list[dict[str, Any]]:
    """Parse RTTM lines into ``{"meeting_id", "speaker", "start", "end"}``."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        if len(f) < 9 or f[0] != "SPEAKER":
            raise PipelineError(f"RTTM line {n}: expected 'SPEAKER <id> 1 <start> <dur> ...': {raw!r}")
        start, dur = float(f[3]), float(f[4])
        out.append({"meeting_id": f[1], "speaker": f[7], "start": start, "end": start + dur})
    return out


#: NIST STM optional label field, e.g. ``<o,f0,male>``: a single ``<...>``
#: token with at least one comma right after the end time.  A comma-less
#: ``<...>`` token (``<unk>``-style) is ambiguous and is kept as text.
STM_LABEL_RE = re.compile(r"^<[^<>\s]*,[^<>\s]*>$")

#: NIST sclite pseudo-speakers: they mark regions, they are not talkers.
STM_PSEUDO_SPEAKERS = frozenset({"inter_segment_gap", "excluded_region", "ignore_time_segment_in_scoring"})


def parse_stm(text: str) -> list[dict[str, Any]]:
    """Parse STM lines into ``{"meeting_id", "speaker", "start", "end", "text", "label"}``.

    NIST STM allows an optional ``<label>`` field between the end time and
    the transcript; it is returned under ``"label"`` (else None) and is NOT
    part of ``"text"``.  Pseudo-speaker rows (``STM_PSEUDO_SPEAKERS``) are
    returned as-is; ``meeting_from_stm`` decides what to do with them.
    """
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(";;"):
            continue
        f = line.split(maxsplit=5)
        if len(f) < 5:
            raise PipelineError(f"STM line {n}: expected '<id> 1 <speaker> <start> <end> [text]': {raw!r}")
        try:
            start, end = float(f[3]), float(f[4])
        except ValueError as exc:
            raise PipelineError(f"STM line {n}: bad time: {raw!r}") from exc
        body = f[5] if len(f) > 5 else ""
        label = None
        head, _, rest = body.partition(" ")
        if STM_LABEL_RE.match(head):
            label, body = head, rest.strip()
        out.append(
            {
                "meeting_id": f[0],
                "speaker": f[2],
                "start": start,
                "end": end,
                "text": body,
                "label": label,
            }
        )
    return out


def hypothesis_paths(out_json: str | Path) -> dict[str, Path]:
    """hyp.json -> {"json": hyp.json, "rttm": hyp.rttm, "stm": hyp.stm}."""
    p = Path(out_json)
    stem = p.with_suffix("") if p.suffix == ".json" else p
    return {"json": stem.with_suffix(".json"), "rttm": stem.with_suffix(".rttm"), "stm": stem.with_suffix(".stm")}


def write_hypothesis_files(hyp: Hypothesis, out_json: str | Path) -> dict[str, Path]:
    """Write hyp.json + hyp.rttm + hyp.stm next to each other; return the paths."""
    hyp.validate()
    paths = hypothesis_paths(out_json)
    paths["json"].parent.mkdir(parents=True, exist_ok=True)
    paths["json"].write_text(json.dumps(hyp.to_dict(), indent=2, sort_keys=False) + "\n")
    paths["rttm"].write_text("".join(l + "\n" for l in hyp.rttm_lines()))
    paths["stm"].write_text("".join(l + "\n" for l in hyp.stm_lines()))
    return paths


def read_hypothesis(path: str | Path) -> Hypothesis:
    return Hypothesis.from_dict(json.loads(Path(path).read_text())).validate()
