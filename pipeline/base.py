"""Interfaces for the plaud-harness ASR/diarization layer (``pipeline/``).

This package is the *system under test* seam of the harness: Layer 2
(synthetic meetings) produces a meeting directory, a Pipeline turns its audio
into a Hypothesis, and Layer 3 (evals) scores the Hypothesis against the
meeting's ground truth.

Evidence classes used in this package follow docs/protocol-ledger.md
("Confidence vocabulary" and the R7 mapping):

  BYTECODE_PROVEN  a literal constant in the shipped AAR bytecode (javap)
  SDK_PROVEN       follows necessarily from shipped source
  HARNESS_POLICY   a choice this harness makes; NOT a claim about the device
  UNKNOWN          not recoverable from the available evidence

Exactly one device fact is encoded in this package, and only as the default
target sample rate of the audio loader:

  * The recorder's audio is 16 000 Hz Opus in 20 ms / 320-sample frames.
    BYTECODE_PROVEN: ``OggUtils.b = 16000`` and ``OggUtils.c = 320``
    (build/evidence/javap/ALL.txt:11496-11498; ``sipush 16000`` /
    ``sipush 320`` in ``OggUtils.a()`` at ALL.txt:11548-11550), corroborated
    by docs/protocol-ledger.md section 8 ("Codec parameters") and mirrored in
    emulator/plaudsim/audio.py:22-25.

Everything else here -- the hypothesis schema, the registry, the mono
downmix, the polyphase resampler, the word-to-speaker assignment rule of
ComposedPipeline, the fallback speaker label -- is HARNESS_POLICY.  The shape
of audio a *real* recorder emits over BLE remains UNKNOWN (R6-S3/R6-S4: no
authentic Plaud recording is available); the Ogg/Opus this loader accepts is
the standard container that the emulator serves from its synthetic fixtures
(tests/fixtures/r6s2_manifest.json, ``not_plaud_capture: true``).
"""

from __future__ import annotations

import abc
import copy
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

# --- schema strings (shared data contract, HARNESS_POLICY) -------------------
MEETING_SCHEMA = "plaud-harness/meeting/1"
HYPOTHESIS_SCHEMA = "plaud-harness/hypothesis/1"

#: BYTECODE_PROVEN: OggUtils.b = 16000 (build/evidence/javap/ALL.txt:11496).
DEVICE_SAMPLE_RATE_HZ = 16000
#: BYTECODE_PROVEN: OggUtils.c = 320 (build/evidence/javap/ALL.txt:11498).
DEVICE_FRAME_SAMPLES = 320

#: HARNESS_POLICY: the label a pipeline uses when it has no diarizer at all.
FALLBACK_SPEAKER = "spk0"

Segment = dict[str, Any]
Word = dict[str, Any]
Turn = tuple[float, float, str]


class PipelineError(RuntimeError):
    """Base class for errors raised by this package."""


class UnknownPipeline(PipelineError, KeyError):
    """The registry has no pipeline of that name."""


class PipelineUnavailable(PipelineError):
    """The pipeline is registered but its optional dependency is absent."""


class AudioFormatError(PipelineError):
    """The audio file could not be decoded into PCM."""


# --- configuration -----------------------------------------------------------


@dataclass
class PipelineConfig:
    """Everything a Pipeline needs beyond the audio itself.

    ``sample_rate`` is the rate audio is loaded at before it reaches the
    transcriber/diarizer.  HARNESS_POLICY: it defaults to the recorder's own
    rate (DEVICE_SAMPLE_RATE_HZ, BYTECODE_PROVEN) so a device-shaped Ogg/Opus
    file is not resampled twice.  ``seed`` is HARNESS_POLICY (only the
    perturbed oracle consumes it).  ``params`` is a free-form bag that each
    pipeline documents for itself.
    """

    name: str = ""
    seed: int = 0
    sample_rate: int = DEVICE_SAMPLE_RATE_HZ
    params: dict[str, Any] = field(default_factory=dict)

    def with_params(self, **kw: Any) -> "PipelineConfig":
        merged = dict(self.params)
        merged.update(kw)
        return PipelineConfig(self.name, self.seed, self.sample_rate, merged)

    def param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


# --- audio loading -----------------------------------------------------------


@dataclass(frozen=True)
class LoadedAudio:
    """Mono float32 PCM in [-1, 1] plus provenance of how it got that way."""

    pcm: np.ndarray
    sample_rate: int
    path: Path
    container: str  # "wav" (soundfile) | "pyav" (Ogg/Opus and anything else)
    source_sample_rate: int  # the decoder's output rate (Opus decodes at 48 kHz)
    source_channels: int

    @property
    def duration_s(self) -> float:
        return float(len(self.pcm)) / float(self.sample_rate)


_SOUNDFILE_SUFFIXES = {".wav", ".flac", ".aiff", ".aif"}


def _to_mono(x: np.ndarray) -> np.ndarray:
    """HARNESS_POLICY: channels are averaged.  Input shape (n, channels)."""
    if x.ndim == 1:
        return x.astype(np.float32, copy=False)
    if x.shape[1] == 1:
        return x[:, 0].astype(np.float32, copy=False)
    return x.mean(axis=1).astype(np.float32)


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Polyphase resampling (scipy.signal.resample_poly) -- HARNESS_POLICY.

    Exact-ratio (gcd-reduced) FIR polyphase; 48 kHz -> 16 kHz is a clean
    1:3 decimation, which is the path every Opus file takes because libopus
    always decodes at 48 kHz regardless of the OpusHead input rate.
    """
    if src_rate == dst_rate:
        return pcm.astype(np.float32, copy=False)
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"bad sample rates {src_rate}->{dst_rate}")
    from scipy.signal import resample_poly

    g = math.gcd(int(src_rate), int(dst_rate))
    up, down = int(dst_rate) // g, int(src_rate) // g
    out = resample_poly(pcm.astype(np.float64), up, down)
    return np.clip(out, -1.0, 1.0).astype(np.float32)


def _load_with_soundfile(path: Path) -> tuple[np.ndarray, int, int]:
    import soundfile as sf

    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    return data, int(rate), int(data.shape[1])


def _load_with_pyav(path: Path) -> tuple[np.ndarray, int, int]:
    """Decode any container PyAV understands (Ogg/Opus is the one we need).

    Frames are converted to packed float32 at the decoder's own rate and
    layout; the mono downmix and resampling happen in numpy/scipy afterwards
    so the wav and Ogg paths share exactly one resampler.
    """
    import av

    try:
        container = av.open(str(path))
    except Exception as exc:  # av.error.* hierarchy varies across versions
        raise AudioFormatError(f"{path}: PyAV could not open: {exc}") from exc
    try:
        if not container.streams.audio:
            raise AudioFormatError(f"{path}: no audio stream")
        stream = container.streams.audio[0]
        channels = int(stream.codec_context.channels or 1)
        layout = "mono" if channels == 1 else ("stereo" if channels == 2 else None)
        resampler_kwargs: dict[str, Any] = {"format": "flt"}
        if layout is not None:
            resampler_kwargs["layout"] = layout
        resampler = av.AudioResampler(**resampler_kwargs)
        chunks: list[np.ndarray] = []
        rate = 0
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                rate = int(out.sample_rate)
                arr = out.to_ndarray()  # packed: shape (1, n*channels)
                chunks.append(arr.reshape(-1, channels) if channels > 1 else arr.reshape(-1, 1))
        for out in resampler.resample(None):
            arr = out.to_ndarray()
            chunks.append(arr.reshape(-1, channels) if channels > 1 else arr.reshape(-1, 1))
    finally:
        container.close()
    if not chunks or rate == 0:
        raise AudioFormatError(f"{path}: decoded zero audio frames")
    return np.concatenate(chunks, axis=0), rate, channels


def load_audio(path: str | Path, target_sr: int = DEVICE_SAMPLE_RATE_HZ) -> LoadedAudio:
    """Load wav (soundfile) or Ogg/Opus and friends (PyAV) as mono float32.

    HARNESS_POLICY: routing is by suffix -- soundfile for .wav/.flac/.aiff,
    PyAV for everything else.  A wav that soundfile rejects is NOT retried
    with PyAV; a clear error is preferred to a silent format guess.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    if p.suffix.lower() in _SOUNDFILE_SUFFIXES:
        try:
            data, rate, channels = _load_with_soundfile(p)
        except Exception as exc:
            raise AudioFormatError(f"{p}: soundfile could not read: {exc}") from exc
        container = "wav"
    else:
        data, rate, channels = _load_with_pyav(p)
        container = "pyav"
    mono = _to_mono(data)
    pcm = resample(mono, rate, int(target_sr))
    return LoadedAudio(
        pcm=pcm,
        sample_rate=int(target_sr),
        path=p,
        container=container,
        source_sample_rate=rate,
        source_channels=channels,
    )


# --- hypothesis --------------------------------------------------------------


def _round(x: float) -> float:
    return float(x)


@dataclass
class Hypothesis:
    """The shared hypothesis contract (``plaud-harness/hypothesis/1``)."""

    meeting_id: str
    system: str
    segments: list[Segment] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema": HYPOTHESIS_SCHEMA,
            "meeting_id": self.meeting_id,
            "system": self.system,
            "segments": [copy.deepcopy(s) for s in self.segments],
        }
        if self.extra:
            d["extra"] = copy.deepcopy(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Hypothesis":
        if d.get("schema") != HYPOTHESIS_SCHEMA:
            raise PipelineError(f"not a {HYPOTHESIS_SCHEMA} document: {d.get('schema')!r}")
        return cls(
            meeting_id=str(d["meeting_id"]),
            system=str(d["system"]),
            segments=[dict(s) for s in d.get("segments", [])],
            extra=dict(d.get("extra", {})),
        )

    def validate(self) -> "Hypothesis":
        """Raise PipelineError on any contract violation; return self."""
        if not self.meeting_id:
            raise PipelineError("hypothesis has empty meeting_id")
        if not self.system:
            raise PipelineError("hypothesis has empty system")
        last_start = -math.inf
        for i, s in enumerate(self.segments):
            for k in ("speaker", "start", "end", "text"):
                if k not in s:
                    raise PipelineError(f"segment {i} lacks {k!r}")
            if not isinstance(s["speaker"], str) or not s["speaker"]:
                raise PipelineError(f"segment {i} speaker must be a non-empty string")
            start, end = float(s["start"]), float(s["end"])
            if not (math.isfinite(start) and math.isfinite(end)):
                raise PipelineError(f"segment {i} has non-finite times")
            if start < 0 or end < start:
                raise PipelineError(f"segment {i} has bad times {start}..{end}")
            if start < last_start:
                raise PipelineError(f"segment {i} is out of start order")
            last_start = start
            if not isinstance(s["text"], str):
                raise PipelineError(f"segment {i} text must be a string")
            for j, w in enumerate(s.get("words", []) or []):
                for k in ("w", "start", "end"):
                    if k not in w:
                        raise PipelineError(f"segment {i} word {j} lacks {k!r}")
                if float(w["end"]) < float(w["start"]):
                    raise PipelineError(f"segment {i} word {j} ends before it starts")
        return self

    @property
    def speakers(self) -> list[str]:
        seen: dict[str, None] = {}
        for s in self.segments:
            seen.setdefault(s["speaker"], None)
        return list(seen)

    def rttm_lines(self) -> list[str]:
        from .formats import segments_to_rttm_lines

        return segments_to_rttm_lines(self.meeting_id, self.segments)

    def stm_lines(self) -> list[str]:
        from .formats import segments_to_stm_lines

        return segments_to_stm_lines(self.meeting_id, self.segments)


def make_segment(
    speaker: str, start: float, end: float, text: str = "", words: Iterable[Word] | None = None
) -> Segment:
    seg: Segment = {"speaker": speaker, "start": float(start), "end": float(end), "text": text}
    if words is not None:
        seg["words"] = [dict(w) for w in words]
    return seg


def sort_segments(segments: list[Segment]) -> list[Segment]:
    """Contract: segments sorted by start (ties by end, then speaker)."""
    return sorted(segments, key=lambda s: (float(s["start"]), float(s["end"]), s["speaker"]))


# --- component interfaces ----------------------------------------------------


class Transcriber(abc.ABC):
    """ASR: PCM -> segments with words.

    ``transcribe`` receives mono float32 PCM at ``sample_rate`` and returns
    segments ``{"start", "end", "text", "words": [{"w", "start", "end"}]}``
    with NO speaker field; speaker attribution is the Pipeline's job.  Text
    is lowercase tokens separated by single spaces (see ``normalize_text``).
    """

    name: str = "transcriber"

    @abc.abstractmethod
    def transcribe(self, pcm: np.ndarray, sample_rate: int) -> list[Segment]: ...


class Diarizer(abc.ABC):
    """Speaker segmentation: PCM -> [(start, end, speaker), ...]."""

    name: str = "diarizer"

    @abc.abstractmethod
    def diarize(
        self, pcm: np.ndarray, sample_rate: int, num_speakers: int | None = None
    ) -> list[Turn]: ...


def normalize_text(text: str) -> str:
    """HARNESS_POLICY minimal normalisation for pipeline *output*.

    Lowercase, whitespace-split, strip surrounding ASCII punctuation from
    each token, drop empties.  The evals layer owns real normalisation; this
    only guarantees the contract's "lowercase tokens, single spaces".
    """
    toks = []
    for t in text.lower().split():
        t = t.strip(".,;:!?\"'()[]{}<>-")
        if t:
            toks.append(t)
    return " ".join(toks)


class Pipeline(abc.ABC):
    """audio path (+ optional meeting dir) -> Hypothesis.

    ``is_system_under_test`` is False for harness self-test pipelines (the
    oracle family), which read ground truth and must never be reported as a
    system's score.
    """

    name: str = "pipeline"
    description: str = ""
    is_system_under_test: bool = True
    requires_meeting_dir: bool = False

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig(name=self.name)
        if not self.config.name:
            self.config.name = self.name

    @abc.abstractmethod
    def run(self, audio_path: str | Path, meeting_dir: str | Path | None = None) -> Hypothesis: ...

    # helpers shared by implementations
    def resolve_meeting_id(self, audio_path: str | Path, meeting_dir: str | Path | None) -> str:
        """HARNESS_POLICY: the meeting.json id when one is discoverable, else
        the audio file stem."""
        from .meeting import find_meeting_json, read_meeting

        mj = find_meeting_json(audio_path, meeting_dir)
        if mj is not None:
            try:
                return str(read_meeting(mj)["meeting_id"])
            except Exception:
                pass
        return Path(audio_path).stem

    def load(self, audio_path: str | Path) -> LoadedAudio:
        return load_audio(audio_path, self.config.sample_rate)


# --- composition: transcriber + diarizer -> hypothesis -----------------------


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_speakers(
    asr_segments: list[Segment], turns: list[Turn], fallback: str = FALLBACK_SPEAKER
) -> list[Segment]:
    """HARNESS_POLICY word-to-speaker rule (the same idea whisperx documents):

    each word takes the speaker whose turn overlaps it most; a word that
    overlaps no turn takes the nearest turn's speaker; with no turns at all
    every word takes ``fallback``.  Consecutive same-speaker words inside one
    ASR segment become one hypothesis segment.
    """
    out: list[Segment] = []
    for seg in asr_segments:
        words = seg.get("words") or []
        if not words:
            # No word timing: assign the whole segment by overlap.
            spk = _best_speaker(float(seg["start"]), float(seg["end"]), turns, fallback)
            out.append(make_segment(spk, seg["start"], seg["end"], normalize_text(seg.get("text", ""))))
            continue
        runs: list[tuple[str, list[Word]]] = []
        for w in words:
            spk = _best_speaker(float(w["start"]), float(w["end"]), turns, fallback)
            if runs and runs[-1][0] == spk:
                runs[-1][1].append(w)
            else:
                runs.append((spk, [w]))
        for spk, ws in runs:
            toks = [normalize_text(str(w["w"])) for w in ws]
            ws_norm = [
                {"w": t, "start": float(w["start"]), "end": float(w["end"])}
                for t, w in zip(toks, ws)
                if t
            ]
            if not ws_norm:
                continue
            out.append(
                make_segment(
                    spk,
                    ws_norm[0]["start"],
                    ws_norm[-1]["end"],
                    " ".join(w["w"] for w in ws_norm),
                    ws_norm,
                )
            )
    return sort_segments(out)


def _best_speaker(start: float, end: float, turns: list[Turn], fallback: str) -> str:
    if not turns:
        return fallback
    best, best_ov = None, 0.0
    for t0, t1, spk in turns:
        ov = _overlap(start, end, t0, t1)
        if ov > best_ov:
            best, best_ov = spk, ov
    if best is not None:
        return best
    mid = 0.5 * (start + end)
    t0, t1, spk = min(turns, key=lambda t: min(abs(t[0] - mid), abs(t[1] - mid)))
    return spk


class ComposedPipeline(Pipeline):
    """A Pipeline made of an optional Transcriber and an optional Diarizer.

    * both present   -> words attributed to speakers by ``assign_speakers``
    * diarizer only  -> one empty-text segment per turn (DER/JER-only system)
    * transcriber only -> every segment labelled FALLBACK_SPEAKER
    """

    def __init__(
        self,
        transcriber: Transcriber | None,
        diarizer: Diarizer | None,
        config: PipelineConfig | None = None,
        name: str | None = None,
    ) -> None:
        if name:
            self.name = name
        super().__init__(config)
        if transcriber is None and diarizer is None:
            raise PipelineError("ComposedPipeline needs a transcriber or a diarizer")
        self.transcriber = transcriber
        self.diarizer = diarizer

    def run(self, audio_path: str | Path, meeting_dir: str | Path | None = None) -> Hypothesis:
        audio = self.load(audio_path)
        meeting_id = self.resolve_meeting_id(audio_path, meeting_dir)
        turns: list[Turn] = []
        if self.diarizer is not None:
            num = self.config.param("num_speakers")
            turns = list(self.diarizer.diarize(audio.pcm, audio.sample_rate, num_speakers=num))
        if self.transcriber is None:
            segments = sort_segments([make_segment(spk, t0, t1, "") for t0, t1, spk in turns])
        else:
            asr = self.transcriber.transcribe(audio.pcm, audio.sample_rate)
            segments = assign_speakers(asr, turns)
        hyp = Hypothesis(
            meeting_id=meeting_id,
            system=self.config.name or self.name,
            segments=segments,
            extra={
                "audio": {
                    "path": str(audio.path),
                    "container": audio.container,
                    "source_sample_rate": audio.source_sample_rate,
                    "source_channels": audio.source_channels,
                    "sample_rate": audio.sample_rate,
                    "duration_s": audio.duration_s,
                },
                "components": {
                    "transcriber": getattr(self.transcriber, "name", None),
                    "diarizer": getattr(self.diarizer, "name", None),
                },
            },
        )
        return hyp.validate()


# --- registry ----------------------------------------------------------------

PipelineFactory = Callable[[PipelineConfig | None], Pipeline]


@dataclass
class RegistryEntry:
    name: str
    factory: PipelineFactory
    description: str
    is_system_under_test: bool
    availability: Callable[[], str | None]  # None == available, else the reason

    def available(self) -> bool:
        return self.availability() is None

    def reason(self) -> str | None:
        return self.availability()


_REGISTRY: dict[str, RegistryEntry] = {}


def _always_available() -> str | None:
    return None


def register(
    name: str,
    *,
    description: str = "",
    is_system_under_test: bool = True,
    availability: Callable[[], str | None] = _always_available,
) -> Callable[[PipelineFactory], PipelineFactory]:
    """Register a factory ``(config | None) -> Pipeline`` under ``name``."""

    def deco(factory: PipelineFactory) -> PipelineFactory:
        if name in _REGISTRY:
            raise PipelineError(f"pipeline {name!r} already registered")
        _REGISTRY[name] = RegistryEntry(
            name, factory, description, is_system_under_test, availability
        )
        return factory

    return deco


def registered() -> dict[str, RegistryEntry]:
    _ensure_builtin_registrations()
    return dict(_REGISTRY)


def describe_registry() -> list[dict[str, Any]]:
    rows = []
    for name, e in sorted(registered().items()):
        reason = e.reason()
        rows.append(
            {
                "name": name,
                "available": reason is None,
                "reason": reason,
                "system_under_test": e.is_system_under_test,
                "description": e.description,
            }
        )
    return rows


def get_pipeline(name: str, config: PipelineConfig | None = None) -> Pipeline:
    entries = registered()
    if name not in entries:
        raise UnknownPipeline(f"unknown pipeline {name!r}; known: {sorted(entries)}")
    e = entries[name]
    reason = e.reason()
    if reason is not None:
        raise PipelineUnavailable(f"pipeline {name!r} is unavailable: {reason}")
    cfg = config or PipelineConfig(name=name)
    if not cfg.name:
        cfg.name = name
    return e.factory(cfg)


_BUILTINS_LOADED = False


def _ensure_builtin_registrations() -> None:
    """Import the implementation modules once so their @register runs."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from . import adapters, energy_vad, oracle  # noqa: F401  (side effect: registration)
