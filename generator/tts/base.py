"""Common TTS backend interface.

synth(text, voice, seed) -> SynthResult with 16 kHz mono int16 PCM and one
WordTiming per token of `text`. `timing_exact` is True only when the backend
knows word boundaries by construction: the formant backend builds the audio
word by word; the piper backend reads them from the voice's own duration
alignment (generator/tts/piper.py states what that does and does not mean).
Backends that estimate boundaries report False so downstream users can tell
them apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from generator.contract import SAMPLE_RATE


class BackendUnavailable(RuntimeError):
    """Raised by optional backends when their model or weights are absent.

    Never triggers a download: the generator is offline by design.
    """


@dataclass(frozen=True)
class WordTiming:
    word: str
    start_sample: int
    end_sample: int

    @property
    def start_s(self) -> float:
        return self.start_sample / SAMPLE_RATE

    @property
    def end_s(self) -> float:
        return self.end_sample / SAMPLE_RATE


@dataclass
class SynthResult:
    pcm: np.ndarray  # int16, shape (n,)
    words: list[WordTiming]
    backend: str
    voice: str
    timing_exact: bool
    sample_rate: int = SAMPLE_RATE
    notes: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.pcm.dtype != np.int16 or self.pcm.ndim != 1:
            raise ValueError("SynthResult.pcm must be a 1-D int16 array")
        if self.sample_rate != SAMPLE_RATE:
            raise ValueError(f"SynthResult.sample_rate must be {SAMPLE_RATE}")
        prev = 0
        for w in self.words:
            if not (0 <= w.start_sample <= w.end_sample <= len(self.pcm)):
                raise ValueError(f"word {w.word!r} timing outside the pcm")
            if w.start_sample < prev:
                raise ValueError(f"word {w.word!r} timing not monotonic")
            prev = w.end_sample

    @property
    def duration_samples(self) -> int:
        return int(len(self.pcm))


def proportional_words(tokens: list[str], n_samples: int) -> list[WordTiming]:
    """ESTIMATED timings: split n_samples in proportion to len(token) + 1.
    Only for backends that cannot report boundaries (timing_exact=False)."""
    weights = np.array([len(t) + 1 for t in tokens], dtype=np.float64)
    edges = np.round(np.cumsum(np.concatenate([[0.0], weights])) / weights.sum() * n_samples).astype(int)
    return [WordTiming(t, int(edges[i]), int(edges[i + 1])) for i, t in enumerate(tokens)]


class TTSBackend(Protocol):
    name: str

    def voices(self) -> list[str]: ...

    def synth(self, text: str, voice: str, seed: int) -> SynthResult: ...


__all__ = ["BackendUnavailable", "SynthResult", "TTSBackend", "WordTiming", "proportional_words"]
