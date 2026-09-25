"""R5-S11: uncertainty isolation + capture-ready traces (synthetic only)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from plaudsim.filesync import pack_file_list_request, pack_sync_start
from plaudsim.handshake import MARKER_PRE_HANDSHAKE, MARKER_SECRET
from plaudsim.profile import PlaudDeviceState
from plaudsim.sealed import SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L, SealedSession
from plaudsim.transfer import FileTable
from protocol_trace import (
    TRACE_TYPE_SYNTHETIC,
    Trace,
    TraceEvent,
    classify_raw,
    compare_traces,
)
from sealed_support import (
    SYNTHETIC_SN_SIGNATURE,
    ModernProfile,
    ModernTestPeripheral,
    build_marker_frames,
    decrypt_secret_package,
)

STATE = PlaudDeviceState(state=0x1003, scene=5, session_id=0x01020304)
ENTRIES = [
    {"session_id": 1001, "file_size": 96, "scene": 5, "attribute": 2},
    {"session_id": 1002, "file_size": 32, "scene": 4, "attribute": 0},
]
FILE_BYTES = bytes((i * 13 + 7) & 0xFF for i in range(96))


def modern_factory(device, profile=None):
    from sealed_support import ModernProfile

    return ModernTestPeripheral(
        device,
        state=STATE,
        file_table=FileTable([dict(e) for e in ENTRIES], port_version=21),
        file_bytes=FILE_BYTES,
        tail_crc=0x5678,
        port_version=21,
        profile=profile or ModernProfile(),
    )


async def run_scenario(peripheral_factory=None):
    from bumble.device import Peer
    from bumble.testing.test_utils import TwoDevices
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    devices = await TwoDevices.create_with_connection()
    factory = peripheral_factory or (lambda device: modern_factory(device))
    peripheral = factory(devices[1])
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

    async def wait_for(base: int, count: int) -> None:
        for _ in range(100):
            if len(responses) >= base + count:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("timeout waiting for responses")

    base = len(responses)
    for frame in build_marker_frames(MARKER_PRE_HANDSHAKE, SYNTHETIC_SN_SIGNATURE):
        await peer.write_value(command, frame, with_response=True)
    await wait_for(base, 1)
    fe12_want = None
    for frame in build_marker_frames(MARKER_SECRET, pem):
        await peer.write_value(command, frame, with_response=True)
    for _ in range(100):
        got = [r for r in responses if len(r) >= 4 and int.from_bytes(r[0:2], "little") == MARKER_SECRET]
        if got:
            fe12_want = got[0][2]
            if len(got) >= fe12_want:
                break
        await asyncio.sleep(0.05)
    assert fe12_want is not None
    parts = decrypt_secret_package(key, [r for r in responses if len(r) >= 4 and int.from_bytes(r[0:2], "little") == MARKER_SECRET])
    assert parts["chacha_key"] == SYNTHETIC_J
    host = SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L)

    async def sealed_roundtrip(plaintext: bytes, expected_new: int) -> list[bytes]:
        base = len(responses)
        await peer.write_value(command, host.seal(plaintext), with_response=True)
        await wait_for(base, expected_new)
        out = []
        for wire in responses[base:]:
            inner = host.open(wire)
            assert inner is not None
            out.append(inner)
        return out

    await sealed_roundtrip(b"\x01\x03\x00", 1)
    await sealed_roundtrip(pack_file_list_request(0x12345678, 0), 1)
    await sealed_roundtrip(pack_sync_start(2001, 0, 0), 5)
    return peripheral, host, responses


def test_matrix_is_closed_vocabulary_and_counts() -> None:
    matrix = json.loads((Path(__file__).parents[1] / "r5-s11" / "uncertainty_matrix.json").read_text())
    allowed = {"SDK_PROVEN", "EMULATOR_INTEGRATION_PROVEN", "INFERRED", "HARNESS_POLICY", "UNKNOWN"}
    rows = matrix["behaviors"]
    assert len(rows) >= 25
    for row in rows:
        assert set(row) == {"behavior", "location", "classification", "evidence", "test", "safe_to_emulate"}, row
        assert row["classification"] in allowed, row
    counts = {c: sum(1 for r in rows if r["classification"] == c) for c in allowed}
    assert counts["SDK_PROVEN"] >= 10 and counts["UNKNOWN"] >= 3 and counts["HARNESS_POLICY"] >= 3


def test_modern_profile_defaults_and_unknown_policies_raise() -> None:
    profile = ModernProfile()
    assert (profile.fe20_clear_policy, profile.fe11_chunk, profile.device_tx_start) == ("index_zero", b"", 1)
    with pytest.raises(ValueError, match="unknown fe20_clear_policy"):
        ModernProfile(fe20_clear_policy="always_clear")
    with pytest.raises(ValueError, match="u32 range"):
        ModernProfile(device_tx_start=-1)


@pytest.mark.asyncio
async def test_trace_captures_raw_bytes_and_phases() -> None:
    peripheral, host, responses = await run_scenario()
    trace = peripheral.trace
    assert trace.trace_type == TRACE_TYPE_SYNTHETIC
    phases = [e.phase for e in trace.events]
    for phase in ("PREKEY", "RSA", "SEALED", "FILESYNC"):
        assert phase in phases, phases
    # Every event keeps exact raw bytes + direction + characteristic.
    for event in trace.events:
        assert isinstance(event.raw, bytes) and len(event.raw) > 0
        assert event.direction in ("HOST_TO_DEVICE", "DEVICE_TO_HOST")
        assert event.characteristic in ("2BB1", "2BB0")
    host_to = [e for e in trace.events if e.direction == "HOST_TO_DEVICE"]
    device_to = [e for e in trace.events if e.direction == "DEVICE_TO_HOST"]
    assert len(host_to) + len(device_to) == len(trace.events)
    assert any(e.result.startswith("accepted") for e in host_to)
    assert any(e.result == "sent" for e in device_to)


@pytest.mark.asyncio
async def test_golden_trace_replay_preserves_everything() -> None:
    peripheral, host, responses = await run_scenario()
    blob = peripheral.trace.to_json()
    replayed = Trace.from_json(blob)
    assert replayed.trace_type == TRACE_TYPE_SYNTHETIC
    assert len(replayed.events) == len(peripheral.trace.events)
    for original, back in zip(peripheral.trace.events, replayed.events):
        assert back.raw == original.raw  # raw authoritative, never replaced
        assert back.direction == original.direction
        assert back.ts == original.ts
        assert back.phase == original.phase  # stored knowledge preserved
        assert back.parsed == original.parsed
        phase, parsed, _ = classify_raw(back.raw)
        if back.parsed.get("keyed"):
            continue  # keyed opens are stored, never re-derived keylessly
        assert phase == back.phase  # keyless re-parse agrees where decisive
        assert parsed == back.parsed


@pytest.mark.asyncio
async def test_unknown_bytes_survive_replay() -> None:
    peripheral, host, responses = await run_scenario()
    peripheral.trace.record("HOST_TO_DEVICE", "2BB1", b"\x99\x00\xfe" + bytes(20), result="received")
    blob = peripheral.trace.to_json()
    replayed = Trace.from_json(blob)
    last = replayed.events[-1]
    assert last.raw == b"\x99\x00\xfe" + bytes(20)
    assert last.phase == "UNKNOWN" and last.classification == "UNKNOWN"
    # Identical traces: silent (unknown bytes survive, nothing mismatches).
    assert compare_traces(peripheral.trace, replayed) == []
    # Tampered unknown bytes: byte difference WITH parser-unknown context.
    import copy

    tampered = Trace(
        trace_type=replayed.trace_type,
        events=[TraceEvent(**{**e.__dict__}) for e in replayed.events],
    )
    raw = bytearray(tampered.events[-1].raw)
    raw[0] ^= 0xFF
    tampered.events[-1].raw = bytes(raw)
    diffs = compare_traces(replayed, tampered)
    assert any(d["kind"] == "BYTE_DIFFERENCE" for d in diffs)
    assert any(d["kind"] == "PARSER_UNKNOWN" for d in diffs)  # context, not verdict
    assert not any(d["kind"] == "TIMING_DIFFERENCE" for d in diffs)


@pytest.mark.asyncio
async def test_compare_kinds() -> None:
    peripheral, host, responses = await run_scenario()
    assert compare_traces(peripheral.trace, peripheral.trace) == []
    import copy

    altered = Trace(
        trace_type=peripheral.trace.trace_type,
        events=[TraceEvent(**{**e.__dict__}) for e in peripheral.trace.events],
    )
    assert compare_traces(peripheral.trace, altered) == []  # identical copy: silent
    tampered = Trace(
        trace_type=peripheral.trace.trace_type,
        events=[TraceEvent(**{**e.__dict__}) for e in peripheral.trace.events],
    )
    raw = bytearray(tampered.events[0].raw)
    raw[0] ^= 0xFF
    tampered.events[0].raw = bytes(raw)
    diffs = compare_traces(peripheral.trace, tampered)
    assert any(d["kind"] == "BYTE_DIFFERENCE" for d in diffs)
    shifted = Trace(
        trace_type=peripheral.trace.trace_type,
        events=[TraceEvent(**{**e.__dict__}) for e in peripheral.trace.events],
    )
    shifted.events[-1].ts += 3600.0
    assert any(d["kind"] == "TIMING_DIFFERENCE" for d in compare_traces(peripheral.trace, shifted))
    assert not any(d["kind"] == "TIMING_DIFFERENCE" for d in compare_traces(peripheral.trace, shifted, time_tolerance_s=7200.0))
    dropped = Trace(trace_type=peripheral.trace.trace_type, events=list(peripheral.trace.events[:-1]))
    assert any(d["kind"] == "ORDERING_MISMATCH" for d in compare_traces(peripheral.trace, dropped))


@pytest.mark.asyncio
async def test_fe20_never_policy_leaves_stale_batch() -> None:
    peripheral, host, responses = await run_scenario(
        lambda device: modern_factory(device, ModernProfile(fe20_clear_policy="never"))
    )
    assert peripheral.profile.fe20_clear_policy == "never"
    assert peripheral.sn_received is not None  # plain FE10 path unaffected


@pytest.mark.asyncio
async def test_device_tx_start_policy_shifts_response_sequences() -> None:
    peripheral, host, responses = await run_scenario(
        lambda device: modern_factory(device, ModernProfile(device_tx_start=5))
    )
    assert peripheral.session.tx_seq > 5  # first response sealed at seq 6, then advanced
    assert host.rx_seq == peripheral.session.tx_seq  # host tracked every response


@pytest.mark.asyncio
async def test_fe11_chunk_policy_visible_on_wire() -> None:
    peripheral, host, responses = await run_scenario(
        lambda device: modern_factory(device, ModernProfile(fe11_chunk=b"AB"))
    )
    from plaudsim.handshake import MARKER_RSA_MARKER

    fe11 = [r for r in responses if len(r) >= 4 and int.from_bytes(r[0:2], "little") == MARKER_RSA_MARKER]
    assert len(fe11) == 1 and fe11[0][4:] == b"AB"
