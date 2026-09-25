"""The exported meeting directory: contract, determinism, device shapes.

Device-shape claims are checked with the emulator's own classifier
(emulator/plaudsim/audio.classify_recording) rather than re-implemented here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from plaudsim import audio as A

from generator.contract import GRID_SAMPLES, SAMPLE_RATE, SCHEMA_MEETING, load_meeting, parse_rttm, parse_stm, validate_meeting
from generator.e2ee import SYNTHETIC_KEY, open_recording
from generator.export import DEVICE_FILES, load_activity_mask, read_wav, wav_bytes, write_meeting
from generator.meeting import generate_meeting
from generator.opus import SYNTHETIC_OGG_SERIAL, decode_ogg_opus, encode_ogg_opus, ogg_audio_packets, verify_ogg_crcs, walk_pages
from generator.scenario import load_scenario
from generator.testing import fixture_meeting
from generator.turns import activity_mask, mask_to_segments


@pytest.fixture(scope="module")
def smoke(tmp_path_factory) -> tuple[Path, dict]:
    return fixture_meeting(tmp_path_factory.mktemp("gen"), "smoke", 3)


def test_directory_layout_and_contract(smoke) -> None:
    d, m = smoke
    assert m["schema"] == SCHEMA_MEETING and validate_meeting(m) == []
    for rel in ("meeting.json", "ref.rttm", "ref.stm", "mix.wav", "mics.wav", "activity.npy", *m["audio"]["stems"].values(), *DEVICE_FILES.values()):
        assert (d / rel).is_file(), rel
    assert m["sample_rate"] == SAMPLE_RATE == 16000
    assert m["duration_s"] == 12.0 and m["channels"] == 1
    assert [s["id"] for s in m["speakers"]] == ["spk0", "spk1"]
    assert load_meeting(d) == m


def test_rttm_and_stm_are_exactly_the_segment_table(smoke) -> None:
    d, m = smoke
    segs = m["segments"]
    rttm = parse_rttm((d / "ref.rttm").read_text())
    stm = parse_stm((d / "ref.stm").read_text())
    assert len(rttm) == len(stm) == len(segs) >= 4
    for seg, r, s in zip(segs, rttm, stm):
        assert r["meeting_id"] == s["meeting_id"] == m["meeting_id"]
        assert (r["speaker"], r["start"], r["end"]) == (seg["speaker"], seg["start"], seg["end"])
        assert (s["speaker"], s["start"], s["end"], s["text"]) == (seg["speaker"], seg["start"], seg["end"], seg["text"])
    starts = [s["start"] for s in segs]
    assert starts == sorted(starts)
    for line in (d / "ref.rttm").read_text().splitlines():
        parts = line.split()
        assert parts[0] == "SPEAKER" and parts[2] == "1" and parts[5:7] == ["<NA>", "<NA>"] and parts[8:10] == ["<NA>", "<NA>"]


def test_boundaries_are_on_the_dyadic_grid_and_words_inside_segments(smoke) -> None:
    _, m = smoke
    for seg in m["segments"]:
        for edge in (seg["start"], seg["end"]):
            samples = edge * SAMPLE_RATE
            assert samples == int(samples) and int(samples) % GRID_SAMPLES == 0
        words = seg["words"]
        assert [w["w"] for w in words] == seg["text"].split()
        assert words[0]["start"] == seg["start"]
        assert words[-1]["end"] <= seg["end"] < words[-1]["end"] + GRID_SAMPLES / SAMPLE_RATE
        for a, b in zip(words, words[1:]):
            assert a["start"] < a["end"] <= b["start"] < b["end"]


def test_same_seed_produces_identical_bytes(tmp_path: Path) -> None:
    sc = load_scenario("smoke", {"duration_s": 6.0, "seed": 21, "device.preset": "note_pro_4mic", "noise.kind": "white", "noise.snr_db": 15})
    write_meeting(generate_meeting(sc), tmp_path / "a")
    write_meeting(generate_meeting(sc), tmp_path / "b")
    files_a = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(tmp_path / "b") for p in (tmp_path / "b").rglob("*") if p.is_file())
    assert files_a == files_b and len(files_a) >= 12
    for rel in files_a:
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes(), rel
    mics, rate = read_wav(tmp_path / "a" / "mics.wav")
    assert mics.shape[0] == 4 and rate == 16000
    # and a different seed changes the audio
    write_meeting(generate_meeting(load_scenario("smoke", {"duration_s": 6.0, "seed": 22, "device.preset": "note_pro_4mic"})), tmp_path / "c")
    assert (tmp_path / "c" / "mix.wav").read_bytes() != (tmp_path / "a" / "mix.wav").read_bytes()


def test_device_outputs_classify_as_intended(smoke) -> None:
    d, m = smoke
    details = m["audio"]["device_details"]
    expected = {"ogg_opus": "plain_ogg", "ogg_opus_stereo": "plain_ogg", "raw_opus": "plain_raw_opus", "g4_raw_opus": "plain_raw_opus", "e2ee_ogg": "encrypted_unresolved"}
    for key, shape in expected.items():
        data = (d / DEVICE_FILES[key]).read_bytes()
        info = A.classify_recording(data)
        assert info["shape"] == shape == details[key]["shape"], key
        assert details[key]["bytes"] == len(data)
    ogg = (d / DEVICE_FILES["ogg_opus"]).read_bytes()
    _, plain = open_recording((d / DEVICE_FILES["e2ee_ogg"]).read_bytes(), SYNTHETIC_KEY)
    assert plain == ogg, "E2EE payload is exactly the mono Ogg"
    assert A.classify_recording(plain)["shape"] == "plain_ogg"


def test_opus_packets_are_exactly_the_sdk_frame_size(smoke) -> None:
    d, m = smoke
    n_frames = int(m["duration_s"] * SAMPLE_RATE) // A.FRAME_SAMPLES
    for key, ch in (("ogg_opus", 1), ("ogg_opus_stereo", 2)):
        packets = ogg_audio_packets((d / DEVICE_FILES[key]).read_bytes())
        assert {len(p) for p in packets} == {A.frame_bytes(ch)} == {80 * ch}
        assert n_frames <= len(packets) <= n_frames + 1, "whole 20 ms frames plus at most the encoder flush"
    raw = (d / DEVICE_FILES["raw_opus"]).read_bytes()
    assert raw == b"".join(ogg_audio_packets((d / DEVICE_FILES["ogg_opus"]).read_bytes()))
    assert len(A.chunk_frames(raw, 1)) == len(raw) // 80
    # BleFile.calculateOpusDuration(byteLen, ch) = (byteLen / (ch*80)) * 20 ms
    assert abs((len(raw) // 80) * 20 - m["duration_s"] * 1000) <= 20
    with pytest.raises(ValueError):
        A.chunk_frames(raw[:-1], 1)


def test_g4_stream_pages_carry_the_raw_packets(smoke) -> None:
    d, m = smoke
    g4 = (d / DEVICE_FILES["g4_raw_opus"]).read_bytes()
    raw = (d / DEVICE_FILES["raw_opus"]).read_bytes()
    p = A.g4_frame_params(1)
    spans = A.g4_payload_spans(len(g4), 1)
    assert len(spans) == m["audio"]["device_details"]["g4_raw_opus"]["pages"] >= 100
    assert b"".join(g4[o : o + n] for o, n in spans) == raw[: len(spans) * p["payload"]]
    assert g4[: p["lead"]] == bytes(p["lead"]), "zero lead is a HARNESS_POLICY filler"
    assert (len(raw) // 80) - len(spans) * p["packets_per_page"] == m["audio"]["device_details"]["g4_raw_opus"]["packets_dropped_at_tail"]


def test_ogg_structure_satisfies_the_sdk_parser_and_is_deterministic(smoke) -> None:
    d, m = smoke
    ogg = (d / DEVICE_FILES["ogg_opus"]).read_bytes()
    pages = walk_pages(ogg)
    roles = A.classify_ogg_pages(list(A.iter_ogg_pages(ogg)))
    assert roles["head"] == {"channels": 1, "pre_skip": 312, "sample_rate": 16000}
    assert roles["tags_dropped"] is True and pages[1].payload.startswith(b"OpusTags")
    assert not pages[2].continued, "OpusTags must fit one page (the SDK discards page 1 unread)"
    for p in pages[2:]:
        assert not p.continued and p.lacing[-1] != 255, "no packet may straddle pages"
    assert {p.serial for p in pages} == {SYNTHETIC_OGG_SERIAL}
    assert verify_ogg_crcs(ogg)
    assert pages[-1].eos


def test_ogg_crc_implementation_agrees_with_libavformat() -> None:
    """Encode WITHOUT the serial rewrite: every stored CRC was computed by
    libavformat, and our independent implementation must reproduce it."""
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    pcm = (np.sin(2 * np.pi * 300 * t) * 12000).astype(np.int16)
    original = encode_ogg_opus(pcm, 1, deterministic=False)
    assert verify_ogg_crcs(original)
    rewritten = encode_ogg_opus(pcm, 1)
    assert rewritten != original or walk_pages(original)[0].serial == SYNTHETIC_OGG_SERIAL
    assert verify_ogg_crcs(rewritten)
    broken = bytearray(rewritten)
    broken[100] ^= 0x01
    assert not verify_ogg_crcs(bytes(broken))


def test_opus_decode_duration_within_one_frame_and_audio_correlates(smoke) -> None:
    from scipy.signal import resample_poly

    d, m = smoke
    decoded, rate = decode_ogg_opus((d / DEVICE_FILES["ogg_opus"]).read_bytes())
    assert rate == 48000 and decoded.shape[0] == 1
    assert abs(decoded.shape[1] / rate - m["duration_s"]) <= 0.020
    heard = resample_poly(decoded[0].astype(np.float64), 1, 3)
    mix, _ = read_wav(d / "mix.wav")
    src = mix[0].astype(np.float64)
    n = min(len(heard), len(src)) - 200

    def corr(lag: int) -> float:
        a, b = heard[100 + lag : 100 + lag + n], src[100 : 100 + n]
        return float(np.dot(a, b) / np.sqrt(np.dot(a, a) * np.dot(b, b)))

    best = max(corr(lag) for lag in range(-24, 25))
    assert best > 0.6, best  # lossy codec: a threshold, never identity
    shifted = np.roll(src, 8000)
    a = heard[100 : 100 + n]
    assert abs(np.dot(a, shifted[100 : 100 + n]) / np.sqrt(np.dot(a, a) * np.dot(shifted[100 : 100 + n], shifted[100 : 100 + n]))) < 0.3
    stereo, _ = decode_ogg_opus((d / DEVICE_FILES["ogg_opus_stereo"]).read_bytes())
    assert stereo.shape[0] == 2 and abs(stereo.shape[1] / 48000 - m["duration_s"]) <= 0.020


def test_wav_files_round_trip_with_the_sdk_header(smoke) -> None:
    d, m = smoke
    raw = (d / "mix.wav").read_bytes()
    mix, rate = read_wav(d / "mix.wav")
    assert rate == 16000 and mix.shape == (1, int(m["duration_s"] * SAMPLE_RATE))
    assert raw[:44] == A.wav_header_44(len(raw) - 44, 1, 16000)
    assert wav_bytes(mix) == raw
    for sid, rel in m["audio"]["stems"].items():
        stem, _ = read_wav(d / rel)
        assert stem.shape == mix.shape
        mask = load_activity_mask(d)[int(sid[3:])]
        assert np.all(stem[0][~mask] == 0), "dry stems are silent outside their speaker's segments"
        for seg in [s for s in m["segments"] if s["speaker"] == sid]:
            a, b = int(seg["start"] * SAMPLE_RATE), int(seg["end"] * SAMPLE_RATE)
            assert np.any(stem[0][a:b] != 0)


def test_activity_mask_round_trips_to_the_segment_table(smoke) -> None:
    d, m = smoke
    mask = load_activity_mask(d)
    n = int(m["duration_s"] * SAMPLE_RATE)
    assert mask.shape == (2, n) and mask.dtype == bool
    rebuilt = [(f"spk{s}", a / SAMPLE_RATE, b / SAMPLE_RATE) for s, a, b in mask_to_segments(mask)]
    assert rebuilt == [(s["speaker"], s["start"], s["end"]) for s in m["segments"]]


def test_meeting_json_records_policy_and_evidence_labels(smoke) -> None:
    _, m = smoke
    assert "HARNESS_POLICY" in m["device"]["policy"]
    assert m["audio"]["device_details"]["e2ee_ogg"]["synthetic"] is True
    assert m["generator"]["name"] == "plaud-harness-generator" and m["generator"]["timing_exact"] is True
    assert m["generator"]["scenario"]["export"]["opus_bitrate_per_channel"] == 32000
    assert m["turn_taking"]["grid_hz"] == 128
