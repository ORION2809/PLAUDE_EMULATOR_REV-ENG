"""Pluggable TTS backends.

REQUIRED and always available: "formant" (model-free, deterministic, exact
word timings). OPTIONAL and import-guarded: "piper" (real speech; word
timings from the voice's own duration alignment, see generator/tts/piper.py)
and "kokoro" (estimated timings) -- both raise BackendUnavailable when their
package or weights are absent (piper also when no voice file can be loaded
with alignments here, e.g. no verified aligned copy and no `onnx`), and
neither ever downloads anything. `available_backends()` reports the reason.
"""

from __future__ import annotations

from typing import Callable

from generator.tts.base import BackendUnavailable, SynthResult, TTSBackend, WordTiming
from generator.tts.formant import FormantBackend


def _piper() -> TTSBackend:
    from generator.tts.piper import PiperBackend

    return PiperBackend()


def _kokoro() -> TTSBackend:
    from generator.tts.kokoro import KokoroBackend

    return KokoroBackend()


BACKENDS: dict[str, Callable[[], TTSBackend]] = {
    "formant": FormantBackend,
    "piper": _piper,
    "kokoro": _kokoro,
}


def get_backend(name: str) -> TTSBackend:
    if name not in BACKENDS:
        raise KeyError(f"unknown tts backend {name!r}; have {sorted(BACKENDS)}")
    return BACKENDS[name]()


def available_backends() -> dict[str, str]:
    """name -> 'available' or the BackendUnavailable reason."""
    out: dict[str, str] = {}
    for name in BACKENDS:
        try:
            get_backend(name)
            out[name] = "available"
        except BackendUnavailable as exc:
            out[name] = str(exc)
    return out


__all__ = [
    "BACKENDS",
    "BackendUnavailable",
    "FormantBackend",
    "SynthResult",
    "TTSBackend",
    "WordTiming",
    "available_backends",
    "get_backend",
]
