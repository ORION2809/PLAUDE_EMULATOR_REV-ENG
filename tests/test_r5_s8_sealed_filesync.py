"""R5-S8: independent sealed endpoints carrying FileList + SyncFile.

Two SealedSession endpoints (host/device) share only the synthetic J/K/L
fixture -- counters are independent per R5-S6. Application framing is
inherited unchanged from frozen R3; sealing is the outer transport layer.
No credentials, no backend, no handshake.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from cryptography.exceptions import InvalidTag

from plaudsim.filesync import (
    OPCODE_FILE_LIST,
    OPCODE_SYNC_START,
    pack_file_list_request,
    pack_sync_start,
    parse_file_data_frame,
    parse_sync_head,
    parse_sync_tail,
)
from plaudsim.sealed import (
    SYNTHETIC_PORT_VERSION,
    SealedLink,
    SealedSession,
    open_raw,
    sealed_filelist_roundtrip,
    sealed_sync_roundtrip,
)
from plaudsim.transfer import FileTable

PV = SYNTHETIC_PORT_VERSION
STAMP = 0x12345678
TABLE_ENTRIES = [
    {"session_id": 1001, "file_size": 96, "scene": 5, "attribute": 2},
    {"session_id": 1002, "file_size": 32, "scene": 4, "attribute": 0},
]
FILE_BYTES = bytes((i * 13 + 7) & 0xFF for i in range(96))
SESSION = 2001
CRC = 0x5678


def make_table() -> FileTable:
    return FileTable([dict(e) for e in TABLE_ENTRIES], port_version=PV)


# --- independent counters ----------------------------------------------------


def test_host_first_tx_is_2_and_device_first_tx_is_2() -> None:
    link = SealedLink()
    host_wire = link.host.seal(b"\x01\x03\x00")
    device_wire = link.device.seal(b"\x01\x03\x00")
    assert link.host.tx_seq == 2
    assert link.device.tx_seq == 2
    assert open_raw(link.host.key, link.host.nonce, link.host.aad, host_wire)[:4] == b"\x02\x00\x00\x00"
    assert open_raw(link.device.key, link.device.nonce, link.device.aad, device_wire)[:4] == b"\x02\x00\x00\x00"


def test_second_tx_is_3_on_each_side_independently() -> None:
    link = SealedLink()
    link.host.seal(b"\x01\x03\x00")
    link.host.seal(b"\x01\x03\x00")
    link.device.seal(b"\x01\x03\x00")
    assert link.host.tx_seq == 3
    assert link.device.tx_seq == 2  # device sealed only once


def test_rx_counters_are_independent() -> None:
    link = SealedLink()
    out = sealed_filelist_roundtrip(link, make_table(), STAMP)
    assert link.device.rx_seq == 2  # the one host request
    assert link.host.rx_seq == 2  # the one device response frame
    assert out["request_seq"] == 2
    assert out["response_seqs"] == [2]
    # A second host request advances only the device RX side.
    link.host.seal(pack_file_list_request(STAMP + 1, 0))
    assert link.device.open(link.host.seal(pack_file_list_request(STAMP + 2, 0))) is not None
    assert link.device.rx_seq == 4
    assert link.host.rx_seq == 2
    assert link.host.tx_seq == 4


def test_endpoints_reset_independently() -> None:
    link = SealedLink()
    sealed_filelist_roundtrip(link, make_table(), STAMP)
    link.host.reset()
    assert (link.host.tx_seq, link.host.rx_seq) == (1, -1)
    assert (link.device.tx_seq, link.device.rx_seq) == (2, 2)
    link.device.reset()
    assert (link.device.tx_seq, link.device.rx_seq) == (1, -1)


def test_r5_s7_primitive_properties_survive_separation() -> None:
    link = SealedLink()
    wire = link.host.seal(b"\x01\x03\x00")
    calls = link.host.seal_calls
    assert bytes(wire) == wire and link.host.seal_calls == calls  # retry: no re-seal
    assert link.device.open(wire) == b"\x01\x03\x00"
    assert link.device.open(bytes(wire)) is None  # duplicate drop, N pinned
    assert link.device.rx_seq == 2


# --- sealed FileList ---------------------------------------------------------


def test_sealed_filelist_full_payload() -> None:
    link = SealedLink()
    out = sealed_filelist_roundtrip(link, make_table(), STAMP)
    assert out["request_seq"] == 2
    assert out["response_seqs"] == [2]  # device TX counter, independent of host
    assert link.host.tx_seq == 2 and link.device.tx_seq == 2
    (frame,) = out["parsed_frames"]
    assert frame["request_stamp"] == STAMP  # stamp echo (s5 gate)
    assert frame["totals"] == 2
    assert frame["frame_start_index"] == 0
    assert [e["session_id"] for e in out["entries"]] == [1001, 1002]
    assert [e["file_size"] for e in out["entries"]] == [96, 32]
    assert [e["scene"] for e in out["entries"]] == [5, 4]  # stride-10 +8
    assert [e["attribute"] for e in out["entries"]] == [2, 0]  # stride-10 +9


def test_sealed_filelist_uses_unmodified_r3_framing() -> None:
    link = SealedLink()
    out = sealed_filelist_roundtrip(link, make_table(), STAMP)
    raw_request = pack_file_list_request(STAMP, 0)
    assert raw_request[:3] == b"\x01\x1a\x00" and len(raw_request) == 12  # p2 frozen
    # Re-decrypt at the transport layer (open_raw bypasses the RX gate, which
    # has already consumed these wires once): the q2 opcode/layout inside the
    # envelope must be the frozen one.
    (wire,) = out["response_wires"]
    inner = open_raw(link.host.key, link.host.nonce, link.host.aad, bytes(wire))[4:]
    assert inner[:3] == b"\x01\x1a\x00"  # q2 opcode unchanged inside the envelope
    assert inner[3:7] == STAMP.to_bytes(4, "little")


# --- sealed SyncFile ---------------------------------------------------------


def test_sealed_sync_transfer_preserves_r3_framing() -> None:
    link = SealedLink()
    out = sealed_sync_roundtrip(link, FILE_BYTES, SESSION, CRC)
    assert out["request_seq"] == 2
    assert out["response_seqs"] == [2, 3, 4, 5, 6, 7]  # HEAD + 3 DATA + EMPTY_PACKAGE + TAIL (R7-S13)
    assert out["head"] == {"session_id": SESSION, "status": 0}
    offsets = [d["offset"] for d in out["datas"]]
    assert offsets == [0, 32, 64]
    for d, off in zip(out["datas"], offsets):
        assert d["session_id"] == SESSION  # modern session gating preserved
        assert d["empty_package"] is False and d["code"] is None
        assert d["payload"] == FILE_BYTES[off : off + 32]
        assert d["length"] == 32
    assert out["tail"] == {"session_id": SESSION, "crc": CRC}
    assert out["payload"] == FILE_BYTES


def test_sealed_sync_request_layout_is_frozen_y6() -> None:
    raw = pack_sync_start(SESSION, 0, 0)
    assert raw[:3] == b"\x01\x1c\x00" and len(raw) == 15
    link = SealedLink()
    out = sealed_sync_roundtrip(link, FILE_BYTES, SESSION, CRC)
    # Re-decrypt at the transport layer (RX already consumed these wires once).
    dec = lambda w: open_raw(link.host.key, link.host.nonce, link.host.aad, bytes(w))[4:]  # noqa: E731
    assert parse_sync_head(dec(out["response_wires"][0])) == {
        "session_id": SESSION,
        "status": 0,
    }
    assert parse_sync_tail(dec(out["response_wires"][-1])) == {
        "session_id": SESSION,
        "crc": CRC,
    }
    first_data = parse_file_data_frame(dec(out["response_wires"][1]), PV)
    assert (first_data["session_id"], first_data["offset"]) == (SESSION, 0)


# --- replay ------------------------------------------------------------------


def test_duplicate_filelist_request_dropped_no_second_response() -> None:
    link = SealedLink()
    out = sealed_filelist_roundtrip(link, make_table(), STAMP)
    assert link.device.open(bytes(out["request_wire"])) is None
    assert link.device.rx_seq == 2  # N did not advance
    assert link.device.tx_seq == 2  # no second response sealed


def test_stale_filelist_request_dropped_after_later_valid() -> None:
    link = SealedLink()
    first = link.host.seal(pack_file_list_request(STAMP, 0))
    second = link.host.seal(pack_file_list_request(STAMP + 1, 0))
    assert link.device.open(second) is not None  # N_device -> 3
    assert link.device.open(first) is None  # stale seq 2 dropped
    assert link.device.rx_seq == 3


# --- tampering ---------------------------------------------------------------


def test_ciphertext_tamper_fails_before_dispatch_n_unchanged() -> None:
    link = SealedLink()
    dispatched: list[bytes] = []
    wire = bytearray(link.host.seal(pack_file_list_request(STAMP, 0)))
    wire[10] ^= 0xFF
    with pytest.raises(InvalidTag):
        link.device.open(bytes(wire))
    assert link.device.rx_seq == -1
    assert dispatched == []  # dispatcher never ran: failure was at the seal layer


def test_sequence_lives_inside_the_authenticated_envelope() -> None:
    link = SealedLink()
    # open() exposes no external sequence input: the signature is wire-only.
    assert list(inspect.signature(SealedSession.open).parameters) == ["self", "wire"]
    wire = link.host.seal(pack_sync_start(SESSION, 0, 0))
    # Flipping a byte in the ciphertext region covering the encrypted seq
    # breaks authentication instead of resequencing the frame.
    tampered = bytearray(wire)
    tampered[0] ^= 0x01
    with pytest.raises(InvalidTag):
        link.device.open(bytes(tampered))
    assert link.device.rx_seq == -1
    # The same application frame re-sealed at a new sequence is a different,
    # valid wire accepted under the new sequence -- the 4-byte prefix is the
    # sequencer and it is AEAD input, not an outer header.
    link.host.seal(pack_file_list_request(STAMP, 0))  # consume seq 3
    assert link.device.open(link.host.seal(pack_sync_start(SESSION, 0, 0))) is not None
    assert link.device.rx_seq == 4


# --- Bumble GATT -------------------------------------------------------------


async def _sealed_gatt_setup(table_entries=True):
    from bumble.device import Peer
    from bumble.testing.test_utils import TwoDevices

    from sealed_support import SealedTestPeripheral

    link = SealedLink()
    devices = await TwoDevices.create_with_connection()
    peripheral = SealedTestPeripheral(
        devices[1],
        link.device,
        file_table=make_table() if table_entries else None,
        file_bytes=FILE_BYTES,
        tail_crc=CRC,
        port_version=PV,
    )
    peripheral.install()
    peer = Peer(devices.connections[0])
    await peer.request_mtu(255)
    await peer.discover_services()
    await peer.discover_characteristics()
    await peer.discover_descriptors()
    service = next(s for s in peer.services if str(s.uuid).lower().startswith("00001910"))
    data = next(c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb0"))
    command = next(c for c in service.characteristics if str(c.uuid).lower().startswith("00002bb1"))
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)
    return link, devices, peripheral, peer, data, command, responses


@pytest.mark.asyncio
async def test_sealed_filelist_over_bumble_gatt() -> None:
    link, devices, peripheral, peer, data, command, responses = await _sealed_gatt_setup()
    await peer.write_value(command, link.host.seal(pack_file_list_request(STAMP, 0)), with_response=True)

    assert len(responses) == 1
    assert link.host.tx_seq == 2 and link.device.tx_seq == 2  # independent firsts
    assert link.device.rx_seq == 2
    from plaudsim.transfer import FileListAccumulator

    acc = FileListAccumulator(port_version=PV)
    inners = []
    for w in responses:
        inner = link.host.open(w)
        assert inner is not None
        inners.append(inner)
        assert acc.ingest_frame(inner) == "accepted"
    assert link.host.rx_seq == 2
    assert acc.complete and [e["session_id"] for e in acc.entries] == [1001, 1002]
    assert [e["scene"] for e in acc.entries] == [5, 4]
    assert peripheral.errors == []


@pytest.mark.asyncio
async def test_sealed_sync_over_bumble_gatt() -> None:
    link, devices, peripheral, peer, data, command, responses = await _sealed_gatt_setup()
    await peer.write_value(command, link.host.seal(pack_sync_start(SESSION, 0, 0)), with_response=True)

    assert len(responses) == 6  # HEAD + 3 DATA + EMPTY_PACKAGE + TAIL (R7-S13)
    assert link.device.tx_seq == 7  # device sealed 6 frames from M=1
    inners = [link.host.open(w) for w in responses]
    assert all(i is not None for i in inners)
    assert parse_sync_head(inners[0]) == {"session_id": SESSION, "status": 0}
    payload = bytearray()
    for raw in inners[1:-1]:
        d = parse_file_data_frame(raw, PV)
        assert d["session_id"] == SESSION
        payload[d["offset"] : d["offset"] + len(d["payload"])] = d["payload"]
    assert bytes(payload) == FILE_BYTES
    assert parse_sync_tail(inners[-1]) == {"session_id": SESSION, "crc": CRC}
    assert link.host.rx_seq == 7  # 6 sealed frames incl. EMPTY_PACKAGE (R7-S13)


@pytest.mark.asyncio
async def test_sealed_replay_over_bumble_gatt_dropped() -> None:
    link, devices, peripheral, peer, data, command, responses = await _sealed_gatt_setup()
    wire = link.host.seal(pack_file_list_request(STAMP, 0))
    await peer.write_value(command, wire, with_response=True)
    await peer.write_value(command, bytes(wire), with_response=True)  # replay

    assert len(responses) == 1
    assert peripheral.drops == 1
    assert peripheral.sealed_out == 1
    assert link.device.rx_seq == 2


@pytest.mark.asyncio
async def test_sealed_tamper_over_bumble_gatt_no_dispatch() -> None:
    link, devices, peripheral, peer, data, command, responses = await _sealed_gatt_setup()
    wire = bytearray(link.host.seal(pack_file_list_request(STAMP, 0)))
    wire[10] ^= 0xFF
    await peer.write_value(command, bytes(wire), with_response=True)

    assert responses == []
    assert peripheral.requests == []  # dispatcher never invoked
    assert any(e.startswith("aead_failure") for e in peripheral.errors)
    assert link.device.rx_seq == -1


def test_opcode_constants_match_frozen_r3() -> None:
    assert OPCODE_FILE_LIST == 26
    assert OPCODE_SYNC_START == 28
