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

Device facts encoded in this package (three, all about the codec):

  * The recorder's audio is 16 000 Hz Opus in 20 ms / 320-sample frames.
    BYTECODE_PROVEN: ``OggUtils.b = 16000`` and ``OggUtils.c = 320``
    (build/evidence/javap/ALL.txt:11496-11498; ``sipush 16000`` /
    ``sipush 320`` in ``OggUtils.a()`` at ALL.txt:11548-11550), corroborated
    by docs/protocol-ledger.md section 8 ("Codec parameters") and mirrored in
    emulator/plaudsim/audio.py:21-22.  This module uses the rate only, as the
    audio loader's default target rate; the frame size is exported as a
    constant and not otherwise used.
  * Opus at 32 kbps CBR, exactly 80 bytes per 20 ms frame per channel
    (SOURCE-DERIVED, docs/protocol-ledger.md:1171-1180).  Used only by
    ``pipeline.synthetic`` to shape its synthetic device/recording.ogg.

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
    """The audio file could not be decoded into PCM, or decoded damaged."""


class ParamError(PipelineError, ValueError):
    """A ``--param`` key the pipeline does not accept, or a bad value."""


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
    """Mono float32 PCM clipped to [-1, 1] plus provenance of how it got that way.

    ``gaps`` lists ``(start_s, end_s)`` spans where the container's timestamps
    jumped forward (lost or corrupted packets) and zeros were inserted so that
    later audio keeps its true time (PyAV path only).  ``warnings`` records
    anything else suspicious (e.g. decoded length != container duration).
    Both are empty for an intact file.  HARNESS_POLICY.
    """

    pcm: np.ndarray
    sample_rate: int
    path: Path
    container: str  # "wav" (soundfile) | "pyav" (Ogg/Opus and anything else)
    source_sample_rate: int  # the decoder's output rate (Opus decodes at 48 kHz)
    source_channels: int
    gaps: tuple[tuple[float, float], ...] = ()
    container_duration_s: float | None = None
    warnings: tuple[str, ...] = ()

    @property
    def duration_s(self) -> float:
        return float(len(self.pcm)) / float(self.sample_rate)

    @property
    def damaged(self) -> bool:
        return bool(self.gaps or self.warnings)


_SOUNDFILE_SUFFIXES = {".wav", ".flac", ".aiff", ".aif"}

#: HARNESS_POLICY: a forward jump in frame timestamps larger than this is a gap.
PTS_GAP_TOLERANCE_S = 0.001
#: HARNESS_POLICY: decoded length may differ from the container's declared
#: duration by this much (one 20 ms Opus frame) before it is recorded.
CONTAINER_DURATION_TOLERANCE_S = 0.020
#: HARNESS_POLICY: decoded audio may differ from meeting.json ``duration_s``
#: by this much before ``audio_check="strict"`` refuses it.
DEFAULT_DURATION_TOLERANCE_S = 0.05
GAP_POLICIES = ("zero_fill", "error")


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


@dataclass
class _PyavResult:
    data: np.ndarray
    rate: int
    channels: int
    gaps: list[tuple[float, float]]
    container_duration_s: float | None
    warnings: list[str]


def _load_with_pyav(path: Path, on_gap: str = "zero_fill") -> _PyavResult:
    """Decode any container PyAV understands (Ogg/Opus is the one we need).

    Frames are converted to packed float32 at the decoder's own rate and
    layout; the mono downmix and resampling happen in numpy/scipy afterwards
    so the wav and Ogg paths share exactly one resampler.

    Frame timestamps are checked (HARNESS_POLICY): libopus/PyAV decode past a
    damaged Ogg page without an error and simply skip the lost packets, so a
    naive concatenation would shift every later sample early.  A forward
    jump larger than PTS_GAP_TOLERANCE_S is either zero-filled
    (``on_gap="zero_fill"``, recorded in the result) or refused
    (``on_gap="error"``).  Errors raised while decoding become
    AudioFormatError, like errors raised while opening.
    """
    import av

    if on_gap not in GAP_POLICIES:
        raise ValueError(f"on_gap must be one of {GAP_POLICIES}, got {on_gap!r}")
    try:
        container = av.open(str(path))
    except Exception as exc:  # av.error.* hierarchy varies across versions
        raise AudioFormatError(f"{path}: PyAV could not open: {exc}") from exc
    gaps: list[tuple[float, float]] = []
    warnings: list[str] = []
    chunks: list[np.ndarray] = []
    rate = 0
    container_duration_s: float | None = None
    try:
        if not container.streams.audio:
            raise AudioFormatError(f"{path}: no audio stream")
        stream = container.streams.audio[0]
        channels = int(stream.codec_context.channels or 1)
        if stream.duration is not None and stream.time_base is not None:
            container_duration_s = float(stream.duration * stream.time_base)
        layout = "mono" if channels == 1 else ("stereo" if channels == 2 else None)
        resampler_kwargs: dict[str, Any] = {"format": "flt"}
        if layout is not None:
            resampler_kwargs["layout"] = layout
        resampler = av.AudioResampler(**resampler_kwargs)

        def emit(frames: Any) -> None:
            nonlocal rate
            for out in frames:
                rate = int(out.sample_rate)
                arr = out.to_ndarray()  # packed: shape (1, n*channels)
                chunks.append(arr.reshape(-1, channels) if channels > 1 else arr.reshape(-1, 1))

        expected: float | None = None  # where the next frame should start (s)
        for frame in container.decode(stream):
            t0 = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else None
            if t0 is not None and expected is not None and t0 - expected > PTS_GAP_TOLERANCE_S:
                gap = (round(expected, 6), round(t0, 6))
                if on_gap == "error":
                    raise AudioFormatError(
                        f"{path}: timestamps jump from {gap[0]:.3f} s to {gap[1]:.3f} s "
                        "(lost or corrupted packets)"
                    )
                gaps.append(gap)
                fill = int(round((t0 - expected) * int(frame.sample_rate)))
                chunks.append(np.zeros((fill, channels), dtype=np.float32))
            if t0 is not None:
                expected = t0 + frame.samples / float(frame.sample_rate)
            elif expected is not None:
                expected += frame.samples / float(frame.sample_rate)
            emit(resampler.resample(frame))
        emit(resampler.resample(None))
    except AudioFormatError:
        raise
    except Exception as exc:  # decode-time av.error.* (damaged packets, bad headers)
        raise AudioFormatError(f"{path}: PyAV could not decode: {type(exc).__name__}: {exc}") from exc
    finally:
        container.close()
    if not chunks or rate == 0:
        raise AudioFormatError(f"{path}: decoded zero audio frames")
    data = np.concatenate(chunks, axis=0)
    if container_duration_s is not None:
        decoded = len(data) / float(rate)
        if abs(decoded - container_duration_s) > CONTAINER_DURATION_TOLERANCE_S:
            warnings.append(
                f"decoded {decoded:.3f} s but the container declares {container_duration_s:.3f} s"
            )
    return _PyavResult(data, rate, channels, gaps, container_duration_s, warnings)


def load_audio(
    path: str | Path, target_sr: int = DEVICE_SAMPLE_RATE_HZ, *, on_gap: str = "zero_fill"
) -> LoadedAudio:
    """Load wav (soundfile) or Ogg/Opus and friends (PyAV) as mono float32.

    HARNESS_POLICY: routing is by suffix -- soundfile for .wav/.flac/.aiff,
    PyAV for everything else.  A wav that soundfile rejects is NOT retried
    with PyAV; a clear error is preferred to a silent format guess.  Every
    path ends clipped to [-1, 1] (a float wav can exceed full scale).
    ``on_gap`` is the PyAV timestamp-gap policy (see ``_load_with_pyav``).
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    gaps: list[tuple[float, float]] = []
    warnings: list[str] = []
    container_duration_s: float | None = None
    if p.suffix.lower() in _SOUNDFILE_SUFFIXES:
        try:
            data, rate, channels = _load_with_soundfile(p)
        except Exception as exc:
            raise AudioFormatError(f"{p}: soundfile could not read: {exc}") from exc
        container = "wav"
    else:
        r = _load_with_pyav(p, on_gap=on_gap)
        data, rate, channels = r.data, r.rate, r.channels
        gaps, warnings, container_duration_s = r.gaps, r.warnings, r.container_duration_s
        container = "pyav"
    mono = _to_mono(data)
    pcm = resample(mono, rate, int(target_sr))
    # Every buffer here was freshly decoded by us, so clip in place (no second
    # full-length copy); resample() only clips on its resampling branch.
    pcm = np.clip(pcm, -1.0, 1.0, out=pcm) if pcm.flags.writeable else np.clip(pcm, -1.0, 1.0)
    return LoadedAudio(
        pcm=pcm,
        sample_rate=int(target_sr),
        path=p,
        container=container,
        source_sample_rate=rate,
        source_channels=channels,
        gaps=tuple(gaps),
        container_duration_s=container_duration_s,
        warnings=tuple(warnings),
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
    def find_meeting(self, audio_path: str | Path, meeting_dir: str | Path | None) -> dict[str, Any] | None:
        """meeting.json for this audio (validated), or None when there is none.

        A meeting.json that exists but does not validate raises
        MeetingFormatError: silently falling back to the audio stem would let
        the evals layer join the hypothesis to the wrong reference.
        """
        from .meeting import find_meeting_json, read_meeting

        mj = find_meeting_json(audio_path, meeting_dir)
        return read_meeting(mj) if mj is not None else None

    def resolve_meeting_id(self, audio_path: str | Path, meeting_dir: str | Path | None) -> str:
        """HARNESS_POLICY: the meeting.json id when one is discoverable, else
        the audio file stem.  A malformed meeting.json is an error."""
        m = self.find_meeting(audio_path, meeting_dir)
        return str(m["meeting_id"]) if m is not None else Path(audio_path).stem

    def load(self, audio_path: str | Path) -> LoadedAudio:
        return load_audio(audio_path, self.config.sample_rate)

    def check_audio(self, audio: LoadedAudio, meeting: dict[str, Any] | None) -> list[str]:
        """Apply the ``audio_check`` policy; return the problems found.

        ``strict`` (default) raises AudioFormatError on timestamp gaps, a
        decoded length that disagrees with the container, or a decoded
        length that differs from meeting.json ``duration_s`` by more than
        ``duration_tolerance_s``.  ``warn`` returns them (the pipeline records
        them in ``hyp.extra["audio"]["problems"]``); ``off`` skips the check.
        """
        return check_audio(
            audio,
            meeting,
            policy=str(self.config.param("audio_check", "strict")),
            tolerance_s=float(self.config.param("duration_tolerance_s", DEFAULT_DURATION_TOLERANCE_S)),
        )


#: HARNESS_POLICY: what a pipeline does with damaged or mis-sized audio.
AUDIO_CHECKS = ("strict", "warn", "off")

#: Parameter keys every audio-loading pipeline accepts (HARNESS_POLICY).
COMMON_AUDIO_PARAMS = frozenset({"num_speakers", "audio_check", "duration_tolerance_s"})


def check_audio(
    audio: LoadedAudio,
    meeting: dict[str, Any] | None,
    *,
    policy: str = "strict",
    tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
) -> list[str]:
    """See ``Pipeline.check_audio``."""
    if policy not in AUDIO_CHECKS:
        raise ParamError(f"audio_check must be one of {AUDIO_CHECKS}, got {policy!r}")
    if policy == "off":
        return []
    problems = [f"timestamp gap {a:.3f}-{b:.3f} s zero-filled" for a, b in audio.gaps]
    problems += list(audio.warnings)
    if meeting is not None and "duration_s" in meeting:
        want = float(meeting["duration_s"])
        if abs(audio.duration_s - want) > tolerance_s:
            problems.append(
                f"decoded {audio.duration_s:.3f} s but meeting.json says duration_s={want:.3f} "
                f"(tolerance {tolerance_s:.3f} s)"
            )
    if problems and policy == "strict":
        raise AudioFormatError(
            f"{audio.path}: damaged or mis-sized audio: " + "; ".join(problems)
            + " (pass --param audio_check=warn to score it anyway)"
        )
    return problems


def num_speakers_param(value: Any) -> int | None:
    """Validate the ``num_speakers`` hint: None or a positive int (not a bool)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or int(value) < 1:
        raise ParamError(f"num_speakers must be a positive integer, got {value!r}")
    return int(value)


def validate_params(name: str, accepted: frozenset[str] | None, params: dict[str, Any]) -> None:
    """Reject unknown keys (when the pipeline declares its keys) and bad
    values of the common keys.  Raises ParamError."""
    if accepted is not None:
        unknown = sorted(set(params) - set(accepted))
        if unknown:
            raise ParamError(
                f"pipeline {name!r} does not accept parameter(s) {', '.join(unknown)}; "
                f"accepted: {', '.join(sorted(accepted)) or '(none)'}"
            )
    if "num_speakers" in params:
        num_speakers_param(params["num_speakers"])
    if "audio_check" in params and params["audio_check"] not in AUDIO_CHECKS:
        raise ParamError(f"audio_check must be one of {AUDIO_CHECKS}, got {params['audio_check']!r}")
    if "duration_tolerance_s" in params:
        v = params["duration_tolerance_s"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or float(v) < 0:
            raise ParamError(f"duration_tolerance_s must be a finite number >= 0, got {v!r}")


# --- composition: transcriber + diarizer -> hypothesis -----------------------


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


#: HARNESS_POLICY: the shortest hypothesis segment a word run may produce.
#: The evals contract needs ``end > start`` and ASR engines emit zero-length
#: words (faster-whisper: 4 of 198 words on a 60 s AMI clip), so a run whose
#: words span no time is widened to this length.
MIN_SEGMENT_S = 0.01


#: HARNESS_POLICY: overlap totals closer than this (seconds) are a tie.
TIE_TOLERANCE_S = 1e-9

#: The rule ``SpeakerIndex`` implements, recorded in ``hyp.extra["assignment"]``
#: so a score can be tied to the rule that produced it.  Its tie-break was
#: chosen without evidence (docs/pipeline.md §11.5 shows it can move cpWER).
ASSIGNMENT_RULE = (
    "max total overlap per speaker; ties to the speaker of the earliest-starting overlapping turn; "
    "no overlap -> nearest turn by gap (HARNESS_POLICY, 2026-09-25)"
)


class SpeakerIndex:
    """Diarization turns prepared for word lookup (HARNESS_POLICY rule).

    ``speaker_for(start, end)`` returns ``(speaker, how)``:

    * ``"overlap"``: the speaker whose turns overlap ``[start, end]`` for the
      largest TOTAL time (summed over all of that speaker's turns, so two
      turns of one speaker beat one longer-overlapping turn of another only
      when their sum is larger).  A tie (typically a word lying wholly inside
      two overlapping turns) goes to the tied speaker whose overlapping turn
      started earliest -- the one holding the floor.  This tie-break is a
      choice made WITHOUT evidence: on one 60 s AMI clip the alternative
      "tie to the speaker that appeared first in the meeting" gave a lower
      cpWER with num_speakers=4 and a slightly higher one without a hint
      (docs/pipeline.md §11.5); choosing between them needs a dev split.
    * ``"nearest"``: nothing overlaps with positive length (a word outside
      every turn, or a zero-length word): the speaker of the turn at the
      smallest gap ``max(0, t0 - end, start - t1)``; a zero-length word
      inside a turn has gap 0.  Ties go to the earliest turn.
    * ``"fallback"``: there are no turns.

    Vectorised over turns with numpy so an hour of words against an hour of
    turns stays well under a second.
    """

    def __init__(self, turns: Iterable[Turn], fallback: str = FALLBACK_SPEAKER) -> None:
        ts = sorted(((float(t0), float(t1), str(spk)) for t0, t1, spk in turns), key=lambda t: (t[0], t[1]))
        self.fallback = fallback
        self.speakers: list[str] = []
        index: dict[str, int] = {}
        for _, _, spk in ts:
            if spk not in index:
                index[spk] = len(self.speakers)
                self.speakers.append(spk)
        self.t0 = np.array([t[0] for t in ts], dtype=np.float64)
        self.t1 = np.array([t[1] for t in ts], dtype=np.float64)
        self.spk = np.array([index[t[2]] for t in ts], dtype=np.int64)

    def speaker_for(self, start: float, end: float) -> tuple[str, str]:
        if self.t0.size == 0:
            return self.fallback, "fallback"
        ov = np.minimum(end, self.t1) - np.maximum(start, self.t0)
        pos = ov > 0.0
        if pos.any():
            totals = np.bincount(self.spk[pos], weights=ov[pos], minlength=len(self.speakers))
            tied = totals >= totals.max() - TIE_TOLERANCE_S
            # turns are sorted by start: the first overlapping turn of a tied speaker
            first = int(np.flatnonzero(pos & tied[self.spk])[0])
            return self.speakers[int(self.spk[first])], "overlap"
        gap = np.maximum(0.0, np.maximum(self.t0 - end, start - self.t1))
        return self.speakers[int(self.spk[int(np.argmin(gap))])], "nearest"


def _split_word(w: Word) -> list[Word]:
    """A word whose text normalises to k tokens becomes k words sharing its
    interval equally (the evals layer's rule); pure punctuation vanishes."""
    toks = normalize_text(str(w["w"])).split()
    start, end = float(w["start"]), float(w["end"])
    end = max(start, end)
    if len(toks) <= 1:
        return [{"w": toks[0], "start": start, "end": end}] if toks else []
    step = (end - start) / len(toks)
    return [
        {"w": t, "start": start + i * step, "end": start + (i + 1) * step if i + 1 < len(toks) else end}
        for i, t in enumerate(toks)
    ]


def _run_segment(spk: str, words: list[Word]) -> Segment:
    ws = sorted(words, key=lambda w: (w["start"], w["end"]))
    start = min(w["start"] for w in ws)
    end = max(max(w["end"] for w in ws), start + MIN_SEGMENT_S)
    return make_segment(spk, start, end, " ".join(w["w"] for w in ws), ws)


def assign_speakers(
    asr_segments: list[Segment],
    turns: list[Turn],
    fallback: str = FALLBACK_SPEAKER,
    stats: dict[str, Any] | None = None,
) -> list[Segment]:
    """HARNESS_POLICY word-to-speaker rule (the idea whisperx documents):

    each ASR word takes the diarization speaker with the maximal total
    overlap (``SpeakerIndex``); a word that overlaps no turn takes the nearest
    turn's speaker; with no turns at all every word takes ``fallback``.
    Consecutive same-speaker words inside one ASR segment become one
    hypothesis segment spanning exactly its words (never shorter than
    ``MIN_SEGMENT_S``), with its words sorted by start.  An ASR segment with
    no word timings is assigned as a whole by the same rule.  ``stats``, when
    given, receives the counts ``{"words", "overlap", "nearest", "fallback",
    "segments_without_words"}``.
    """
    index = SpeakerIndex(turns, fallback)
    counts = {"words": 0, "overlap": 0, "nearest": 0, "fallback": 0, "segments_without_words": 0}
    out: list[Segment] = []
    for seg in asr_segments:
        words = [x for w in (seg.get("words") or []) for x in _split_word(w)]
        if not words:
            text = normalize_text(seg.get("text", ""))
            if not text:
                continue
            spk, _how = index.speaker_for(float(seg["start"]), float(seg["end"]))
            counts["segments_without_words"] += 1
            start = float(seg["start"])
            out.append(make_segment(spk, start, max(float(seg["end"]), start + MIN_SEGMENT_S), text))
            continue
        runs: list[tuple[str, list[Word]]] = []
        for w in words:
            spk, how = index.speaker_for(w["start"], w["end"])
            counts["words"] += 1
            counts[how] += 1
            if runs and runs[-1][0] == spk:
                runs[-1][1].append(w)
            else:
                runs.append((spk, [w]))
        out.extend(_run_segment(spk, ws) for spk, ws in runs)
    if stats is not None:
        stats.update(counts)
    return sort_segments(out)


def _best_speaker(start: float, end: float, turns: list[Turn], fallback: str) -> str:
    """One-off form of ``SpeakerIndex.speaker_for`` (kept for callers)."""
    return SpeakerIndex(turns, fallback).speaker_for(float(start), float(end))[0]


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
        num = num_speakers_param(self.config.param("num_speakers"))
        audio = self.load(audio_path)
        meeting = self.find_meeting(audio_path, meeting_dir)
        meeting_id = str(meeting["meeting_id"]) if meeting is not None else Path(audio_path).stem
        problems = self.check_audio(audio, meeting)
        turns: list[Turn] = []
        if self.diarizer is not None:
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
                "audio": audio_provenance(audio, problems),
                "components": {
                    "transcriber": getattr(self.transcriber, "name", None),
                    "diarizer": getattr(self.diarizer, "name", None),
                },
            },
        )
        return hyp.validate()


def audio_provenance(audio: LoadedAudio, problems: list[str] | None = None) -> dict[str, Any]:
    """The ``hyp.extra["audio"]`` block (HARNESS_POLICY field names)."""
    d: dict[str, Any] = {
        "path": str(audio.path),
        "container": audio.container,
        "source_sample_rate": audio.source_sample_rate,
        "source_channels": audio.source_channels,
        "sample_rate": audio.sample_rate,
        "duration_s": audio.duration_s,
    }
    if audio.container_duration_s is not None:
        d["container_duration_s"] = audio.container_duration_s
    if audio.gaps:
        d["gaps"] = [list(g) for g in audio.gaps]
    if problems:
        d["problems"] = list(problems)
    return d


# --- registry ----------------------------------------------------------------

PipelineFactory = Callable[[PipelineConfig | None], Pipeline]


@dataclass
class RegistryEntry:
    name: str
    factory: PipelineFactory
    description: str
    is_system_under_test: bool
    availability: Callable[[], str | None]  # None == available, else the reason
    params: frozenset[str] | None = None  # accepted ``--param`` keys; None = undeclared

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
    params: Iterable[str] | None = None,
) -> Callable[[PipelineFactory], PipelineFactory]:
    """Register a factory ``(config | None) -> Pipeline`` under ``name``.

    ``params`` is the set of ``config.params`` keys the pipeline reads;
    ``get_pipeline`` rejects any other key (a typo'd ``--param`` must not be
    silently ignored).  ``None`` leaves the pipeline's keys unchecked.
    """

    def deco(factory: PipelineFactory) -> PipelineFactory:
        if name in _REGISTRY:
            raise PipelineError(f"pipeline {name!r} already registered")
        _REGISTRY[name] = RegistryEntry(
            name, factory, description, is_system_under_test, availability,
            frozenset(params) if params is not None else None,
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
                "params": sorted(e.params) if e.params is not None else None,
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
    validate_params(name, e.params, cfg.params)
    try:
        return e.factory(cfg)
    except PipelineError:
        raise
    except (ValueError, TypeError) as exc:  # a param value the factory could not coerce
        raise ParamError(f"pipeline {name!r}: bad parameter value: {exc}") from exc


_BUILTINS_LOADED = False


def _ensure_builtin_registrations() -> None:
    """Import the implementation modules once so their @register runs."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from . import adapters, energy_vad, oracle, whisper_sherpa  # noqa: F401  (side effect: registration)
