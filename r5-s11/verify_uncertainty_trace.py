#!/usr/bin/env python3
"""R5-S11 verifier: uncertainty isolation + capture-ready traces.

Checks the matrix (closed vocabulary, counts, no UNKNOWN-as-fact),
ModernProfile isolation (defaults + unknown-policy refusal), a full
synthetic R5-S9 trace (phases + raw authority), golden replay, and the
emulator-vs-candidate comparison kinds. Exit 0 = verified.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "reference/upstream/bumble"))
sys.path.insert(0, str(ROOT / "tests"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


matrix = json.loads((ROOT / "r5-s11" / "uncertainty_matrix.json").read_text())
rows = matrix["behaviors"]
ALLOWED = {"SDK_PROVEN", "EMULATOR_INTEGRATION_PROVEN", "INFERRED", "HARNESS_POLICY", "UNKNOWN"}
check("matrix rows well-formed with closed vocabulary",
      len(rows) >= 25 and all(
          set(r) == {"behavior", "location", "classification", "evidence", "test", "safe_to_emulate"}
          and r["classification"] in ALLOWED for r in rows))
counts = {c: sum(1 for r in rows if r["classification"] == c) for c in ALLOWED}
check("matrix counts (SDK>=10, UNKNOWN>=3, POLICY>=3, INFERRED>=1)",
      counts["SDK_PROVEN"] >= 10 and counts["UNKNOWN"] >= 3
      and counts["HARNESS_POLICY"] >= 3 and counts["INFERRED"] >= 1, str(counts))
check("no UNKNOWN row claims safe emulation as plain true",
      all(r["safe_to_emulate"] is not True for r in rows if r["classification"] == "UNKNOWN"))
check("FE20/FE11-payload/device-TX-start rows are not SDK_PROVEN",
      all(next(r for r in rows if k in r["behavior"])["classification"] != "SDK_PROVEN"
          for k in ("FE20 device-side", "FE11 live payload", "Device sealed TX start")))

from protocol_trace import TRACE_TYPE_SYNTHETIC, Trace, classify_raw, compare_traces  # noqa: E402
from sealed_support import ModernProfile  # noqa: E402

p = ModernProfile()
check("profile defaults preserve R5-S9 semantics",
      (p.fe20_clear_policy, p.fe11_chunk, p.device_tx_start) == ("index_zero", b"", 1))
for kwargs in ({"fe20_clear_policy": "always"}, {"device_tx_start": -1}):
    try:
        ModernProfile(**kwargs)
        check(f"unknown policy {kwargs} refused", False)
    except ValueError:
        check(f"unknown policy {kwargs} refused", True)


async def main() -> None:
    from bumble.device import Peer  # noqa: E402
    from bumble.testing.test_utils import TwoDevices  # noqa: E402
    from cryptography.hazmat.primitives import serialization  # noqa: E402
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

    from plaudsim.filesync import pack_file_list_request, pack_sync_start  # noqa: E402
    from plaudsim.handshake import MARKER_PRE_HANDSHAKE, MARKER_SECRET  # noqa: E402
    from plaudsim.profile import PlaudDeviceState  # noqa: E402
    from plaudsim.sealed import SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L, SealedSession  # noqa: E402
    from plaudsim.transfer import FileTable  # noqa: E402
    from sealed_support import (  # noqa: E402
        SYNTHETIC_SN_SIGNATURE,
        ModernTestPeripheral,
        build_marker_frames,
        decrypt_secret_package,
    )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    devices = await TwoDevices.create_with_connection()
    peripheral = ModernTestPeripheral(
        devices[1],
        state=PlaudDeviceState(state=0x1003, scene=5, session_id=0x01020304),
        file_table=FileTable([
            {"session_id": 1001, "file_size": 96, "scene": 5, "attribute": 2},
            {"session_id": 1002, "file_size": 32, "scene": 4, "attribute": 0},
        ], port_version=21),
        file_bytes=bytes((i * 13 + 7) & 0xFF for i in range(96)),
        tail_crc=0x5678, port_version=21)
    peripheral.install()
    peer = Peer(devices.connections[0])
    await peer.request_mtu(255)
    await peer.discover_services()
    await peer.discover_characteristics()
    await peer.discover_descriptors()
    svc = next(s for s in peer.services if str(s.uuid).lower().startswith("00001910"))
    data = next(c for c in svc.characteristics if str(c.uuid).lower().startswith("00002bb0"))
    cmd = next(c for c in svc.characteristics if str(c.uuid).lower().startswith("00002bb1"))
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    async def wait_for(base: int, count: int) -> None:
        for _ in range(100):
            if len(responses) >= base + count:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("timeout")

    base = len(responses)
    for f in build_marker_frames(MARKER_PRE_HANDSHAKE, SYNTHETIC_SN_SIGNATURE):
        await peer.write_value(cmd, f, with_response=True)
    await wait_for(base, 1)
    for f in build_marker_frames(MARKER_SECRET, pem):
        await peer.write_value(cmd, f, with_response=True)
    await wait_for(base, 4)
    parts = decrypt_secret_package(
        key, [r for r in responses if len(r) >= 4 and int.from_bytes(r[0:2], "little") == MARKER_SECRET])
    assert parts["chacha_key"] == SYNTHETIC_J
    host = SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L)

    async def sealed_roundtrip(plaintext: bytes, expected_new: int) -> None:
        base = len(responses)
        await peer.write_value(cmd, host.seal(plaintext), with_response=True)
        await wait_for(base, expected_new)
        for wire in responses[base:]:
            assert host.open(wire) is not None

    await sealed_roundtrip(b"\x01\x03\x00", 1)
    await sealed_roundtrip(pack_file_list_request(1, 0), 1)
    await sealed_roundtrip(pack_sync_start(2001, 0, 0), 5)

    trace = peripheral.trace
    check("synthetic trace labeled + phased (PREKEY/RSA/SEALED/FILESYNC)",
          trace.trace_type == TRACE_TYPE_SYNTHETIC
          and {"PREKEY", "RSA", "SEALED", "FILESYNC"} <= {e.phase for e in trace.events})
    check("every event keeps exact raw bytes + direction + characteristic",
          all(isinstance(e.raw, bytes) and e.raw
              and e.direction in ("HOST_TO_DEVICE", "DEVICE_TO_HOST")
              and e.characteristic in ("2BB1", "2BB0") for e in trace.events))
    blob = trace.to_json()
    replayed = Trace.from_json(blob)
    check("golden replay preserves order/bytes/phases",
          [e.raw for e in replayed.events] == [e.raw for e in trace.events]
          and [e.phase for e in replayed.events] == [e.phase for e in trace.events]
          and [e.direction for e in replayed.events] == [e.direction for e in trace.events])
    check("self-comparison is silent", compare_traces(trace, replayed) == [])
    from protocol_trace import TraceEvent  # noqa: E402

    tampered = Trace(trace_type=replayed.trace_type,
                     events=[TraceEvent(**{**e.__dict__}) for e in replayed.events])
    raw = bytearray(tampered.events[0].raw)
    raw[4] ^= 0xFF  # chunk payload: phase unchanged, bytes differ
    tampered.events[0].raw = bytes(raw)
    kinds = {d["kind"] for d in compare_traces(replayed, tampered)}
    check("comparison yields BYTE_DIFFERENCE on tampered bytes", "BYTE_DIFFERENCE" in kinds)
    check("comparison never calls parser-unknown a mismatch without bytes",
          "PARSER_UNKNOWN" not in {d["kind"] for d in compare_traces(trace, replayed)})


asyncio.run(main())
print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
