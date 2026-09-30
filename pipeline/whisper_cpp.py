"""whisper.cpp adapter (research branch; docs/nvidia-speech.md).

The current stack's ASR is faster-whisper ``small.en`` (CTranslate2), which
has no supported Android build.  Its on-device form is whisper.cpp
(https://github.com/ggml-org/whisper.cpp, MIT) running the same OpenAI
``small.en`` weights as a ggml file.  This adapter runs ``whisper-cli`` as a
subprocess, like pipeline/nemo_speech.py runs NVIDIA's CLI, and reads its full
JSON output: each whisper segment becomes an ASR segment, and each word is the
run of tokens from one space-prefixed token to the next, timed by the tokens'
offsets.  whisper.cpp's defaults are kept (4 threads, beam 5, temperature
fallback, flash attention); DTW token timestamps are not used because they
require flash attention off.

The binary is ``$WHISPER_CPP_BIN`` or ``whisper-cli`` on PATH; the model is a
ggml file (``$WHISPER_CPP_MODEL`` or ``--param wcpp_model``).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from .adapters import ASR_CACHE_SCHEMA, ModelComposedPipeline
from .base import (
    ASSIGNMENT_PARAMS,
    COMMON_AUDIO_PARAMS,
    PipelineConfig,
    PipelineError,
    PipelineUnavailable,
    Pipeline,
    Segment,
    Transcriber,
    normalize_text,
    register,
)
from .nemo_speech import file_sha256, run_measured, write_wav16
from .whisper_sherpa import SHERPA_PARAMS

WCPP_PARAMS = frozenset({"wcpp_model", "wcpp_bin", "language", "threads", "beam_size", "no_gpu", "asr_cache"})


def whisper_cpp_bin(explicit: str | None = None) -> str | None:
    path = explicit or os.environ.get("WHISPER_CPP_BIN") or shutil.which("whisper-cli")
    return path if path and Path(path).is_file() and os.access(path, os.X_OK) else None


def _availability() -> str | None:
    if whisper_cpp_bin() is None:
        return "whisper-cli not found ($WHISPER_CPP_BIN or PATH); build ggml-org/whisper.cpp (docs/nvidia-speech.md)"
    if not os.environ.get("WHISPER_CPP_MODEL"):
        return "no ggml model: set $WHISPER_CPP_MODEL (or pass --param wcpp_model)"
    return None


def tokens_to_words(tokens: list[dict[str, Any]], duration_s: float) -> list[dict[str, Any]]:
    """whisper.cpp tokens -> contract words.  Special tokens (``[_...]``) are
    skipped; a token with a leading space starts a new word, any other token
    extends the current one; times are the tokens' ``offsets`` (ms), clipped
    to the audio.  Text is normalised (``normalize_text``); a word that
    normalises to nothing is dropped, one that normalises to several tokens
    splits its interval equally."""
    raw: list[list[Any]] = []
    for t in tokens:
        text = str(t.get("text", ""))
        if not text or text.startswith("[_") or text.startswith("<|"):
            continue
        a, b = float(t["offsets"]["from"]) / 1000.0, float(t["offsets"]["to"]) / 1000.0
        if text.startswith(" ") or not raw:
            raw.append([text, a, b])
        else:
            raw[-1][0] += text
            raw[-1][2] = max(raw[-1][2], b)
    out: list[dict[str, Any]] = []
    for text, a, b in raw:
        toks = normalize_text(text).split()
        if not toks:
            continue
        a, b = min(max(0.0, a), duration_s), min(max(0.0, b), duration_s)
        b = max(a, b)
        step = (b - a) / len(toks)
        for i, tok in enumerate(toks):
            out.append({"w": tok, "start": a + i * step, "end": a + (i + 1) * step if i + 1 < len(toks) else b})
    return out


class WhisperCppTranscriber(Transcriber):
    name = "whisper.cpp"

    def __init__(self, model: str | None = None, language: str = "en", threads: int = 4, beam_size: int = 5,
                 no_gpu: bool = False, cache_dir: str | Path | None = None, binary: str | None = None) -> None:
        b = whisper_cpp_bin(binary)
        if b is None:
            raise PipelineUnavailable("whisper-cli not found ($WHISPER_CPP_BIN or PATH)")
        model = model or os.environ.get("WHISPER_CPP_MODEL")
        if not model or not Path(model).is_file():
            raise PipelineUnavailable(f"whisper.cpp model not found: {model!r}")
        self.binary = b
        mp = Path(model)
        self.model_provenance = {"source": mp.name, "path": str(mp), "sha256": file_sha256(mp), "bytes": mp.stat().st_size}
        bdir = Path(b).resolve().parent
        self.runtime = {"binary": str(Path(b).resolve()),
                        "files": {q.name: {"sha256": file_sha256(q), "bytes": q.stat().st_size}
                                  for q in sorted(bdir.iterdir()) if q.is_file() and not q.is_symlink()
                                  and (q.name == Path(b).name or q.suffix in (".dylib", ".so"))}}
        self.settings = {"model": mp.name, "language": language, "threads": int(threads), "beam_size": int(beam_size),
                         "no_gpu": bool(no_gpu), "flash_attn": True, "word_times": "token offsets"}
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

    def _decode(self, x: np.ndarray, sample_rate: int) -> list[Segment]:
        duration = len(x) / float(sample_rate)
        s = self.settings
        with tempfile.TemporaryDirectory(prefix="wcpp-") as d:
            wav, base = Path(d) / "audio.wav", Path(d) / "out"
            write_wav16(x, sample_rate, wav)
            cmd = [self.binary, "-m", self.model_provenance["path"], "-f", str(wav), "-l", s["language"],
                   "-t", str(s["threads"]), "-bs", str(s["beam_size"]), "-ojf", "-of", str(base), "-np"]
            if s["no_gpu"]:
                cmd.append("-ng")
            out, res = run_measured(cmd)
            if out.returncode != 0 or not base.with_suffix(".json").is_file():
                raise PipelineError(f"whisper-cli failed ({out.returncode}): {out.stderr[-1500:]}")
            doc = json.loads(base.with_suffix(".json").read_text(errors="replace"))
        segs: list[Segment] = []
        for seg in doc.get("transcription") or []:
            words = tokens_to_words(seg.get("tokens") or [], duration)
            if not words:
                continue
            segs.append({"start": words[0]["start"], "end": max(w["end"] for w in words),
                         "text": " ".join(w["w"] for w in words), "words": words})
        segs.sort(key=lambda g: (g["start"], g["end"]))
        self.last_info = {"cli": res, "command": [Path(cmd[0]).name, "-m", "<model>", "-f", "<audio.wav>", *cmd[5:13],
                                                  "<out>", *cmd[14:]],
                          "n_segments": len(segs), "n_words": sum(len(g["words"]) for g in segs), "duration": duration}
        return segs


def _transcriber_from_params(p: dict[str, Any]) -> WhisperCppTranscriber:
    return WhisperCppTranscriber(model=p.get("wcpp_model"), language=str(p.get("language", "en")),
                                 threads=int(p.get("threads", 4)), beam_size=int(p.get("beam_size", 5)),
                                 no_gpu=str(p.get("no_gpu", "false")).lower() in ("1", "true", "yes"),
                                 cache_dir=p.get("asr_cache"), binary=p.get("wcpp_bin"))


@register(
    "whisper-cpp",
    description="whisper.cpp small.en (ggml) ASR; the model-free diarizer is NOT attached: every word gets one "
                "speaker (use for WER and as a transcript for recombination)",
    availability=_availability,
    params=WCPP_PARAMS | COMMON_AUDIO_PARAMS | ASSIGNMENT_PARAMS,
)
def _make_whisper_cpp(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="whisper-cpp")
    t0 = time.perf_counter()
    asr = _transcriber_from_params(cfg.params)
    return ModelComposedPipeline(asr, None, cfg, name="whisper-cpp", load_s=time.perf_counter() - t0)


def _whisper_cpp_sherpa_availability() -> str | None:
    from .whisper_sherpa import _whisper_sherpa_availability  # sherpa-onnx + its pinned models

    return _availability() or _whisper_sherpa_availability()


@register(
    "whisper-cpp+sherpa",
    description="the current stack in on-device form: whisper.cpp small.en (ggml) + sherpa-onnx diarization at the "
                "dev-calibrated cluster_threshold 1.15; words take the max-overlap speaker",
    availability=_whisper_cpp_sherpa_availability,
    params=WCPP_PARAMS | SHERPA_PARAMS | COMMON_AUDIO_PARAMS | ASSIGNMENT_PARAMS,
)
def _make_whisper_cpp_sherpa(config: PipelineConfig | None = None) -> Pipeline:
    from .whisper_sherpa import _sherpa_from_params

    cfg = config or PipelineConfig(name="whisper-cpp+sherpa")
    p = {"cluster_threshold": 1.15, **cfg.params}
    t0 = time.perf_counter()
    di = _sherpa_from_params(p)
    asr = _transcriber_from_params(p)
    return ModelComposedPipeline(asr, di, cfg, name="whisper-cpp+sherpa", load_s=time.perf_counter() - t0)
