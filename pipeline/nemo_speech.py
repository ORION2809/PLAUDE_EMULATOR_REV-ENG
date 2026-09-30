"""NVIDIA NeMo-Speech.cpp adapters (research branch; docs/nvidia-speech.md).

NeMo-Speech.cpp (https://github.com/NVIDIA/NeMo-Speech.cpp, Apache-2.0) is
NVIDIA's ggml runtime for its speech models.  These adapters run its
``nemo-speech`` CLI as a subprocess on a 16 kHz PCM16 copy of the audio, the
way a local app would call the runtime, and translate the output into the
harness contract:

* ``NemoSpeechTranscriber`` - ``nemo-speech transcribe --json``: words with
  start/end (Nemotron 3.5 ASR by default, or any indexed/local ASR GGUF);
* ``NemoSpeechDiarizer`` - ``nemo-speech diarize --format rttm``: turns
  (Nemotron 3 Diarization by default, or Streaming Sortformer v2);
* ``NemoSpeechTaggedPipeline`` - ``nemo-speech transcribe --diarize --json``:
  NVIDIA's own word-level speaker tagging, no harness assignment rule.

The binary is ``$NEMO_SPEECH_BIN`` or ``nemo-speech`` on PATH; models are
local GGUF paths or indexed names resolved with ``nemo-speech pull`` (which
verifies the pinned size and SHA-256; the cache is ``$NEMO_SPEECH_MODEL_DIR``
or the platform default).  Every run records the runtime files' hashes, the
GGUF's sha256, the command line, the wall time and the peak memory of the CLI
process (``/usr/bin/time``).  The runtime's CPU backend uses 4 threads
(hard-coded in NeMo-Speech.cpp src/runtime/ggml/session.cpp at commit
0f706e4).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from .adapters import (
    ASR_CACHE_SCHEMA,
    FASTER_WHISPER_PARAMS,
    FasterWhisperTranscriber,
    ModelComposedPipeline,
    _faster_whisper_from_params,
    hint_info,
)
from .base import (
    ASSIGNMENT_PARAMS,
    COMMON_AUDIO_PARAMS,
    Diarizer,
    Hypothesis,
    Pipeline,
    PipelineConfig,
    PipelineError,
    PipelineUnavailable,
    Segment,
    Transcriber,
    Turn,
    audio_provenance,
    make_segment,
    normalize_text,
    num_speakers_param,
    register,
    sort_segments,
)

#: HARNESS_POLICY defaults: the models NVIDIA's CLI uses by default (its model
#: index, NeMo-Speech.cpp 0.1.0), English, the backend the CLI picks.
DEFAULT_ASR_MODEL = "nemotron-3.5"
DEFAULT_DIAR_MODEL = "nemotron-diar"
DEFAULT_LANGUAGE = "en-US"
#: HARNESS_POLICY: consecutive words further apart than this start a new ASR
#: segment (the CLI returns one word list per file, no segments).
DEFAULT_SEGMENT_GAP_S = 0.5

NEMO_ASR_PARAMS = frozenset({"asr_model", "language", "device", "stream", "segment_gap_s", "asr_cache", "nemo_bin",
                             "right_context"})
NEMO_DIAR_PARAMS = frozenset({"diar_model", "diar_preset", "device", "offline", "onset", "offset", "pad_onset",
                              "pad_offset", "min_duration_on", "min_duration_off", "nemo_bin"})


def nemo_speech_bin(explicit: str | None = None) -> str | None:
    path = explicit or os.environ.get("NEMO_SPEECH_BIN") or shutil.which("nemo-speech")
    return path if path and Path(path).is_file() and os.access(path, os.X_OK) else None


def _availability() -> str | None:
    if nemo_speech_bin() is None:
        return "nemo-speech CLI not found ($NEMO_SPEECH_BIN or PATH); build NVIDIA/NeMo-Speech.cpp (docs/nvidia-speech.md)"
    return None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


_RUNTIME_CACHE: dict[str, dict[str, Any]] = {}
_MODEL_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def runtime_provenance(binary: str) -> dict[str, Any]:
    """The CLI's version and the sha256 of the binary and of every shared
    library beside it (the build that ran)."""
    if binary in _RUNTIME_CACHE:
        return _RUNTIME_CACHE[binary]
    b = Path(binary).resolve()
    version = subprocess.run([str(b), "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    files = {q.name: {"sha256": file_sha256(q), "bytes": q.stat().st_size}
             for q in sorted(b.parent.iterdir())
             if q.is_file() and not q.is_symlink() and (q == b or q.suffix in (".dylib", ".so") or ".so." in q.name)}
    doc = {"binary": str(b), "version": version, "files": files, "cpu_threads": 4}
    _RUNTIME_CACHE[binary] = doc
    return doc


def resolve_model(binary: str, model: str) -> dict[str, Any]:
    """A local GGUF path, or an indexed name/repo fetched (or verified) by
    ``nemo-speech pull``; returns the file's provenance."""
    key = (binary, model)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    p = Path(model).expanduser()
    source: dict[str, Any] = {"source": model}
    if not p.is_file():
        out = subprocess.run([binary, "pull", model], capture_output=True, text=True, timeout=3600)
        rows = [line.split("\t") for line in out.stdout.splitlines() if line.count("\t") >= 2]
        ggufs = [r for r in rows if r[-1].endswith(".gguf")]
        if out.returncode != 0 or not ggufs:
            raise PipelineUnavailable(f"nemo-speech pull {model!r} failed: {(out.stderr or out.stdout)[-400:]}")
        repo, role, path = ggufs[0][0], ggufs[0][1], ggufs[0][-1]
        p = Path(path)
        source.update({"repo": repo, "role": role, "revision": p.parent.name})
    doc = {**source, "path": str(p), "sha256": file_sha256(p), "bytes": p.stat().st_size}
    _MODEL_CACHE[key] = doc
    return doc


_TIME_PATTERNS = {
    "max_rss_bytes": re.compile(r"^\s*(\d+)\s+maximum resident set size", re.M),
    "peak_memory_footprint_bytes": re.compile(r"^\s*(\d+)\s+peak memory footprint", re.M),
    "user_s": re.compile(r"^\s*[\d.]+\s+real\s+([\d.]+)\s+user", re.M),
    "sys_s": re.compile(r"\s([\d.]+)\s+sys$", re.M),
}
_GNU_RSS = re.compile(r"Maximum resident set size \(kbytes\):\s*(\d+)")


def run_measured(cmd: list[str], timeout: float = 4 * 3600) -> tuple[subprocess.CompletedProcess, dict[str, Any]]:
    """Run ``cmd`` under ``/usr/bin/time`` (BSD ``-l`` / GNU ``-v``) and
    return its result and the wall time, CPU time and peak memory."""
    time_bin = "/usr/bin/time"
    flag = "-l" if platform.system() == "Darwin" else "-v"
    wrapped = [time_bin, flag, *cmd] if Path(time_bin).exists() else cmd
    t0 = time.perf_counter()
    out = subprocess.run(wrapped, capture_output=True, text=True, timeout=timeout)
    res: dict[str, Any] = {"wall_s": time.perf_counter() - t0}
    for k, pat in _TIME_PATTERNS.items():
        m = pat.search(out.stderr)
        if m:
            res[k] = float(m.group(1)) if k.endswith("_s") else int(m.group(1))
    m = _GNU_RSS.search(out.stderr)
    if m:
        res["max_rss_bytes"] = int(m.group(1)) * 1024
    return out, res


def write_wav16(pcm: np.ndarray, sample_rate: int, path: Path) -> None:
    import soundfile as sf

    if sample_rate != 16000:
        raise PipelineUnavailable("NeMo-Speech.cpp adapters expect 16 kHz input; load with sample_rate=16000")
    x = np.clip(np.asarray(pcm, dtype=np.float64), -1.0, 32767 / 32768)
    sf.write(str(path), np.round(x * 32768).astype(np.int16), sample_rate, subtype="PCM_16")


def words_to_segments(words: list[dict[str, Any]], gap_s: float, duration_s: float) -> list[Segment]:
    """CLI words -> contract ASR segments: normalised tokens (a word that
    normalises to several tokens splits its interval equally, one that
    normalises to nothing is dropped), times clipped to the audio, and a new
    segment wherever the pause between words exceeds ``gap_s``."""
    toks: list[dict[str, Any]] = []
    for w in words:
        parts = normalize_text(str(w.get("word", ""))).split()
        if not parts:
            continue
        a = min(max(0.0, float(w["start"])), duration_s)
        b = min(max(a, float(w["end"])), duration_s)
        step = (b - a) / len(parts)
        for i, t in enumerate(parts):
            toks.append({"w": t, "start": a + i * step, "end": a + (i + 1) * step if i + 1 < len(parts) else b})
    segs: list[list[dict[str, Any]]] = []
    for t in toks:
        if segs and t["start"] - segs[-1][-1]["end"] <= gap_s:
            segs[-1].append(t)
        else:
            segs.append([t])
    return [{"start": ws[0]["start"], "end": max(w["end"] for w in ws), "text": " ".join(w["w"] for w in ws),
             "words": ws} for ws in segs]


class NemoSpeechTranscriber(Transcriber):
    """ASR through ``nemo-speech transcribe --json`` (word timestamps).

    ``stream=True`` feeds the WAV through the cache-aware streaming recognizer
    in 160 ms chunks (``--stream``), as live capture would; the default is the
    CLI's whole-file mode, which itself switches to the streaming runner for
    audio past the model's positional limit (about 400 s: every AMI meeting).
    ``right_context`` sets that runner's cache-aware right context in 80 ms
    encoder frames (``--asr.streaming.rnnt_right_context``; the runtime
    default is 1, i.e. 160 ms chunks; the models' cards use 13 = 1.12 s).
    ``cache_dir`` works as in FasterWhisperTranscriber (same schema), so
    ``reassign_hypothesis`` can re-use a transcript."""

    name = "nemo-speech-asr"

    def __init__(self, model: str = DEFAULT_ASR_MODEL, language: str | None = DEFAULT_LANGUAGE,
                 device: str = "auto", stream: bool = False, segment_gap_s: float = DEFAULT_SEGMENT_GAP_S,
                 cache_dir: str | Path | None = None, binary: str | None = None,
                 right_context: int | None = None) -> None:
        b = nemo_speech_bin(binary)
        if b is None:
            raise PipelineUnavailable(_availability() or "nemo-speech not found")
        self.binary = b
        self.runtime = runtime_provenance(b)
        self.model_provenance = resolve_model(b, model)
        self.settings = {"model": model, "language": language, "device": device, "stream": bool(stream),
                         "segment_gap_s": float(segment_gap_s), "runtime": self.runtime["version"]}
        if right_context is not None:  # only when set, so the cache keys of default runs are unchanged
            self.settings["right_context"] = int(right_context)
        self.cache_dir = None if cache_dir is None else Path(cache_dir)
        self.last_info: dict[str, Any] = {}

    def cache_key(self, x: np.ndarray) -> str:
        h = hashlib.sha256(ASR_CACHE_SCHEMA.encode() + b"\0")
        h.update(json.dumps({"settings": self.settings, "model": self.model_provenance["sha256"],
                             "runtime": self.runtime["files"]}, sort_keys=True).encode() + b"\0")
        h.update(np.ascontiguousarray(x, dtype=np.float32).tobytes())
        return h.hexdigest()

    def transcribe(self, pcm: np.ndarray, sample_rate: int) -> list[Segment]:
        x = np.asarray(pcm, dtype=np.float32)
        if self.cache_dir is None:
            return self._decode(x, sample_rate)
        key = self.cache_key(x)
        entry = self.cache_dir / f"{key}.json"
        if entry.is_file():
            doc = json.loads(entry.read_text())
            if doc.get("schema") != ASR_CACHE_SCHEMA or doc.get("key") != key:
                raise PipelineError(f"{entry}: not an asr_cache entry for this audio and settings")
            self.last_info = {**doc["info"], "cache": {"hit": True, "key": key}}
            return doc["segments"]
        out = self._decode(x, sample_rate)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = entry.with_suffix(f".{os.getpid()}.part")
        tmp.write_text(json.dumps({"schema": ASR_CACHE_SCHEMA, "key": key, "settings": self.settings,
                                   "info": self.last_info, "segments": out}) + "\n")
        tmp.replace(entry)
        self.last_info["cache"] = {"hit": False, "key": key}
        return out

    def command(self, wav: Path) -> list[str]:
        cmd = [self.binary, "transcribe", str(wav), "--model", self.model_provenance["path"], "--json",
               "--device", self.settings["device"]]
        if self.settings["language"]:
            cmd += ["--language", self.settings["language"]]
        if self.settings["stream"]:
            cmd.append("--stream")
        if "right_context" in self.settings:
            cmd += ["--asr.streaming.rnnt_right_context", str(self.settings["right_context"])]
        return cmd

    def _decode(self, x: np.ndarray, sample_rate: int) -> list[Segment]:
        duration = len(x) / float(sample_rate)
        with tempfile.TemporaryDirectory(prefix="nemo-asr-") as d:
            wav = Path(d) / "audio.wav"
            write_wav16(x, sample_rate, wav)
            cmd = self.command(wav)
            out, res = run_measured(cmd)
        if out.returncode != 0:
            raise PipelineError(f"nemo-speech transcribe failed ({out.returncode}): {out.stderr[-1500:]}")
        doc = json.loads(out.stdout)
        segs = words_to_segments(doc.get("words") or [], self.settings["segment_gap_s"], duration)
        self.last_info = {"cli": res, "command": [Path(cmd[0]).name, *cmd[1:2], "<audio.wav>", *cmd[3:]],
                          "languages": doc.get("languages"), "cli_words": len(doc.get("words") or []),
                          "n_segments": len(segs), "n_words": sum(len(s["words"]) for s in segs),
                          "duration": duration}
        return segs


def parse_rttm(text: str) -> list[Turn]:
    turns: list[Turn] = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 8 and f[0] == "SPEAKER":
            start, dur = float(f[3]), float(f[4])
            if dur > 0:
                turns.append((start, start + dur, f[7]))
    return sorted(turns)


class NemoSpeechDiarizer(Diarizer):
    """Sortformer diarization through ``nemo-speech diarize --format rttm``.

    Sortformer has no speaker-count input: a ``num_speakers`` hint is
    recorded but cannot be applied (``hint_honoured`` says whether the count
    happened to match).  ``preset``, ``offline`` and the postprocessing
    thresholds are passed through when set; unset, the model's defaults
    apply."""

    name = "nemo-speech-diarization"

    def __init__(self, model: str = DEFAULT_DIAR_MODEL, preset: str | None = None, device: str = "auto",
                 offline: bool = False, post: dict[str, float] | None = None, binary: str | None = None) -> None:
        b = nemo_speech_bin(binary)
        if b is None:
            raise PipelineUnavailable(_availability() or "nemo-speech not found")
        self.binary = b
        self.runtime = runtime_provenance(b)
        prov = resolve_model(b, model)
        self.models = {"diarization": prov}
        self.model_path = prov["path"]
        self.settings = {"model": model, "preset": preset, "device": device, "offline": bool(offline),
                         "post": dict(post or {}), "runtime": self.runtime["version"]}
        self.last_info: dict[str, Any] = {}

    def command(self, wav: Path, rttm: Path) -> list[str]:
        s = self.settings
        cmd = [self.binary, "diarize", str(wav), "--model", self.model_path, "--format", "rttm",
               "--output", str(rttm), "--force", "--device", s["device"]]
        if s["preset"]:
            cmd += ["--preset", s["preset"]]
        if s["offline"]:
            cmd.append("--offline")
        for k, v in sorted(s["post"].items()):
            cmd += [f"--{k.replace('_', '-')}", str(v)]
        return cmd

    def diarize(self, pcm: np.ndarray, sample_rate: int, num_speakers: int | None = None) -> list[Turn]:
        with tempfile.TemporaryDirectory(prefix="nemo-diar-") as d:
            wav, rttm = Path(d) / "audio.wav", Path(d) / "audio.rttm"
            write_wav16(pcm, sample_rate, wav)
            cmd = self.command(wav, rttm)
            out, res = run_measured(cmd)
            if out.returncode != 0 or not rttm.is_file():
                raise PipelineError(f"nemo-speech diarize failed ({out.returncode}): {out.stderr[-1500:]}")
            turns = parse_rttm(rttm.read_text())
        self.last_info = {"cli": res, "command": [Path(cmd[0]).name, cmd[1], "<audio.wav>", *cmd[3:8], "<audio.rttm>",
                                                 *cmd[9:]], **hint_info(turns, num_speakers)}
        return turns


def _diarizer_from_params(p: dict[str, Any]) -> NemoSpeechDiarizer:
    post = {k: float(p[k]) for k in ("onset", "offset", "pad_onset", "pad_offset", "min_duration_on", "min_duration_off")
            if p.get(k) is not None}
    return NemoSpeechDiarizer(model=str(p.get("diar_model", DEFAULT_DIAR_MODEL)), preset=p.get("diar_preset"),
                              device=str(p.get("device", "auto")), offline=_bool(p.get("offline", False)), post=post,
                              binary=p.get("nemo_bin"))


def _transcriber_from_params(p: dict[str, Any]) -> NemoSpeechTranscriber:
    return NemoSpeechTranscriber(model=str(p.get("asr_model", DEFAULT_ASR_MODEL)),
                                 language=p.get("language", DEFAULT_LANGUAGE), device=str(p.get("device", "auto")),
                                 stream=_bool(p.get("stream", False)),
                                 segment_gap_s=float(p.get("segment_gap_s", DEFAULT_SEGMENT_GAP_S)),
                                 cache_dir=p.get("asr_cache"), binary=p.get("nemo_bin"),
                                 right_context=None if p.get("right_context") is None else int(p["right_context"]))


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        if v.lower() in ("1", "true", "yes", "on"):
            return True
        if v.lower() in ("0", "false", "no", "off", ""):
            return False
        raise PipelineError(f"not a boolean: {v!r}")
    return bool(v)


class NemoSpeechTaggedPipeline(Pipeline):
    """``nemo-speech transcribe --diarize``: the runtime tags each word with a
    Sortformer speaker (NVIDIA's integration).  Consecutive words of one
    speaker within ``segment_gap_s`` form a segment; no harness assignment
    rule is involved."""

    name = "nemotron-tagged"

    def __init__(self, config: PipelineConfig | None = None) -> None:
        super().__init__(config or PipelineConfig(name=self.name))
        p = self.config.params
        t0 = time.perf_counter()
        self.asr = _transcriber_from_params({**p, "asr_cache": None})
        self.diar_model = resolve_model(self.asr.binary, str(p.get("diar_model", DEFAULT_DIAR_MODEL)))
        self.diar_preset = p.get("diar_preset")
        self.load_s = time.perf_counter() - t0

    def run(self, audio_path: str | Path, meeting_dir: str | Path | None = None) -> Hypothesis:
        num = num_speakers_param(self.config.param("num_speakers"))
        audio = self.load(audio_path)
        meeting = self.find_meeting(audio_path, meeting_dir)
        meeting_id = str(meeting["meeting_id"]) if meeting is not None else Path(audio_path).stem
        problems = self.check_audio(audio, meeting)
        with tempfile.TemporaryDirectory(prefix="nemo-tagged-") as d:
            wav = Path(d) / "audio.wav"
            write_wav16(audio.pcm, audio.sample_rate, wav)
            cmd = self.asr.command(wav) + ["--diar-model", self.diar_model["path"]]
            if self.diar_preset:
                cmd += ["--diar-preset", str(self.diar_preset)]
            out, res = run_measured(cmd)
        if out.returncode != 0:
            raise PipelineError(f"nemo-speech transcribe --diarize failed ({out.returncode}): {out.stderr[-1500:]}")
        doc = json.loads(out.stdout)
        gap = self.asr.settings["segment_gap_s"]
        toks = [dict(w, speaker=f"spk{int(w.get('speaker', 0))}") for w in (doc.get("words") or [])]
        segments: list[Segment] = []
        run: list[dict[str, Any]] = []
        for tok in self._tokens(toks, audio.duration_s):
            if run and (tok["speaker"] != run[-1]["speaker"] or tok["start"] - run[-1]["end"] > gap):
                segments.append(self._segment(run))
                run = []
            run.append(tok)
        if run:
            segments.append(self._segment(run))
        spk_turns = sorted((s["start"], s["end"], s["speaker"]) for s in segments)
        timing = {"model_load_s": self.load_s, "asr_diarization_s": res["wall_s"], "audio_s": audio.duration_s}
        timing["rtf"] = res["wall_s"] / audio.duration_s if audio.duration_s > 0 else float("nan")
        extra = {
            "audio": audio_provenance(audio, problems),
            "components": {"transcriber": self.asr.name, "diarizer": "nemo-speech-diarization (word tags)"},
            "models": {"asr": self.asr.model_provenance, "diarization": self.diar_model},
            "runtime": self.asr.runtime,
            "timing": timing,
            "asr": {"settings": self.asr.settings, "cli": res, "languages": doc.get("languages"),
                    "cli_words": len(doc.get("words") or [])},
            "assignment": {"rule": "NVIDIA NeMo-Speech.cpp word-level speaker tags (transcribe --diarize); "
                                   f"a segment per run of one speaker's words with pauses <= {gap} s"},
            "diarization": {"preset": self.diar_preset, "word_runs": len(spk_turns),
                            **hint_info(spk_turns, num)},
        }
        return Hypothesis(meeting_id=meeting_id, system=self.config.name or self.name,
                          segments=sort_segments(segments), extra=extra).validate()

    @staticmethod
    def _tokens(words: list[dict[str, Any]], duration_s: float) -> list[dict[str, Any]]:
        out = []
        for w in words:
            parts = normalize_text(str(w.get("word", ""))).split()
            if not parts:
                continue
            a = min(max(0.0, float(w["start"])), duration_s)
            b = min(max(a, float(w["end"])), duration_s)
            step = (b - a) / len(parts)
            for i, t in enumerate(parts):
                out.append({"w": t, "start": a + i * step, "end": a + (i + 1) * step if i + 1 < len(parts) else b,
                            "speaker": w["speaker"]})
        return out

    @staticmethod
    def _segment(run: list[dict[str, Any]]) -> Segment:
        return make_segment(run[0]["speaker"], run[0]["start"], max(max(w["end"] for w in run), run[0]["start"] + 0.01),
                            " ".join(w["w"] for w in run), [{"w": w["w"], "start": w["start"], "end": w["end"]} for w in run])


# --- registry ---------------------------------------------------------------------------------------------


@register(
    "nemo-speech-diarization",
    description="NVIDIA Nemotron 3 Diarization (Sortformer, up to 8 speakers) through NeMo-Speech.cpp; turns only",
    availability=_availability,
    params=NEMO_DIAR_PARAMS | COMMON_AUDIO_PARAMS,
)
def _make_nemo_diarization(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="nemo-speech-diarization")
    t0 = time.perf_counter()
    di = _diarizer_from_params(cfg.params)
    return ModelComposedPipeline(None, di, cfg, name="nemo-speech-diarization", load_s=time.perf_counter() - t0)


@register(
    "nemotron",
    description="NVIDIA Nemotron 3.5 ASR words on Nemotron 3 Diarization turns (both through NeMo-Speech.cpp); "
                "words take the max-overlap speaker (harness rule)",
    availability=_availability,
    params=NEMO_ASR_PARAMS | NEMO_DIAR_PARAMS | COMMON_AUDIO_PARAMS | ASSIGNMENT_PARAMS,
)
def _make_nemotron(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="nemotron")
    t0 = time.perf_counter()
    asr = _transcriber_from_params(cfg.params)
    di = _diarizer_from_params(cfg.params)
    return ModelComposedPipeline(asr, di, cfg, name="nemotron", load_s=time.perf_counter() - t0)


@register(
    "nemotron-asr",
    description="NVIDIA ASR (Nemotron 3.5 by default) through NeMo-Speech.cpp, no diarizer: every word gets one "
                "speaker (for WER, and as a transcript for recombination with any diarizer's turns)",
    availability=_availability,
    params=NEMO_ASR_PARAMS | COMMON_AUDIO_PARAMS | ASSIGNMENT_PARAMS,
)
def _make_nemotron_asr(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="nemotron-asr")
    t0 = time.perf_counter()
    asr = _transcriber_from_params(cfg.params)
    return ModelComposedPipeline(asr, None, cfg, name="nemotron-asr", load_s=time.perf_counter() - t0)


@register(
    "nemotron-tagged",
    description="NVIDIA Nemotron 3.5 ASR with NeMo-Speech.cpp's own word-level Sortformer speaker tags "
                "(transcribe --diarize); no harness assignment rule",
    availability=_availability,
    params=(NEMO_ASR_PARAMS - {"asr_cache"}) | {"diar_model", "diar_preset"} | COMMON_AUDIO_PARAMS,
)
def _make_nemotron_tagged(config: PipelineConfig | None = None) -> Pipeline:
    return NemoSpeechTaggedPipeline(config or PipelineConfig(name="nemotron-tagged"))


def _whisper_nemo_availability() -> str | None:
    from .whisper_sherpa import _whisper_sherpa_availability

    return _availability() or _whisper_sherpa_availability()


@register(
    "faster-whisper+nemotron-diar",
    description="faster-whisper small.en (the whisper-sherpa transcript) on Nemotron 3 Diarization turns; "
                "words take the max-overlap speaker",
    availability=_whisper_nemo_availability,
    params=FASTER_WHISPER_PARAMS | NEMO_DIAR_PARAMS | COMMON_AUDIO_PARAMS | ASSIGNMENT_PARAMS,
)
def _make_whisper_nemo(config: PipelineConfig | None = None) -> Pipeline:
    from .whisper_sherpa import WHISPER_SHERPA_ASR_DEFAULTS

    cfg = config or PipelineConfig(name="faster-whisper+nemotron-diar")
    t0 = time.perf_counter()
    di = _diarizer_from_params(cfg.params)
    asr: FasterWhisperTranscriber = _faster_whisper_from_params(cfg.params, allow_library_names=False,
                                                                 **WHISPER_SHERPA_ASR_DEFAULTS)
    return ModelComposedPipeline(asr, di, cfg, name="faster-whisper+nemotron-diar", load_s=time.perf_counter() - t0)
