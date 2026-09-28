"""``whisper-sherpa``: faster-whisper ASR + sherpa-onnx speaker diarization, on CPU.

Both components run open, ungated models pinned in pipeline/model_store.py
(sources, sha256 and licences in docs/pipeline.md §11):

  ASR          faster-whisper 1.2.1, Whisper small.en (CTranslate2), int8 on CPU,
               word timestamps on
  diarization  sherpa-onnx 1.13.8 OfflineSpeakerDiarization: pyannote
               segmentation-3.0 (ONNX) + 3D-Speaker CAM++ English speaker
               embeddings (ONNX) + sherpa's agglomerative "fast clustering"

Every ASR word goes to the diarization speaker with the maximal total
overlap (``base.assign_speakers`` / ``base.SpeakerIndex``, HARNESS_POLICY),
so the hypothesis carries per-word timings and speakers; hyp.rttm is the
word-run segments and ``hyp.extra["diarization"]["turns"]`` keeps the raw
diarization turns for diarization-only scoring.  ``num_speakers`` (the
common ``--param``) is passed as sherpa's cluster count, but sherpa can
return FEWER speakers than asked (it lowers the count silently when it
cannot form that many clusters); ``hyp.extra["diarization"]`` records
``n_speakers`` and ``hint_honoured`` and a RuntimeWarning is issued when the
two differ.  Without a hint the count comes from ``cluster_threshold``.

Every default below is HARNESS_POLICY.  ``cluster_threshold`` = 0.5 is the
sherpa-onnx default and is UNCALIBRATED for this embedding model; on the one
60 s AMI clip timed here it over-counts (§11).  Nothing here downloads:
missing weights make the registry entry unavailable with the fetch command
in the reason, and the constructor hashes every weight file it loads
(``model_store.require``), refusing any that differ from the pin.
"""

from __future__ import annotations

import time
import warnings
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from . import model_store
from .adapters import (  # ModelComposedPipeline is re-exported here (it lived here until 2026-09-25)
    FASTER_WHISPER_PARAMS,
    FasterWhisperTranscriber,
    ModelComposedPipeline,
    _faster_whisper_from_params,
    _missing,
    _missing_any,
    _timed,
)
from .base import (
    ASSIGNMENT_PARAMS,
    COMMON_AUDIO_PARAMS,
    Diarizer,
    ParamError,
    Pipeline,
    PipelineConfig,
    PipelineUnavailable,
    Turn,
    register,
)

__all__ = ["ModelComposedPipeline", "SherpaOnnxDiarizer", "sherpa_turns"]

SEGMENTATION_ASSET = "pyannote-segmentation-3.0-onnx"
EMBEDDING_ASSET = "3dspeaker-campplus-en-voxceleb"
ASR_ASSET = "faster-whisper-small.en"

#: ``--param`` keys of the sherpa-onnx diarizer.
SHERPA_PARAMS = frozenset(
    {"segmentation_model", "embedding_model", "cluster_threshold", "min_duration_on", "min_duration_off", "diar_threads"}
)

#: HARNESS_POLICY defaults.  cluster_threshold, min_duration_on/off are the
#: sherpa-onnx defaults (OfflineSpeakerDiarizationConfig / FastClusteringConfig
#: in 1.13.8); 4 threads = the M1's performance cores.
SHERPA_DEFAULTS: dict[str, Any] = {
    "cluster_threshold": 0.5,
    "min_duration_on": 0.3,
    "min_duration_off": 0.5,
    "diar_threads": 4,
}

#: HARNESS_POLICY defaults of the whisper-sherpa transcriber (the library's
#: own defaults except the model, English, and an explicit thread count).
WHISPER_SHERPA_ASR_DEFAULTS: dict[str, Any] = {
    "model": "small.en",
    "compute_type": "int8",
    "language": "en",
    "beam_size": 5,
    "vad_filter": False,
    "cpu_threads": 4,
}


def sherpa_turns(segments: Iterable[Any], duration_s: float | None = None) -> list[Turn]:
    """sherpa-onnx ``OfflineSpeakerDiarizationSegment`` objects (``.start
    .end .speaker``) -> contract turns (HARNESS_POLICY):

    * times clipped to ``[0, duration_s]``: sherpa pads the last 10 s
      segmentation window, so on short input a turn can end after the audio
      (0.031-0.639 s on a 0.5 s clip); turns empty after clipping are dropped;
    * sorted by time; integer speaker ids renamed ``spk0, spk1, ...`` by
      first appearance among the kept turns (the energy-vad-cluster naming);
    * overlapping turns are kept (the segmentation model is overlap-aware).
    """
    hi = float("inf") if duration_s is None else max(0.0, float(duration_s))
    clipped = ((max(0.0, float(s.start)), min(hi, float(s.end)), int(s.speaker)) for s in segments)
    raw = sorted(((t0, t1, spk) for t0, t1, spk in clipped if t1 > t0), key=lambda t: (t[0], t[1], t[2]))
    names: dict[int, str] = {}
    out: list[Turn] = []
    for t0, t1, spk in raw:
        if spk not in names:
            names[spk] = f"spk{len(names)}"
        out.append((t0, t1, names[spk]))
    return out


def _positive_float(name: str, value: Any, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(float(value)):
        raise ParamError(f"{name} must be a finite number, got {value!r}")
    v = float(value)
    if v < 0 or (v == 0 and not allow_zero):
        raise ParamError(f"{name} must be {'>= 0' if allow_zero else '> 0'}, got {value!r}")
    return v


def _model_file(param: Any, asset_key: str) -> tuple[Path, dict[str, Any]]:
    """An explicit model path (hashed, recorded as unpinned), or the pinned
    asset, which must be fetched and match its sha256 (``model_store.require``)."""
    if param is not None:
        p = Path(str(param)).expanduser()
        if not p.is_file():
            raise PipelineUnavailable(f"model file not found: {p}")
        return p, model_store.describe_local(p)
    return model_store.require(asset_key)


class SherpaOnnxDiarizer(Diarizer):
    """sherpa-onnx offline speaker diarization.

    API checked against the installed sherpa-onnx 1.13.8:
        config = OfflineSpeakerDiarizationConfig(
            segmentation=OfflineSpeakerSegmentationModelConfig(
                pyannote=OfflineSpeakerSegmentationPyannoteModelConfig(model=...), num_threads=..),
            embedding=SpeakerEmbeddingExtractorConfig(model=..., num_threads=..),
            clustering=FastClusteringConfig(num_clusters=-1 | k, threshold=..),
            min_duration_on=0.3, min_duration_off=0.5)
        sd = OfflineSpeakerDiarization(config); sd.sample_rate == 16000
        sd.set_config(config)          # used to change the cluster count per call
        sd.process(float32_samples).sort_by_start_time() -> [.start .end .speaker]
    """

    name = "sherpa-onnx"

    def __init__(
        self,
        segmentation_model: str | Path | None = None,
        embedding_model: str | Path | None = None,
        *,
        cluster_threshold: float = SHERPA_DEFAULTS["cluster_threshold"],
        min_duration_on: float = SHERPA_DEFAULTS["min_duration_on"],
        min_duration_off: float = SHERPA_DEFAULTS["min_duration_off"],
        num_threads: int = SHERPA_DEFAULTS["diar_threads"],
    ) -> None:
        reason = _missing("sherpa_onnx")
        if reason:
            raise PipelineUnavailable(reason)
        import sherpa_onnx  # type: ignore[import-not-found]

        self._so = sherpa_onnx
        self.segmentation_path, seg_prov = _model_file(segmentation_model, SEGMENTATION_ASSET)
        self.embedding_path, emb_prov = _model_file(embedding_model, EMBEDDING_ASSET)
        self.models = {"segmentation": seg_prov, "embedding": emb_prov}
        self.settings = {
            "cluster_threshold": float(cluster_threshold),
            "min_duration_on": float(min_duration_on),
            "min_duration_off": float(min_duration_off),
            "num_threads": int(num_threads),
        }
        config = self._config(-1)
        if not config.validate():
            raise PipelineUnavailable(
                f"sherpa-onnx rejected the diarization config (segmentation={self.segmentation_path}, "
                f"embedding={self.embedding_path})"
            )
        self.sd = sherpa_onnx.OfflineSpeakerDiarization(config)
        self._num_clusters = -1
        self.last_info: dict[str, Any] = {}

    def _config(self, num_clusters: int) -> Any:
        so = self._so
        st = self.settings
        return so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(self.segmentation_path)),
                num_threads=st["num_threads"],
            ),
            embedding=so.SpeakerEmbeddingExtractorConfig(model=str(self.embedding_path), num_threads=st["num_threads"]),
            clustering=so.FastClusteringConfig(num_clusters=int(num_clusters), threshold=st["cluster_threshold"]),
            min_duration_on=st["min_duration_on"],
            min_duration_off=st["min_duration_off"],
        )

    def diarize(self, pcm: np.ndarray, sample_rate: int, num_speakers: int | None = None) -> list[Turn]:
        want = int(self.sd.sample_rate)
        if int(sample_rate) != want:
            raise PipelineUnavailable(f"sherpa-onnx diarization expects {want} Hz input, got {sample_rate}")
        k = int(num_speakers) if num_speakers is not None else -1
        if k != self._num_clusters:
            self.sd.set_config(self._config(k))  # only the clustering part is taken from it
            self._num_clusters = k
        x = np.ascontiguousarray(pcm, dtype=np.float32)
        turns = [] if x.size == 0 else sherpa_turns(self.sd.process(x).sort_by_start_time(), x.size / float(want))
        n = len({t[2] for t in turns})
        honoured = None if num_speakers is None else n == int(num_speakers)
        self.last_info = {
            "num_speakers_hint": num_speakers,
            "n_speakers": n,
            "hint_honoured": honoured,
            "n_turns": len(turns),
        }
        if honoured is False:
            warnings.warn(
                f"sherpa-onnx returned {n} speaker(s) for num_speakers={num_speakers} "
                f"({x.size / float(want):.2f} s of audio); the hint was not honoured",
                RuntimeWarning,
                stacklevel=2,
            )
        return turns


def _sherpa_from_params(p: dict[str, Any]) -> SherpaOnnxDiarizer:
    d = SHERPA_DEFAULTS
    threads = p.get("diar_threads", d["diar_threads"])
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise ParamError(f"diar_threads must be a positive integer, got {threads!r}")
    return SherpaOnnxDiarizer(
        p.get("segmentation_model"),
        p.get("embedding_model"),
        cluster_threshold=_positive_float("cluster_threshold", p.get("cluster_threshold", d["cluster_threshold"])),
        min_duration_on=_positive_float("min_duration_on", p.get("min_duration_on", d["min_duration_on"]), allow_zero=True),
        min_duration_off=_positive_float("min_duration_off", p.get("min_duration_off", d["min_duration_off"]), allow_zero=True),
        num_threads=threads,
    )


def _whisper_sherpa_availability() -> str | None:
    return _missing_any("faster_whisper", "sherpa_onnx") or next(
        (r for r in (model_store.missing_reason(k) for k in (ASR_ASSET, SEGMENTATION_ASSET, EMBEDDING_ASSET)) if r), None
    )


def _sherpa_availability() -> str | None:
    return _missing("sherpa_onnx") or next(
        (r for r in (model_store.missing_reason(k) for k in (SEGMENTATION_ASSET, EMBEDDING_ASSET)) if r), None
    )


@register(
    "whisper-sherpa",
    description=(
        "faster-whisper small.en (int8, word timestamps) + sherpa-onnx diarization (pyannote "
        "segmentation-3.0 + 3D-Speaker CAM++ embeddings); words take the max-overlap speaker"
    ),
    availability=_whisper_sherpa_availability,
    params=FASTER_WHISPER_PARAMS | SHERPA_PARAMS | COMMON_AUDIO_PARAMS | ASSIGNMENT_PARAMS,
)
def _make_whisper_sherpa(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="whisper-sherpa")
    p = cfg.params
    t0 = time.perf_counter()
    diarizer = _sherpa_from_params(p)
    asr: FasterWhisperTranscriber = _faster_whisper_from_params(p, allow_library_names=False, **WHISPER_SHERPA_ASR_DEFAULTS)
    return ModelComposedPipeline(asr, diarizer, cfg, name="whisper-sherpa", load_s=time.perf_counter() - t0)


@register(
    "sherpa-onnx-diarization",
    description="sherpa-onnx diarization only (pyannote segmentation-3.0 + 3D-Speaker CAM++); no ASR (DER/JER only)",
    availability=_sherpa_availability,
    params=SHERPA_PARAMS | COMMON_AUDIO_PARAMS,
)
def _make_sherpa_diarization(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="sherpa-onnx-diarization")
    diarizer, load_s = _timed(lambda: _sherpa_from_params(cfg.params))
    return ModelComposedPipeline(None, diarizer, cfg, name="sherpa-onnx-diarization", load_s=load_s)
