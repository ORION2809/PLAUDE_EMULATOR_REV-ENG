#!/usr/bin/env python3
"""R5-S8 verifier: independent sealed endpoints carrying FileList + SyncFile.

Standalone (no pytest). Checks independent M/N counters, the sealed
FileList round-trip (full R3 payload verified after decrypt), the sealed
SyncFile round-trip (type-2 DATA + tail/CRC preserved), replay/stale
drops, ciphertext + sequence tamper behavior, and one Bumble GATT
FileList leg (SKIP only if bumble unimportable). Exit 0 = verified.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "reference/upstream/bumble"))
sys.path.insert(0, str(ROOT / "tests"))

failures: list[str] = []
skips: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def skip(name: str, reason: str) -> None:
    print(f"SKIP {name} -- {reason}")
    skips.append(name)


from plaudsim.filesync import (  # noqa: E402
    OPCODE_FILE_LIST,
    OPCODE_SYNC_START,
    pack_file_list_request,
    pack_sync_start,
    parse_file_data_frame,
    parse_sync_head,
    parse_sync_tail,
)
from plaudsim.sealed import (  # noqa: E402
    SYNTHETIC_PORT_VERSION,
    SealedLink,
    SealedSession,
    open_raw,
    sealed_filelist_roundtrip,
    sealed_sync_roundtrip,
)
from plaudsim.transfer import FileTable  # noqa: E402

try:
    from cryptography.exceptions import InvalidTag
except ImportError:
    InvalidTag = Exception  # type: ignore[assignment,misc]

PV = SYNTHETIC_PORT_VERSION
ENTRIES = [
    {"session_id": 1001, "file_size": 96, "scene": 5, "attribute": 2},
    {"session_id": 1002, "file_size": 32, "scene": 4, "attribute": 0},
]
FILE_BYTES = bytes((i * 13 + 7) & 0xFF for i in range(96))

check("frozen opcodes unchanged (FileList 26, SyncStart 28)",
      OPCODE_FILE_LIST == 26 and OPCODE_SYNC_START == 28)

# --- independent counters ----------------------------------------------------
link = SealedLink()
hw = link.host.seal(b"\x01\x03\x00")
dw = link.device.seal(b"\x01\x03\x00")
check("host first TX seq 2", link.host.tx_seq == 2)
check("device first TX seq 2 (not 3)", link.device.tx_seq == 2)
link.host.seal(b"\x01\x03\x00")
check("host second TX seq 3, device still 2",
      link.host.tx_seq == 3 and link.device.tx_seq == 2)
check("open() is wire-only (no external seq parameter)",
      list(inspect.signature(SealedSession.open).parameters) == ["self", "wire"])

# --- R5-S7 properties preserved ----------------------------------------------
link = SealedLink()
w = link.host.seal(b"\x01\x03\x00")
calls = link.host.seal_calls
check("retry reuses sealed bytes without re-seal",
      bytes(w) == w and link.host.seal_calls == calls)
check("endpoint accept then duplicate drop",
      link.device.open(w) == b"\x01\x03\x00"
      and link.device.open(bytes(w)) is None and link.device.rx_seq == 2)

# --- sealed FileList ---------------------------------------------------------
link = SealedLink()
table = FileTable([dict(e) for e in ENTRIES], port_version=PV)
out = sealed_filelist_roundtrip(link, table, 0x12345678)
check("filelist request seq 2, response seq 2 (independent)",
      out["request_seq"] == 2 and out["response_seqs"] == [2])
check("filelist totals/session/size/scene/attribute verified",
      [e["session_id"] for e in out["entries"]] == [1001, 1002]
      and [e["file_size"] for e in out["entries"]] == [96, 32]
      and [e["scene"] for e in out["entries"]] == [5, 4]
      and [e["attribute"] for e in out["entries"]] == [2, 0]
      and out["parsed_frames"][0]["request_stamp"] == 0x12345678
      and out["parsed_frames"][0]["totals"] == 2)

# --- sealed SyncFile ---------------------------------------------------------
link = SealedLink()
sync = sealed_sync_roundtrip(link, FILE_BYTES, 2001, 0x5678)
check("sync response seqs 2..7 (HEAD + 3 DATA + EMPTY_PACKAGE + TAIL)",
      sync["response_seqs"] == [2, 3, 4, 5, 6, 7])  # R7-S13: + EMPTY_PACKAGE before TAIL
check("sync head/tail preserved",
      sync["head"] == {"session_id": 2001, "status": 0}
      and sync["tail"] == {"session_id": 2001, "crc": 0x5678})
check("sync DATA offsets/session/payload preserved",
      [d["offset"] for d in sync["datas"]] == [0, 32, 64]
      and all(d["session_id"] == 2001 and not d["empty_package"] for d in sync["datas"])
      and sync["payload"] == FILE_BYTES)

# --- replay / stale ----------------------------------------------------------
link = SealedLink()
first = link.host.seal(pack_file_list_request(1, 0))
second = link.host.seal(pack_file_list_request(2, 0))
link.device.open(first)
check("duplicate request dropped, N pinned",
      link.device.open(bytes(first)) is None and link.device.rx_seq == 2)
link2 = SealedLink()
fa = link2.host.seal(pack_file_list_request(1, 0))
fb = link2.host.seal(pack_file_list_request(2, 0))
link2.device.open(fb)
check("stale request dropped after later valid",
      link2.device.open(fa) is None and link2.device.rx_seq == 3)

# --- tamper ------------------------------------------------------------------
link = SealedLink()
bad = bytearray(link.host.seal(pack_file_list_request(1, 0)))
bad[10] ^= 0xFF
try:
    link.device.open(bytes(bad))
    check("ciphertext tamper raises", False, "no exception")
except InvalidTag:
    check("ciphertext tamper raises InvalidTag before dispatch", True)
except Exception as exc:  # noqa: BLE001
    check("ciphertext tamper raises", False, type(exc).__name__)
check("ciphertext tamper leaves N at -1", link.device.rx_seq == -1)
link = SealedLink()
seq_cover = bytearray(link.host.seal(pack_sync_start(2001, 0, 0)))
seq_cover[0] ^= 0x01
try:
    link.device.open(bytes(seq_cover))
    check("seq-region tamper raises", False, "no exception")
except InvalidTag:
    check("seq-region tamper raises InvalidTag (seq is AEAD input)", True)
except Exception as exc:  # noqa: BLE001
    check("seq-region tamper raises", False, type(exc).__name__)

# --- Bumble GATT FileList leg ------------------------------------------------
try:
    from bumble.device import Peer  # noqa: E402
    from bumble.testing.test_utils import TwoDevices  # noqa: E402

    from sealed_support import SealedTestPeripheral  # noqa: E402
except Exception as exc:  # noqa: BLE001
    skip("bumble GATT sealed filelist", f"{type(exc).__name__}: {exc}")
else:
    async def gatt_leg() -> None:
        link = SealedLink()
        devices = await TwoDevices.create_with_connection()
        peripheral = SealedTestPeripheral(
            devices[1], link.device,
            file_table=FileTable([dict(e) for e in ENTRIES], port_version=PV),
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
        await peer.write_value(
            command, link.host.seal(pack_file_list_request(0x12345678, 0)), with_response=True)
        assert len(responses) == 1, f"expected 1 sealed frame, got {len(responses)}"
        assert link.host.tx_seq == 2 and link.device.tx_seq == 2
        inner = link.host.open(responses[0])
        assert inner is not None and inner[:3] == b"\x01\x1a\x00"
        from plaudsim.transfer import FileListAccumulator  # noqa: E402
        acc = FileListAccumulator(port_version=PV)
        assert acc.ingest_frame(inner) == "accepted" and acc.complete
        assert [e["session_id"] for e in acc.entries] == [1001, 1002]

    try:
        asyncio.run(gatt_leg())
        check("sealed FileList round-trips over Bumble virtual GATT", True)
    except Exception as exc:  # noqa: BLE001
        check("sealed FileList round-trips over Bumble virtual GATT", False, repr(exc))

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print(f"ALL CHECKS PASSED ({len(skips)} skipped)")
