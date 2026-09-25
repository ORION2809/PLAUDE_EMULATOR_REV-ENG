"""Hand-built test signals and meeting dirs -- HARNESS TEST SUPPORT ONLY.

This is NOT the Layer 2 generator (no TTS, no room acoustics).  It builds
two things the pipeline tests need without any external data:

  * ``render_layout``: distinct synthetic "voices" (pulse trains at
    different f0 through different band-pass filters) laid out on a
    timeline with silence gaps -- a signal whose speaker segmentation is
    known analytically, so a model-free diarizer can be checked against it.
  * ``synthetic_meeting``: a complete meeting directory in the shared
    contract (meeting.json + ref.rttm + ref.stm + mix.wav [+ device/
    recording.ogg]) whose ground truth is the layout that rendered it.

Everything here is HARNESS_POLICY.  Nothing is a claim about speech or
about the device; the Ogg/Opus written under ``device/`` is a *standard*
Ogg/Opus container encoded by PyAV/libopus at the recorder's rate
(BYTECODE_PROVEN 16 kHz, base.py) and bit rate (32 kbps CBR,
docs/protocol-ledger.md section 8) -- the same shape as the R6-S2 fixtures
and, like them, explicitly ``not_plaud_capture``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import butter, lfilter

from .base import DEVICE_SAMPLE_RATE_HZ, MEETING_SCHEMA, make_segment, sort_segments
from .meeting import interpolate_words, write_meeting_dir

#: (f0 Hz, (band_lo, band_hi) Hz) per synthetic voice.  HARNESS_POLICY.
VOICES: dict[str, tuple[float, tuple[float, float]]] = {
    "A": (100.0, (200.0, 1200.0)),
    "B": (230.0, (1200.0, 3800.0)),
    "C": (160.0, (500.0, 2500.0)),
}

#: A tiny closed vocabulary so segments have words the perturbed oracle can
#: substitute.  HARNESS_POLICY.
WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu"
).split()

#: Opus bit rate the SDK repacks at: 32 kbps CBR (docs/protocol-ledger.md
#: section 8, "Codec parameters").  Used only to shape the device/ file.
DEVICE_OPUS_BITRATE = 32000


def voice_signal(f0: float, band: tuple[float, float], dur_s: float, seed: int, sr: int = DEVICE_SAMPLE_RATE_HZ) -> np.ndarray:
    """Pulse train at f0 through a 4th-order Butterworth band-pass, peak 0.5."""
    n = int(round(dur_s * sr))
    x = np.zeros(n, dtype=np.float64)
    period = max(1, int(round(sr / f0)))
    x[::period] = 1.0
    b, a = butter(4, [band[0] / (sr / 2), band[1] / (sr / 2)], btype="band")
    y = lfilter(b, a, x)
    y = y + 0.01 * np.random.default_rng(seed).standard_normal(n)
    peak = float(np.max(np.abs(y))) or 1.0
    return 0.5 * y / peak


def render_layout(
    layout: list[tuple[float, float, str]],
    *,
    noise_db: float = -60.0,
    seed: int = 0,
    sr: int = DEVICE_SAMPLE_RATE_HZ,
    tail_s: float = 0.5,
) -> np.ndarray:
    """Mix voices ``[(start, end, voice_key), ...]`` over a white-noise floor."""
    total = max((e for _, e, _ in layout), default=0.0) + tail_s
    rng = np.random.default_rng(seed)
    x = (10 ** (noise_db / 20.0)) * rng.standard_normal(int(round(total * sr)))
    for k, (s, e, key) in enumerate(layout):
        f0, band = VOICES[key]
        seg = voice_signal(f0, band, e - s, seed=1000 + k, sr=sr)
        i0 = int(round(s * sr))
        x[i0 : i0 + len(seg)] += seg
    return np.clip(x, -1.0, 1.0).astype(np.float32)


def segment_text(words_per_segment: int, k: int) -> str:
    return " ".join(WORDS[(k * 7 + i) % len(WORDS)] for i in range(words_per_segment))


def synthetic_meeting(
    out_dir: str | Path,
    *,
    meeting_id: str = "synth0",
    layout: list[tuple[float, float, str]] | None = None,
    speaker_voices: dict[str, str] | None = None,
    words_per_segment: int = 4,
    seed: int = 0,
    write_device_ogg: bool = False,
    noise_db: float = -60.0,
) -> tuple[dict[str, Any], Path]:
    """Write a complete meeting dir; return (meeting dict, dir path).

    ``layout`` entries are ``(start, end, speaker_id)``; ``speaker_voices``
    maps speaker ids to VOICES keys (default spk0->A, spk1->B, spk2->C).
    """
    layout = layout or [(0.5, 2.5, "spk0"), (3.0, 5.0, "spk1"), (5.5, 7.0, "spk0"), (7.5, 8.5, "spk1")]
    speaker_voices = speaker_voices or {"spk0": "A", "spk1": "B", "spk2": "C"}
    sr = DEVICE_SAMPLE_RATE_HZ
    voice_layout = [(s, e, speaker_voices[spk]) for s, e, spk in layout]
    pcm = render_layout(voice_layout, noise_db=noise_db, seed=seed, sr=sr)
    speakers = []
    for _, _, spk in layout:
        if spk not in speakers:
            speakers.append(spk)
    segments = []
    for k, (s, e, spk) in enumerate(layout):
        text = segment_text(words_per_segment, k)
        segments.append(make_segment(spk, s, e, text, interpolate_words(text, s, e)))
    segments = sort_segments(segments)
    audio: dict[str, Any] = {"mix_wav": "mix.wav", "stems": {}, "device": {}}
    if write_device_ogg:
        audio["device"] = {"ogg_opus": "device/recording.ogg"}
    meeting = {
        "schema": MEETING_SCHEMA,
        "meeting_id": meeting_id,
        "sample_rate": sr,
        "duration_s": len(pcm) / sr,
        "channels": 1,
        "speakers": [
            {"id": spk, "voice": f"synthetic:{speaker_voices[spk]}", "position_m": [0.0, 0.0, 0.0]} for spk in speakers
        ],
        "segments": segments,
        "audio": audio,
        "generator": {
            "name": "pipeline.synthetic",
            "version": "1",
            "seed": seed,
            "scenario": {"layout": layout, "noise_db": noise_db, "harness_test_support": True},
        },
    }
    out = write_meeting_dir(out_dir, meeting, pcm, sr)
    if write_device_ogg:
        write_device_ogg_opus(out / "device" / "recording.ogg", pcm, sr)
    return meeting, out


def write_device_ogg_opus(path: Path, pcm: np.ndarray, sr: int = DEVICE_SAMPLE_RATE_HZ) -> Path:
    """Standard Ogg/Opus, mono, 16 kHz, 32 kbps (PyAV/libopus) -- SYNTHETIC."""
    import av

    path.parent.mkdir(parents=True, exist_ok=True)
    int16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16).reshape(1, -1)
    container = av.open(str(path), "w", format="ogg")
    stream = container.add_stream("libopus", rate=sr)
    stream.layout = "mono"
    stream.bit_rate = DEVICE_OPUS_BITRATE
    frame = av.AudioFrame.from_ndarray(int16, format="s16", layout="mono")
    frame.sample_rate = sr
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path
