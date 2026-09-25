"""R7: the recording-format shape space, and the g4 page geometry.

Why this exists. R6-S3/S4 concluded that claim C ("actual Plaud bytes are
Ogg/Opus and decodable") could not be closed without an authentic capture. It
cannot be closed in that form -- but the question was too coarse. The BLE
receive path applies NO transform (PlaudDeviceAgent.syncFile builds its sink
with t7.d() -> o4, a pass-through), so the concatenated DATA payloads ARE the
stored file. That makes the SDK's own ingestion a specification of what a
device is permitted to send, and the SDK's ingestion is a two-test decision
tree with exactly four outcomes.

So the format SPACE is recoverable without a capture. What a capture would
still settle is which of the four shapes a particular firmware emits -- a much
narrower question, and one the emulator no longer has to answer in order to
serve a conforming recording.

Everything asserted here is read out of bytecode. The tests below deliberately
re-derive the SDK's constants from first principles (Ogg's 27-byte page header
plus one lacing byte per packet) so that agreement is evidence rather than
restatement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from plaudsim.audio import (
    OGG_MAGIC_BYTES,
    RECORDING_HEADER_MAGIC,
    RECORDING_HEADER_SIZE,
    build_g4_stream,
    classify_container,
    classify_recording,
    frame_bytes,
    g4_frame_params,
    g4_has_complete_tail,
    g4_payload_spans,
    h4_frame_params,
)

EVIDENCE = Path(__file__).parents[1] / "build/evidence"


# --- the shape space -------------------------------------------------------


def test_plain_ogg_and_plain_raw_opus_are_distinguished_by_the_4_byte_sniff() -> None:
    """AudioExporter compares four literal bytes: 79,103,103,83."""
    assert OGG_MAGIC_BYTES == tuple(b"OggS")
    assert classify_container(b"OggS" + bytes(100)) == "ogg"
    assert classify_container(b"\x01\x02\x03\x04" + bytes(100)) == "raw_opus"
    # The length guard precedes the compare, so a stub is not Ogg.
    assert classify_container(b"Ogg") == "raw_opus"
    assert classify_container(b"") == "raw_opus"


def test_encrypted_recordings_are_detected_by_the_512_byte_header() -> None:
    assert RECORDING_HEADER_SIZE == 512
    assert RECORDING_HEADER_MAGIC == b"PLAUD.AI"

    header = bytearray(512)
    header[0:8] = RECORDING_HEADER_MAGIC
    enc = classify_recording(bytes(header) + b"ciphertext")
    assert enc["encrypted"] is True
    assert enc["payload_offset"] == 512
    # The container is genuinely unknowable before decryption; do not guess.
    assert enc["container"] is None


def test_a_short_file_can_never_be_classified_encrypted() -> None:
    """`if (file.length() < 512) return` guards PlaudEncryptHeader.fromFile,
    so a file carrying the magic but shorter than 512 bytes is NOT encrypted
    as far as the SDK is concerned."""
    short = RECORDING_HEADER_MAGIC + bytes(100)
    assert classify_recording(short)["encrypted"] is False


def test_plain_ogg_file_classifies_end_to_end() -> None:
    out = classify_recording(b"OggS" + bytes(200))
    assert out == {
        "shape": "plain_ogg",
        "encrypted": False,
        "container": "ogg",
        "payload_offset": 0,
    }


def test_an_ogg_file_longer_than_512_bytes_is_not_mistaken_for_encrypted() -> None:
    """Falsification: the header test is the magic, not the length. A 4 kB
    plain Ogg recording must not be routed down the decrypt path."""
    plain = b"OggS" + bytes(4096)
    assert classify_recording(plain)["encrypted"] is False
    assert classify_recording(plain)["container"] == "ogg"


# --- g4 geometry -----------------------------------------------------------


@pytest.mark.parametrize("channels", [1, 2, 4])
def test_g4_page_header_equals_ogg_27_plus_lacing(channels: int) -> None:
    """Independent re-derivation.

    The SDK hard-codes k = 32 (<=2 ch) and 43 (>2 ch). Ogg says a page header
    is 27 bytes plus one lacing byte per packet, and a packet needs an extra
    lacing byte for every full 255 bytes. Compute that from the frame size and
    require it to equal the SDK's literal -- if the reconstruction were wrong,
    these would disagree.
    """
    params = g4_frame_params(channels)
    packets = params["packets_per_page"]
    packet_len = frame_bytes(channels)
    lacing_per_packet = packet_len // 255 + 1
    assert 27 + packets * lacing_per_packet == params["page_header"]


def test_g4_constants_match_the_shipped_bytecode() -> None:
    """Read the operands straight out of javap rather than trusting jadx."""
    javap = EVIDENCE / "javap/com/plaud/sdk/proto/g4.txt"
    if not javap.is_file():
        pytest.skip("no decompiled evidence; run ./scripts/build-evidence.sh")
    text = javap.read_text()
    init = text.split("public com.plaud.sdk.proto.g4(int);")[1].split("public ")[0]
    for literal in ("sipush        512", "sipush        847", "bipush        32",
                    "bipush        43", "sipush        400", "sipush        640",
                    "bipush        80"):
        assert literal in init, f"missing {literal!r} in g4.<init>"

    assert g4_frame_params(1) == {
        "lead": 512, "page_header": 32, "payload": 400,
        "stride": 432, "frame": 80, "packets_per_page": 5,
    }
    assert g4_frame_params(4) == {
        "lead": 847, "page_header": 43, "payload": 2560,
        "stride": 2603, "frame": 320, "packets_per_page": 8,
    }


def test_g4_and_h4_are_different_geometries() -> None:
    """Both de-frame Ogg, but they are NOT interchangeable: g4 carries 5
    packets per page and h4 carries 18. A single 'the Plaud Ogg geometry'
    does not exist, and code that assumes one will mis-frame the other.
    """
    g, h = g4_frame_params(1), h4_frame_params(1)
    assert g["payload"] == 400 and h["p"] == 1440
    assert g["packets_per_page"] == 5
    assert h["p"] // h["i"] == 18
    assert h["n"] == 27 + 18, "h4's 45-byte skip is 27 + 18 lacing bytes"
    assert g["page_header"] != h["n"]


def test_g4_payload_spans_walk_whole_pages_only() -> None:
    p = g4_frame_params(1)
    total = p["lead"] + p["stride"] * 3 + 7  # three whole pages plus a stub
    spans = g4_payload_spans(total, 1)
    assert len(spans) == 3
    assert spans[0] == (p["lead"] + p["page_header"], p["payload"])
    for i, (off, ln) in enumerate(spans):
        assert off == p["lead"] + i * p["stride"] + p["page_header"]
        assert ln == p["payload"]
    assert g4_payload_spans(p["lead"], 1) == []


def test_g4_stream_round_trips_through_the_span_walker() -> None:
    payloads = [bytes([i]) * 400 for i in range(1, 4)]
    stream = build_g4_stream(payloads, channels=1)
    assert len(stream) == 512 + 3 * 432
    spans = g4_payload_spans(len(stream), 1)
    assert [stream[o : o + n] for o, n in spans] == payloads


def test_build_g4_stream_rejects_wrong_sized_pages() -> None:
    with pytest.raises(ValueError):
        build_g4_stream([bytes(399)], channels=1)
    with pytest.raises(ValueError):
        build_g4_stream([], channels=1, lead=bytes(100))


@pytest.mark.parametrize("channels", [1, 2, 4])
def test_g4_has_complete_tail_matches_the_lacing_rule(channels: int) -> None:
    frame = frame_bytes(channels)
    lacing = 2 if channels == 4 else 1
    top = 8 if channels == 4 else 5
    for idx in range(1, top):
        assert g4_has_complete_tail(frame * idx + 27 + idx * lacing, channels)
    assert not g4_has_complete_tail(frame + 1, channels)
    assert not g4_has_complete_tail(0, channels)


def test_g4_rejects_nonsense_channel_counts() -> None:
    for bad in (0, -1):
        with pytest.raises(ValueError):
            g4_frame_params(bad)
