"""OPTIONAL kokoro backend (import-guarded; never downloads).

reference/upstream/kokoro is SOURCE ONLY: the package needs torch plus the
`hexgrad/Kokoro-82M` weights, which are not present and which the harness
must never fetch. `KokoroBackend()` therefore raises BackendUnavailable in
this environment. The implementation below is what would run if a user
installed kokoro and placed the weights locally; it is untested here.

Word timings: recent kokoro releases attach per-token timestamps
(`result.tokens[i].start_ts/end_ts`) for the English pipelines. Those are
model alignments, not construction facts, so timing_exact=False.
"""

from __future__ import annotations

import os

import numpy as np

from generator.contract import SAMPLE_RATE
from generator.tts.base import BackendUnavailable, SynthResult, WordTiming, proportional_words


class KokoroBackend:
    name = "kokoro"

    def __init__(self, lang_code: str = "a") -> None:
        if os.environ.get("HF_HUB_OFFLINE") not in ("1", "true"):
            # Refuse to construct anything that could reach the network.
            raise BackendUnavailable(
                "kokoro backend: set HF_HUB_OFFLINE=1 with local weights present; "
                "the harness never downloads model weights"
            )
        try:
            from kokoro import KPipeline  # type: ignore
        except Exception as exc:
            raise BackendUnavailable(
                "kokoro backend: the `kokoro` package (and torch) is not installed; "
                "reference/upstream/kokoro is source only and carries no weights"
            ) from exc
        try:
            self._pipeline = KPipeline(lang_code=lang_code)
        except Exception as exc:
            raise BackendUnavailable(f"kokoro backend: weights not available offline ({exc})") from exc

    def voices(self) -> list[str]:
        return ["af_heart", "am_adam", "bf_emma", "bm_george"]

    def synth(self, text: str, voice: str, seed: int) -> SynthResult:
        from scipy.signal import resample_poly

        tokens = text.split()
        if not tokens:
            raise ValueError("text must contain at least one token")
        audio_parts = []
        words: list[WordTiming] = []
        offset = 0
        for result in self._pipeline(text, voice=voice):
            audio = np.asarray(result.audio, dtype=np.float64)  # 24 kHz float
            n16 = int(round(len(audio) * SAMPLE_RATE / 24000))
            for tok in getattr(result, "tokens", None) or []:
                if getattr(tok, "start_ts", None) is None:
                    continue
                w = "".join(ch for ch in str(tok.text).lower() if "a" <= ch <= "z")
                if not w:
                    continue
                words.append(
                    WordTiming(w, offset + int(tok.start_ts * SAMPLE_RATE), offset + int(tok.end_ts * SAMPLE_RATE))
                )
            audio_parts.append(audio)
            offset += n16
        audio = np.concatenate(audio_parts) if audio_parts else np.zeros(0)
        pcm = resample_poly(audio, SAMPLE_RATE // np.gcd(24000, SAMPLE_RATE), 24000 // np.gcd(24000, SAMPLE_RATE))
        pcm = np.clip(np.round(pcm * 32767.0), -32768, 32767).astype(np.int16)
        if [w.word for w in words] != tokens:
            # Fall back to proportional estimates when the model's token list
            # does not line up with the input tokens.
            words = proportional_words(tokens, len(pcm))
        return SynthResult(pcm=pcm, words=words, backend=self.name, voice=voice, timing_exact=False)


__all__ = ["KokoroBackend"]
