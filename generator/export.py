"""Write a Meeting as a meeting directory (the Layer 2 -> Layer 3 contract).

Layout written:
    meeting.json  ref.rttm  ref.stm
    mix.wav                 device mix at scenario.device.channels (1|2), 16 kHz int16
    mics.wav                raw simulated mic-array channels (optional)
    stems/spkN.wav          DRY per-speaker stems (ground-truth audio)
    activity.npy            sample-accurate activity mask, np.packbits per speaker row
    device/recording.ogg            plain_ogg, mono                         (a)
    device/recording_stereo.ogg     plain_ogg, stereo                       (a)
    device/recording_raw.opus       plain_raw_opus, bare 80-B packets       (b)
    device/recording_g4.bin         g4_framed: g4 page framing, NOT an AudioExporter shape
    device/recording_e2ee.bin       encrypted_ogg: 512-B PLAUD.AI header + ChaCha20(a mono)
    device/recording_raw_e2ee.bin   encrypted_raw_opus: header + ChaCha20(b)

`audio.device_primary` names THE device recording: the plain Ogg at the
meeting's channel count (see contract.device_primary_path).

Each device output is sniffed with emulator/plaudsim/audio.classify_recording
(AudioExporter's two tests) and the writer raises if the sniff is not the
intended one; the encrypted outputs are also decrypted with the synthetic key
and their plaintext container sniffed. meeting.json records, per file,
`shape` (the layout the generator vouches for: one of the SDK's four
RECORDING_SHAPES, or "g4_framed"), `sniffed_shape` (what AudioExporter sees
before decryption) and `sdk_exporter_layout` (whether the bytes are laid out
for one of AudioExporter's four branches). The WAV header is the emulator's
`wav_header_44` (DIRECT field order from AudioExporter), so the WAV shape is
evidence-backed too; everything else about the layout is HARNESS_POLICY.

Speaker activity in ref.rttm comes from the turn table, never from energy.

Rewriting a directory (HARNESS_POLICY): everything is built and validated in
memory first; then the generator-owned entries (GENERATOR_OWNED) of an
existing meeting directory are removed before writing, so no stem, mics.wav
or device file of an earlier meeting survives next to the new meeting.json.
A non-empty directory holding anything else (hidden dotfiles excepted) is
refused with FileExistsError and left untouched.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from generator._evidence import plaud_audio as A
from generator.contract import SAMPLE_RATE, SCHEMA_MEETING, format_rttm, format_stm, validate_meeting, write_json
from generator.devices import PRESETS as DEVICE_PRESETS
from generator.e2ee import SYNTHETIC_KEY, SYNTHETIC_NONCE, open_recording, seal_recording
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
    "e2ee_raw_opus": "device/recording_raw_e2ee.bin",
}

#: Top-level entries of a meeting directory that the generator writes and may
#: therefore remove when it rewrites the directory (stems/ and device/ whole).
GENERATOR_OWNED = ("meeting.json", "ref.rttm", "ref.stm", "mix.wav", "mics.wav", "activity.npy", "stems", "device")

#: RFC 7845: OpusHead pre-skip is counted at 48 kHz whatever the input rate.
OPUS_GRANULE_RATE = 48000


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


def _expect_sniff(name: str, data: bytes, expected: str) -> dict[str, Any]:
    info = A.classify_recording(data)
    if info["shape"] != expected:
        raise AssertionError(f"{name}: AudioExporter's sniff gives {info['shape']!r}, intended {expected!r}")
    return info


def _codec_timing(packets: list[bytes], pre_skip_48k: int, n_samples: int) -> dict[str, Any]:
    """What a decoder of a bare packet stream outputs beyond the audio (GEN-5).

    The Ogg carries pre_skip in OpusHead and every Ogg demuxer trims it; a bare
    packet stream has nowhere to carry it, so decoding it yields the encoder's
    lookahead as extra leading samples, plus the flush packet(s) at the end."""
    lookahead = pre_skip_48k * SAMPLE_RATE // OPUS_GRANULE_RATE
    frames = -(-n_samples // A.FRAME_SAMPLES)
    return {
        "codec_lookahead_samples_16k": int(lookahead),
        "flush_packets": int(len(packets) - frames),
        "decoded_samples_16k": int(len(packets) * A.FRAME_SAMPLES),
        "timing_note": (
            "no pre-skip carrier: a decoder outputs codec_lookahead_samples_16k extra leading samples "
            "(on top of each speaker's direct_path_delay_s) and flush_packets extra 20 ms frames; "
            "decoded[lookahead : lookahead + n_samples] aligns with mix.wav"
        ),
    }


def build_device_outputs(meeting: Meeting) -> dict[str, dict[str, Any]]:
    """Encode the device-shaped outputs, sniff each one and label its layout."""
    exp = meeting.scenario.export
    br = exp.opus_bitrate_per_channel
    n = meeting.n_samples
    out: dict[str, dict[str, Any]] = {}

    def entry(data: bytes, channels: int, shape: str, sniffed: str, **extra: Any) -> dict[str, Any]:
        info = _expect_sniff(shape, data, sniffed)
        return {
            "bytes_": data,
            "channels": channels,
            "shape": shape,
            "sniffed_shape": info["shape"],
            "sdk_exporter_layout": shape in A.RECORDING_SHAPES,
            **extra,
        }

    ogg_mono = encode_ogg_opus(meeting.mono_mix, 1, br, exp.opus_complexity)
    report = check_sdk_ogg_constraints(ogg_mono, 1)
    out["ogg_opus"] = entry(ogg_mono, 1, "plain_ogg", "plain_ogg", ogg=report)

    if exp.include_stereo_ogg:
        ogg_st = encode_ogg_opus(meeting.stereo_mix, 2, br, exp.opus_complexity)
        report_st = check_sdk_ogg_constraints(ogg_st, 2)
        out["ogg_opus_stereo"] = entry(ogg_st, 2, "plain_ogg", "plain_ogg", ogg=report_st)

    packets = ogg_audio_packets(ogg_mono)
    raw = raw_packet_stream(packets, 1)
    timing = _codec_timing(packets, int(report["pre_skip"]), n)
    if exp.include_raw_opus:
        out["raw_opus"] = entry(
            raw, 1, "plain_raw_opus", "plain_raw_opus",
            packets=len(packets),
            packet_bytes=A.frame_bytes(1),
            sdk_duration_ms=(len(raw) // A.frame_bytes(1)) * 20,  # BleFile.calculateOpusDuration
            **timing,
        )
    if exp.include_g4:
        g4, dropped = g4_stream(packets, 1)
        params = A.g4_frame_params(1)
        pages = (len(g4) - params["lead"]) // params["stride"]
        out["g4_raw_opus"] = entry(
            g4, 1, "g4_framed", "plain_raw_opus",
            pages=pages,
            packets_carried=pages * params["packets_per_page"],
            packets_dropped_at_tail=dropped,
            g4_params=params,
            codec_lookahead_samples_16k=timing["codec_lookahead_samples_16k"],
            note=(
                "g4 page framing (zero lead, zero page headers). AudioExporter's 4-byte sniff classifies it "
                "as raw Opus, but its raw branch reads back-to-back 80*ch-byte packets and would mis-read "
                "this layout; only a g4 de-framer (audio.g4_payload_spans) recovers the packets. g4 has no "
                "call sites in the shipped app (emulator/plaudsim/audio.py)."
            ),
        )
    if exp.include_e2ee:
        for key, plain_key, plaintext, container in (("e2ee_ogg", "ogg_opus", ogg_mono, "ogg"), ("e2ee_raw_opus", "raw_opus", raw, "raw_opus")):
            sealed = seal_recording(plaintext, channels=1, duration_s=meeting.duration_s)
            _, opened = open_recording(sealed, SYNTHETIC_KEY)
            if opened != plaintext or A.classify_container(opened) != container:
                raise AssertionError(f"{key}: plaintext does not round-trip to a {container} stream")
            extra: dict[str, Any] = dict(timing) if container == "raw_opus" else {}
            out[key] = entry(
                sealed, 1, f"encrypted_{container}", "encrypted_unresolved",
                payload_offset=A.RECORDING_HEADER_SIZE,
                plaintext_container=container,
                plaintext_of=plain_key,
                synthetic=True,
                synthetic_key_hex=SYNTHETIC_KEY.hex(),
                synthetic_nonce_hex=SYNTHETIC_NONCE.hex(),
                note="HARNESS_POLICY synthetic key; keyCipher is a labelled filler, not RSA-2048",
                **extra,
            )
    return out


def primary_device_key(meeting: Meeting, device_meta: dict[str, dict[str, Any]]) -> str:
    """The plain Ogg at the meeting's channel count (scenario.validate makes
    sure it is emitted)."""
    key = "ogg_opus" if meeting.mix.shape[0] == 1 else "ogg_opus_stereo"
    if key not in device_meta or device_meta[key]["channels"] != meeting.mix.shape[0]:
        raise AssertionError(f"primary device recording {key!r} was not emitted")
    return key


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
            "device_primary": primary_device_key(meeting, device_meta),
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


def _prepare_out_dir(out: Path) -> None:
    """Create `out`, or clear the generator-owned entries of an existing
    meeting directory. Refuses (FileExistsError, nothing touched) when the
    directory holds anything the generator does not own; dotfiles are ignored."""
    if out.exists() and not out.is_dir():
        raise NotADirectoryError(f"{out} exists and is not a directory")
    if not out.exists():
        out.mkdir(parents=True)
        return
    foreign = sorted(p.name for p in out.iterdir() if not p.name.startswith(".") and p.name not in GENERATOR_OWNED)
    if foreign:
        raise FileExistsError(
            f"refusing to write a meeting into {out}: it holds entries the generator does not own "
            f"({', '.join(foreign)}); remove them or choose a fresh directory"
        )
    for name in GENERATOR_OWNED:
        path = out / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()


def write_meeting(meeting: Meeting, out_dir: str | Path) -> dict[str, Any]:
    out = Path(out_dir)
    # 1. build and validate everything in memory: a failure touches no file
    files: dict[str, bytes] = {"mix.wav": wav_bytes(meeting.mix)}
    mics_wav = None
    if meeting.scenario.export.write_mics_wav:
        files["mics.wav"] = wav_bytes(meeting.mics)
        mics_wav = "mics.wav"
    stems: dict[str, str] = {}
    for i, sid in enumerate(meeting.speaker_ids):
        rel = f"stems/{sid}.wav"
        files[rel] = wav_bytes(meeting.dry_stems[i : i + 1])
        stems[sid] = rel
    device_meta = build_device_outputs(meeting)
    for key, meta in device_meta.items():
        files[DEVICE_FILES[key]] = meta["bytes_"]
    payload = build_meeting_json(meeting, device_meta, stems, mics_wav)
    errors = validate_meeting(payload)
    if errors:
        raise AssertionError("generated meeting.json violates the contract: " + "; ".join(errors))

    # 2. clear what an earlier meeting left, then write; meeting.json last
    _prepare_out_dir(out)
    (out / "stems").mkdir()
    (out / "device").mkdir()
    for rel, data in files.items():
        (out / rel).write_bytes(data)
    np.save(out / "activity.npy", np.packbits(meeting.mask, axis=1))
    (out / "ref.rttm").write_text(format_rttm(meeting.meeting_id, payload["segments"]))
    (out / "ref.stm").write_text(format_stm(meeting.meeting_id, payload["segments"]))
    write_json(out / "meeting.json", payload)
    return payload


def unlisted_files(directory: str | Path, meeting: dict[str, Any]) -> list[str]:
    """Generator-owned files present on disk but not listed in meeting.json
    (e.g. left by another meeting): stems/*, device/*, mics.wav."""
    d = Path(directory)
    audio = meeting.get("audio") or {}
    listed = {audio.get("mix_wav"), audio.get("mics_wav"), *(audio.get("stems") or {}).values(), *(audio.get("device") or {}).values()}
    found = [f"{sub}/{p.name}" for sub in ("stems", "device") if (d / sub).is_dir() for p in sorted((d / sub).iterdir()) if not p.name.startswith(".")]
    if (d / "mics.wav").exists():
        found.append("mics.wav")
    return [rel for rel in found if rel not in listed]


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
    "GENERATOR_OWNED",
    "build_device_outputs",
    "build_meeting_json",
    "build_segments",
    "load_activity_mask",
    "primary_device_key",
    "read_wav",
    "unlisted_files",
    "wav_bytes",
    "write_meeting",
]
