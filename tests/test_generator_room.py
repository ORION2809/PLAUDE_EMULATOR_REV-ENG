"""Room simulation: analytic checks (delay, spreading, SNR), device presets,
and the MUSAN-style noise path exercised with synthetic WAV files."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from generator.contract import SAMPLE_RATE
from generator.devices import PRESETS, device_mix, mic_positions
from generator.room import SPEED_OF_SOUND, load_noise_clip, simulate_room
from generator.scenario import Device, Noise, Room, Scenario, Speakers


def anechoic(n_speakers: int, positions: list[tuple[float, float, float]], device=(2.0, 2.0, 1.0), **noise) -> Scenario:
    return Scenario(
        name="anechoic",
        n_speakers=n_speakers,
        duration_s=2.0,
        room=Room(dims_m=(8.0, 8.0, 4.0), rt60_s=None, absorption=1.0, max_order=0),
        device=Device(preset="mono", position_m=device),
        speakers=Speakers(positions_m=positions),
        noise=Noise(**noise) if noise else Noise(),
    ).validate()


def impulse_stems(n_speakers: int, at: int = 8000) -> np.ndarray:
    stems = np.zeros((n_speakers, 2 * SAMPLE_RATE), dtype=np.int16)
    stems[:, at] = 20000
    return stems


def test_direct_path_delay_and_inverse_distance_law() -> None:
    sc = anechoic(2, [(3.0, 2.0, 1.0), (4.0, 2.0, 1.0)])  # 1 m and 2 m from the mic
    res = simulate_room(sc, impulse_stems(2))
    for s, dist in enumerate((1.0, 2.0)):
        wet = res.wet_stems[s, 0]
        peak = int(np.argmax(np.abs(wet)))
        expected = 8000 + dist / SPEED_OF_SOUND * SAMPLE_RATE
        assert abs(peak - expected) <= 2, (s, peak, expected)
        assert abs(res.direct_path_delay_s[s] - dist / SPEED_OF_SOUND) < 1e-9
    e1 = float(np.sum(res.wet_stems[0, 0] ** 2))
    e2 = float(np.sum(res.wet_stems[1, 0] ** 2))
    assert abs(e1 / e2 - 4.0) < 0.4, "energy falls as 1/r^2"
    assert res.max_order == 0


def test_mix_is_the_sum_of_wet_stems_and_is_deterministic() -> None:
    sc = anechoic(2, [(3.0, 2.0, 1.0), (2.0, 3.5, 1.0)])
    a = simulate_room(sc, impulse_stems(2))
    b = simulate_room(sc, impulse_stems(2))
    assert np.array_equal(a.mic_signals, b.mic_signals)
    assert np.allclose(a.mic_signals, a.wet_stems.sum(axis=0))


def test_white_noise_hits_the_requested_snr() -> None:
    rng = np.random.default_rng(0)
    stems = np.zeros((1, 2 * SAMPLE_RATE), dtype=np.int16)
    stems[0, 4000:28000] = (rng.standard_normal(24000) * 8000).astype(np.int16)
    sc = anechoic(1, [(3.0, 2.0, 1.0)], kind="white", snr_db=12.0)
    res = simulate_room(sc, stems, np.random.default_rng(1))
    active = stems[0] != 0
    speech = res.mic_signals - res.noise
    snr = 10 * np.log10(np.mean(speech[:, active] ** 2) / np.mean(res.noise ** 2))
    assert abs(snr - 12.0) < 0.3
    assert abs(res.noise_meta["snr_db_realised_ch0"] - 12.0) < 0.3


def test_musan_style_directory_is_loaded_resampled_and_spatialised(tmp_path: Path) -> None:
    import soundfile as sf

    rng = np.random.default_rng(3)
    d = tmp_path / "musan" / "noise"
    d.mkdir(parents=True)
    sf.write(str(d / "a.wav"), rng.standard_normal(8000) * 0.1, 8000)  # 1 s at 8 kHz
    sf.write(str(d / "b.wav"), rng.standard_normal((16000, 2)) * 0.1, 16000)  # stereo 16 kHz
    clip, name = load_noise_clip(d, 40000, np.random.default_rng(0))
    assert clip.shape == (40000,) and name in ("a.wav", "b.wav")
    clip2, name2 = load_noise_clip(d, 40000, np.random.default_rng(0))
    assert np.array_equal(clip, clip2) and name == name2

    stems = np.zeros((1, 2 * SAMPLE_RATE), dtype=np.int16)
    stems[0, 4000:28000] = (rng.standard_normal(24000) * 8000).astype(np.int16)
    sc = anechoic(1, [(3.0, 2.0, 1.0)], kind="musan", snr_db=6.0, musan_dir=str(d))
    res = simulate_room(sc, stems, np.random.default_rng(1))
    assert res.noise.shape == res.mic_signals.shape
    assert abs(res.noise_meta["snr_db_realised_ch0"] - 6.0) < 0.5
    assert res.noise_meta["kind"] == "musan" and res.noise_meta["file"] in ("a.wav", "b.wav")
    again = simulate_room(sc, stems, np.random.default_rng(1))
    assert again.noise_meta["file"] == res.noise_meta["file"] and np.array_equal(again.noise, res.noise)

    with pytest.raises(FileNotFoundError):
        load_noise_clip(tmp_path / "absent", 100, np.random.default_rng(0))


def test_device_presets_match_the_evidenced_mic_counts() -> None:
    assert PRESETS["note_pro_4mic"].n_mics == 4  # 4 MEMS (+1 VPU, not simulated)
    assert PRESETS["notepin_s_2mic"].n_mics == 2  # 2 MEMS
    assert PRESETS["mono"].n_mics == 1
    for p in PRESETS.values():
        assert "HARNESS_POLICY" in p.policy
        left, right = p.stereo_groups
        assert set(left) | set(right) <= set(range(p.n_mics))


def test_mic_positions_rotate_and_translate() -> None:
    p = PRESETS["notepin_s_2mic"]
    r = mic_positions(p, (1.0, 2.0, 0.5), yaw_deg=90.0)
    assert r.shape == (3, 2)
    assert np.allclose(r[:, 0], [1.0, 2.0 - 0.015, 0.5], atol=1e-9)
    assert np.allclose(r[:, 1], [1.0, 2.0 + 0.015, 0.5], atol=1e-9)


def test_device_mix_reduction_rules() -> None:
    p = PRESETS["note_pro_4mic"]
    mics = np.arange(4.0).reshape(4, 1) * np.ones((4, 10))
    assert np.allclose(device_mix(mics, p, 1)[0], 1.5)
    st = device_mix(mics, p, 2)
    assert np.allclose(st[0], (0 + 3) / 2) and np.allclose(st[1], (1 + 2) / 2)
    with pytest.raises(ValueError):
        device_mix(mics, p, 3)


def test_reverberant_room_realises_the_requested_rt60_roughly() -> None:
    sc = Scenario(name="rev", n_speakers=1, duration_s=2.0, room=Room(dims_m=(6.0, 5.0, 3.0), rt60_s=0.3)).validate()
    res = simulate_room(sc, impulse_stems(1))
    assert res.max_order == 24  # DEFAULT_MAX_ORDER_CAP
    assert res.rt60_measured_s is not None and 0.2 < res.rt60_measured_s < 0.45
    assert res.rir_len > 2000


# --- GEN-2: the requested SNR is the SNR of the recording, not of one capsule ------
#
# Averaging k capsules (Note Pro mono: 4, stereo: 2 per side; NotePin mono: 2)
# lowers independent noise by 10*log10(k) while the correlated speech stays, so a
# per-capsule SNR made mix.wav and every device/* file 3-6 dB cleaner than
# meeting.json said. The reference point is now the device mix at
# scenario.device.channels, i.e. mix.wav, and the other mixes are reported.


@pytest.mark.parametrize(
    "preset, channels",
    [("note_pro_4mic", 1), ("note_pro_4mic", 2), ("notepin_s_2mic", 1), ("notepin_s_2mic", 2)],
)
def test_requested_snr_is_measured_in_mix_wav(preset: str, channels: int, tmp_path: Path) -> None:
    from generator.export import read_wav, write_meeting
    from generator.meeting import generate_meeting
    from generator.scenario import load_scenario

    sc = load_scenario("smoke", {
        "duration_s": 5.0, "seed": 4, "device.preset": preset, "device.channels": channels,
        "noise.kind": "white", "noise.snr_db": 15.0,
        "export.include_raw_opus": False, "export.include_g4": False, "export.include_e2ee": False,
    })
    meeting = generate_meeting(sc)
    payload = write_meeting(meeting, tmp_path / "m")
    mix, rate = read_wav(tmp_path / "m" / payload["audio"]["mix_wav"])
    assert rate == SAMPLE_RATE and mix.shape[0] == channels
    # mix.wav = int16(gain * (device_mix(speech) + device_mix(noise))); remove the known noise
    noise = device_mix(meeting.room.noise, PRESETS[preset], channels) * meeting.gain
    speech = mix.astype(np.float64) / 32767.0 - noise
    active = np.any(meeting.dry_stems != 0, axis=0)
    snr = 10 * np.log10(np.mean(speech[:, active] ** 2) / np.mean(noise ** 2))
    assert abs(snr - 15.0) < 0.1, snr
    realised = payload["noise"]["snr_db_realised"]
    assert payload["noise"]["snr_reference"] == "mix"
    assert abs(realised["mix"] - 15.0) < 0.01
    assert abs(realised["mono_mix" if channels == 1 else "stereo_mix"] - realised["mix"]) < 1e-9
    # averaging capsules is what made the old per-capsule figure wrong; it is still reported
    if PRESETS[preset].n_mics > channels:
        assert realised["capsules"] < realised["mix"] - 2.0
