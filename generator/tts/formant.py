"""Model-free deterministic pseudo-speech: formant-filtered glottal pulses.

No neural model, no weights, no downloads. The output is not intelligible
speech; it is speech-LIKE audio (voiced vowel nuclei with per-speaker
pitch and vocal-tract identity, noise consonants, natural-ish syllable
rhythm) whose word boundaries are known EXACTLY because the waveform is
assembled word by word from silence-separated pieces. That makes every
timing in the ground truth a fact of construction rather than a detection.

Everything in this module is HARNESS_POLICY: the formant table, durations,
voice presets, source model and levels are harness choices. Nothing here
models a Plaud device or any real speaker.

Construction guarantees relied on by the tests:
* the utterance starts at the first word's first sample (no lead-in);
* words are separated by exact digital silence (all-zero gaps);
* word i's end_sample <= word i+1's start_sample;
* the last word ends exactly at len(pcm);
* same (text, voice, seed) -> bit-identical pcm.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, lfilter

from generator.contract import SAMPLE_RATE
from generator.tts.base import SynthResult, WordTiming

FS = SAMPLE_RATE
VOWELS = set("aeiouy")

# HARNESS_POLICY: (F1, F2, F3) in Hz, loosely after classic adult-male
# vowel-formant tables; "@" is a schwa used for vowel-less tokens.
VOWEL_FORMANTS: dict[str, tuple[float, float, float]] = {
    "a": (730.0, 1090.0, 2440.0),
    "e": (530.0, 1840.0, 2480.0),
    "i": (270.0, 2290.0, 3010.0),
    "o": (570.0, 840.0, 2410.0),
    "u": (300.0, 870.0, 2240.0),
    "y": (390.0, 1990.0, 2550.0),
    "@": (500.0, 1500.0, 2500.0),
}
FORMANT_BANDWIDTHS = (80.0, 100.0, 140.0)

FRICATIVES: dict[str, tuple[float, float]] = {  # (low, high) band edges in Hz
    "s": (4500.0, 7500.0),
    "z": (4000.0, 7500.0),
    "f": (2500.0, 7000.0),
    "v": (2000.0, 6000.0),
    "h": (800.0, 3500.0),
    "x": (3500.0, 7500.0),
}
PLOSIVES = {"p", "t", "k", "b", "d", "g", "c", "q"}
VOICED_PLOSIVES = {"b", "d", "g"}
SONORANTS: dict[str, tuple[float, float, float]] = {
    "m": (250.0, 1000.0, 2200.0),
    "n": (250.0, 1400.0, 2300.0),
    "l": (350.0, 1100.0, 2600.0),
    "r": (400.0, 1200.0, 1700.0),
    "w": (300.0, 700.0, 2200.0),
    "j": (280.0, 2100.0, 2900.0),
}

# HARNESS_POLICY durations (ms) at speaking rate 1.0.
VOWEL_MS = 110.0
FINAL_VOWEL_MS = 150.0
FRICATIVE_MS = 70.0
PLOSIVE_CLOSURE_MS = 22.0
PLOSIVE_BURST_MS = 12.0
SONORANT_MS = 55.0
WORD_GAP_MS = (40.0, 90.0)  # uniform range between words
FADE_MS = 4.0

PEAK_LEVEL = 0.4  # utterance peak relative to full scale (about -8 dBFS)


@dataclass(frozen=True)
class FormantVoice:
    """A synthetic voice identity (HARNESS_POLICY; not a real person)."""

    name: str
    f0_hz: float
    tract_scale: float  # multiplies all formant frequencies
    rate: float  # 1.0 == nominal syllable durations
    breathiness: float  # noise mixed into voiced segments, relative rms
    jitter: float  # per-pulse f0 perturbation, relative


VOICES: dict[str, FormantVoice] = {
    v.name: v
    for v in (
        FormantVoice("fv-bass", 92.0, 1.00, 0.95, 0.03, 0.010),
        FormantVoice("fv-baritone", 110.0, 0.98, 1.00, 0.03, 0.012),
        FormantVoice("fv-tenor", 135.0, 0.94, 1.05, 0.04, 0.012),
        FormantVoice("fv-alto", 175.0, 0.88, 1.00, 0.05, 0.014),
        FormantVoice("fv-mezzo", 205.0, 0.85, 1.05, 0.05, 0.014),
        FormantVoice("fv-soprano", 240.0, 0.82, 1.10, 0.06, 0.016),
        FormantVoice("fv-child", 270.0, 0.78, 1.15, 0.07, 0.020),
        FormantVoice("fv-gravel", 80.0, 1.03, 0.90, 0.10, 0.030),
    )
}


def _seed_int(*parts: object) -> int:
    material = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "little")


def syllabify(word: str) -> list[tuple[str, str, str]]:
    """Split a letters-only token into (onset, nucleus, coda) syllables.

    Each maximal vowel-letter run is a nucleus; consonants before the first
    nucleus are its onset; consonants between nuclei go to the next syllable's
    onset except the first one, which closes the previous syllable; trailing
    consonants are the last coda. Vowel-less tokens get a schwa nucleus.
    """
    letters = [c for c in word.lower() if "a" <= c <= "z"]
    if not letters:
        raise ValueError(f"token has no letters: {word!r}")
    groups: list[tuple[bool, str]] = []
    for c in letters:
        is_v = c in VOWELS
        if groups and groups[-1][0] == is_v:
            groups[-1] = (is_v, groups[-1][1] + c)
        else:
            groups.append((is_v, c))
    if not any(v for v, _ in groups):
        return [("".join(g for _, g in groups), "@", "")]
    syllables: list[list[str]] = []  # [onset, nucleus, coda]
    pending_onset = ""
    for is_v, run in groups:
        if is_v:
            syllables.append([pending_onset, run, ""])
            pending_onset = ""
        else:
            if not syllables:
                pending_onset = run
            elif len(run) == 1:
                pending_onset = run
            else:
                syllables[-1][2] = run[0]
                pending_onset = run[1:]
    if pending_onset:
        syllables[-1][2] += pending_onset
    return [(o, n, c) for o, n, c in syllables]


def _ms(ms: float, rate: float) -> int:
    return max(1, int(round(ms / rate * FS / 1000.0)))


def _fade(x: np.ndarray, fade_samples: int) -> np.ndarray:
    n = len(x)
    f = min(fade_samples, n // 2)
    if f <= 0:
        return x
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, f))
    x = x.copy()
    x[:f] *= ramp
    x[-f:] *= ramp[::-1]
    return x


def _resonate(x: np.ndarray, formants: tuple[float, float, float], scale: float) -> np.ndarray:
    y = x
    for f, bw in zip(formants, FORMANT_BANDWIDTHS):
        fc = min(f * scale, 0.45 * FS)
        r = np.exp(-np.pi * bw / FS)
        theta = 2.0 * np.pi * fc / FS
        a = np.array([1.0, -2.0 * r * np.cos(theta), r * r])
        y = lfilter([1.0 - r], a, y)
    return y


def _glottal_source(n: int, f0: float, voice: FormantVoice, rng: np.random.Generator, contour: float) -> np.ndarray:
    """Pulse train with jitter/shimmer, shaped by a two-pole low-pass."""
    out = np.zeros(n)
    phase = 0.0
    i = 0
    while i < n:
        f = f0 * contour * (1.0 + voice.jitter * rng.standard_normal())
        period = max(int(round(FS / max(f, 40.0))), 8)
        amp = 1.0 + 0.05 * rng.standard_normal()
        out[i] = amp
        i += period
        phase += 1.0
    # -12 dB/oct glottal roll-off via two one-pole low-passes
    for _ in range(2):
        out = lfilter([1.0 - 0.96], [1.0, -0.96], out)
    if voice.breathiness > 0:
        noise = rng.standard_normal(n)
        out = out + voice.breathiness * np.sqrt(np.mean(out ** 2) + 1e-12) / (np.sqrt(np.mean(noise ** 2)) + 1e-12) * noise
    return out


def _rms_norm(x: np.ndarray, target: float) -> np.ndarray:
    rms = float(np.sqrt(np.mean(x ** 2)))
    if rms < 1e-9:
        return x
    return x * (target / rms)


def _voiced(n: int, formants: tuple[float, float, float], voice: FormantVoice, rng: np.random.Generator, contour: float, level: float) -> np.ndarray:
    src = _glottal_source(n, voice.f0_hz, voice, rng, contour)
    y = _resonate(src, formants, voice.tract_scale)
    y = lfilter([1.0, -0.9], [1.0], y)  # lip radiation / pre-emphasis
    return _fade(_rms_norm(y, level), _ms(FADE_MS, 1.0))


def _noise_band(n: int, low: float, high: float, rng: np.random.Generator, level: float) -> np.ndarray:
    hi = min(high, 0.49 * FS)
    lo = min(low, hi * 0.8)
    b, a = butter(2, [lo / (FS / 2), hi / (FS / 2)], btype="band")
    y = lfilter(b, a, rng.standard_normal(n))
    return _fade(_rms_norm(y, level), _ms(FADE_MS, 1.0))


def _consonant(letter: str, voice: FormantVoice, rng: np.random.Generator, contour: float) -> list[np.ndarray]:
    rate = voice.rate
    if letter in FRICATIVES:
        lo, hi = FRICATIVES[letter]
        return [_noise_band(_ms(FRICATIVE_MS, rate), lo, hi, rng, 0.045)]
    if letter in PLOSIVES:
        closure = _ms(PLOSIVE_CLOSURE_MS * (0.6 if letter in VOICED_PLOSIVES else 1.0), rate)
        burst = _noise_band(_ms(PLOSIVE_BURST_MS, rate), 1500.0, 7000.0, rng, 0.06)
        return [np.zeros(closure), burst]
    if letter in SONORANTS:
        return [_voiced(_ms(SONORANT_MS, rate), SONORANTS[letter], voice, rng, contour, 0.07)]
    # any other letter: brief schwa-like voiced transition
    return [_voiced(_ms(SONORANT_MS * 0.6, rate), VOWEL_FORMANTS["@"], voice, rng, contour, 0.06)]


def synth_word_pieces(word: str, voice: FormantVoice, rng: np.random.Generator, contour_start: float, contour_end: float, final: bool) -> np.ndarray:
    """Render one word as a contiguous float array (no surrounding silence)."""
    syllables = syllabify(word)
    pieces: list[np.ndarray] = []
    for k, (onset, nucleus, coda) in enumerate(syllables):
        t = k / max(1, len(syllables) - 1) if len(syllables) > 1 else 0.0
        contour = contour_start + (contour_end - contour_start) * t
        for c in onset:
            pieces.extend(_consonant(c, voice, rng, contour))
        last = final and k == len(syllables) - 1
        dur = _ms(FINAL_VOWEL_MS if last else VOWEL_MS, voice.rate)
        key = nucleus[0] if nucleus[0] in VOWEL_FORMANTS else "@"
        pieces.append(_voiced(dur, VOWEL_FORMANTS[key], voice, rng, contour, 0.12))
        for c in coda:
            pieces.extend(_consonant(c, voice, rng, contour))
    return np.concatenate(pieces) if pieces else np.zeros(1)


class FormantBackend:
    """The required offline backend. `timing_exact` is always True."""

    name = "formant"

    def __init__(self, voices: dict[str, FormantVoice] | None = None) -> None:
        self._voices = dict(voices or VOICES)

    def voices(self) -> list[str]:
        return list(self._voices)

    def synth(self, text: str, voice: str, seed: int) -> SynthResult:
        if voice not in self._voices:
            raise KeyError(f"unknown formant voice {voice!r}; have {sorted(self._voices)}")
        tokens = text.split()
        if not tokens:
            raise ValueError("text must contain at least one token")
        if text != " ".join(tokens) or text != text.lower():
            raise ValueError("text must be lowercase tokens separated by single spaces")
        v = self._voices[voice]
        rng = np.random.default_rng(_seed_int("formant", seed, voice, text))
        pieces: list[np.ndarray] = []
        words: list[WordTiming] = []
        cursor = 0
        n = len(tokens)
        for i, tok in enumerate(tokens):
            # f0 declination across the utterance: 1.08 -> 0.92 (HARNESS_POLICY)
            c0 = 1.08 - 0.16 * (i / max(1, n))
            c1 = 1.08 - 0.16 * ((i + 1) / max(1, n))
            rendered = synth_word_pieces(tok, v, rng, c0, c1, final=(i == n - 1))
            start = cursor
            end = cursor + len(rendered)
            pieces.append(rendered)
            words.append(WordTiming(tok, start, end))
            cursor = end
            if i != n - 1:
                gap = _ms(float(rng.uniform(*WORD_GAP_MS)), v.rate)
                pieces.append(np.zeros(gap))
                cursor += gap
        wave = np.concatenate(pieces)
        peak = float(np.max(np.abs(wave)))
        if peak > 0:
            wave = wave * (PEAK_LEVEL / peak)
        pcm = np.clip(np.round(wave * 32767.0), -32768, 32767).astype(np.int16)
        return SynthResult(pcm=pcm, words=words, backend=self.name, voice=voice, timing_exact=True)


__all__ = ["FormantBackend", "FormantVoice", "VOICES", "VOWEL_FORMANTS", "syllabify"]
