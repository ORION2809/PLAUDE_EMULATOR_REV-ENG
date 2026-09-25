"""OPTIONAL piper backend (import-guarded; never downloads).

Requires the `piper` (piper-tts) Python package AND at least one voice
model `data/voices/*.onnx` (with its `.onnx.json` config) already present.
Neither is installed in the harness venv, so `PiperBackend()` raises
BackendUnavailable and callers skip cleanly.

Word timings: piper does not expose word alignments, so this backend
splits the rendered utterance proportionally to token character counts.
Those timings are ESTIMATES (timing_exact=False); only the formant
backend gives ground-truth-by-construction timings.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from generator._evidence import ROOT
from generator.contract import SAMPLE_RATE
from generator.tts.base import BackendUnavailable, SynthResult, WordTiming

VOICES_DIR = ROOT / "data" / "voices"


def _proportional_words(tokens: list[str], n_samples: int) -> list[WordTiming]:
    weights = np.array([len(t) + 1 for t in tokens], dtype=np.float64)
    edges = np.round(np.cumsum(np.concatenate([[0.0], weights])) / weights.sum() * n_samples).astype(int)
    return [WordTiming(t, int(edges[i]), int(edges[i + 1])) for i, t in enumerate(tokens)]


class PiperBackend:
    name = "piper"

    def __init__(self, voices_dir: Path | None = None) -> None:
        try:
            from piper import PiperVoice  # type: ignore
        except Exception as exc:  # ImportError or a broken install
            raise BackendUnavailable(
                "piper backend: the `piper` package is not installed (pip install piper-tts); "
                "not installed by the harness on purpose"
            ) from exc
        self._piper_voice_cls = PiperVoice
        self._dir = Path(voices_dir or VOICES_DIR)
        self._models = sorted(self._dir.glob("*.onnx")) if self._dir.is_dir() else []
        if not self._models:
            raise BackendUnavailable(
                f"piper backend: no *.onnx voice under {self._dir}; the harness never downloads voices"
            )
        self._loaded: dict[str, object] = {}

    def voices(self) -> list[str]:
        return [m.stem for m in self._models]

    def _load(self, voice: str):
        if voice not in self._loaded:
            path = next((m for m in self._models if m.stem == voice), None)
            if path is None:
                raise KeyError(f"unknown piper voice {voice!r}")
            self._loaded[voice] = self._piper_voice_cls.load(str(path))
        return self._loaded[voice]

    def synth(self, text: str, voice: str, seed: int) -> SynthResult:
        from scipy.signal import resample_poly

        tokens = text.split()
        if not tokens:
            raise ValueError("text must contain at least one token")
        model = self._load(voice)
        rate = int(model.config.sample_rate)
        chunks = [np.frombuffer(b, dtype=np.int16) for b in model.synthesize_stream_raw(text)]
        pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
        if rate != SAMPLE_RATE:
            g = np.gcd(rate, SAMPLE_RATE)
            pcm = resample_poly(pcm.astype(np.float64), SAMPLE_RATE // g, rate // g)
            pcm = np.clip(np.round(pcm), -32768, 32767).astype(np.int16)
        return SynthResult(
            pcm=pcm,
            words=_proportional_words(tokens, len(pcm)),
            backend=self.name,
            voice=voice,
            timing_exact=False,
            notes={"timing_method": "proportional-by-characters (estimate)", "seed_ignored": True},
        )


__all__ = ["PiperBackend", "VOICES_DIR"]
