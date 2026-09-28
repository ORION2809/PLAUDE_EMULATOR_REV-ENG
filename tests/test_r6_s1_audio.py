"""R6-S1: audio-path contracts, mechanically recovered from SDK bytecode.

Every layout constant here mirrors javap-observed behavior (see
emulator/plaudsim/audio.py provenance notes). Ogg fixtures are synthetic,
built in-test to the walked parser shape -- no Plaud audio bytes exist in
this repo. Opus *decoding* is specified structurally only: no decoder
library is available in this environment, so nothing here executes a
decode; the input/output shapes are asserted instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from plaudsim.audio import (
    FRAME_SAMPLES,
    OGG_PAGE_HEADER_LEN,
    OPUS_FRAME_BYTES,
    SAMPLE_RATE_HZ,
    chunk_frames,
    classify_ogg_pages,
    frame_bytes,
    h4_frame_params,
    h4_payload_spans,
    has_complete_tail,
    iter_ogg_pages,
    m4_output_len,
    parse_ogg_page,
    parse_opus_head,
    pcm_callback_bytes_per_frame,
    reassemble_page_packets,
    shorts_to_le16_bytes,
    wav_header_44,
)


def make_page(payload: bytes, serial: int = 1, seq: int = 0, seg_size: int = 255) -> bytes:
    """Build a well-formed synthetic Ogg page (lacing all-255 except tail)."""
    lacing = bytearray()
    remaining = len(payload)
    while remaining > 255:
        lacing.append(255)
        remaining -= 255
    lacing.append(remaining)
    out = bytearray(b"OggS")
    out += bytes([0, 0])  # version, header type
    out += bytes(8)  # granule (SDK never reads)
    out += serial.to_bytes(4, "little")
    out += seq.to_bytes(4, "little")
    out += bytes(4)  # crc (SDK never verifies)
    out += bytes([len(lacing)])
    out += bytes(lacing)
    out += payload
    return bytes(out)


def make_opus_head(channels: int = 1, sample_rate: int = 16000, pre_skip: int = 0) -> bytes:
    return (
        b"OpusHead"
        + bytes([1, channels])
        + pre_skip.to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + bytes(4)  # output gain + mapping family + padding to 19
    )


# --- geometry ------------------------------------------------------------


def test_codec_geometry_matches_bytecode() -> None:
    assert SAMPLE_RATE_HZ == 16000
    assert FRAME_SAMPLES == 320
    assert OPUS_FRAME_BYTES == 80
    assert frame_bytes(1) == 80
    assert frame_bytes(2) == 160
    with pytest.raises(ValueError):
        frame_bytes(0)


def test_pcm_callback_bytes_mono_640() -> None:
    # 80B packet -> short[320] -> 640 LE bytes == 20 ms @16 kHz mono.
    assert pcm_callback_bytes_per_frame(1) == 640


# --- Ogg page walk ---------------------------------------------------------


def test_page_parse_round_trip_and_crc_ignored() -> None:
    payload = bytes(range(100))
    page = make_page(payload, serial=7, seq=3)
    parsed = parse_ogg_page(page)
    assert parsed["payload"] == payload
    assert parsed["granule"] is None and parsed["serial"] is None
    assert parsed["sequence"] is None and parsed["crc_validated"] is False
    assert parsed["total_len"] == len(page)
    # Corrupt the CRC bytes: the SDK skips them, so parsing still succeeds.
    bad = bytearray(page)
    bad[22:26] = b"\xde\xad\xbe\xef"
    assert parse_ogg_page(bytes(bad))["payload"] == payload


def test_page_magic_is_strict_no_resync() -> None:
    with pytest.raises(ValueError):
        parse_ogg_page(b"XXXX" + bytes(30))
    with pytest.raises(ValueError):
        parse_ogg_page(b"OggS" + bytes(10))  # truncated header


def test_iter_pages_stops_at_truncation() -> None:
    good = make_page(bytes(50)) + make_page(bytes(60))
    assert sum(1 for _ in iter_ogg_pages(good)) == 2
    assert sum(1 for _ in iter_ogg_pages(good + b"Ogg")) == 2


def test_opus_head_fields() -> None:
    head = make_opus_head(channels=2, sample_rate=48000, pre_skip=312)
    assert parse_opus_head(head) == {"channels": 2, "pre_skip": 312, "sample_rate": 48000}
    with pytest.raises(ValueError):
        parse_opus_head(bytes(10))
    with pytest.raises(ValueError):
        parse_opus_head(b"BadHeadr" + bytes(11))


def test_page_roles_by_count() -> None:
    pages = [
        parse_ogg_page(make_page(make_opus_head())),
        parse_ogg_page(make_page(bytes(300))),  # OpusTags: dropped unread
        parse_ogg_page(make_page(bytes(80))),
    ]
    roles = classify_ogg_pages(pages)
    assert roles["head"] == {"channels": 1, "pre_skip": 0, "sample_rate": 16000}
    assert roles["tags_dropped"] is True
    assert len(roles["audio_pages"]) == 1


def test_trailing_255_lacing_flushed_as_complete() -> None:
    # Ledger-adjudicated against bytecode 432-442: a 255 in the LAST lacing
    # slot is emitted as a complete packet, not carried to the next page.
    payload = bytes(300)
    packets = reassemble_page_packets(bytes([255, 45]), payload)
    assert packets == [payload]
    # Cross-page state is page-local: the next page starts fresh.
    assert reassemble_page_packets(bytes([80]), bytes(80)) == [bytes(80)]


def test_normal_lacing_split() -> None:
    payload = bytes(100) + bytes(50)
    assert reassemble_page_packets(bytes([100, 50]), payload) == [bytes(100), bytes(50)]
    assert reassemble_page_packets(bytes([0, 50]), bytes(50)) == [bytes(50)]


# --- chunking / decoder shape ----------------------------------------------


def test_chunk_frames_multiple_rule() -> None:
    assert chunk_frames(bytes(160), 1) == [bytes(80), bytes(80)]
    assert chunk_frames(bytes(160), 2) == [bytes(160)]
    with pytest.raises(ValueError, match="multiple of 80"):
        chunk_frames(bytes(100), 1)


def test_m4_output_shape() -> None:
    assert m4_output_len(80, 320) == 320  # full short[320] even if fewer produced
    assert m4_output_len(80, 0) == 0  # decode failure -> empty array


# --- h4 wire framing ---------------------------------------------------------


def test_h4_params() -> None:
    assert h4_frame_params(1) == {"l": 512, "n": 45, "p": 1440, "o": 1536, "i": 80, "k": 18}
    assert h4_frame_params(2)["o"] == 2880 + 96


def test_h4_spans_recover_ordered_payload() -> None:
    channels = 1
    chunks = [bytes([i]) * 80 for i in range(36)]  # 2 frames x 18 packets
    blob = bytearray(512)
    for f in range(2):
        blob += bytes(45)  # per-frame skip (n)
        for c in chunks[f * 18 : (f + 1) * 18]:
            blob += c
        blob += bytes(51)  # Ogg page overhead: 45 + 1440 + 51 == o (1536)
    spans = h4_payload_spans(len(blob), channels)
    assert len(spans) == 2
    recovered = []
    for off, length in spans:
        assert length == 1440
        recovered.extend(chunk_frames(bytes(blob[off : off + length]), channels))
    assert recovered == chunks


def test_has_complete_tail() -> None:
    assert has_complete_tail(80 + 27 + 1, 1) is True
    assert has_complete_tail(107, 1) is False
    assert has_complete_tail(320 + 27 + 2, 4) is True
    assert has_complete_tail(320 + 27 + 1, 4) is False


# --- PCM / WAV ---------------------------------------------------------------


def test_shorts_to_le16() -> None:
    assert shorts_to_le16_bytes([1, -1, 0x7FFF, -0x8000]) == bytes(
        [1, 0, 0xFF, 0xFF, 0xFF, 0x7F, 0x00, 0x80]
    )


def test_wav_header_fields() -> None:
    hdr = wav_header_44(640, 1)
    assert len(hdr) == 44
    assert hdr[0:4] == b"RIFF" and hdr[8:12] == b"WAVE" and hdr[12:16] == b"fmt "
    assert hdr[36:40] == b"data"
    assert int.from_bytes(hdr[24:28], "little") == 16000
    assert int.from_bytes(hdr[40:44], "little") == 640
    assert OGG_PAGE_HEADER_LEN == 27


def test_h4_bytecode_spans_do_not_skip_the_lead_and_step_1485_bytes() -> None:
    """Review 2026-09-28: h4.a sets r = l on the first call, so no 512-byte
    lead is skipped, and its drain loop steps n + p = 1485 bytes (mono) while
    remaining() >= o = 1536.  The inferred geometry (h4_payload_spans) differs."""
    from plaudsim.audio import h4_bytecode_payload_spans

    total = 512 + 3 * 1536
    assert h4_bytecode_payload_spans(total, 1) == [(45, 1440), (1530, 1440), (3015, 1440)]
    assert h4_payload_spans(total, 1) == [(557, 1440), (2093, 1440), (3629, 1440)]
    assert h4_bytecode_payload_spans(1535, 1) == []


def test_ogg_page_of_zero_segments_is_27_bytes_and_parses() -> None:
    """Review 2026-09-28: the 27-byte header includes the segment count, so a
    zero-segment page (27 bytes) is a page (OggOpusParser counts it)."""
    from plaudsim.audio import parse_ogg_page

    page = b"OggS" + bytes([0, 4]) + bytes(20) + bytes([0])
    assert len(page) == 27
    parsed = parse_ogg_page(page)
    assert parsed["payload"] == b"" and parsed["total_len"] == 27


def test_invalid_opus_head_keeps_the_defaults_and_the_audio_pages() -> None:
    """Review 2026-09-28: the SDK logs an invalid page 0 and carries on with
    48000/1/0; classify_ogg_pages now does the same instead of raising."""
    from plaudsim.audio import OPUS_HEAD_DEFAULTS

    pages = [{"payload": b"short"}, {"payload": b"OpusTags"}, {"payload": b"audio", "lacing": b"\x05"}]
    roles = classify_ogg_pages(pages)
    assert roles["head"] == OPUS_HEAD_DEFAULTS and roles["head_valid"] is False
    assert "too short" in roles["head_error"] and len(roles["audio_pages"]) == 1
