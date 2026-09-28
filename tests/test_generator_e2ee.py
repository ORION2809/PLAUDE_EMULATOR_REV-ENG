"""The 512-byte PLAUD.AI header and the raw-ChaCha20 payload shape.

Layout evidence: docs/protocol-ledger.md section 8 and the constructor at
build/evidence/javap/ALL.txt:125016-125147. The cipher primitive is pinned
to the RFC 7539 section 2.4.2 test vector (an analytically known answer),
which also proves the (counter, nonce) layout the SDK's ChaCha7539Engine uses.
"""

from __future__ import annotations

import sys
from pathlib import Path
from struct import unpack_from

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from plaudsim.audio import classify_container, classify_recording

from generator.e2ee import (
    HEADER_SIZE,
    MAGIC,
    SYNTHETIC_KEY,
    SYNTHETIC_KEY_CIPHER,
    SYNTHETIC_NONCE,
    PlaudEncryptHeader,
    chacha20_raw,
    open_recording,
    seal_recording,
)

RFC7539_KEY = bytes(range(32))
RFC7539_NONCE = bytes.fromhex("000000000000004a00000000")
RFC7539_PLAINTEXT = (
    b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it."
)
RFC7539_CIPHERTEXT = bytes.fromhex(
    "6e2e359a2568f98041ba0728dd0d6981e97e7aec1d4360c20a27afccfd9fae0b"
    "f91b65c5524733ab8f593dabcd62b3571639d624e65152ab8f530c359f0861d8"
    "07ca0dbf500d6a6156a38e088a22b65e52bc514d16ccf806818ce91ab7793736"
    "5af90bbf74a35be6b40b8eedf2785e42874d"
)


def test_chacha20_matches_the_rfc_7539_test_vector() -> None:
    assert chacha20_raw(RFC7539_KEY, RFC7539_NONCE, RFC7539_PLAINTEXT, counter=1) == RFC7539_CIPHERTEXT
    # decryption is the same keystream XOR
    assert chacha20_raw(RFC7539_KEY, RFC7539_NONCE, RFC7539_CIPHERTEXT, counter=1) == RFC7539_PLAINTEXT
    # a different counter is a different keystream (so counter 0 vs 1 matters)
    assert chacha20_raw(RFC7539_KEY, RFC7539_NONCE, RFC7539_PLAINTEXT, counter=0) != RFC7539_CIPHERTEXT


def test_no_poly1305_means_malleable_and_keystream_reuse_is_visible() -> None:
    """Ledger section 8: raw ChaCha20, same key/nonce/counter for every segment.
    Consequence, by construction: c1 XOR c2 == p1 XOR p2."""
    p1 = bytes(range(64))
    p2 = bytes(64 - i for i in range(64))
    c1 = chacha20_raw(SYNTHETIC_KEY, SYNTHETIC_NONCE, p1)
    c2 = chacha20_raw(SYNTHETIC_KEY, SYNTHETIC_NONCE, p2)
    assert bytes(a ^ b for a, b in zip(c1, c2)) == bytes(a ^ b for a, b in zip(p1, p2))
    assert len(c1) == len(p1), "no tag is appended"


def test_key_and_nonce_length_gates() -> None:
    with pytest.raises(ValueError):
        chacha20_raw(b"k" * 31, SYNTHETIC_NONCE, b"x")  # AudioDecryptor requires exactly 32 bytes
    with pytest.raises(ValueError):
        chacha20_raw(SYNTHETIC_KEY, b"n" * 11, b"x")


def test_header_layout_matches_the_ledger_offsets() -> None:
    h = PlaudEncryptHeader(version=7, crc=0xDEADBEEF, user_id=b"user-42", file_type=3, channel=2, encrypt_type=5, duration_s=123, counter=9, segment=0, nonce=b"N" * 12)
    raw = h.pack()
    assert len(raw) == HEADER_SIZE == 512
    assert raw[0:8] == MAGIC == b"PLAUD.AI"
    assert unpack_from("<H", raw, 8)[0] == 7
    assert unpack_from("<H", raw, 10)[0] == 512
    assert unpack_from("<I", raw, 12)[0] == 0xDEADBEEF
    assert raw[16:48] == b"user-42".ljust(32, b"\x00")
    assert unpack_from("<HHH", raw, 48) == (3, 2, 5)
    assert unpack_from("<I", raw, 54)[0] == 123
    assert raw[58:128] == bytes(70)
    assert unpack_from("<I", raw, 128)[0] == 9
    assert raw[132:144] == b"N" * 12
    assert unpack_from("<I", raw, 144)[0] == 0
    assert raw[148:256] == bytes(108)
    assert raw[256:512] == SYNTHETIC_KEY_CIPHER
    assert SYNTHETIC_KEY_CIPHER.startswith(b"SYNTHETIC-KEYCIPHER-NOT-RSA-2048")
    assert PlaudEncryptHeader.unpack(raw) == h
    with pytest.raises(ValueError, match="at least 512 bytes"):
        PlaudEncryptHeader.unpack(raw[:511])


def test_sealed_recording_is_classified_like_the_sdk_and_opens_with_the_key() -> None:
    payload = b"OggS" + bytes(range(256)) * 4
    sealed = seal_recording(payload, channels=1, duration_s=12.5)
    info = classify_recording(sealed)
    assert info == {"shape": "encrypted_unresolved", "encrypted": True, "container": None, "payload_offset": 512}
    header, plain = open_recording(sealed, SYNTHETIC_KEY)
    assert plain == payload and classify_container(plain) == "ogg"
    assert header.channel == 1 and header.duration_s == 12 and header.segment == 0
    # wrong key: still "encrypted" on the outside, garbage inside (no tag to say so)
    _, wrong = open_recording(sealed, b"W" * 32)
    assert wrong != payload and classify_container(wrong) == "raw_opus"
    with pytest.raises(ValueError):
        open_recording(payload, SYNTHETIC_KEY)


def test_header_alone_is_still_512_bytes_and_a_truncated_one_is_not_encrypted() -> None:
    assert len(seal_recording(b"", channels=2, duration_s=0)) == 512
    assert classify_recording(seal_recording(b"", 1, 0))["encrypted"] is True
    assert classify_recording(seal_recording(b"", 1, 0)[:511])["encrypted"] is False  # the SDK's length guard


def test_protocol_ledger_citations_resolve_to_real_section_headings() -> None:
    """GEN-7: ledger citations by line number drifted into section 7 (Wi-Fi).
    Citations are by section and heading now, and every one must resolve."""
    import re

    root = Path(__file__).parents[1]
    ledger = (root / "docs" / "protocol-ledger.md").read_text(encoding="utf-8").splitlines()
    sections: dict[str, list[str]] = {}
    current = None
    for line in ledger:
        m = re.match(r"^## (\d+)\. (.*)$", line)
        if m:
            current = m.group(1)
            sections[current] = [m.group(2)]  # the section title itself, then its ### headings
        elif current and line.startswith("### "):
            sections[current].append(line[4:])
    sources = [*sorted((root / "generator").rglob("*.py")), root / "docs" / "generator.md"]
    cited = 0
    for path in sources:
        text = path.read_text(encoding="utf-8")
        stale = re.findall(r"(?:ledger|protocol-ledger\.md)[^\n]{0,60}?(?:§|section )\d+[^\n]{0,40}?lines? \d{3,}", text)
        assert stale == [], f"{path.name}: line-number ledger citations drift: {stale}"
        for sec, heading in re.findall(r"(?:§|section )(\d+) \"([^\"]+)\"", text):
            heading = " ".join(heading.split())  # citations may wrap inside docstrings
            cited += 1
            assert sec in sections, f"{path.name}: no section {sec} in the ledger"
            assert any(h.startswith(heading) for h in sections[sec]), f"{path.name}: §{sec} has no heading {heading!r}"
    assert cited >= 5
