"""Formant backend: ground truth by construction, and analytic checks on the
source model. Optional backends must fail closed without downloading."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from generator.contract import SAMPLE_RATE
from generator.tts import BackendUnavailable, available_backends, get_backend
from generator.tts.base import SynthResult, WordTiming
from generator.tts.formant import VOICES, FormantBackend, FormantVoice, syllabify

TEXT = "the budget review starts tomorrow morning"


def test_syllabify_known_answers() -> None:
    assert syllabify("meeting") == [("m", "ee", ""), ("t", "i", "ng")]
    assert syllabify("a") == [("", "a", "")]
    assert syllabify("hmm") == [("hmm", "@", "")]
    assert syllabify("strengths") == [("str", "e", "ngths")]
    assert syllabify("release") == [("r", "e", ""), ("l", "ea", ""), ("s", "e", "")]
    with pytest.raises(ValueError):
        syllabify("123")


def test_synthesis_is_bit_deterministic_and_identity_dependent() -> None:
    b = FormantBackend()
    r1 = b.synth(TEXT, "fv-alto", seed=9)
    r2 = b.synth(TEXT, "fv-alto", seed=9)
    assert np.array_equal(r1.pcm, r2.pcm) and r1.words == r2.words
    r3 = b.synth(TEXT, "fv-alto", seed=10)
    r4 = b.synth(TEXT, "fv-bass", seed=9)
    assert not np.array_equal(r1.pcm[: len(r3.pcm)], r3.pcm[: len(r1.pcm)])
    assert not np.array_equal(r1.pcm[: len(r4.pcm)], r4.pcm[: len(r1.pcm)])


def test_word_timings_are_exact_by_construction() -> None:
    r = FormantBackend().synth(TEXT, "fv-tenor", seed=1)
    assert r.timing_exact and r.sample_rate == SAMPLE_RATE and r.pcm.dtype == np.int16
    words = r.words
    assert [w.word for w in words] == TEXT.split()
    assert words[0].start_sample == 0
    assert words[-1].end_sample == len(r.pcm)
    for a, b in zip(words, words[1:]):
        assert a.end_sample <= b.start_sample, "words must not overlap"
        assert np.all(r.pcm[a.end_sample : b.start_sample] == 0), "inter-word gap must be digital silence"
        assert b.start_sample - a.end_sample > 0
    for w in words:
        seg = r.pcm[w.start_sample : w.end_sample].astype(np.float64)
        assert np.sqrt(np.mean(seg ** 2)) > 300, f"{w.word} is not audible"
    assert 0.3 * 32767 < np.max(np.abs(r.pcm)) <= 0.41 * 32767


def test_speaking_rate_scales_duration_analytically() -> None:
    slow = FormantVoice("slow", 120.0, 1.0, 1.0, 0.0, 0.0)
    fast = replace(slow, name="fast", rate=1.25)
    b = FormantBackend({"slow": slow, "fast": fast})
    d_slow = len(b.synth(TEXT, "slow", 3).pcm)
    d_fast = len(b.synth(TEXT, "fast", 3).pcm)
    # every piece is ms / rate, so the ratio of durations is the ratio of rates
    assert abs(d_fast * 1.25 - d_slow) / d_slow < 0.03


def test_vowel_pitch_matches_the_voice_f0() -> None:
    """A one-syllable word is rendered at contour 1.08 x f0 (declination start)."""
    v = replace(VOICES["fv-bass"], jitter=0.0, breathiness=0.0)
    b = FormantBackend({"v": v})
    r = b.synth("a", "v", 1)
    x = r.pcm.astype(np.float64)
    x = x[len(x) // 4 : -len(x) // 4]
    x -= x.mean()
    ac = np.correlate(x, x, mode="full")[len(x) - 1 :]
    expected_period = round(SAMPLE_RATE / (v.f0_hz * 1.08))
    lo, hi = int(expected_period * 0.7), int(expected_period * 1.3)
    lag = lo + int(np.argmax(ac[lo:hi]))
    f0 = SAMPLE_RATE / lag
    assert abs(f0 - v.f0_hz * 1.08) / (v.f0_hz * 1.08) < 0.05, f0


def test_bad_inputs_are_rejected() -> None:
    b = FormantBackend()
    with pytest.raises(ValueError):
        b.synth("", "fv-alto", 1)
    with pytest.raises(ValueError):
        b.synth("Hello there", "fv-alto", 1)
    with pytest.raises(ValueError):
        b.synth("two  spaces", "fv-alto", 1)
    with pytest.raises(KeyError):
        b.synth("fine", "no-such-voice", 1)


def test_synth_result_validates_timings() -> None:
    pcm = np.zeros(100, dtype=np.int16)
    with pytest.raises(ValueError):
        SynthResult(pcm=pcm, words=[WordTiming("a", 0, 101)], backend="x", voice="y", timing_exact=True)
    with pytest.raises(ValueError):
        SynthResult(pcm=pcm, words=[WordTiming("a", 50, 60), WordTiming("b", 40, 70)], backend="x", voice="y", timing_exact=True)
    with pytest.raises(ValueError):
        SynthResult(pcm=pcm.astype(np.float32), words=[], backend="x", voice="y", timing_exact=True)


def test_registry_reports_formant_available_and_unknown_names_fail() -> None:
    status = available_backends()
    assert status["formant"] == "available"
    assert set(status) == {"formant", "piper", "kokoro"}
    with pytest.raises(KeyError):
        get_backend("espeak")


@pytest.mark.parametrize("name", ["piper", "kokoro"])
def test_optional_backends_fail_closed_without_models(name: str) -> None:
    """No weights and no network: constructing them must raise cleanly, and
    the reason must say so. If someone installed one locally, skip."""
    try:
        backend = get_backend(name)
    except BackendUnavailable as exc:
        assert "download" in str(exc).lower() or "not installed" in str(exc).lower() or "weights" in str(exc).lower()
        return
    pytest.skip(f"{name} is installed locally with voices: {backend.voices()[:3]}")
