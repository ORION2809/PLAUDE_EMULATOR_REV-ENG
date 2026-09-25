"""Audio loading: wav via soundfile, Ogg/Opus via PyAV, one shared resampler.

The Ogg fixtures are the R6-S2 synthetic files (tests/fixtures/
r6s2_manifest.json, ``not_plaud_capture: true``); their sha256 is pinned
so a silently regenerated fixture cannot make these tests lie.  The
"known answer" is the manifest's generator tone (440 + 880 Hz, from
scripts/generate_r6s2_fixture.py), rebuilt here and correlated against
the decoded PCM.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parents[1]))

from pipeline import DEVICE_SAMPLE_RATE_HZ, AudioFormatError, load_audio
from pipeline.base import resample

FIX = Path(__file__).parent / "fixtures"
MANIFEST = json.loads((FIX / "r6s2_manifest.json").read_text())


def _pinned(name: str) -> Path:
    entry = next(e for e in MANIFEST["fixtures"] if e["path"] == name)
    p = FIX / name
    assert hashlib.sha256(p.read_bytes()).hexdigest() == entry["sha256"], f"{name} is not the pinned fixture"
    assert entry["not_plaud_capture"] is True
    return p


def _source_tone(n: int, sr: int = 16000) -> np.ndarray:
    t = np.arange(n) / sr
    x = 0.6 * np.sin(2 * np.pi * 440.0 * t) + 0.4 * np.sin(2 * np.pi * 880.0 * t)
    return x / np.abs(x).max()


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    return float(np.corrcoef(a[:n], b[:n])[0, 1])


def test_device_rate_is_the_bytecode_constant():
    # OggUtils.b = 16000 (build/evidence/javap/ALL.txt:11496); mirrors plaudsim.audio
    sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
    from plaudsim.audio import SAMPLE_RATE_HZ

    assert DEVICE_SAMPLE_RATE_HZ == SAMPLE_RATE_HZ == 16000


def test_ogg_opus_mono_fixture_loads_as_16k_mono():
    a = load_audio(_pinned("r6s2_16k_mono.ogg"))
    assert a.sample_rate == 16000 and a.container == "pyav"
    assert a.source_sample_rate == 48000  # libopus always decodes at 48 kHz
    assert a.source_channels == 1
    assert a.pcm.dtype == np.float32 and a.pcm.ndim == 1
    assert abs(len(a.pcm) - 32000) <= 160  # 2.0 s +- 10 ms of codec padding
    assert a.duration_s == pytest.approx(2.0, abs=0.01)
    assert 0.5 < float(np.abs(a.pcm).max()) <= 1.0
    assert _corr(_source_tone(32000), a.pcm) > 0.99


def test_ogg_opus_stereo_fixture_downmixes_to_mono():
    a = load_audio(_pinned("r6s2_16k_stereo.ogg"))
    assert a.source_channels == 2 and a.pcm.ndim == 1
    assert abs(len(a.pcm) - 32000) <= 160
    # right channel is 0.5 x left in the generator, so the mean is 0.75 x the tone
    assert _corr(_source_tone(32000), a.pcm) > 0.99


def test_ogg_opus_written_by_the_synthetic_helper_round_trips(tmp_path):
    from pipeline.synthetic import write_device_ogg_opus

    x = (0.5 * _source_tone(3 * 16000)).astype(np.float32)
    p = write_device_ogg_opus(tmp_path / "device" / "recording.ogg", x)
    a = load_audio(p)
    assert abs(len(a.pcm) - len(x)) <= 160
    assert _corr(x, a.pcm) > 0.99


def test_wav_pcm16_round_trip_is_exact_to_quantisation(tmp_path):
    x = (0.25 * _source_tone(16000)).astype(np.float32)
    sf.write(str(tmp_path / "a.wav"), x, 16000, subtype="PCM_16")
    a = load_audio(tmp_path / "a.wav")
    assert a.container == "wav" and a.source_sample_rate == 16000 and a.source_channels == 1
    assert len(a.pcm) == 16000
    assert np.max(np.abs(a.pcm - x)) <= 1.0 / 32768 + 1e-7


def test_stereo_wav_is_averaged(tmp_path):
    left = 0.5 * _source_tone(8000)
    right = np.zeros(8000)
    sf.write(str(tmp_path / "s.wav"), np.stack([left, right], axis=1), 16000, subtype="PCM_16")
    a = load_audio(tmp_path / "s.wav")
    assert a.source_channels == 2
    assert np.max(np.abs(a.pcm - 0.25 * _source_tone(8000))) < 2.0 / 32768


def test_48k_wav_is_resampled_to_16k(tmp_path):
    t = np.arange(48000 * 2) / 48000
    x = 0.5 * np.sin(2 * np.pi * 440 * t)
    sf.write(str(tmp_path / "hi.wav"), x, 48000, subtype="PCM_16")
    a = load_audio(tmp_path / "hi.wav")
    assert a.source_sample_rate == 48000 and a.sample_rate == 16000
    assert len(a.pcm) == 32000
    assert _corr(0.5 * np.sin(2 * np.pi * 440 * np.arange(32000) / 16000), a.pcm) > 0.999


def test_target_rate_override():
    a = load_audio(_pinned("r6s2_16k_mono.ogg"), target_sr=8000)
    assert a.sample_rate == 8000 and abs(len(a.pcm) - 16000) <= 80


def test_resample_identity_and_ratio():
    x = np.random.default_rng(0).standard_normal(1000).astype(np.float32)
    assert resample(x, 16000, 16000) is x or np.array_equal(resample(x, 16000, 16000), x)
    assert len(resample(x, 16000, 8000)) == 500
    assert len(resample(x, 44100, 16000)) == round(1000 * 16000 / 44100)
    with pytest.raises(ValueError):
        resample(x, 0, 16000)


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_audio(FIX / "does-not-exist.ogg")


def test_garbage_ogg_raises_audio_format_error(tmp_path):
    (tmp_path / "junk.ogg").write_bytes(b"OggS" + bytes(range(256)) * 8)
    with pytest.raises(AudioFormatError):
        load_audio(tmp_path / "junk.ogg")


def test_garbage_wav_raises_audio_format_error(tmp_path):
    (tmp_path / "junk.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunkjunk")
    with pytest.raises(AudioFormatError):
        load_audio(tmp_path / "junk.wav")


def test_routing_is_by_suffix_and_libsndfile_also_decodes_opus(tmp_path):
    """Routing is by suffix (HARNESS_POLICY), so an Ogg/Opus renamed .wav goes
    down the soundfile path -- and still decodes, because libsndfile 1.2.2
    carries its own Opus support.  Pinned so a future change to content
    sniffing or a libsndfile without Opus shows up here, not in a batch."""
    (tmp_path / "renamed.wav").write_bytes(_pinned("r6s2_16k_mono.ogg").read_bytes())
    a = load_audio(tmp_path / "renamed.wav")
    assert a.container == "wav"
    # The two decoders disagree on what "source rate" means: libsndfile hands
    # back PCM at the OpusHead input rate (16 000), PyAV/libopus at 48 000.
    # Both end at the 16 kHz working rate; the provenance field records which.
    assert a.source_sample_rate == 16000
    assert load_audio(_pinned("r6s2_16k_mono.ogg")).source_sample_rate == 48000
    assert abs(len(a.pcm) - 32000) <= 160
    assert _corr(_source_tone(32000), a.pcm) > 0.99
