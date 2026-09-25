"""Write a Meeting as a meeting directory (the Layer 2 -> Layer 3 contract).

Layout written:
    meeting.json  ref.rttm  ref.stm
    mix.wav                 device mix at scenario.device.channels (1|2), 16 kHz int16
    mics.wav                raw simulated mic-array channels (optional)
    stems/spkN.wav          DRY per-speaker stems (ground-truth audio)
    activity.npy            sample-accurate activity mask, np.packbits per speaker row
    device/recording.ogg            plain_ogg, mono            (a)
    device/recording_stereo.ogg     plain_ogg, stereo          (a)
    device/recording_raw.opus       plain_raw_opus, bare 80-B packets (b)
    device/recording_g4.bin         plain_raw_opus, g4 page framing   (b)
    device/recording_e2ee.bin       encrypted_unresolved, 512-B PLAUD.AI header (c)

Each device output is classified with emulator/plaudsim/audio.classify_recording
before it is recorded in meeting.json, and the writer raises if the
classification is not the intended one. The WAV header is the emulator's
`wav_header_44` (DIRECT field order from AudioExporter), so the WAV shape
is evidence-backed too; everything else about the layout is HARNESS_POLICY.

Speaker activity in ref.rttm comes from the turn table, never from energy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from generator._evidence import plaud_audio as A
from generator.contract import SAMPLE_RATE, SCHEMA_MEETING, format_rttm, format_stm, validate_meeting, write_json
from generator.devices import PRESETS as DEVICE_PRESETS
from generator.e2ee import SYNTHETIC_KEY, SYNTHETIC_NONCE, seal_recording
from generator.meeting import GENERATOR_NAME, GENERATOR_VERSION, Meeting
from generator.opus import (
    SYNTHETIC_OGG_SERIAL,
    check_sdk_ogg_constraints,
    encode_ogg_opus,
    g4_stream,
    ogg_audio_packets,
    raw_packet_stream,
)

DEVICE_FILES = {
    "ogg_opus": "device/recording.ogg",
    "ogg_opus_stereo": "device/recording_stereo.ogg",
    "raw_opus": "device/recording_raw.opus",
    "g4_raw_opus": "device/recording_g4.bin",
    "e2ee_ogg": "device/recording_e2ee.bin",
}


def wav_bytes(pcm: np.ndarray) -> bytes:
    """(channels, n) int16 -> RIFF/WAVE bytes with the SDK's 44-byte header."""
    x = np.asarray(pcm, dtype=np.int16)
    if x.ndim == 1:
        x = x.reshape(1, -1)
    channels, n = x.shape
    interleaved = np.empty(n * channels, dtype="<i2")
    for c in range(channels):
        interleaved[c::channels] = x[c]
    data = interleaved.tobytes()
    return A.wav_header_44(len(data), channels, SAMPLE_RATE) + data


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a PCM16 WAV back as (channels, n) int16 via soundfile."""
    import soundfile as sf

    data, rate = sf.read(str(path), dtype="int16", always_2d=True)
    return data.T.copy(), int(rate)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expect_shape(name: str, data: bytes, expected: str) -> dict[str, Any]:
    info = A.classify_recording(data)
    if info["shape"] != expected:
        raise AssertionError(f"{name}: classified as {info['shape']!r}, intended {expected!r}")
    return info


def build_device_outputs(meeting: Meeting) -> dict[str, dict[str, Any]]:
    """Encode the device-shaped outputs and classify each one."""
    exp = meeting.scenario.export
    br = exp.opus_bitrate_per_channel
    out: dict[str, dict[str, Any]] = {}

    ogg_mono = encode_ogg_opus(meeting.mono_mix, 1, br, exp.opus_complexity)
    report = check_sdk_ogg_constraints(ogg_mono, 1)
    info = _expect_shape("recording.ogg", ogg_mono, "plain_ogg")
    out["ogg_opus"] = {"bytes_": ogg_mono, "channels": 1, "shape": info["shape"], "ogg": report}

    if exp.include_stereo_ogg:
        ogg_st = encode_ogg_opus(meeting.stereo_mix, 2, br, exp.opus_complexity)
        report_st = check_sdk_ogg_constraints(ogg_st, 2)
        info = _expect_shape("recording_stereo.ogg", ogg_st, "plain_ogg")
        out["ogg_opus_stereo"] = {"bytes_": ogg_st, "channels": 2, "shape": info["shape"], "ogg": report_st}

    packets = ogg_audio_packets(ogg_mono)
    if exp.include_raw_opus:
        raw = raw_packet_stream(packets, 1)
        info = _expect_shape("recording_raw.opus", raw, "plain_raw_opus")
        out["raw_opus"] = {
            "bytes_": raw,
            "channels": 1,
            "shape": info["shape"],
            "packets": len(packets),
            "packet_bytes": A.frame_bytes(1),
            "sdk_duration_ms": (len(raw) // A.frame_bytes(1)) * 20,  # BleFile.calculateOpusDuration
        }
    if exp.include_g4:
        g4, dropped = g4_stream(packets, 1)
        info = _expect_shape("recording_g4.bin", g4, "plain_raw_opus")
        params = A.g4_frame_params(1)
        out["g4_raw_opus"] = {
            "bytes_": g4,
            "channels": 1,
            "shape": info["shape"],
            "pages": (len(g4) - params["lead"]) // params["stride"],
            "packets_dropped_at_tail": dropped,
            "g4_params": params,
        }
    if exp.include_e2ee:
        sealed = seal_recording(ogg_mono, channels=1, duration_s=meeting.duration_s)
        info = _expect_shape("recording_e2ee.bin", sealed, "encrypted_unresolved")
        out["e2ee_ogg"] = {
            "bytes_": sealed,
            "channels": 1,
            "shape": info["shape"],
            "payload_offset": info["payload_offset"],
            "plaintext_container": "ogg",
            "synthetic": True,
            "synthetic_key_hex": SYNTHETIC_KEY.hex(),
            "synthetic_nonce_hex": SYNTHETIC_NONCE.hex(),
            "note": "HARNESS_POLICY synthetic key; keyCipher is a labelled filler, not RSA-2048",
        }
    return out


def build_segments(meeting: Meeting) -> list[dict[str, Any]]:
    segments = []
    for t in meeting.turns:
        segments.append(
            {
                "speaker": f"spk{t.speaker}",
                "start": t.start_s,
                "end": t.end_s,
                "text": t.text,
                "words": [{"w": w.word, "start": w.start_s, "end": w.end_s} for w in t.words],
            }
        )
    segments.sort(key=lambda s: (s["start"], s["speaker"]))
    return segments


def build_meeting_json(meeting: Meeting, device_meta: dict[str, dict[str, Any]], stems: dict[str, str], mics_wav: str | None) -> dict[str, Any]:
    sc = meeting.scenario
    preset = DEVICE_PRESETS[sc.device.preset]
    device_entries = {k: DEVICE_FILES[k] for k in device_meta}
    device_details = {}
    for k, v in device_meta.items():
        d = {kk: vv for kk, vv in v.items() if kk != "bytes_"}
        d["bytes"] = len(v["bytes_"])
        d["sha256"] = _sha256(v["bytes_"])
        d["path"] = DEVICE_FILES[k]
        device_details[k] = d
    return {
        "schema": SCHEMA_MEETING,
        "meeting_id": meeting.meeting_id,
        "sample_rate": SAMPLE_RATE,
        "duration_s": meeting.duration_s,
        "channels": int(meeting.mix.shape[0]),
        "speakers": [
            {
                "id": sid,
                "voice": meeting.voices[i],
                "position_m": [float(v) for v in meeting.room.speaker_positions_m[i]],
                "direct_path_delay_s": round(meeting.room.direct_path_delay_s[i], 6),
            }
            for i, sid in enumerate(meeting.speaker_ids)
        ],
        "segments": build_segments(meeting),
        "audio": {
            "mix_wav": "mix.wav",
            "mics_wav": mics_wav,
            "stems": stems,
            "activity": {"path": "activity.npy", "encoding": "np.packbits(mask, axis=1)", "n_samples": meeting.n_samples},
            "device": device_entries,
            "device_details": device_details,
            "device_primary": "ogg_opus" if meeting.mix.shape[0] == 1 else "ogg_opus_stereo",
            "gain": meeting.gain,
        },
        "device": {
            "preset": preset.name,
            "product": preset.product,
            "channels": int(meeting.mix.shape[0]),
            "position_m": list(sc.device.position_m),
            "yaw_deg": sc.device.yaw_deg,
            "mic_positions_m": [[round(float(v), 6) for v in col] for col in meeting.room.mic_positions_m.T],
            "evidence": preset.evidence,
            "policy": preset.policy,
        },
        "room": {
            "dims_m": list(sc.room.dims_m),
            "absorption": meeting.room.absorption,
            "max_order": meeting.room.max_order,
            "rt60_requested_s": meeting.room.rt60_requested_s,
            "rt60_measured_s": None if meeting.room.rt60_measured_s is None else round(meeting.room.rt60_measured_s, 4),
            "rir_len_samples": meeting.room.rir_len,
        },
        "noise": meeting.room.noise_meta or {"kind": "none"},
        "turn_taking": {
            "model": sc.turn_taking.model,
            "overlap_ratio_target": sc.turn_taking.overlap_ratio,
            "overlap_ratio_realised": round(meeting.overlap_ratio, 6),
            "n_turns": len(meeting.turns),
            "grid_hz": 128,
        },
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
            "seed": sc.seed,
            "scenario": sc.to_dict(),
            "tts_backend": meeting.tts_backend,
            "timing_exact": all(t.timing_exact for t in meeting.turns),
            "ogg_serial": SYNTHETIC_OGG_SERIAL,
        },
        "provenance": {
            "ground_truth": "segments, words and activity.npy are facts of construction (see docs/generator.md)",
            "device_shapes": "classified with emulator/plaudsim/audio.classify_recording; evidence in docs/generator.md",
            "harness_policy": "geometry, voices, room, noise, bitrate settings and file layout are harness choices",
        },
    }


def write_meeting(meeting: Meeting, out_dir: str | Path) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "stems").mkdir(exist_ok=True)
    (out / "device").mkdir(exist_ok=True)

    (out / "mix.wav").write_bytes(wav_bytes(meeting.mix))
    mics_wav = None
    if meeting.scenario.export.write_mics_wav:
        (out / "mics.wav").write_bytes(wav_bytes(meeting.mics))
        mics_wav = "mics.wav"
    stems: dict[str, str] = {}
    for i, sid in enumerate(meeting.speaker_ids):
        rel = f"stems/{sid}.wav"
        (out / rel).write_bytes(wav_bytes(meeting.dry_stems[i : i + 1]))
        stems[sid] = rel
    np.save(out / "activity.npy", np.packbits(meeting.mask, axis=1))

    device_meta = build_device_outputs(meeting)
    for key, meta in device_meta.items():
        (out / DEVICE_FILES[key]).write_bytes(meta["bytes_"])

    payload = build_meeting_json(meeting, device_meta, stems, mics_wav)
    errors = validate_meeting(payload)
    if errors:
        raise AssertionError("generated meeting.json violates the contract: " + "; ".join(errors))
    (out / "ref.rttm").write_text(format_rttm(meeting.meeting_id, payload["segments"]))
    (out / "ref.stm").write_text(format_stm(meeting.meeting_id, payload["segments"]))
    write_json(out / "meeting.json", payload)
    return payload


def load_activity_mask(directory: str | Path) -> np.ndarray:
    """Inverse of the activity.npy encoding -> (n_speakers, n_samples) bool."""
    import json

    d = Path(directory)
    meeting = json.loads((d / "meeting.json").read_text())
    n = int(meeting["audio"]["activity"]["n_samples"])
    packed = np.load(d / meeting["audio"]["activity"]["path"])
    return np.unpackbits(packed, axis=1)[:, :n].astype(bool)


__all__ = [
    "DEVICE_FILES",
    "build_device_outputs",
    "build_meeting_json",
    "build_segments",
    "load_activity_mask",
    "read_wav",
    "wav_bytes",
    "write_meeting",
]
