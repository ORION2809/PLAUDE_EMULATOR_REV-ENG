"""Ground truth -> the documented transcription result, plus the placeholder.

Result schema (DOC-EXACT keys, build/docs-plaud-ai/openapi_transcription.json
TranscriptionResultResponse.data and the GET example):

    {text, language, duration (integer seconds),
     results[{start, end, text, speaker_id, language, language_probability}],
     embeddings: {"Speaker N": [256 floats]}}

Two doc-internal inconsistencies are resolved here and recorded:
  * the schema spells `language_probabilitiy`, the example and prose say
    `language_probability` -> the mock emits `language_probability`;
  * the prose page (transcription-api-overview.md) shows `segments[].speaker`
    while the OpenAPI shows `results[].speaker_id` -> the mock emits `results`
    / `speaker_id` (what both templates read: Android reads `data.results` at
    TranscriptionManager.kt:225 and `speaker_id` at FileDetailActivity.kt:238-239;
    iOS decodes `speaker_id` into TranscriptionResult.speakerId,
    PlaudAPIService.swift:444,452).

A third, recorded contradiction is left as documented: the OpenAPI example
labels speakers "Speaker 1" (DOC-EXACT, openapi_transcription.json GET
example), while a template comment expects "SPEAKER_00" and rewrites
"SPEAKER_" to "Speaker " for display (FileDetailActivity.kt:235-239,
FileDetailViewController.swift:570; OFFICIAL_SOURCE). Both label forms render
as "Speaker N" in the templates.

Input: a "meeting directory" per the shared Layer 2->3 data contract
(meeting.json schema "plaud-harness/meeting/1"). The pipeline's own oracle
(pipeline.oracle.OraclePipeline) is preferred when it imports and accepts the
meeting (see try_pipeline_oracle); otherwise meeting.json IS the ground truth
and is read directly. The task's `source` records which path ran.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path
from typing import Any

MEETING_SCHEMA = "plaud-harness/meeting/1"
HYPOTHESIS_SCHEMA = "plaud-harness/hypothesis/1"
#: DOC-EXACT: the GET example annotates the vectors "... 256 dim".
EMBEDDING_DIM = 256
#: DOC-EXACT label style: "Speaker 1", "Speaker 2".
SPEAKER_LABEL = "Speaker {n}"
#: HARNESS_POLICY: the fixed placeholder text (lowercase tokens per the shared
#: contract) when no ground truth exists and no ASR is available.
PLACEHOLDER_TEXT = "plaud harness mock cloud placeholder transcript no asr was run"
OPUS_RATE = 48000


# ------------------------------------------------------------------ params
def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalise_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Defaults are DOC-EXACT (openapi_transcription.json TranscriptionRequest).

    Types are enforced at submit (routers/transcription.py answers 422); this
    function still treats a non-object sub-parameter as absent so a task
    restored from an older state file cannot crash the worker."""
    p = _obj(params)
    tr = _obj(p.get("transcribe"))
    di = _obj(p.get("diarization"))
    vad = _obj(p.get("vad"))
    return {
        "language": str(tr.get("language") or "auto"),
        "model": str(tr.get("model") or "plaud-fast-whisper"),
        "detection_level": str(tr.get("detection_level") or "segment"),
        "decode_silence": bool(vad.get("decode_silence", False)),
        "diarization": bool(di.get("enabled", False)),
        "return_embedding": bool(di.get("return_embedding", False)),
        "hotwords": p.get("hotwords"),
    }


def _languages(norm: dict[str, Any]) -> tuple[str, str]:
    """(top-level language, per-segment language). HARNESS_POLICY: "auto"
    resolves to the doc example pair ("en", "en-US"); an explicit BCP-47 code
    is echoed in both places with its primary subtag on top."""
    lang = norm["language"]
    if lang == "auto":
        return "en", "en-US"
    primary = lang.split("-")[0]
    return primary, lang


# ------------------------------------------------------------ meeting dir
def find_meeting_dir(path: Path, max_up: int = 2) -> Path | None:
    """meeting.json beside the file, or up to `max_up` directories above it
    (device files live in <meeting>/device/recording.ogg per the contract)."""
    d = path if path.is_dir() else path.parent
    for _ in range(max_up + 1):
        if (d / "meeting.json").is_file():
            return d
        if d.parent == d:
            break
        d = d.parent
    return None


def load_meeting(meeting_dir: Path) -> dict[str, Any]:
    meeting = json.loads((Path(meeting_dir) / "meeting.json").read_text())
    if meeting.get("schema") != MEETING_SCHEMA:
        raise ValueError(f"not a {MEETING_SCHEMA} meeting: {meeting.get('schema')!r}")
    return meeting


def try_pipeline_oracle(
    meeting_dir: Path, audio_path: Path | None = None
) -> tuple[list[dict[str, Any]] | None, str]:
    """Run the pipeline's oracle on a meeting dir: (segments, how).

    The real API (pipeline/oracle.py:47-71, pipeline/base.py:381-400) is
    ``OraclePipeline().run(audio_path, meeting_dir) -> Hypothesis`` whose
    ``.segments`` are contract-shaped ({speaker, start, end, text, words}).
    With ``meeting_dir`` given, pipeline.meeting.find_meeting_json looks only
    there, so ``audio_path`` is informational (the meeting.json path is used
    when there is no local audio file).

    Returns ``(segments, "pipeline-oracle")`` on success, or ``(None, reason)``
    with reason ``pipeline-unavailable:<ExcType>`` (the package does not
    import) or ``pipeline-error:<ExcType>`` (it imported but refused the
    meeting -- pipeline.meeting.read_meeting validates more strictly than
    load_meeting here). The caller then reads meeting.json itself and records
    the reason in the task's ``source``; nothing is swallowed silently.
    """
    try:
        from pipeline.oracle import OraclePipeline
    except Exception as exc:  # noqa: BLE001 - absent or broken pipeline: fall back, with the reason
        return None, f"pipeline-unavailable:{type(exc).__name__}"
    mdir = Path(meeting_dir)
    try:
        hyp = OraclePipeline().run(audio_path or mdir / "meeting.json", mdir)
        segs = [dict(s) for s in hyp.segments]
    except Exception as exc:  # noqa: BLE001 - recorded by the caller in task.source
        return None, f"pipeline-error:{type(exc).__name__}"
    return segs, "pipeline-oracle"


def speaker_embedding(meeting_id: str, label: str) -> list[float]:
    """HARNESS_POLICY: a deterministic unit-norm 256-vector derived from
    sha256(meeting_id, label). It is not a voice embedding of anything."""
    vals: list[float] = []
    counter = 0
    while len(vals) < EMBEDDING_DIM:
        h = hashlib.sha256(f"{meeting_id}\x00{label}\x00{counter}".encode()).digest()
        for i in range(0, 32, 4):
            u = struct.unpack("<I", h[i:i + 4])[0]
            vals.append((u / 0xFFFFFFFF) * 2.0 - 1.0)
        counter += 1
    vals = vals[:EMBEDDING_DIM]
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [round(v / norm, 6) for v in vals]


def result_from_meeting(
    meeting: dict[str, Any],
    params: dict[str, Any] | None,
    segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ground-truth result. Speakers are relabelled "Speaker N" in order of
    first appearance (HARNESS_POLICY; the cloud's numbering rule is UNKNOWN).
    `duration` is an integer (DOC-EXACT schema type) rounded from duration_s."""
    norm = normalise_params(params)
    top_lang, seg_lang = _languages(norm)
    segs = sorted(segments if segments is not None else meeting["segments"], key=lambda s: float(s["start"]))
    labels: dict[str, str] = {}
    results = []
    for s in segs:
        item: dict[str, Any] = {
            "start": float(s["start"]),
            "end": float(s["end"]),
            "text": str(s["text"]),
            "language": seg_lang,
            "language_probability": 1.0,
        }
        if norm["diarization"]:
            spk = str(s["speaker"])
            if spk not in labels:
                labels[spk] = SPEAKER_LABEL.format(n=len(labels) + 1)
            item["speaker_id"] = labels[spk]
        results.append(item)
    data: dict[str, Any] = {
        "text": " ".join(r["text"] for r in results if r["text"]),
        "language": top_lang,
        "duration": int(round(float(meeting.get("duration_s") or (results[-1]["end"] if results else 0.0)))),
        "results": results,
    }
    if norm["diarization"] and norm["return_embedding"]:
        mid = str(meeting.get("meeting_id", ""))
        data["embeddings"] = {label: speaker_embedding(mid, label) for label in labels.values()}
    return data


def placeholder_result(data: bytes, params: dict[str, Any] | None) -> dict[str, Any]:
    """No ground truth and no ASR: one placeholder segment spanning the audio
    (duration from the Ogg/Opus container when the bytes are one, else 0)."""
    norm = normalise_params(params)
    top_lang, seg_lang = _languages(norm)
    dur = ogg_opus_duration_s(data) or 0.0
    seg: dict[str, Any] = {
        "start": 0.0, "end": round(dur, 3), "text": PLACEHOLDER_TEXT,
        "language": seg_lang, "language_probability": 0.0,
    }
    if norm["diarization"]:
        seg["speaker_id"] = SPEAKER_LABEL.format(n=1)
    out: dict[str, Any] = {"text": PLACEHOLDER_TEXT, "language": top_lang, "duration": int(round(dur)), "results": [seg]}
    if norm["diarization"] and norm["return_embedding"]:
        out["embeddings"] = {SPEAKER_LABEL.format(n=1): speaker_embedding("placeholder", SPEAKER_LABEL.format(n=1))}
    return out


def result_to_hypothesis(result: dict[str, Any], meeting_id: str, system: str = "mockcloud") -> dict[str, Any]:
    """Shared contract hyp.json from a SUCCESS `data` object. Segments without
    speaker_id (diarization off) get the single opaque speaker "spk".
    Text is lowercased and whitespace-normalised (the generator emits clean
    text; normalisation proper is the evals layer's job)."""
    segments = []
    for r in result.get("results", []):
        segments.append({
            "speaker": str(r.get("speaker_id", "spk")),
            "start": float(r["start"]),
            "end": float(r["end"]),
            "text": " ".join(str(r.get("text", "")).lower().split()),
        })
    return {"schema": HYPOTHESIS_SCHEMA, "meeting_id": meeting_id, "system": system, "segments": segments}


# --------------------------------------------------------------- ogg/opus
def ogg_opus_duration_s(data: bytes) -> float | None:
    """Duration of an Ogg/Opus stream from its last granule position.

    Ogg page: "OggS", version, header_type, granule (i64le @6), serial, seq,
    crc, nsegs (@26), segment table (RFC 3533). OpusHead carries pre_skip as
    u16le at offset 10 (RFC 7845 section 5.1); granules are at 48 kHz
    regardless of the input rate (RFC 7845 section 4). Returns None for
    anything that is not an Ogg/Opus stream.
    """
    pos = 0
    n = len(data)
    last_granule: int | None = None
    pre_skip = 0
    first = True
    while pos + 27 <= n:
        if data[pos:pos + 4] != b"OggS":
            return None
        nsegs = data[pos + 26]
        table = data[pos + 27:pos + 27 + nsegs]
        if len(table) < nsegs:
            break
        body_len = sum(table)
        granule = int.from_bytes(data[pos + 6:pos + 14], "little", signed=True)
        body_start = pos + 27 + nsegs
        body = data[body_start:body_start + body_len]
        if first:
            if len(body) < 19 or body[:8] != b"OpusHead":
                return None
            pre_skip = struct.unpack_from("<H", body, 10)[0]
            first = False
        if granule != -1:
            last_granule = granule
        pos = body_start + body_len
    if first or last_granule is None:
        return None
    return max(0, last_granule - pre_skip) / OPUS_RATE
