"""Contract I/O for Layer 3: meeting.json, hyp.json, RTTM, STM, normalisation.

The shared data contract (Layer 2 -> Layer 3 -> pipeline) is fixed by the
task brief and restated in docs/evals.md.  This module is the only place the
harness parses or writes it, so a contract violation is reported once, with a
message that names the file, the offending element and the rule it breaks.

Nothing here is device, SDK or cloud behaviour: the formats are the harness's
own (meeting.json, hyp.json) or public conventions (NIST RTTM, meeteval STM).
Choices that are not forced by those conventions are labelled HARNESS_POLICY.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

MEETING_SCHEMA = "plaud-harness/meeting/1"
HYPOTHESIS_SCHEMA = "plaud-harness/hypothesis/1"

#: HARNESS_POLICY: tolerance used when comparing times that should coincide
#: (word inside its segment, segment inside the meeting).  Times are floats
#: written with six decimals, so anything below 1e-6 is formatting noise.
TIME_TOLERANCE = 1e-6

#: HARNESS_POLICY: RTTM/STM writers emit six decimals with trailing zeros
#: trimmed (microsecond precision).  Nothing in the contract requires more.
_TIME_DECIMALS = 6


class ContractError(ValueError):
    """A contract file is malformed.  The message says what is wrong and where."""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Word:
    w: str
    start: float
    end: float

    def to_dict(self) -> dict[str, Any]:
        return {"w": self.w, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class Segment:
    speaker: str
    start: float
    end: float
    text: str = ""
    words: tuple[Word, ...] = ()

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self, *, include_words: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "speaker": self.speaker,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }
        if include_words and self.words:
            d["words"] = [w.to_dict() for w in self.words]
        return d


@dataclass
class Meeting:
    """One meeting directory's ground truth (the contract's meeting.json)."""

    meeting_id: str
    sample_rate: int
    duration_s: float
    channels: int
    speakers: list[dict[str, Any]]
    segments: list[Segment]
    audio: dict[str, Any] = field(default_factory=dict)
    generator: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @property
    def speaker_ids(self) -> list[str]:
        return [s["id"] for s in self.speakers]

    @property
    def active_speakers(self) -> list[str]:
        """Declared speakers that have at least one segment, in declaration order."""
        seen = {s.speaker for s in self.segments}
        return [sid for sid in self.speaker_ids if sid in seen]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MEETING_SCHEMA,
            "meeting_id": self.meeting_id,
            "sample_rate": self.sample_rate,
            "duration_s": self.duration_s,
            "channels": self.channels,
            "speakers": self.speakers,
            "segments": [s.to_dict() for s in self.segments],
            "audio": self.audio,
            "generator": self.generator,
        }


@dataclass
class Hypothesis:
    """A pipeline output for one meeting (the contract's hyp.json)."""

    meeting_id: str
    system: str
    segments: list[Segment]
    path: Path | None = None

    @property
    def speaker_ids(self) -> list[str]:
        out: list[str] = []
        for s in self.segments:
            if s.speaker not in out:
                out.append(s.speaker)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": HYPOTHESIS_SCHEMA,
            "meeting_id": self.meeting_id,
            "system": self.system,
            "segments": [s.to_dict() for s in self.segments],
        }


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _is_number(x: Any) -> bool:
    """A JSON number that is finite.

    ``json.loads`` accepts the non-standard ``NaN``/``Infinity`` tokens; an
    infinite segment end used to load and then turn tcpWER into total failure
    through NaN pseudo-timings, so non-finite values are refused here, where
    the error can name the element.
    """
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _require(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise ContractError(f"{where}: {msg}")


_WHITESPACE_CHAR = re.compile(r"\s")


def field_problem(value: str) -> str | None:
    """Why ``value`` cannot be written as one RTTM/STM field, or None if it can.

    Both formats are whitespace-separated, ``;;`` opens a comment line (a
    meeting id is the first field of an STM line), and ``<NA>`` is RTTM's
    placeholder for an absent field (the parsers read a ``<NA>`` speaker as
    "no speaker").
    """
    if value == "":
        return "is empty"
    if _WHITESPACE_CHAR.search(value):
        return "must not contain whitespace"
    if value.startswith(";;"):
        return "starts with ';;' (the RTTM/STM comment marker)"
    if value == "<NA>":
        return "is '<NA>' (RTTM's placeholder for an absent field)"
    return None


def _require_field(value: str, where: str, what: str) -> None:
    problem = field_problem(value)
    _require(problem is None, where, f"{what} {value!r} {problem} (RTTM/STM field)")


def _parse_word(raw: Any, where: str, seg_start: float, seg_end: float) -> Word:
    _require(isinstance(raw, dict), where, f"word must be an object, got {type(raw).__name__}")
    for key in ("w", "start", "end"):
        _require(key in raw, where, f"word is missing required key '{key}'")
    _require(isinstance(raw["w"], str) and raw["w"].strip() != "", where, "word 'w' must be a non-empty string")
    _require(" " not in raw["w"].strip(), where, f"word 'w' must be a single token, got {raw['w']!r}")
    _require(_is_number(raw["start"]) and _is_number(raw["end"]), where, "word 'start'/'end' must be finite numbers")
    start, end = float(raw["start"]), float(raw["end"])
    _require(end >= start, where, f"word end {end} is before its start {start}")
    _require(
        start >= seg_start - TIME_TOLERANCE and end <= seg_end + TIME_TOLERANCE,
        where,
        f"word [{start}, {end}] lies outside its segment [{seg_start}, {seg_end}]",
    )
    return Word(raw["w"], start, end)


def _parse_segment(
    raw: Any,
    where: str,
    *,
    allowed_speakers: set[str] | None,
    duration_s: float | None,
) -> Segment:
    _require(isinstance(raw, dict), where, f"segment must be an object, got {type(raw).__name__}")
    for key in ("speaker", "start", "end", "text"):
        _require(key in raw, where, f"segment is missing required key '{key}'")
    speaker = raw["speaker"]
    _require(
        isinstance(speaker, str) and speaker.strip() != "", where, "segment 'speaker' must be a non-empty string"
    )
    if allowed_speakers is not None:
        _require(
            speaker in allowed_speakers,
            where,
            f"segment speaker {speaker!r} is not declared in 'speakers' {sorted(allowed_speakers)}",
        )
    _require(
        _is_number(raw["start"]) and _is_number(raw["end"]), where, "segment 'start'/'end' must be finite numbers"
    )
    start, end = float(raw["start"]), float(raw["end"])
    _require(start >= 0.0, where, f"segment start {start} is negative")
    _require(end > start, where, f"segment end {end} must be strictly after start {start}")
    if duration_s is not None:
        _require(
            end <= duration_s + TIME_TOLERANCE,
            where,
            f"segment end {end} exceeds the meeting duration {duration_s}",
        )
    text = raw["text"]
    _require(isinstance(text, str), where, f"segment 'text' must be a string, got {type(text).__name__}")

    words: tuple[Word, ...] = ()
    if "words" in raw and raw["words"] is not None:
        _require(isinstance(raw["words"], list), where, "segment 'words' must be a list")
        parsed: list[Word] = []
        for i, w in enumerate(raw["words"]):
            parsed.append(_parse_word(w, f"{where}.words[{i}]", start, end))
        for i in range(1, len(parsed)):
            _require(
                parsed[i].start >= parsed[i - 1].start - TIME_TOLERANCE,
                f"{where}.words[{i}]",
                f"words are not sorted by start ({parsed[i].start} < {parsed[i - 1].start})",
            )
        spelled = [w.w for w in parsed]
        _require(
            spelled == text.split(),
            where,
            f"'words' do not spell 'text': words={spelled} text tokens={text.split()}",
        )
        words = tuple(parsed)
    return Segment(speaker, start, end, text, words)


def _parse_segments(
    raw: Any,
    where: str,
    *,
    allowed_speakers: set[str] | None,
    duration_s: float | None,
    require_sorted: bool,
) -> list[Segment]:
    _require(isinstance(raw, list), where, f"'segments' must be a list, got {type(raw).__name__}")
    segs = [
        _parse_segment(s, f"{where}[{i}]", allowed_speakers=allowed_speakers, duration_s=duration_s)
        for i, s in enumerate(raw)
    ]
    if require_sorted:
        for i in range(1, len(segs)):
            _require(
                segs[i].start >= segs[i - 1].start,
                f"{where}[{i}]",
                f"segments must be sorted by start ({segs[i].start} < {segs[i - 1].start})",
            )
    return segs


def _read_text(path: Path, where: str) -> str:
    """UTF-8 text of a contract file; every way that can fail is a ContractError."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ContractError(f"{where}: file not found: {path}") from None
    except UnicodeDecodeError as exc:
        raise ContractError(
            f"{where}: not valid UTF-8 ({exc.reason} at byte {exc.start}); contract files are UTF-8"
        ) from None
    except OSError as exc:
        raise ContractError(f"{where}: cannot read file ({exc.strerror or exc})") from None


def _load_json(path: Path, where: str) -> Any:
    text = _read_text(path, where)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{where}: not valid JSON ({exc.msg} at line {exc.lineno} column {exc.colno})") from None


# --------------------------------------------------------------------------- #
# meeting.json
# --------------------------------------------------------------------------- #


def meeting_from_dict(data: Any, *, where: str = "meeting.json", path: Path | None = None) -> Meeting:
    """Validate a decoded meeting.json object and build a :class:`Meeting`."""
    _require(isinstance(data, dict), where, f"top level must be an object, got {type(data).__name__}")
    _require("schema" in data, where, "missing required key 'schema'")
    _require(
        data["schema"] == MEETING_SCHEMA,
        where,
        f"schema is {data['schema']!r}, expected {MEETING_SCHEMA!r}",
    )
    for key in ("meeting_id", "sample_rate", "duration_s", "channels", "speakers", "segments", "audio", "generator"):
        _require(key in data, where, f"missing required key '{key}'")

    mid = data["meeting_id"]
    _require(isinstance(mid, str) and mid.strip() != "", where, "'meeting_id' must be a non-empty string")
    _require_field(mid, where, "'meeting_id'")

    sr = data["sample_rate"]
    _require(isinstance(sr, int) and not isinstance(sr, bool) and sr > 0, where, "'sample_rate' must be a positive integer")
    dur = data["duration_s"]
    _require(_is_number(dur) and dur > 0, where, "'duration_s' must be a positive finite number")
    ch = data["channels"]
    _require(isinstance(ch, int) and not isinstance(ch, bool) and ch >= 1, where, "'channels' must be an integer >= 1")

    speakers = data["speakers"]
    _require(isinstance(speakers, list) and len(speakers) > 0, where, "'speakers' must be a non-empty list")
    ids: list[str] = []
    for i, spk in enumerate(speakers):
        w = f"{where}.speakers[{i}]"
        _require(isinstance(spk, dict), w, "speaker must be an object")
        _require("id" in spk, w, "speaker is missing required key 'id'")
        _require(isinstance(spk["id"], str) and spk["id"] != "", w, "speaker 'id' must be a non-empty string")
        _require_field(spk["id"], w, "speaker id")
        _require(spk["id"] not in ids, w, f"duplicate speaker id {spk['id']!r}")
        if "position_m" in spk:
            pos = spk["position_m"]
            _require(
                isinstance(pos, list) and len(pos) == 3 and all(_is_number(v) for v in pos),
                w,
                "'position_m' must be [x, y, z] finite numbers",
            )
        ids.append(spk["id"])

    segments = _parse_segments(
        data["segments"],
        f"{where}.segments",
        allowed_speakers=set(ids),
        duration_s=float(dur),
        require_sorted=True,
    )
    _require(isinstance(data["audio"], dict), where, "'audio' must be an object")
    _require(isinstance(data["generator"], dict), where, "'generator' must be an object")
    gen = data["generator"]
    if "seed" in gen:
        _require(isinstance(gen["seed"], int) and not isinstance(gen["seed"], bool), where, "'generator.seed' must be an integer")

    return Meeting(
        meeting_id=mid,
        sample_rate=sr,
        duration_s=float(dur),
        channels=ch,
        speakers=speakers,
        segments=segments,
        audio=data["audio"],
        generator=gen,
        path=path,
    )


def meeting_json_path(path: str | Path) -> Path:
    """Accept either a meeting directory or the meeting.json inside it."""
    p = Path(path)
    return p / "meeting.json" if p.is_dir() else p


def load_meeting(path: str | Path) -> Meeting:
    """Load and validate ``meeting.json`` from a meeting directory (or file path)."""
    p = meeting_json_path(path)
    data = _load_json(p, str(p))
    return meeting_from_dict(data, where=str(p), path=p)


def write_meeting(meeting: Meeting, path: str | Path) -> Path:
    """Write meeting.json (path may be the directory or the file)."""
    p = Path(path)
    if p.is_dir() or p.suffix != ".json":
        p.mkdir(parents=True, exist_ok=True)
        p = p / "meeting.json"
    p.write_text(json.dumps(meeting.to_dict(), indent=2) + "\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# hyp.json
# --------------------------------------------------------------------------- #


def hypothesis_from_dict(data: Any, *, where: str = "hyp.json", path: Path | None = None) -> Hypothesis:
    _require(isinstance(data, dict), where, f"top level must be an object, got {type(data).__name__}")
    _require("schema" in data, where, "missing required key 'schema'")
    _require(
        data["schema"] == HYPOTHESIS_SCHEMA,
        where,
        f"schema is {data['schema']!r}, expected {HYPOTHESIS_SCHEMA!r}",
    )
    for key in ("meeting_id", "system", "segments"):
        _require(key in data, where, f"missing required key '{key}'")
    mid = data["meeting_id"]
    _require(isinstance(mid, str) and mid.strip() != "", where, "'meeting_id' must be a non-empty string")
    # It must equal the reference's meeting_id, so it obeys the same rule.
    _require_field(mid, where, "'meeting_id'")
    system = data["system"]
    _require(isinstance(system, str) and system.strip() != "", where, "'system' must be a non-empty string")
    # HARNESS_POLICY: hypothesis segments need not be sorted (the contract only
    # requires sortedness for the reference); they are sorted when scored.
    # Speaker labels are opaque non-blank strings and MAY contain whitespace
    # (the mock cloud's documented labels are "Speaker 1", ...); the RTTM/STM
    # writers map such labels to single fields (see rttm_speaker_fields).
    segments = _parse_segments(
        data["segments"], f"{where}.segments", allowed_speakers=None, duration_s=None, require_sorted=False
    )
    return Hypothesis(meeting_id=mid, system=system, segments=segments, path=path)


def load_hypothesis(path: str | Path) -> Hypothesis:
    p = Path(path)
    if p.is_dir():
        p = p / "hyp.json"
    data = _load_json(p, str(p))
    return hypothesis_from_dict(data, where=str(p), path=p)


def write_hypothesis(hyp: Hypothesis, path: str | Path) -> Path:
    p = Path(path)
    if p.is_dir() or p.suffix != ".json":
        p.mkdir(parents=True, exist_ok=True)
        p = p / "hyp.json"
    p.write_text(json.dumps(hyp.to_dict(), indent=2) + "\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# RTTM / STM
# --------------------------------------------------------------------------- #


def format_time(x: float) -> str:
    """Six decimals, trailing zeros trimmed, at least one decimal kept (HARNESS_POLICY)."""
    s = f"{x:.{_TIME_DECIMALS}f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _parse_time(token: str, where: str, what: str) -> float:
    try:
        v = float(token)
    except ValueError:
        raise ContractError(f"{where}: {what} {token!r} is not a number") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise ContractError(f"{where}: {what} {token!r} is not finite")
    return v


def _field_token(label: str) -> str:
    tok = re.sub(r"\s+", "_", label.strip()) or "_"
    if tok == "<NA>":
        tok = "_NA_"
    if tok.startswith(";;"):
        tok = "_" + tok
    return tok


def rttm_speaker_fields(labels: Iterable[str]) -> dict[str, str]:
    """Injective map from speaker labels to single RTTM/STM fields (HARNESS_POLICY).

    A label that already is one field (:func:`field_problem` is None) is kept
    as-is.  Any other label has each whitespace run replaced by ``_``
    (``"Speaker 1"`` -> ``"Speaker_1"``), ``<NA>`` becomes ``_NA_`` and a
    leading ``;;`` gets a ``_`` prefix; if the result is already taken, ``#2``,
    ``#3``, ... is appended, so distinct speakers stay distinct.  Labels are
    processed in sorted order, so the map depends only on the set of labels
    (hyp.rttm and hyp.stm always agree).  hyp.json keeps the original labels.
    """
    unique = sorted(set(labels))
    out: dict[str, str] = {lab: lab for lab in unique if field_problem(lab) is None}
    used = set(out.values())
    for lab in unique:
        if lab in out:
            continue
        base = cand = _field_token(lab)
        n = 2
        while cand in used:
            cand = f"{base}#{n}"
            n += 1
        out[lab] = cand
        used.add(cand)
    return out


def _check_file_id(meeting_id: str) -> None:
    problem = field_problem(meeting_id)
    if problem is not None:
        raise ContractError(f"meeting_id {meeting_id!r} {problem}; it cannot be the RTTM/STM file-id field")


def rttm_lines(segments: Iterable[Segment], meeting_id: str) -> list[str]:
    """``SPEAKER <meeting_id> 1 <start> <dur> <NA> <NA> <speaker> <NA> <NA>``

    Speaker labels go through :func:`rttm_speaker_fields`.
    """
    _check_file_id(meeting_id)
    segs = list(segments)
    field = rttm_speaker_fields(s.speaker for s in segs)
    return [
        f"SPEAKER {meeting_id} 1 {format_time(s.start)} {format_time(s.duration)} <NA> <NA> {field[s.speaker]} <NA> <NA>"
        for s in segs
    ]


def parse_rttm(
    text: str,
    *,
    source: str = "<rttm>",
    meeting_id: str | None = None,
    ignore_other_types: bool = False,
) -> list[Segment]:
    """Parse contract RTTM.  Segments carry no text.

    Rules enforced: exactly ten whitespace-separated fields; type ``SPEAKER``
    (other NIST types are an error unless ``ignore_other_types``); channel
    ``1``; numeric, non-negative start; strictly positive duration; non-empty
    speaker.  ``;;`` comment lines and blank lines are skipped.
    """
    out: list[Segment] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(";;"):
            continue
        where = f"{source}:{lineno}"
        fields = line.split()
        _require(len(fields) == 10, where, f"RTTM line must have 10 fields, got {len(fields)}: {line!r}")
        typ, fid, chan, start_s, dur_s, _o1, _o2, speaker, _conf, _slat = fields
        if typ != "SPEAKER":
            if ignore_other_types:
                continue
            raise ContractError(f"{where}: RTTM type must be SPEAKER, got {typ!r}")
        if meeting_id is not None:
            _require(fid == meeting_id, where, f"RTTM file id {fid!r} does not match meeting_id {meeting_id!r}")
        _require(chan == "1", where, f"RTTM channel must be 1, got {chan!r}")
        start = _parse_time(start_s, where, "start")
        dur = _parse_time(dur_s, where, "duration")
        _require(start >= 0.0, where, f"start {start} is negative")
        _require(dur > 0.0, where, f"duration {dur} must be strictly positive")
        _require(speaker != "<NA>" and speaker != "", where, "speaker field is empty")
        out.append(Segment(speaker, start, start + dur))
    return out


def load_rttm(path: str | Path, *, meeting_id: str | None = None) -> list[Segment]:
    p = Path(path)
    return parse_rttm(_read_text(p, str(p)), source=str(p), meeting_id=meeting_id)


def write_rttm(segments: Iterable[Segment], meeting_id: str, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = rttm_lines(segments, meeting_id)
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return p


def stm_lines(segments: Iterable[Segment], meeting_id: str) -> list[str]:
    """``<meeting_id> 1 <speaker> <start> <end> <text>`` (one line per segment).

    Speaker labels go through :func:`rttm_speaker_fields`; the text is written
    whitespace-collapsed on one line (STM cannot hold a line break, and
    :func:`parse_stm` collapses whitespace anyway).
    """
    _check_file_id(meeting_id)
    segs = list(segments)
    field = rttm_speaker_fields(s.speaker for s in segs)
    return [
        f"{meeting_id} 1 {field[s.speaker]} {format_time(s.start)} {format_time(s.end)} {' '.join(s.text.split())}".rstrip()
        for s in segs
    ]


def parse_stm(text: str, *, source: str = "<stm>", meeting_id: str | None = None) -> list[Segment]:
    """Parse contract STM.  Text may be empty (a segment with no words).

    Rules enforced: at least five fields; channel ``1``; numeric start and end
    with end strictly after start; non-empty speaker.  ``;;`` comment lines
    and blank lines are skipped.  Text is whitespace-normalised to single
    spaces (the format cannot represent anything else).
    """
    out: list[Segment] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(";;"):
            continue
        where = f"{source}:{lineno}"
        fields = line.split(maxsplit=5)
        _require(len(fields) >= 5, where, f"STM line must have at least 5 fields, got {len(fields)}: {line!r}")
        fid, chan, speaker, start_s, end_s = fields[:5]
        txt = " ".join(fields[5].split()) if len(fields) == 6 else ""
        if meeting_id is not None:
            _require(fid == meeting_id, where, f"STM file id {fid!r} does not match meeting_id {meeting_id!r}")
        _require(chan == "1", where, f"STM channel must be 1, got {chan!r}")
        _require(speaker != "", where, "speaker field is empty")
        start = _parse_time(start_s, where, "start")
        end = _parse_time(end_s, where, "end")
        _require(start >= 0.0, where, f"start {start} is negative")
        _require(end > start, where, f"end {end} must be strictly after start {start}")
        out.append(Segment(speaker, start, end, txt))
    return out


def load_stm(path: str | Path, *, meeting_id: str | None = None) -> list[Segment]:
    p = Path(path)
    return parse_stm(_read_text(p, str(p)), source=str(p), meeting_id=meeting_id)


def write_stm(segments: Iterable[Segment], meeting_id: str, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = stm_lines(segments, meeting_id)
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return p


def write_reference_files(meeting: Meeting, out_dir: str | Path) -> tuple[Path, Path]:
    """Write ``ref.rttm`` and ``ref.stm`` next to a meeting.json."""
    d = Path(out_dir)
    return (
        write_rttm(meeting.segments, meeting.meeting_id, d / "ref.rttm"),
        write_stm(meeting.segments, meeting.meeting_id, d / "ref.stm"),
    )


def write_hypothesis_files(hyp: Hypothesis, out_dir: str | Path) -> tuple[Path, Path, Path]:
    """Write ``hyp.json``, ``hyp.rttm`` and ``hyp.stm`` (what the pipeline CLI emits)."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    segs = sorted(hyp.segments, key=lambda s: (s.start, s.end, s.speaker))
    return (
        write_hypothesis(hyp, d / "hyp.json"),
        write_rttm(segs, hyp.meeting_id, d / "hyp.rttm"),
        write_stm(segs, hyp.meeting_id, d / "hyp.stm"),
    )


# --------------------------------------------------------------------------- #
# Text normalisation
# --------------------------------------------------------------------------- #

# HARNESS_POLICY: a deliberately small English contraction table.  ``'s`` is
# left alone because it is ambiguous (is / has / possessive).  Applied after
# lower-casing and before punctuation stripping, on whole tokens only.
CONTRACTIONS: dict[str, str] = {
    "can't": "cannot",
    "won't": "will not",
    "shan't": "shall not",
    "ain't": "is not",
    "let's": "let us",
    "i'm": "i am",
    "gonna": "going to",
    "wanna": "want to",
    "gotta": "got to",
}
_CONTRACTION_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("n't", " not"),
    ("'re", " are"),
    ("'ve", " have"),
    ("'ll", " will"),
    ("'d", " would"),
    ("'m", " am"),
)

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(10**9, "billion"), (10**6, "million"), (10**3, "thousand")]


def integer_to_words(n: int) -> str:
    """English words for 0 <= n < 10**12 (HARNESS_POLICY: no 'and', no hyphens)."""
    if n < 0:
        return "minus " + integer_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("" if ones == 0 else " " + _ONES[ones])
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _ONES[hundreds] + " hundred" + ("" if rest == 0 else " " + integer_to_words(rest))
    for value, name in _SCALES:
        if n >= value:
            head, rest = divmod(n, value)
            return integer_to_words(head) + " " + name + ("" if rest == 0 else " " + integer_to_words(rest))
    raise ValueError(f"integer_to_words: {n} is out of range")


_NUMBER_TOKEN = re.compile(r"^(-?)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?$")


def number_token_to_words(token: str) -> str | None:
    """'1,234' -> 'one thousand two hundred thirty four'; '3.5' -> 'three point five'.

    Returns None when the token is not a plain integer/decimal number.
    """
    m = _NUMBER_TOKEN.match(token)
    if not m:
        return None
    sign, int_part, frac = m.groups()
    n = int(int_part.replace(",", ""))
    if n >= 10**12:
        return None
    words = integer_to_words(n)
    if frac is not None:
        words += " point " + " ".join(_ONES[int(c)] for c in frac)
    return ("minus " + words) if sign else words


_PUNCT_TO_SPACE = re.compile(r"[-‐-―_/]+")
_APOSTROPHES = re.compile(r"[‘’`´]")
_NON_WORD = re.compile(r"[^\w\s']", flags=re.UNICODE)
_STRAY_APOSTROPHE = re.compile(r"(?<!\w)'|'(?!\w)")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Normalizer:
    """Text normalisation applied identically to reference and hypothesis.

    Order (HARNESS_POLICY): lowercase -> contractions (optional) -> numbers
    (optional) -> punctuation stripping -> whitespace collapse.  Hyphens,
    dashes, underscores and slashes become spaces ('well-known' -> 'well
    known'); apostrophes between word characters survive ('o'clock'); every
    other non-word character is removed.
    """

    lowercase: bool = True
    strip_punctuation: bool = True
    collapse_whitespace: bool = True
    expand_contractions: bool = False
    numbers_to_words: bool = False

    def __call__(self, text: str) -> str:
        if self.lowercase:
            text = text.lower()
        if self.expand_contractions:
            text = self._expand_contractions(text)
        if self.numbers_to_words:
            text = self._numbers(text)
        if self.strip_punctuation:
            text = _PUNCT_TO_SPACE.sub(" ", text)
            text = _APOSTROPHES.sub("'", text)
            text = _NON_WORD.sub("", text)
            text = _STRAY_APOSTROPHE.sub("", text)
        if self.collapse_whitespace:
            text = _WHITESPACE.sub(" ", text).strip()
        return text

    def tokens(self, text: str) -> list[str]:
        return self(text).split()

    @staticmethod
    def _expand_contractions(text: str) -> str:
        text = _APOSTROPHES.sub("'", text)
        out: list[str] = []
        for tok in text.split():
            core = tok.strip(".,;:!?\"()[]")
            lead = tok[: len(tok) - len(tok.lstrip(".,;:!?\"()[]"))]
            trail = tok[len(tok.rstrip(".,;:!?\"()[]")):]
            if core in CONTRACTIONS:
                out.append(lead + CONTRACTIONS[core] + trail)
                continue
            for suffix, repl in _CONTRACTION_SUFFIXES:
                if core.endswith(suffix) and len(core) > len(suffix):
                    out.append(lead + core[: -len(suffix)] + repl + trail)
                    break
            else:
                out.append(tok)
        return " ".join(out)

    @staticmethod
    def _numbers(text: str) -> str:
        out: list[str] = []
        for tok in text.split():
            core = tok.rstrip(".,;:!?")
            trail = tok[len(core):]
            words = number_token_to_words(core)
            out.append((words + trail) if words is not None else tok)
        return " ".join(out)

    def to_dict(self) -> dict[str, bool]:
        return {
            "lowercase": self.lowercase,
            "strip_punctuation": self.strip_punctuation,
            "collapse_whitespace": self.collapse_whitespace,
            "expand_contractions": self.expand_contractions,
            "numbers_to_words": self.numbers_to_words,
        }


DEFAULT_NORMALIZER = Normalizer()


def normalized_words(segment: Segment, normalizer: Normalizer) -> list[Word]:
    """Segment words after normalisation, timings preserved.

    A word that normalises to several tokens ("don't" -> "do not") has its
    interval split equally between them; a word that normalises to nothing
    (pure punctuation) is dropped.  Without word timings, the segment's text
    is normalised as a whole and no timings are produced.
    """
    if not segment.words:
        return []
    out: list[Word] = []
    for w in segment.words:
        toks = normalizer.tokens(w.w)
        if not toks:
            continue
        if len(toks) == 1:
            out.append(Word(toks[0], w.start, w.end))
            continue
        step = (w.end - w.start) / len(toks)
        for i, t in enumerate(toks):
            out.append(Word(t, w.start + i * step, w.start + (i + 1) * step))
    return out


def sorted_segments(segments: Sequence[Segment]) -> list[Segment]:
    return sorted(segments, key=lambda s: (s.start, s.end, s.speaker))
