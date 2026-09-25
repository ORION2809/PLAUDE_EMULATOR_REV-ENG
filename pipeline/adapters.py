"""Optional adapters for real ASR/diarization systems -- UNTESTED HERE.

Status (docs/pipeline.md): no model weights are present in this
environment and none may be downloaded, so nothing in this module has been
executed against a model.  Each adapter is written against the package's
*documented public API* (cited per class), imports lazily, and the registry
reports it as unavailable with a reason instead of failing at import time.
When the package IS present, ``get_pipeline(name)`` will construct the
adapter; the first ``run`` then needs the model files and (for pyannote)
a Hugging Face token that the adapter reads from an environment variable
you choose -- it never embeds one.

All parameter defaults below are HARNESS_POLICY.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import numpy as np

from .base import (
    ComposedPipeline,
    Diarizer,
    Hypothesis,
    Pipeline,
    PipelineConfig,
    PipelineUnavailable,
    Segment,
    Transcriber,
    Turn,
    assign_speakers,
    make_segment,
    normalize_text,
    register,
    sort_segments,
)
from .energy_vad import EnergyVadClusterDiarizer


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


# --- faster-whisper -----------------------------------------------------------


class FasterWhisperTranscriber(Transcriber):
    """faster-whisper (SYSTRAN) transcriber.  UNTESTED here.

    Documented API (faster-whisper README, "Usage"):
        model = WhisperModel(model_size_or_path, device="cpu", compute_type="int8")
        segments, info = model.transcribe(audio, beam_size=5, word_timestamps=True,
                                          language=None, vad_filter=False)
    ``audio`` may be a float32 numpy array at 16 kHz.  Each segment has
    ``.start .end .text`` and ``.words`` -> ``Word(start, end, word, probability)``.
    """

    name = "faster-whisper"

    def __init__(
        self,
        model: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        beam_size: int = 5,
        vad_filter: bool = False,
    ) -> None:
        reason = _missing("faster_whisper")
        if reason:
            raise PipelineUnavailable(reason)
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter

    def transcribe(self, pcm: np.ndarray, sample_rate: int) -> list[Segment]:
        if sample_rate != 16000:
            raise PipelineUnavailable("faster-whisper expects 16 kHz input; load with sample_rate=16000")
        segments, _info = self.model.transcribe(
            np.asarray(pcm, dtype=np.float32),
            beam_size=self.beam_size,
            word_timestamps=True,
            language=self.language,
            vad_filter=self.vad_filter,
        )
        out: list[Segment] = []
        for s in segments:
            words = [
                {"w": normalize_text(w.word), "start": float(w.start), "end": float(w.end)}
                for w in (s.words or [])
                if normalize_text(w.word)
            ]
            text = " ".join(w["w"] for w in words) if words else normalize_text(s.text)
            out.append(make_segment_no_speaker(float(s.start), float(s.end), text, words))
        return out


def make_segment_no_speaker(start: float, end: float, text: str, words: list[dict[str, Any]]) -> Segment:
    return {"start": float(start), "end": float(end), "text": text, "words": words}


# --- pyannote.audio -----------------------------------------------------------


class PyannoteDiarizer(Diarizer):
    """pyannote.audio speaker-diarization pipeline.  UNTESTED here.

    Documented API (pyannote.audio README / pipeline docs, 3.x):
        from pyannote.audio import Pipeline
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1",
                                            use_auth_token=HF_TOKEN)
        diarization = pipeline({"waveform": torch.Tensor[1, n], "sample_rate": sr},
                               num_speakers=None | int)
        for turn, _, speaker in diarization.itertracks(yield_label=True): ...
    The token is read from the env var named by ``token_env`` (default
    ``HF_TOKEN``); it is never stored in this repo.
    """

    name = "pyannote-audio"

    def __init__(
        self,
        model: str = "pyannote/speaker-diarization-3.1",
        token_env: str = "HF_TOKEN",
        device: str | None = None,
    ) -> None:
        reason = _missing_any("pyannote.audio", "torch")
        if reason:
            raise PipelineUnavailable(reason)
        from pyannote.audio import Pipeline as PAPipeline  # type: ignore[import-not-found]

        token = os.environ.get(token_env) or None
        try:
            self.pipeline = PAPipeline.from_pretrained(model, use_auth_token=token)
        except TypeError:  # newer releases renamed the kwarg
            self.pipeline = PAPipeline.from_pretrained(model, token=token)
        if self.pipeline is None:
            raise PipelineUnavailable(f"pyannote could not load {model!r} (token in ${token_env}?)")
        if device:
            import torch  # type: ignore[import-not-found]

            self.pipeline.to(torch.device(device))

    def diarize(self, pcm: np.ndarray, sample_rate: int, num_speakers: int | None = None) -> list[Turn]:
        import torch  # type: ignore[import-not-found]

        waveform = torch.from_numpy(np.asarray(pcm, dtype=np.float32)).unsqueeze(0)
        kwargs: dict[str, Any] = {}
        if num_speakers is not None:
            kwargs["num_speakers"] = int(num_speakers)
        ann = self.pipeline({"waveform": waveform, "sample_rate": int(sample_rate)}, **kwargs)
        turns: list[Turn] = []
        for turn, _, speaker in ann.itertracks(yield_label=True):
            turns.append((float(turn.start), float(turn.end), str(speaker)))
        return sorted(turns)


# --- whisperx -----------------------------------------------------------------


class WhisperXPipeline(Pipeline):
    """whisperx end-to-end (ASR + alignment + diarization).  UNTESTED here.

    Documented API (whisperx README, "Python usage"):
        model = whisperx.load_model("large-v2", device, compute_type="float16")
        result = model.transcribe(audio, batch_size=16)
        model_a, metadata = whisperx.load_align_model(language_code=result["language"], device=device)
        result = whisperx.align(result["segments"], model_a, metadata, audio, device,
                                return_char_alignments=False)
        diarize_model = whisperx.DiarizationPipeline(use_auth_token=HF_TOKEN, device=device)
          (>= 3.3: whisperx.diarize.DiarizationPipeline)
        diarize_segments = diarize_model(audio, min_speakers=..., max_speakers=...)
        result = whisperx.assign_word_speakers(diarize_segments, result)
        result["segments"][i] -> {"start","end","text","speaker","words":[{"word","start","end","speaker"}]}
    ``audio`` is a float32 numpy array at 16 kHz (whisperx.load_audio's shape).
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

    def run(self, audio_path: str | Path, meeting_dir: str | Path | None = None) -> Hypothesis:
        wx = self.whisperx
        audio = self.load(audio_path)
        if audio.sample_rate != 16000:
            raise PipelineUnavailable("whisperx expects 16 kHz input; load with sample_rate=16000")
        meeting_id = self.resolve_meeting_id(audio_path, meeting_dir)
        pcm = np.asarray(audio.pcm, dtype=np.float32)
        result = self.model.transcribe(pcm, batch_size=self.batch_size)
        model_a, metadata = wx.load_align_model(language_code=result["language"], device=self.device)
        result = wx.align(result["segments"], model_a, metadata, pcm, self.device, return_char_alignments=False)
        diarize_cls = getattr(wx, "DiarizationPipeline", None)
        if diarize_cls is None:
            diarize_cls = importlib.import_module("whisperx.diarize").DiarizationPipeline
        diarize_model = diarize_cls(use_auth_token=self.token, device=self.device)
        num = self.config.param("num_speakers")
        kwargs = {"min_speakers": num, "max_speakers": num} if num is not None else {}
        diarize_segments = diarize_model(pcm, **kwargs)
        result = wx.assign_word_speakers(diarize_segments, result)
        segments: list[Segment] = []
        for s in result["segments"]:
            words = [
                {"w": normalize_text(w["word"]), "start": float(w["start"]), "end": float(w["end"]),
                 "speaker": w.get("speaker")}
                for w in s.get("words", [])
                if "start" in w and normalize_text(w.get("word", ""))
            ]
            spk_default = str(s.get("speaker") or "spk0")
            # group consecutive words by speaker, exactly like assign_speakers does
            runs: list[tuple[str, list[dict[str, Any]]]] = []
            for w in words:
                spk = str(w.get("speaker") or spk_default)
                if runs and runs[-1][0] == spk:
                    runs[-1][1].append(w)
                else:
                    runs.append((spk, [w]))
            if not runs:
                segments.append(make_segment(spk_default, s["start"], s["end"], normalize_text(s.get("text", ""))))
                continue
            for spk, ws in runs:
                clean = [{"w": w["w"], "start": w["start"], "end": w["end"]} for w in ws]
                segments.append(make_segment(spk, clean[0]["start"], clean[-1]["end"], " ".join(w["w"] for w in clean), clean))
        return Hypothesis(
            meeting_id=meeting_id,
            system=self.config.name or self.name,
            segments=sort_segments(segments),
            extra={"components": {"whisperx_model": self.model_name}},
        ).validate()


# --- registrations ------------------------------------------------------------


@register(
    "faster-whisper",
    description="faster-whisper ASR + energy-vad-cluster diarization (UNTESTED here: no models)",
    availability=lambda: _missing("faster_whisper"),
)
def _make_faster_whisper(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="faster-whisper")
    p = cfg.params
    tr = FasterWhisperTranscriber(
        model=str(p.get("model", "base")),
        device=str(p.get("device", "cpu")),
        compute_type=str(p.get("compute_type", "int8")),
        language=p.get("language"),
        beam_size=int(p.get("beam_size", 5)),
        vad_filter=bool(p.get("vad_filter", False)),
    )
    return ComposedPipeline(tr, EnergyVadClusterDiarizer.from_params(p), cfg, name="faster-whisper")


@register(
    "faster-whisper+pyannote",
    description="faster-whisper ASR + pyannote.audio diarization (UNTESTED here: no models)",
    availability=lambda: _missing_any("faster_whisper", "pyannote.audio", "torch"),
)
def _make_faster_whisper_pyannote(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="faster-whisper+pyannote")
    p = cfg.params
    tr = FasterWhisperTranscriber(
        model=str(p.get("model", "base")),
        device=str(p.get("device", "cpu")),
        compute_type=str(p.get("compute_type", "int8")),
        language=p.get("language"),
        beam_size=int(p.get("beam_size", 5)),
    )
    di = PyannoteDiarizer(
        model=str(p.get("diarization_model", "pyannote/speaker-diarization-3.1")),
        token_env=str(p.get("token_env", "HF_TOKEN")),
        device=p.get("device"),
    )
    return ComposedPipeline(tr, di, cfg, name="faster-whisper+pyannote")


@register(
    "pyannote-audio",
    description="pyannote.audio diarization only, no ASR (UNTESTED here: no models)",
    availability=lambda: _missing_any("pyannote.audio", "torch"),
)
def _make_pyannote(config: PipelineConfig | None = None) -> Pipeline:
    cfg = config or PipelineConfig(name="pyannote-audio")
    p = cfg.params
    di = PyannoteDiarizer(
        model=str(p.get("diarization_model", "pyannote/speaker-diarization-3.1")),
        token_env=str(p.get("token_env", "HF_TOKEN")),
        device=p.get("device"),
    )
    return ComposedPipeline(None, di, cfg, name="pyannote-audio")


@register(
    "whisperx",
    description=WhisperXPipeline.description,
    availability=lambda: _missing_any("whisperx", "torch"),
)
def _make_whisperx(config: PipelineConfig | None = None) -> Pipeline:
    return WhisperXPipeline(config)
