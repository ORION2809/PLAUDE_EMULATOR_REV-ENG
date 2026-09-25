#!/usr/bin/env python3
"""Generate R6-S2 synthetic Ogg/Opus fixtures (SYNTHETIC -- NOT PLAUD CAPTURE).

Deterministic 16 kHz PCM sine sources, encoded with PyAV/libopus at
32 kbps CBR (the Plaud wire rate from R6-S1) into standard Ogg/Opus.
Writes tests/fixtures/r6s2_*.ogg + r6s2_manifest.json (params + sha256).
Requires: av (PyAV), numpy.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures"

SAMPLE_RATE = 16000
DURATION_S = 2.0
BITRATE = 32000  # Plaud wire rate: 32 kbps CBR (ledger section 8)


def pcm_source(channels: int) -> np.ndarray:
    """Deterministic multi-tone: 440 Hz + 880 Hz mix, int16.

    Returns packed/interleaved frames: shape (1, n) mono, (1, 2n) stereo.
    """
    n = int(SAMPLE_RATE * DURATION_S)
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    mono = 0.6 * np.sin(2 * np.pi * 440.0 * t) + 0.4 * np.sin(2 * np.pi * 880.0 * t)
    mono = (mono / np.max(np.abs(mono)) * 30000).astype(np.int16)
    if channels == 1:
        return mono.reshape(1, -1)
    right = (mono * 0.5).astype(np.int16)
    stereo = np.empty(2 * n, dtype=np.int16)
    stereo[0::2] = mono
    stereo[1::2] = right
    return stereo.reshape(1, -1)


def encode(path: Path, channels: int) -> dict:
    pcm = pcm_source(channels)
    container = av.open(str(path), "w", format="ogg")
    stream = container.add_stream("libopus", rate=SAMPLE_RATE)
    layout = "mono" if channels == 1 else "stereo"
    stream.layout = layout
    stream.bit_rate = BITRATE
    frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout=layout)
    frame.sample_rate = SAMPLE_RATE
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": path.name,
        "synthetic": True,
        "not_plaud_capture": True,
        "sample_rate": SAMPLE_RATE,
        "channels": channels,
        "duration_s": DURATION_S,
        "pcm_samples_per_channel": int(SAMPLE_RATE * DURATION_S),
        "encoder": f"PyAV {av.__version__} / libopus",
        "bit_rate": BITRATE,
        "sha256": digest,
        "bytes": path.stat().st_size,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generator": "scripts/generate_r6s2_fixture.py",
        "fixtures": [encode(OUT / "r6s2_16k_mono.ogg", 1), encode(OUT / "r6s2_16k_stereo.ogg", 2)],
    }
    (OUT / "r6s2_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    sys.exit(main())
