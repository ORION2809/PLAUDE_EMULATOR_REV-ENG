"""pyroomacoustics image-source simulation of the device in a shoebox room.

The dry stems are convolved with per-(mic, source) RIRs computed once by
`ShoeBox.compute_rir()`, which gives per-speaker WET stems for free (the mic
mix is their sum) and keeps the DRY stems untouched for the ground truth.

Physics the simulation does model: propagation delay (so the wet audio lags
the dry ground truth by distance/343 s, reported per speaker as
`direct_path_delay_s`), 1/r spreading, early reflections and a reverberant
tail up to the image order. It does NOT model the device's DSP, the VPU,
directional capsules or any firmware processing (all UNKNOWN; see devices.py).

Noise (HARNESS_POLICY):
* "white": independent Gaussian noise per channel, i.e. sensor noise, scaled
  so that the SNR over the speech-active samples equals `snr_db`;
* "musan": a noise recording from a MUSAN-style directory of WAV files,
  placed as a room source at `noise.position_m` (default: a corner) and
  convolved through the same RIR model; SNR measured on channel 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve, resample_poly

from generator._evidence import ROOT
from generator.contract import SAMPLE_RATE
from generator.devices import PRESETS as DEVICE_PRESETS
from generator.devices import mic_positions
from generator.scenario import DEFAULT_MAX_ORDER_CAP, Scenario

SPEED_OF_SOUND = 343.0


def _strip_fdl_padding(rir: np.ndarray) -> np.ndarray:
    """pyroomacoustics builds each RIR with a fractional-delay filter of
    `frac_delay_length` (81) taps and places the filter's centre at the true
    arrival time, so the array carries fdl//2 samples of lead. Removing that
    lead makes the wet direct path arrive at exactly distance / c, which is
    what `direct_path_delay_s` reports (verified to +-2 samples in tests)."""
    import pyroomacoustics as pra

    return rir[int(pra.constants.get("frac_delay_length")) // 2 :]


@dataclass
class RoomResult:
    mic_signals: np.ndarray  # (n_mics, n) float64, speech only
    wet_stems: np.ndarray  # (n_speakers, n_mics, n) float64
    mic_positions_m: np.ndarray  # (3, n_mics)
    speaker_positions_m: list[tuple[float, float, float]]
    direct_path_delay_s: list[float]
    absorption: float
    max_order: int
    rt60_requested_s: float | None
    rt60_measured_s: float | None
    rir_len: int
    noise: np.ndarray | None = None  # (n_mics, n) float64 noise added to mic_signals
    noise_meta: dict[str, object] = field(default_factory=dict)


def auto_speaker_positions(scenario: Scenario) -> list[tuple[float, float, float]]:
    """Ring around the device (HARNESS_POLICY), clamped inside the room."""
    if scenario.speakers.positions_m is not None:
        return [tuple(float(v) for v in p) for p in scenario.speakers.positions_m]
    dx, dy, dz = scenario.room.dims_m
    cx, cy, _ = scenario.device.position_m
    r = scenario.speakers.radius_m
    out = []
    for i in range(scenario.n_speakers):
        ang = 2.0 * np.pi * i / scenario.n_speakers
        x = float(np.clip(cx + r * np.cos(ang), 0.3, dx - 0.3))
        y = float(np.clip(cy + r * np.sin(ang), 0.3, dy - 0.3))
        z = float(np.clip(scenario.speakers.height_m, 0.3, dz - 0.3))
        out.append((round(x, 4), round(y, 4), round(z, 4)))
    return out


def _room_params(scenario: Scenario) -> tuple[float, int]:
    import pyroomacoustics as pra

    room = scenario.room
    if room.absorption is not None:
        absorption = float(room.absorption)
        order = room.max_order if room.max_order is not None else 12
    else:
        absorption, sabine_order = pra.inverse_sabine(float(room.rt60_s), list(room.dims_m))
        order = room.max_order if room.max_order is not None else min(int(sabine_order), DEFAULT_MAX_ORDER_CAP)
    return float(absorption), int(max(0, order))


def _make_room(scenario: Scenario, absorption: float, max_order: int):
    import pyroomacoustics as pra

    return pra.ShoeBox(list(scenario.room.dims_m), fs=SAMPLE_RATE, materials=pra.Material(absorption), max_order=max_order)


def simulate_room(scenario: Scenario, dry_stems: np.ndarray, rng: np.random.Generator | None = None) -> RoomResult:
    """dry_stems: (n_speakers, n) int16 or float. Returns wet per-speaker stems
    and the mic mix (float64, dry full-scale == 1.0)."""
    import pyroomacoustics as pra

    if dry_stems.ndim != 2 or dry_stems.shape[0] != scenario.n_speakers:
        raise ValueError("dry_stems must be (n_speakers, n_samples)")
    n = dry_stems.shape[1]
    dry = dry_stems.astype(np.float64) / (32768.0 if dry_stems.dtype == np.int16 else 1.0)
    preset = DEVICE_PRESETS[scenario.device.preset]
    mics = mic_positions(preset, scenario.device.position_m, scenario.device.yaw_deg)
    positions = auto_speaker_positions(scenario)
    absorption, max_order = _room_params(scenario)
    room = _make_room(scenario, absorption, max_order)
    for p in positions:
        room.add_source(list(p))
    room.add_microphone_array(pra.MicrophoneArray(mics, SAMPLE_RATE))
    room.compute_rir()
    n_mics = mics.shape[1]
    wet = np.zeros((scenario.n_speakers, n_mics, n))
    for s in range(scenario.n_speakers):
        if not np.any(dry[s]):
            continue
        for m in range(n_mics):
            wet[s, m] = fftconvolve(dry[s], _strip_fdl_padding(room.rir[m][s]))[:n]
    mix = wet.sum(axis=0)
    delays = [float(np.linalg.norm(np.asarray(p) - mics[:, 0]) / SPEED_OF_SOUND) for p in positions]
    try:
        rt60 = float(pra.experimental.measure_rt60(room.rir[0][0], fs=SAMPLE_RATE))
    except Exception:  # too short a tail to fit a decay
        rt60 = None
    result = RoomResult(
        mic_signals=mix,
        wet_stems=wet,
        mic_positions_m=mics,
        speaker_positions_m=positions,
        direct_path_delay_s=delays,
        absorption=absorption,
        max_order=max_order,
        rt60_requested_s=scenario.room.rt60_s if scenario.room.absorption is None else None,
        rt60_measured_s=rt60,
        rir_len=int(len(_strip_fdl_padding(room.rir[0][0]))),
    )
    if scenario.noise.kind != "none":
        if rng is None:
            rng = np.random.default_rng(scenario.seed)
        active = np.any(dry_stems != 0, axis=0)
        noise, meta = make_noise(scenario, result, active, rng)
        result.noise = noise
        result.noise_meta = meta
        result.mic_signals = mix + noise
    return result


def _noise_dir(scenario: Scenario) -> Path:
    if scenario.noise.musan_dir:
        return Path(scenario.noise.musan_dir)
    return ROOT / "data" / "corpora" / "musan" / scenario.noise.category


def load_noise_clip(directory: Path, n_samples: int, rng: np.random.Generator) -> tuple[np.ndarray, str]:
    """Pick one WAV deterministically (sorted listing + seeded index), make it
    mono 16 kHz, loop/crop to n_samples. Raises when the directory is absent:
    corpora are never downloaded (scripts/fetch-datasets.sh)."""
    import soundfile as sf

    if not directory.is_dir():
        raise FileNotFoundError(f"noise directory {directory} not present; corpora are not downloaded by the harness")
    files = sorted(p for p in directory.rglob("*.wav"))
    if not files:
        raise FileNotFoundError(f"no .wav files under {directory}")
    chosen = files[int(rng.integers(0, len(files)))]
    data, rate = sf.read(str(chosen), dtype="float64", always_2d=True)
    mono = data.mean(axis=1)
    if rate != SAMPLE_RATE:
        g = int(np.gcd(int(rate), SAMPLE_RATE))
        mono = resample_poly(mono, SAMPLE_RATE // g, int(rate) // g)
    if len(mono) == 0:
        raise ValueError(f"{chosen} is empty")
    reps = n_samples // len(mono) + 1
    clip = np.tile(mono, reps)[:n_samples]
    return clip, str(chosen.relative_to(directory))


def make_noise(scenario: Scenario, result: RoomResult, active: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, dict[str, object]]:
    mix = result.mic_signals
    n_mics, n = mix.shape
    if not np.any(active):
        raise ValueError("cannot set an SNR on a meeting with no speech")
    speech_power = float(np.mean(mix[:, active] ** 2))
    target_noise_power = speech_power / (10.0 ** (scenario.noise.snr_db / 10.0))
    kind = scenario.noise.kind
    if kind == "white":
        noise = rng.standard_normal((n_mics, n)) * np.sqrt(target_noise_power)
        meta = {"kind": "white", "snr_db": scenario.noise.snr_db, "per_channel_independent": True}
    elif kind == "musan":
        directory = _noise_dir(scenario)
        clip, name = load_noise_clip(directory, n, rng)
        dx, dy, dz = scenario.room.dims_m
        pos = scenario.noise.position_m or (0.5, 0.5, min(1.5, dz - 0.3))
        room = _make_room(scenario, result.absorption, result.max_order)
        room.add_source(list(pos))
        import pyroomacoustics as pra

        room.add_microphone_array(pra.MicrophoneArray(result.mic_positions_m, SAMPLE_RATE))
        room.compute_rir()
        wet = np.stack([fftconvolve(clip, _strip_fdl_padding(room.rir[m][0]))[:n] for m in range(n_mics)])
        wet_power = float(np.mean(wet[0] ** 2)) + 1e-30
        noise = wet * np.sqrt(target_noise_power / wet_power)
        meta = {"kind": "musan", "snr_db": scenario.noise.snr_db, "file": name, "position_m": list(pos), "directory": str(directory)}
    else:
        raise ValueError(f"unknown noise kind {kind!r}")
    realised = 10.0 * np.log10(speech_power / (float(np.mean(noise[0] ** 2)) + 1e-30))
    meta["snr_db_realised_ch0"] = round(float(realised), 3)
    return noise, meta


__all__ = ["RoomResult", "SPEED_OF_SOUND", "auto_speaker_positions", "load_noise_clip", "make_noise", "simulate_room"]
