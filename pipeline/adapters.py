"""Optional adapters for real ASR/diarization systems.

Status (docs/pipeline.md §2, §8, §11), 2026-09-25:

* ``FasterWhisperTranscriber`` HAS run against the installed faster-whisper
  1.2.1 with the pinned ``small.en`` CTranslate2 weights
  (pipeline/model_store.py); its API use was checked against that release
  (review finding PIPE-05) and tests/test_pipeline_models.py runs it on AMI
  audio when the weights are present.  It is used by the ``faster-whisper``
  and ``faster-whisper+pyannote`` entries here and by ``whisper-sherpa``
  (pipeline/whisper_sherpa.py).  Every model name is resolved to a LOCAL
  directory before ``WhisperModel`` sees it (``resolve_fw_model``), so the
  library never downloads on its own: a pinned name needs
  ``python -m pipeline fetch-models``, any other name needs the Hugging Face
  cache or an explicit ``allow_download=true``.  The files loaded are hashed
  and recorded in ``hyp.extra["models"]``.
* The pyannote.audio, whisperx and SpeechBrain adapters are still UNTESTED
  against a model: those packages are not installed and their default
  pyannote pipelines are gated.  Each is written against the package's
  documented public API (cited per class, from the READMEs as remembered
  offline), imports lazily, and the registry reports it as unavailable with a
  reason instead of failing at import time.  Their own logic (output
  normalisation across library versions, word timing repair, parameter
  plumbing) IS tested against stand-in modules in
  tests/test_pipeline_adapters.py.  For gated models they read a Hugging
  Face token from an environment variable you choose; they never embed one.

All parameter defaults below are HARNESS_POLICY.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import time
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .base import (
    ASSIGNMENT_PARAMS,
    ASSIGNMENT_RULE,
    COMMON_AUDIO_PARAMS,
    ComposedPipeline,
    Diarizer,
    Hypothesis,
    ParamError,
    PipelineError,
    Pipeline,
    PipelineConfig,
    PipelineUnavailable,
    Segment,
    Transcriber,
    Turn,
    assign_speakers,
    audio_provenance,
    make_segment,
    normalize_text,
    tie_break_param,
    num_speakers_param,
    register,
    sort_segments,
)
from .energy_vad import (
    DIARIZER_PARAMS,
    ClusterParams,
    EnergyVadClusterDiarizer,
    VadParams,
    _coerce,
    _relabel_by_first_appearance,
    cell_windows,
    chunk_regions,
    cluster_embeddings,
    energy_vad,
    mask_to_regions,
    smooth_turns,
)


def _missing(module: str) -> str | None:
    """None if importable, else a one-line reason."""
    try:
        importlib.import_module(module)
        return None
    except Exception as exc:  # ImportError, or a broken install raising anything
        return f"python package {module!r} not importable ({type(exc).__name__}: {exc})"


def _missing_any(*modules: str) -> str | None:
    reasons = [r for r in (_missing(m) for m in modules) if r]
    return "; ".join(reasons) if reasons else None


def _major_version(dist: str) -> int | None:
    """Installed distribution's major version, or None when unknown."""
    try:
        from importlib.metadata import version

        return int(version(dist).split(".")[0])
    except Exception:
        return None


# --- faster-whisper -----------------------------------------------------------

#: ``--param`` keys read by the faster-whisper transcriber factories.
FASTER_WHISPER_PARAMS = frozenset(
    {"model", "device", "compute_type", "language", "beam_size", "vad_filter", "cpu_threads", "seed", "temperature",
     "asr_cache"}
)

#: Schema tag of an ``asr_cache`` entry (see FasterWhisperTranscriber).
ASR_CACHE_SCHEMA = "plaud-harness/asr-cache/1"

#: HARNESS_POLICY: the seed given to CTranslate2 before the model is built.
#: faster-whisper retries a window it judges poor at higher temperatures, and
#: those retries SAMPLE; unseeded, one AMI meeting (EN2002a) gave 5504, 5118
#: and 5223 words in three runs (docs/v5-results.md §8.3).  Checked on
#: ctranslate2 4.8.2: a seed set before a model's first decode makes that
#: model's sampled output repeat across fresh processes; calling
#: ``set_random_seed`` again later in the same process does NOT restart the
#: stream (docs/pipeline.md §2).  So the transcriber seeds once, in
#: ``__init__``, and the harness runs one process per meeting.
DEFAULT_FW_SEED = 0

#: faster-whisper 1.2.1's own temperature schedule (``WhisperModel.transcribe``
#: default), recorded explicitly so a hyp's settings say what ran.
FW_LIBRARY_TEMPERATURES: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

#: Extra ``--param`` of the entries that accept library model names
#: (``faster-whisper``, ``faster-whisper+pyannote``; not ``whisper-sherpa``).
FW_DOWNLOAD_PARAM = "allow_download"

#: HARNESS_POLICY: the default ASR model, an English-only CTranslate2 Whisper
#: small enough for an 8 GB machine; ``model=`` may also name a pinned asset
#: key, a local CTranslate2 directory, or (legacy entries only) a size/repo
#: name faster-whisper knows, taken from the Hugging Face cache.
DEFAULT_FW_MODEL = "small.en"

#: model names that resolve to a pinned asset in pipeline.model_store.
FW_MODEL_ALIASES = {"small.en": "faster-whisper-small.en"}


def resolve_fw_model(
    name: str, *, allow_library_names: bool = True, allow_download: bool = False
) -> tuple[str, dict[str, Any]]:
    """``(local directory WhisperModel is given, provenance of its bytes)``.

    Every accepted name becomes a local directory BEFORE ``WhisperModel``
    sees it, so faster-whisper never downloads on its own (HARNESS_POLICY):

    * a pinned asset key or alias (``small.en``): its fetched copy, every
      file sha256-checked against the pin (``model_store.require``).
      Unfetched or mismatched is ``PipelineUnavailable`` naming
      ``python -m pipeline fetch-models``; a pinned name is never handed to
      the library's own downloader, with or without ``allow_download``.
    * an existing directory: used as is; its files are hashed
      (``model_store.describe_local``) and recorded as unpinned.
    * any other string (``tiny``, ``base.en``, ``org/repo``), only when
      ``allow_library_names`` (the legacy entries; ``whisper-sherpa`` refuses
      it with ParamError): ``faster_whisper.utils.download_model`` resolves
      it with ``local_files_only=not allow_download`` and
      ``use_auth_token=False`` (huggingface_hub then sends no stored token).
      Without ``allow_download`` only a copy already in the Hugging Face
      cache is used; otherwise PipelineUnavailable.  With it, the download
      is UNPINNED (the repo's default branch).  The snapshot's files are
      hashed and recorded as unpinned.
    """
    from . import model_store

    key = FW_MODEL_ALIASES.get(name, name)
    if key in model_store.ASSETS and model_store.ASSETS[key].kind == "asr":
        path, prov = model_store.require(key)
        return str(path), prov
    p = Path(name).expanduser()
    if p.is_dir():
        return str(p), model_store.describe_local(p)
    if not allow_library_names:
        raise ParamError(
            f"model {name!r} is neither a pinned asset ({', '.join(sorted(FW_MODEL_ALIASES))} or "
            f"{', '.join(k for k, a in sorted(model_store.ASSETS.items()) if a.kind == 'asr')}) "
            "nor a local CTranslate2 directory"
        )
    try:
        from faster_whisper.utils import available_models, download_model  # type: ignore[import-not-found]
    except Exception as exc:
        raise PipelineUnavailable(f"cannot resolve model name {name!r}: faster_whisper.utils not importable ({exc})") from exc
    if "/" not in name and name not in available_models():
        raise ParamError(
            f"model {name!r} is not a pinned asset, a local CTranslate2 directory, a faster-whisper size "
            f"({', '.join(available_models())}) or an org/repo id"
        )
    try:
        local = download_model(name, local_files_only=not allow_download, use_auth_token=False)
    except Exception as exc:  # huggingface_hub: not cached (local_files_only), network, HTTP
        if allow_download:
            raise PipelineUnavailable(f"model {name!r} could not be downloaded ({type(exc).__name__}: {exc})") from exc
        raise PipelineUnavailable(
            f"model {name!r} is not in the local Hugging Face cache ({type(exc).__name__}); nothing is downloaded "
            f"implicitly: pass a local CTranslate2 directory, the pinned {DEFAULT_FW_MODEL!r} "
            f"(python -m pipeline fetch-models), or {FW_DOWNLOAD_PARAM}=true to let faster-whisper fetch it "
            "(unpinned, no token)"
        ) from exc
    prov = model_store.describe_local(local)
    prov.update({"name": name, "source": "faster_whisper.utils.download_model (Hugging Face cache, unpinned)",
                 FW_DOWNLOAD_PARAM: bool(allow_download)})
    return str(local), prov


def whisper_words(raw_words: Any, duration_s: float | None = None) -> list[dict[str, Any]]:
    """faster-whisper ``Word`` objects -> contract words (HARNESS_POLICY).

    * text: ``normalize_text``; a word that normalises to nothing (pure
      punctuation) is dropped; one that normalises to several tokens has its
      interval split equally between them (the evals layer's rule);
    * times: clipped to ``[0, duration_s]`` and ``end >= start``.
    """
    out: list[dict[str, Any]] = []
    for w in raw_words or []:
        toks = normalize_text(str(w.word)).split()
        if not toks:
            continue
        start, end = float(w.start), float(w.end)
        if duration_s is not None:
            start = min(max(0.0, start), duration_s)
            end = min(max(0.0, end), duration_s)
        start = max(0.0, start)
        end = max(start, end)
        step = (end - start) / len(toks)
        for i, t in enumerate(toks):
            out.append({"w": t, "start": start + i * step, "end": start + (i + 1) * step if i + 1 < len(toks) else end})
    return out


class FasterWhisperTranscriber(Transcriber):
    """faster-whisper (SYSTRAN) transcriber.

    Checked against the installed faster-whisper 1.2.1 (docs/pipeline.md
    §11; requirements/models.txt):
        model = WhisperModel(model_size_or_path, device="cpu", compute_type="int8",
                             cpu_threads=0, download_root=None, local_files_only=False, ...)
        segments, info = model.transcribe(audio, language=None, beam_size=5,
                                          word_timestamps=True, vad_filter=False, ...)
    ``audio`` is a float32 numpy array at 16 kHz.  ``segments`` is a lazy
    generator (decoding happens while it is consumed); each segment has
    ``.start .end .text .words`` and each word is ``Word(start, end, word,
    probability)`` with the word's leading space and punctuation attached.
    ``info`` is a ``TranscriptionInfo`` (``language``,
    ``language_probability``, ``duration``, ``duration_after_vad``, ...);
    the fields used are copied to ``last_info``, together with how many
    segments each decoding temperature produced (``temperatures``) and how
    many needed the sampled fallback (``fallback_segments``).

    Repeatability: ``seed`` (default ``DEFAULT_FW_SEED``) is passed to
    ``ctranslate2.set_random_seed`` before the model is built, which makes the
    first ``transcribe`` of this instance repeat exactly in a fresh process.
    Later calls on the same instance continue the random stream, so they are
    repeatable only as the same sequence of calls.  ``seed=None`` leaves the
    library unseeded.  ``temperature=0`` (or any single value) disables the
    fallback schedule entirely.

    ``cache_dir`` (``--param asr_cache=<dir>``, opt-in) stores each transcript
    under the sha256 of the audio samples, the decoding settings and the model
    provenance, and returns a stored transcript instead of decoding again;
    ``last_info["cache"]`` says whether it was a hit.  Used by
    scripts/run-v5.sh so that several diarization settings of one meeting
    share ONE transcript (the ASR does not depend on the diarizer).  A hit
    reports no ASR time, so timing comes from the run that filled the entry.
    """

    name = "faster-whisper"

    def __init__(
        self,
        model: str = DEFAULT_FW_MODEL,
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        beam_size: int = 5,
        vad_filter: bool = False,
        cpu_threads: int | None = None,
        seed: int | None = DEFAULT_FW_SEED,
        temperature: float | tuple[float, ...] = FW_LIBRARY_TEMPERATURES,
        *,
        allow_library_names: bool = True,
        allow_download: bool = False,
        cache_dir: str | Path | None = None,
    ) -> None:
        reason = _missing("faster_whisper")
        if reason:
            raise PipelineUnavailable(reason)
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        # Always a local directory (resolve_fw_model), so WhisperModel never downloads.
        path, provenance = resolve_fw_model(
            str(model), allow_library_names=allow_library_names, allow_download=allow_download
        )
        kwargs: dict[str, Any] = {"device": device, "compute_type": compute_type}
        if cpu_threads is not None:  # 0 = the library's default (4 threads)
            kwargs["cpu_threads"] = int(cpu_threads)
        seed_applied = False
        if seed is not None:
            try:
                import ctranslate2  # type: ignore[import-not-found]  # a dependency of the real faster-whisper
            except ImportError:  # only with a stand-in faster_whisper module (tests)
                pass
            else:
                ctranslate2.set_random_seed(int(seed))  # before the first decode; see DEFAULT_FW_SEED
                seed_applied = True
        self.model = WhisperModel(path, **kwargs)
        self.model_name = str(model)
        self.model_provenance = provenance
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.seed = seed
        self.temperature: float | list[float] = (
            [float(t) for t in temperature] if isinstance(temperature, (list, tuple)) else float(temperature)
        )
        self.settings = {"model": str(model), "device": device, "compute_type": compute_type, "language": language,
                         "beam_size": beam_size, "vad_filter": vad_filter, "cpu_threads": cpu_threads,
                         "word_timestamps": True, "seed": seed, "seed_applied": seed_applied,
                         "temperature": self.temperature}
        self.cache_dir = None if cache_dir is None else Path(cache_dir)
        self.last_info: dict[str, Any] = {}

    def cache_key(self, x: np.ndarray) -> str:
        """sha256 over the schema tag, the decoding settings, the model
        provenance and the float32 samples."""
        h = hashlib.sha256(ASR_CACHE_SCHEMA.encode() + b"\0")
        h.update(json.dumps({"settings": self.settings, "model": self.model_provenance},
                            sort_keys=True, default=str).encode() + b"\0")
        h.update(np.ascontiguousarray(x, dtype=np.float32).tobytes())
        return h.hexdigest()

    def transcribe(self, pcm: np.ndarray, sample_rate: int) -> list[Segment]:
        if sample_rate != 16000:
            raise PipelineUnavailable("faster-whisper expects 16 kHz input; load with sample_rate=16000")
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
        segments, info = self.model.transcribe(
            x,
            beam_size=self.beam_size,
            word_timestamps=True,
            language=self.language,
            vad_filter=self.vad_filter,
            temperature=self.temperature,
        )
        out: list[Segment] = []
        temps: dict[str, int] = {}
        for s in segments:  # consuming the generator is what runs the decoder
            t = getattr(s, "temperature", None)
            key = "unknown" if t is None else f"{float(t):.1f}"
            temps[key] = temps.get(key, 0) + 1
            words = whisper_words(getattr(s, "words", None), duration)
            text = " ".join(w["w"] for w in words) if words else normalize_text(s.text)
            if not text:
                continue
            start = min(max(0.0, float(s.start)), duration)
            end = min(max(start, float(s.end)), duration)
            out.append(make_segment_no_speaker(start, end, text, words))
        self.last_info = {
            k: (float(v) if isinstance(v, (int, float, np.floating)) and not isinstance(v, bool) else v)
            for k, v in ((k, getattr(info, k, None)) for k in ("language", "language_probability", "duration", "duration_after_vad"))
            if v is not None
        }
        self.last_info["n_segments"] = len(out)
        self.last_info["n_words"] = sum(len(s["words"]) for s in out)
        # counted over every decoded segment, including ones dropped as empty
        self.last_info["temperatures"] = dict(sorted(temps.items()))
        self.last_info["fallback_segments"] = sum(n for k, n in temps.items() if k not in ("0.0", "unknown"))
        return out


def make_segment_no_speaker(start: float, end: float, text: str, words: list[dict[str, Any]]) -> Segment:
    return {"start": float(start), "end": float(end), "text": text, "words": words}


def _faster_whisper_from_params(p: dict[str, Any], **defaults: Any) -> FasterWhisperTranscriber:
    """Build the transcriber from ``--param`` values; ``defaults`` override
    the class defaults for one registry entry (HARNESS_POLICY per entry)."""
    allow = bool(defaults.pop("allow_library_names", True))
    d = {"model": DEFAULT_FW_MODEL, "device": "cpu", "compute_type": "int8", "language": None,
         "beam_size": 5, "vad_filter": False, "cpu_threads": None, "seed": DEFAULT_FW_SEED,
         "temperature": FW_LIBRARY_TEMPERATURES, **defaults}
    download = p.get(FW_DOWNLOAD_PARAM, False)
    if not isinstance(download, bool):
        raise ParamError(f"{FW_DOWNLOAD_PARAM} must be true or false, got {download!r}")
    beam = p.get("beam_size", d["beam_size"])
    if isinstance(beam, bool) or not isinstance(beam, int) or beam < 1:
        raise ParamError(f"beam_size must be a positive integer, got {beam!r}")
    threads = p.get("cpu_threads", d["cpu_threads"])
    if threads is not None and (isinstance(threads, bool) or not isinstance(threads, int) or threads < 0):
        raise ParamError(f"cpu_threads must be an integer >= 0, got {threads!r}")
    vad = p.get("vad_filter", d["vad_filter"])
    if not isinstance(vad, bool):
        raise ParamError(f"vad_filter must be true or false, got {vad!r}")
    seed = p.get("seed", d["seed"])
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32):
        raise ParamError(f"seed must be an integer in [0, 2**32) or null, got {seed!r}")
    temp = p.get("temperature", d["temperature"])
    temps = list(temp) if isinstance(temp, (list, tuple)) else [temp]
    if not temps or any(isinstance(t, bool) or not isinstance(t, (int, float)) or not np.isfinite(float(t)) or t < 0
                        for t in temps):
        raise ParamError(f"temperature must be a number >= 0 or a non-empty list of them, got {temp!r}")
    lang = p.get("language", d["language"])
    return FasterWhisperTranscriber(
        model=str(p.get("model", d["model"])),
        device=str(p.get("device", d["device"])),
        compute_type=str(p.get("compute_type", d["compute_type"])),
        language=None if lang is None else str(lang),
        beam_size=beam,
        vad_filter=vad,
        cpu_threads=threads,
        seed=seed,
        temperature=tuple(float(t) for t in temps) if isinstance(temp, (list, tuple)) else float(temps[0]),
        allow_library_names=allow,
        allow_download=download,
        cache_dir=_cache_dir_param(p.get("asr_cache")),
    )


def _cache_dir_param(value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ParamError(f"asr_cache must be a directory path, got {value!r}")
    path = Path(value).expanduser()
    if path.exists() and not path.is_dir():
        raise ParamError(f"asr_cache {value!r} exists and is not a directory")
    return path


class ModelComposedPipeline(ComposedPipeline):
    """ComposedPipeline that also records model provenance (the sha256 of the
    files actually loaded, ``model_store.require``/``describe_local``),
    component settings, the raw diarization turns, assignment counts and
    wall-clock timing in ``hyp.extra``, so a hypothesis says which weights
    made it and how fast.  Timing makes hyp.json differ between runs; the
    segments do not depend on it.  ``model_load_s`` (set by the factory)
    includes hashing the weights."""

    def __init__(
        self,
        transcriber: Transcriber | None,
        diarizer: Diarizer | None,
        config: PipelineConfig | None = None,
        name: str | None = None,
        load_s: float = 0.0,
    ) -> None:
        super().__init__(transcriber, diarizer, config, name=name)
        self.load_s = float(load_s)

    def run(self, audio_path: str | Path, meeting_dir: str | Path | None = None) -> Hypothesis:
        num = num_speakers_param(self.config.param("num_speakers"))
        audio = self.load(audio_path)
        meeting = self.find_meeting(audio_path, meeting_dir)
        meeting_id = str(meeting["meeting_id"]) if meeting is not None else Path(audio_path).stem
        problems = self.check_audio(audio, meeting)
        timing: dict[str, float] = {"model_load_s": self.load_s}
        turns: list[Turn] = []
        if self.diarizer is not None:
            t0 = time.perf_counter()
            turns = list(self.diarizer.diarize(audio.pcm, audio.sample_rate, num_speakers=num))
            timing["diarization_s"] = time.perf_counter() - t0
        stats: dict[str, Any] = {}
        if self.transcriber is None:
            segments = sort_segments([make_segment(spk, t0, t1, "") for t0, t1, spk in turns])
        else:
            t0 = time.perf_counter()
            asr = self.transcriber.transcribe(audio.pcm, audio.sample_rate)
            timing["asr_s"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            tie_break = tie_break_param(self.config.param("assignment_tie_break"))
            segments = assign_speakers(asr, turns, stats=stats, tie_break=tie_break)
            stats["tie_break"] = tie_break
            timing["assign_s"] = time.perf_counter() - t0
        timing["audio_s"] = audio.duration_s
        compute = sum(v for k, v in timing.items() if k.endswith("_s") and k not in ("audio_s", "model_load_s"))
        timing["rtf"] = compute / audio.duration_s if audio.duration_s > 0 else float("nan")
        extra: dict[str, Any] = {
            "audio": audio_provenance(audio, problems),
            "components": {
                "transcriber": getattr(self.transcriber, "name", None),
                "diarizer": getattr(self.diarizer, "name", None),
            },
            "models": {},
            "timing": timing,
        }
        if self.transcriber is not None:
            extra["models"]["asr"] = getattr(self.transcriber, "model_provenance", None)
            extra["asr"] = {"settings": getattr(self.transcriber, "settings", {}), **getattr(self.transcriber, "last_info", {})}
            extra["assignment"] = {"rule": ASSIGNMENT_RULE, **stats}
        if self.diarizer is not None:
            models = getattr(self.diarizer, "models", None)
            if isinstance(models, dict):
                extra["models"].update(models)
            extra["diarization"] = {
                "settings": getattr(self.diarizer, "settings", {}),
                **getattr(self.diarizer, "last_info", {}),
                "turns": [[t0, t1, spk] for t0, t1, spk in turns],
            }
        return Hypothesis(meeting_id=meeting_id, system=self.config.name or self.name, segments=segments, extra=extra).validate()


def _timed(build: Callable[[], Any]) -> tuple[Any, float]:
    t0 = time.perf_counter()
    obj = build()
    return obj, time.perf_counter() - t0


# --- pyannote.audio -----------------------------------------------------------

#: ``--param`` keys read by the pyannote diarizer factories.
PYANNOTE_PARAMS = frozenset({"diarization_model", "token_env", "device"})

#: HARNESS_POLICY default pipelines per pyannote.audio major version: 4.x's
#: README points at the open "community-1" pipeline, 3.x's at "3.1".
PYANNOTE_DEFAULT_MODELS = {3: "pyannote/speaker-diarization-3.1", 4: "pyannote/speaker-diarization-community-1"}


def default_pyannote_model() -> str:
    major = _major_version("pyannote.audio")
    if major is not None and major >= 4:
        return PYANNOTE_DEFAULT_MODELS[4]
    return PYANNOTE_DEFAULT_MODELS[3]


def pyannote_annotation(output: Any, *, exclusive: bool = False) -> Any:
    """The ``pyannote.core.Annotation`` inside a pipeline's output.

    pyannote.audio 3.x returns the Annotation itself.  4.x returns an output
    object whose ``.speaker_diarization`` (overlap-aware) and
    ``.exclusive_speaker_diarization`` (one speaker at a time -- what word
    attribution wants) are Annotations.  ``exclusive`` picks the latter
    when present.
    """
    if exclusive and getattr(output, "exclusive_speaker_diarization", None) is not None:
        return output.exclusive_speaker_diarization
    if getattr(output, "speaker_diarization", None) is not None:
        return output.speaker_diarization
    if hasattr(output, "itertracks"):
        return output
    raise PipelineUnavailable(
        f"pyannote pipeline returned {type(output).__name__}, which has neither .itertracks nor "
        ".speaker_diarization; unsupported pyannote.audio version"
    )


class PyannoteDiarizer(Diarizer):
    """pyannote.audio speaker-diarization pipeline.  UNTESTED here against a model.

    Documented API (pyannote.audio README):
      4.x: pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=HF_TOKEN)
           output = pipeline(file_or_{"waveform", "sample_rate"}, num_speakers=None | int)
           for turn, speaker in output.speaker_diarization: ...
      3.x: pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=HF_TOKEN)
           diarization = pipeline({"waveform": torch.Tensor[1, n], "sample_rate": sr}, num_speakers=...)
           for turn, _, speaker in diarization.itertracks(yield_label=True): ...
    ``from_pretrained`` is tried with ``token=`` first and ``use_auth_token=``
    on TypeError; the output is normalised by ``pyannote_annotation``.  The
    token is read from the env var named by ``token_env`` (default
    ``HF_TOKEN``); it is never stored in this repo.  Both default models are
    gated on Hugging Face (accept their terms first).
    """

    name = "pyannote-audio"

    def __init__(
        self,
        model: str | None = None,
        token_env: str = "HF_TOKEN",
        device: str | None = None,
        exclusive: bool = False,
    ) -> None:
        reason = _missing_any("pyannote.audio", "torch")
        if reason:
            raise PipelineUnavailable(reason)
        from pyannote.audio import Pipeline as PAPipeline  # type: ignore[import-not-found]

        model = model or default_pyannote_model()
        token = os.environ.get(token_env) or None
        try:
            self.pipeline = PAPipeline.from_pretrained(model, token=token)
        except TypeError:  # pyannote.audio < 4 names the kwarg use_auth_token
            self.pipeline = PAPipeline.from_pretrained(model, use_auth_token=token)
        if self.pipeline is None:
            raise PipelineUnavailable(f"pyannote could not load {model!r} (token in ${token_env}?)")
        if device:
            import torch  # type: ignore[import-not-found]

            self.pipeline.to(torch.device(device))
        self.model_name = model
        self.exclusive = exclusive

    def diarize(self, pcm: np.ndarray, sample_rate: int, num_speakers: int | None = None) -> list[Turn]:
        import torch  # type: ignore[import-not-found]

        waveform = torch.from_numpy(np.asarray(pcm, dtype=np.float32)).unsqueeze(0)
        kwargs: dict[str, Any] = {}
        if num_speakers is not None:
            kwargs["num_speakers"] = int(num_speakers)
        out = self.pipeline({"waveform": waveform, "sample_rate": int(sample_rate)}, **kwargs)
        ann = pyannote_annotation(out, exclusive=self.exclusive)
        turns: list[Turn] = []
        for turn, _, speaker in ann.itertracks(yield_label=True):
            turns.append((float(turn.start), float(turn.end), str(speaker)))
        return sorted(turns)


def _pyannote_from_params(p: dict[str, Any], *, exclusive: bool) -> PyannoteDiarizer:
    return PyannoteDiarizer(
        model=p.get("diarization_model"),
        token_env=str(p.get("token_env", "HF_TOKEN")),
        device=p.get("device"),
        exclusive=exclusive,
    )


# --- whisperx -----------------------------------------------------------------

#: ``--param`` keys read by the whisperx pipeline.
WHISPERX_PARAMS = frozenset({"device", "model", "compute_type", "batch_size", "token_env"})


def repair_word_times(
    words: list[dict[str, Any]], seg_start: float, seg_end: float, default_speaker: str | None
) -> tuple[list[dict[str, Any]], int]:
    """Give every word a time and a speaker; return (words, n_interpolated).

    whisperx's aligner leaves tokens with no characters in the alignment
    model's dictionary (typically numerals like ``2024``) without
    ``start``/``end`` (whisperx README, "Limitations").  Dropping them would
    charge the system a deletion the ASR did not make.  HARNESS_POLICY: a run
    of unaligned words is spread uniformly between the previous aligned
    word's end (or the segment start) and the next aligned word's start (or
    the segment end), and takes the previous aligned word's speaker (else the
    next's, else the segment's).
    """
    out = [dict(w) for w in words]
    timed = {i for i, w in enumerate(out) if "start" in w and "end" in w}
    n_interp = 0
    i = 0
    while i < len(out):
        if i in timed:
            i += 1
            continue
        j = i
        while j < len(out) and j not in timed:
            j += 1
        prev = out[i - 1] if i > 0 else None
        nxt = out[j] if j < len(out) else None
        t0 = float(prev["end"]) if prev is not None else float(seg_start)
        t1 = float(nxt["start"]) if nxt is not None else float(seg_end)
        t1 = max(t0, t1)
        step = (t1 - t0) / (j - i)
        spk = (prev or {}).get("speaker") or (nxt or {}).get("speaker") or default_speaker
        for k in range(i, j):
            out[k]["start"] = round(t0 + (k - i) * step, 6)
            out[k]["end"] = round(t0 + (k - i + 1) * step, 6)
            if spk is not None and not out[k].get("speaker"):
                out[k]["speaker"] = spk
            n_interp += 1
        i = j
    return out, n_interp


def whisperx_segments(result: dict[str, Any], fallback_speaker: str = "spk0") -> tuple[list[Segment], int]:
    """whisperx ``assign_word_speakers`` output -> contract segments; returns
    (segments, number of interpolated words)."""
    segments: list[Segment] = []
    n_interp = 0
    for s in result.get("segments", []):
        spk_default = str(s.get("speaker") or fallback_speaker)
        raw = [w for w in s.get("words", []) or [] if normalize_text(str(w.get("word", "")))]
        raw, n = repair_word_times(raw, float(s["start"]), float(s["end"]), spk_default)
        n_interp += n
        words = [
            {"w": normalize_text(str(w["word"])), "start": float(w["start"]), "end": float(w["end"]),
             "speaker": str(w.get("speaker") or spk_default)}
            for w in raw
        ]
        if not words:
            text = normalize_text(s.get("text", ""))
            if text:
                segments.append(make_segment(spk_default, s["start"], s["end"], text))
            continue
        runs: list[tuple[str, list[dict[str, Any]]]] = []
        for w in words:
            if runs and runs[-1][0] == w["speaker"]:
                runs[-1][1].append(w)
            else:
                runs.append((w["speaker"], [w]))
        for spk, ws in runs:
            clean = [{"w": w["w"], "start": w["start"], "end": w["end"]} for w in ws]
            segments.append(make_segment(spk, clean[0]["start"], clean[-1]["end"], " ".join(w["w"] for w in clean), clean))
    return sort_segments(segments), n_interp


class WhisperXPipeline(Pipeline):
    """whisperx end-to-end (ASR + alignment + diarization).  UNTESTED here against a model.

    Documented API (whisperx README, "Python usage"):
        model = whisperx.load_model("large-v2", device, compute_type="float16")
        result = model.transcribe(audio, batch_size=16)
        model_a, metadata = whisperx.load_align_model(language_code=result["language"], device=device)
        result = whisperx.align(result["segments"], model_a, metadata, audio, device,
                                return_char_alignments=False)
        diarize_model = whisperx.DiarizationPipeline(use_auth_token=HF_TOKEN, device=device)
          (>= 3.3: whisperx.diarize.DiarizationPipeline; the kwarg is tried as
          ``use_auth_token`` then ``token``)
        diarize_segments = diarize_model(audio, min_speakers=..., max_speakers=...)
        result = whisperx.assign_word_speakers(diarize_segments, result)
        result["segments"][i] -> {"start","end","text","speaker","words":[{"word","start","end","speaker"}]}
    ``audio`` is a float32 numpy array at 16 kHz (whisperx.load_audio's shape).
    Words the aligner could not time are kept (``repair_word_times``).
    """

    name = "whisperx"
    description = "whisperx ASR + alignment + pyannote diarization (UNTESTED here: no models)"

    def __init__(self, config: PipelineConfig | None = None) -> None:
        super().__init__(config)
        reason = _missing_any("whisperx", "torch")
        if reason:
            raise PipelineUnavailable(reason)
        import whisperx  # type: ignore[import-not-found]

        self.whisperx = whisperx
        p = self.config.params
        self.device = str(p.get("device", "cpu"))
        self.model_name = str(p.get("model", "base"))
        self.compute_type = str(p.get("compute_type", "int8"))
        self.batch_size = int(p.get("batch_size", 16))
        self.token = os.environ.get(str(p.get("token_env", "HF_TOKEN"))) or None
        self.model = whisperx.load_model(self.model_name, self.device, compute_type=self.compute_type)

    def _diarization_pipeline(self) -> Any:
        wx = self.whisperx
        cls = getattr(wx, "DiarizationPipeline", None)
        if cls is None:
            cls = importlib.import_module("whisperx.diarize").DiarizationPipeline
        try:
            return cls(use_auth_token=self.token, device=self.device)
        except TypeError:  # a release that follows pyannote.audio 4's token= rename
            return cls(token=self.token, device=self.device)

    def run(self, audio_path: str | Path, meeting_dir: str | Path | None = None) -> Hypothesis:
        wx = self.whisperx
        num = num_speakers_param(self.config.param("num_speakers"))
        audio = self.load(audio_path)
        if audio.sample_rate != 16000:
            raise PipelineUnavailable("whisperx expects 16 kHz input; load with sample_rate=16000")
        meeting = self.find_meeting(audio_path, meeting_dir)
        meeting_id = str(meeting["meeting_id"]) if meeting is not None else Path(audio_path).stem
        problems = self.check_audio(audio, meeting)
        pcm = np.asarray(audio.pcm, dtype=np.float32)
        result = self.model.transcribe(pcm, batch_size=self.batch_size)
        model_a, metadata = wx.load_align_model(language_code=result["language"], device=self.device)
        result = wx.align(result["segments"], model_a, metadata, pcm, self.device, return_char_alignments=False)
        diarize_model = self._diarization_pipeline()
        kwargs = {"min_speakers": num, "max_speakers": num} if num is not None else {}
        diarize_segments = diarize_model(pcm, **kwargs)
        result = wx.assign_word_speakers(diarize_segments, result)
        segments, n_interp = whisperx_segments(result)
        return Hypothesis(
            meeting_id=meeting_id,
            system=self.config.name or self.name,
            segments=segments,
            extra={
                "audio": audio_provenance(audio, problems),
                "components": {"whisperx_model": self.model_name},
                "whisperx": {"interpolated_words": n_interp},
            },
        ).validate()


# --- embedding-cluster: a pretrained speaker-embedding model + our clustering ----------

#: ``--param`` keys of the embedding-cluster diarizer: the energy VAD, the cell
#: and clustering keys that apply to any embedding, and the backend's own.
EMBEDDING_CLUSTER_PARAMS = frozenset(
    {f.name for f in fields(VadParams)}
    | ({f.name for f in fields(ClusterParams)} - {"f0_weight", "min_voiced_frames", "octave_fix_frac"})
    | {"embedding_model", "embedding_savedir", "device", "batch_windows"}
)

#: HARNESS_POLICY, UNCALIBRATED: the distance guard in COSINE distance for
#: neural embeddings.  Tune on a dev split once a model is installed.
EMBEDDING_DISTANCE_THRESHOLD = 0.5

Embedder = Callable[[list[np.ndarray], int], np.ndarray]


class EmbeddingClusterDiarizer(Diarizer):
    """Energy VAD + cells/windows + ``cluster_embeddings`` over the vectors of
    any speaker-embedding function ``embed(list_of_pcm_windows, sr) -> (n, d)``.

    This is the seam for an UNGATED pretrained speaker-embedding model, so
    diarization works without Hugging Face gating: it reuses exactly the VAD,
    cell/window layout, speaker-count estimate (cosine distance) and
    smoothing of ``energy-vad-cluster``.  Tested here with a stand-in
    embedder; no model has been run.
    """

    name = "embedding-cluster"

    def __init__(self, embed: Embedder, vad: VadParams | None = None, cluster: ClusterParams | None = None) -> None:
        self.embed = embed
        self.vad = vad or VadParams()
        self.cluster = cluster or ClusterParams(distance_threshold=EMBEDDING_DISTANCE_THRESHOLD, f0_weight=0.0)
        self.last_info: dict[str, Any] = {}

    def diarize(self, pcm: np.ndarray, sample_rate: int, num_speakers: int | None = None) -> list[Turn]:
        c = self.cluster
        vad_info: dict[str, Any] = {}
        mask, hop_s = energy_vad(pcm, sample_rate, self.vad, info=vad_info)
        regions = mask_to_regions(mask, hop_s)
        cells = chunk_regions(regions, c.chunk_s, c.min_chunk_s)
        windows = cell_windows(regions, cells, max(c.window_s, c.chunk_s))
        count: dict[str, Any] = {}
        labels = np.zeros(len(cells), dtype=int)
        if cells:
            x = np.asarray(pcm, dtype=np.float32)
            clips = [x[int(round(a * sample_rate)) : max(int(round(b * sample_rate)), int(round(a * sample_rate)) + 1)] for a, b in windows]
            emb = np.asarray(self.embed(clips, sample_rate), dtype=np.float64)
            if emb.shape[0] != len(clips):
                raise PipelineUnavailable(f"embedder returned {emb.shape[0]} vectors for {len(clips)} windows")
            labels = cluster_embeddings(emb, num_speakers=num_speakers, params=c, metric="cosine", info=count)
        raw: list[Turn] = [(a, b, f"c{int(l)}") for (a, b), l in zip(cells, labels)]
        turns = smooth_turns(raw, min_segment_s=c.min_segment_s, merge_gap_s=c.merge_gap_s)
        self.last_info = {"vad": vad_info, "count": count, "n_chunks": len(cells), "chunk_s": c.chunk_s}
        return _relabel_by_first_appearance(turns)

    @staticmethod
    def params_from(p: dict[str, Any]) -> tuple[VadParams, ClusterParams]:
        v = VadParams()
        c = ClusterParams(distance_threshold=EMBEDDING_DISTANCE_THRESHOLD, f0_weight=0.0)
        for obj in (v, c):
            for f in fields(obj):
                if f.name in p and f.name in EMBEDDING_CLUSTER_PARAMS:
                    setattr(obj, f.name, _coerce(f.name, getattr(obj, f.name), p[f.name]))
        return v, c


def speechbrain_ecapa_embedder(
    source: str = "speechbrain/spkrec-ecapa-voxceleb",
    savedir: str | None = None,
    device: str = "cpu",
    batch_windows: int = 32,
) -> Embedder:
    """SpeechBrain ECAPA-TDNN speaker embeddings.  UNTESTED here (no model).

    Documented API (SpeechBrain >= 1.0; ``speechbrain.pretrained`` before 1.0):
        from speechbrain.inference.speaker import EncoderClassifier
        classifier = EncoderClassifier.from_hparams(source=..., savedir=..., run_opts={"device": ...})
        emb = classifier.encode_batch(wavs, wav_lens)   # wavs [batch, time] @ 16 kHz -> [batch, 1, 192]
    ``wav_lens`` are relative lengths in (0, 1].  The default model is
    published ungated on Hugging Face (licence per its model card; check it
    before use).
    """
    reason = _missing_any("speechbrain", "torch")
    if reason:
        raise PipelineUnavailable(reason)
    import torch  # type: ignore[import-not-found]

    try:
        from speechbrain.inference.speaker import EncoderClassifier  # type: ignore[import-not-found]
    except ImportError:  # SpeechBrain < 1.0
        from speechbrain.pretrained import EncoderClassifier  # type: ignore[import-not-found]
    kw: dict[str, Any] = {"source": source, "run_opts": {"device": device}}
    if savedir:
        kw["savedir"] = savedir
    classifier = EncoderClassifier.from_hparams(**kw)

    def embed(clips: list[np.ndarray], sample_rate: int) -> np.ndarray:
        if sample_rate != 16000:
            raise PipelineUnavailable("the ECAPA embedder expects 16 kHz input")
        out: list[np.ndarray] = []
        for i in range(0, len(clips), max(1, batch_windows)):
            group = clips[i : i + batch_windows]
            longest = max(len(g) for g in group)
            batch = np.zeros((len(group), longest), dtype=np.float32)
            for j, g in enumerate(group):
                batch[j, : len(g)] = g
            lens = torch.tensor([len(g) / float(longest) for g in group])
            with torch.no_grad():
                e = classifier.encode_batch(torch.from_numpy(batch), lens)
            out.append(np.asarray(e.squeeze(1).cpu().numpy(), dtype=np.float64))
        return np.concatenate(out, axis=0)

    return embed


# --- registrations ------------------------------------------------------------


@register(
    "faster-whisper",
    description=(
        "faster-whisper ASR + energy-vad-cluster diarization (model-free diarizer; for real speech use "
        "whisper-sherpa); the default small.en needs `python -m pipeline fetch-models`, other names the "
        "HF cache or allow_download=true"
    ),
    availability=lambda: _missing("faster_whisper"),
    params=FASTER_WHISPER_PARAMS | {FW_DOWNLOAD_PARAM} | DIARIZER_PARAMS | COMMON_AUDIO_PARAMS | ASSIGNMENT_PARAMS,
)
def _make_faster_whisper(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="faster-whisper")
    p = cfg.params

    def build() -> tuple[FasterWhisperTranscriber, EnergyVadClusterDiarizer]:
        diarizer = EnergyVadClusterDiarizer.from_params(
            p, allow=FASTER_WHISPER_PARAMS | {FW_DOWNLOAD_PARAM} | COMMON_AUDIO_PARAMS
        )
        return _faster_whisper_from_params(p), diarizer

    (asr, diarizer), load_s = _timed(build)
    return ModelComposedPipeline(asr, diarizer, cfg, name="faster-whisper", load_s=load_s)


@register(
    "faster-whisper+pyannote",
    description="faster-whisper ASR + pyannote.audio diarization (UNTESTED here: no models)",
    availability=lambda: _missing_any("faster_whisper", "pyannote.audio", "torch"),
    params=FASTER_WHISPER_PARAMS | {FW_DOWNLOAD_PARAM} | PYANNOTE_PARAMS | COMMON_AUDIO_PARAMS | ASSIGNMENT_PARAMS,
)
def _make_faster_whisper_pyannote(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="faster-whisper+pyannote")
    p = cfg.params
    # words are attributed to speakers: use the exclusive (one-at-a-time) timeline
    (di, asr), load_s = _timed(lambda: (_pyannote_from_params(p, exclusive=True), _faster_whisper_from_params(p)))
    return ModelComposedPipeline(asr, di, cfg, name="faster-whisper+pyannote", load_s=load_s)


@register(
    "pyannote-audio",
    description="pyannote.audio diarization only, no ASR (UNTESTED here: no models)",
    availability=lambda: _missing_any("pyannote.audio", "torch"),
    params=PYANNOTE_PARAMS | COMMON_AUDIO_PARAMS,
)
def _make_pyannote(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="pyannote-audio")
    return ComposedPipeline(None, _pyannote_from_params(cfg.params, exclusive=False), cfg, name="pyannote-audio")


@register(
    "whisperx",
    description=WhisperXPipeline.description,
    availability=lambda: _missing_any("whisperx", "torch"),
    params=WHISPERX_PARAMS | COMMON_AUDIO_PARAMS,
)
def _make_whisperx(config: PipelineConfig | None = None) -> Pipeline:
    return WhisperXPipeline(config)


@register(
    "embedding-cluster",
    description=(
        "energy VAD + SpeechBrain ECAPA speaker embeddings (ungated model) + the model-free "
        "clustering; no ASR (first run against the real model 2026-09-28)"
    ),
    availability=lambda: _missing_any("speechbrain", "torch"),
    params=EMBEDDING_CLUSTER_PARAMS | COMMON_AUDIO_PARAMS,
)
def _make_embedding_cluster(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="embedding-cluster")
    p = cfg.params
    v, c = EmbeddingClusterDiarizer.params_from(p)
    batch = p.get("batch_windows", 32)
    if isinstance(batch, bool) or not isinstance(batch, int) or batch < 1:
        raise ParamError(f"batch_windows must be a positive integer, got {batch!r}")
    from . import model_store

    source = str(p.get("embedding_model", "speechbrain/spkrec-ecapa-voxceleb"))
    # HARNESS_POLICY: SpeechBrain's savedir defaults to <models dir>/speechbrain-ecapa
    # (git-ignored) so the model never lands in the working directory; its files
    # are hashed into hyp.extra.models (unpinned: SpeechBrain fetches them).
    savedir = str(p.get("embedding_savedir") or (model_store.models_dir() / "speechbrain-ecapa"))
    t0 = time.perf_counter()
    embed = speechbrain_ecapa_embedder(source=source, savedir=savedir, device=str(p.get("device", "cpu")),
                                       batch_windows=batch)
    d = EmbeddingClusterDiarizer(embed, v, c)
    files = model_store.describe_local(savedir) if Path(savedir).is_dir() else {"path": savedir, "pinned": False, "files": {}}
    d.models = {"embedding": {"source": source, **files}}
    d.settings = {"source": source, "device": str(p.get("device", "cpu")), "batch_windows": batch,
                  "vad": {f.name: getattr(v, f.name) for f in fields(v)},
                  "cluster": {f.name: getattr(c, f.name) for f in fields(c)}}
    return ModelComposedPipeline(None, d, cfg, name="embedding-cluster", load_s=time.perf_counter() - t0)


def reassign_hypothesis(doc: dict[str, Any], asr_cache: str | Path, tie_break: str, system: str | None = None) -> dict[str, Any]:
    """A composed ASR+diarization hypothesis re-assigned with another
    ``tie_break``, WITHOUT re-running any model: the transcript comes from
    the ``asr_cache`` entry the run recorded (``extra.asr.cache.key``) and the
    turns from ``extra.diarization.turns``, so the result is exactly what the
    pipeline would have produced with ``--param assignment_tie_break``
    (assign_speakers is deterministic).  scripts/run-v5.sh checks that
    re-assigning with a run's own tie-break reproduces its segments."""
    extra = doc.get("extra") or {}
    key = ((extra.get("asr") or {}).get("cache") or {}).get("key")
    if not key:
        raise PipelineError("hypothesis has no extra.asr.cache.key (run with --param asr_cache)")
    entry = json.loads((Path(asr_cache) / f"{key}.json").read_text())
    if entry.get("schema") != ASR_CACHE_SCHEMA or entry.get("key") != key:
        raise PipelineError(f"asr_cache entry {key} does not match")
    turns = [(float(a), float(b), str(s)) for a, b, s in (extra.get("diarization") or {}).get("turns") or []]
    tb = tie_break_param(tie_break)
    stats: dict[str, Any] = {}
    segments = assign_speakers(entry["segments"], turns, stats=stats, tie_break=tb)
    stats["tie_break"] = tb
    new_extra = copy.deepcopy(extra)
    new_extra["assignment"] = {"rule": ASSIGNMENT_RULE, **stats}
    new_extra["reassigned_from"] = {"system": doc.get("system"), "tie_break": ((extra.get("assignment") or {}).get("tie_break"))}
    return Hypothesis(meeting_id=doc["meeting_id"], system=system or f"{doc.get('system')}:{tb}", segments=segments,
                      extra=new_extra).validate().to_dict()
