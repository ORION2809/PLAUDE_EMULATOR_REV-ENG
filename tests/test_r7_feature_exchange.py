"""R7: the opcode-138 capability exchange, and the scope of the AEAD selector.

Two findings are pinned here.

1. Opcode 138 (DEV_NEW_FEATURE_REQ) is a device-initiated PUSH that the
   emulator previously did not speak. On receipt the SDK forwards the payload
   to `listener.deviceNewFeature`, derives its Wi-Fi AEAD selector from bit 3,
   and replies with its own FeatureReq (`l1`, `01 8A 00 FF`). It is a genuine
   round-trip, so adding it extends how much of the SDK's lifecycle the
   harness can exercise locally.

2. That selector does NOT reach the BLE transport. `z.O` is readable only via
   `z.w()`, and every call site of `z.w()` is on the Wi-Fi path. The BLE
   sealed path calls the four-argument `q5.d`/`q5.b` helpers, which take no
   algorithm parameter. The ledger previously recorded AES-GCM as "an
   alternate under the same key material" without saying when it applies;
   these tests fix the scope, and they do it by reading javap so a future
   change to the emulator cannot quietly widen it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from support import connect_like_the_sdk

from plaudsim.profile import (
    FEATURE_BIT_WIFI_AES,
    HOST_FEATURE_BITMAP,
    OPCODE_NEW_FEATURE,
    PlaudPeripheral,
    encode_feature_frame,
    parse_feature_request,
)

EVIDENCE = Path(__file__).parents[1] / "build/evidence"


# --- codec -----------------------------------------------------------------


def test_feature_frame_layout() -> None:
    assert OPCODE_NEW_FEATURE == 138
    assert encode_feature_frame(0xFF) == b"\x01\x8a\x00\xff"
    assert len(encode_feature_frame(0)) == 4
    with pytest.raises(ValueError):
        encode_feature_frame(0x100)


def test_host_feature_request_is_all_bits_set() -> None:
    """`l1.enPkg` folds 1|2|4|8|16|32|64|128 into one byte, so the host
    unconditionally claims every capability."""
    assert HOST_FEATURE_BITMAP == 0xFF
    assert encode_feature_frame(HOST_FEATURE_BITMAP) == b"\x01\x8a\x00\xff"


def test_wifi_aes_bit_is_bit_3() -> None:
    assert FEATURE_BIT_WIFI_AES == 0x08
    assert parse_feature_request(encode_feature_frame(0x08))["wifi_aes"] is True
    assert parse_feature_request(encode_feature_frame(0xF7))["wifi_aes"] is False
    assert parse_feature_request(encode_feature_frame(0xFF))["wifi_aes"] is True
    # Bit 3 only -- neighbouring bits must not be mistaken for it.
    for bit in (0x04, 0x10):
        assert parse_feature_request(encode_feature_frame(bit))["wifi_aes"] is False


def test_payloadless_feature_frame_is_tolerated_not_rejected() -> None:
    """The SDK guards `bArr.length > 3` and merely logs when the payload is
    absent; it does not treat it as an error."""
    out = parse_feature_request(b"\x01\x8a\x00")
    assert out == {"bitmap": None, "payload": b"", "wifi_aes": False}


@pytest.mark.parametrize(
    "bad", [b"\x01\x03\x00\xff", b"\x02\x8a\x00\xff", b"\x01\x8a", b""]
)
def test_feature_parser_rejects_other_frames(bad: bytes) -> None:
    with pytest.raises(ValueError):
        parse_feature_request(bad)


# --- over the wire ---------------------------------------------------------


@pytest.mark.asyncio
async def test_device_push_then_host_feature_request_round_trip() -> None:
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, feature_bitmap=FEATURE_BIT_WIFI_AES)
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    # Device announces its capabilities unsolicited.
    await peripheral.push_new_feature()
    await asyncio.sleep(0.05)
    assert responses[-1] == encode_feature_frame(FEATURE_BIT_WIFI_AES)
    assert parse_feature_request(responses[-1])["wifi_aes"] is True

    # The host replies with its own FeatureReq; the device acknowledges.
    await peer.write_value(command, encode_feature_frame(HOST_FEATURE_BITMAP),
                           with_response=True)
    assert parse_feature_request(responses[-1])["bitmap"] == FEATURE_BIT_WIFI_AES

    directions = [e["direction"] for e in peripheral.feature_log]
    assert directions == ["device->host", "host->device"]
    assert peripheral.feature_log[1]["bitmap"] == 0xFF


@pytest.mark.asyncio
async def test_feature_push_does_not_disturb_the_lifecycle() -> None:
    """A capability push is not a bind, and must not advance anything."""
    from plaudsim.profile import PlaudLifecycle

    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device)
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    before = list(peripheral.lifecycle_log)
    await peripheral.push_new_feature(0xFF)
    await asyncio.sleep(0.05)
    await peer.write_value(command, encode_feature_frame(0xFF), with_response=True)
    assert peripheral.lifecycle_log == before
    assert peripheral.lifecycle == PlaudLifecycle.CONNECTED


# --- scope of the AEAD selector (falsification guard) ----------------------


def test_the_wifi_aes_bit_never_reaches_the_ble_transport() -> None:
    """z.O is read only through z.w(), and every z.w() call site is Wi-Fi.

    If a future change made the BLE path algorithm-selectable, this fails --
    which is the point: the emulator's unconditional ChaCha20-Poly1305 on BLE
    is a recovered fact, not a simplification.
    """
    allt = EVIDENCE / "javap/ALL.txt"
    if not allt.is_file():
        pytest.skip("no decompiled evidence; run ./scripts/build-evidence.sh")

    # The BLE transport is class z plus its inner classes (z$c holds the
    # GATT callback, which is where inbound frames are opened).
    ble = "".join(
        p.read_text(errors="replace")
        for p in sorted((EVIDENCE / "javap/com/plaud/sdk/proto").glob("z.txt"))
        + sorted((EVIDENCE / "javap/com/plaud/sdk/proto").glob("z$*.txt"))
    )

    selectable = "Method com/plaud/sdk/proto/q5.a:([B[B[B[BZ)[B"
    assert allt.read_text(errors="replace").count(selectable) >= 1, (
        "the algorithm-selectable helper should exist somewhere (Wi-Fi)"
    )
    assert selectable not in ble, "the BLE transport must not select its AEAD"
    assert "Method com/plaud/sdk/proto/z.w:()Z" not in ble

    # and the BLE path does use the fixed-algorithm ChaCha20-Poly1305 helpers
    assert "Method com/plaud/sdk/proto/q5.d:([B[B[B[B)[B" in ble, "seal"
    assert "Method com/plaud/sdk/proto/q5.b:([B[B[B[B)[B" in ble, "open"


def test_ble_sealed_session_has_no_algorithm_switch() -> None:
    """The emulator's sealed session must expose no AEAD choice at all."""
    from plaudsim import sealed

    source = Path(sealed.__file__).read_text()
    assert "AES" not in source.upper().replace("AEAD", ""), (
        "sealed.py must not offer AES-GCM: that is a Wi-Fi-only alternative"
    )
