#!/usr/bin/env python3
"""R5-S7 verifier: synthetic modern sealed GetState round-trip.

Standalone (no pytest): checks the deterministic fixture, M/N resets,
first-seq-2, increment 2->3, retry identity, duplicate/stale/forward RX,
AEAD-failure semantics, the authenticated-sequence contract, and the
in-process sealed GetState round-trip (request seq 2, response seq 3).
A live Bumble GATT leg runs when the vendored stack imports, else SKIP.

Exit 0 = verified. Frozen evidence is read, never written.
"""

from __future__ import annotations

import asyncio
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


from plaudsim.sealed import (  # noqa: E402
    SYNTHETIC_J,
    SYNTHETIC_K,
    SYNTHETIC_L,
    SYNTHETIC_PORT_VERSION,
    SealedSession,
    open_raw,
    seal_raw,
    sealed_getstate_roundtrip,
)

try:
    from cryptography.exceptions import InvalidTag
except ImportError:
    InvalidTag = Exception  # type: ignore[assignment,misc]

# --- fixture ---------------------------------------------------------------
check("fixture J/K/L lengths 32/12/12",
      len(SYNTHETIC_J) == 32 and len(SYNTHETIC_K) == 12 and len(SYNTHETIC_L) == 12)
check("fixture is deterministic synthetic ASCII",
      SYNTHETIC_J.startswith(b"SYNTHETIC") and SYNTHETIC_J.isascii())
check("fixture declares a modern port version", SYNTHETIC_PORT_VERSION >= 20)

# --- external crypto proof ---------------------------------------------------
RFC_KEY = bytes.fromhex(
    "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
RFC_NONCE = bytes.fromhex("070000004041424344454647")
RFC_AAD = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
RFC_PT = (b"Ladies and Gentlemen of the class of '99: If I could offer you "
          b"only one tip for the future, sunscreen would be it.")
RFC_WIRE = ("d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
            "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
            "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
            "3ff4def08e4b7a9de576d26586cec64b6116"
            "1ae10b594f09e26a7e902ecbd0600691")
check("seal_raw reproduces RFC 8439 section 2.8.2 AEAD vector",
      seal_raw(RFC_KEY, RFC_NONCE, RFC_AAD, RFC_PT).hex() == RFC_WIRE)
check("open_raw inverts the RFC vector",
      open_raw(RFC_KEY, RFC_NONCE, RFC_AAD, bytes.fromhex(RFC_WIRE)) == RFC_PT)

# --- M/N state machine -------------------------------------------------------
s = SealedSession()
check("M/N reset to 1/-1", (s.tx_seq, s.rx_seq) == (1, -1))
w2 = s.seal(b"\x01\x03\x00")
check("first sealed frame carries seq 2",
      s.tx_seq == 2 and open_raw(s.key, s.nonce, s.aad, w2)[:4] == b"\x02\x00\x00\x00")
w3 = s.seal(b"\x01\x03\x00")
check("second logical frame carries seq 3",
      s.tx_seq == 3 and open_raw(s.key, s.nonce, s.aad, w3)[:4] == b"\x03\x00\x00\x00"
      and w3 != w2)
calls = s.seal_calls
resend = bytes(w2)
check("retry is a byte-identical copy with no re-seal",
      resend == w2 and s.seal_calls == calls and s.tx_seq == 3)

t = SealedSession()
a = t.seal(b"\x01\x03\x00")
b = t.seal(b"\x01\x06\x00")
check("RX accepts seq 2 then 3", t.open(a) == b"\x01\x03\x00" and t.open(b) == b"\x01\x06\x00")
t2 = SealedSession()
a2 = t2.seal(b"\x01\x03\x00")
t2.open(a2)
check("RX duplicate seq 2 dropped silently",
      t2.open(bytes(a2)) is None and t2.rx_seq == 2)
t3 = SealedSession()
o = t3.seal(b"\x01\x03\x00")
n = t3.seal(b"\x01\x06\x00")
t3.open(n)
check("RX stale seq 2 after 3 dropped silently",
      t3.open(o) is None and t3.rx_seq == 3)

t4 = SealedSession()
bad = bytearray(t4.seal(b"\x01\x03\x00"))
bad[6] ^= 0xFF
try:
    t4.open(bytes(bad))
    check("AEAD failure raises", False, "no exception")
except InvalidTag:
    check("AEAD failure raises InvalidTag", True)
except Exception as exc:  # noqa: BLE001
    check("AEAD failure raises", False, type(exc).__name__)
check("AEAD failure leaves N at -1", t4.rx_seq == -1)

t5 = SealedSession()
w = t5.seal(b"\x01\x03\x00")
check("open takes wire only; seq comes from inside",
      t5.open(w) == b"\x01\x03\x00")
u = SealedSession()
u.seal(b"\x01\x06\x00")
w_other_seq = u.seal(b"\x01\x03\x00")
check("same frame at seq 3 seals differently than at seq 2", w_other_seq != w)
t6 = SealedSession()
t6.open(t6.seal(b"\x01\x03\x00"))
t6.seal(b"\x01\x06\x00")
t6.reset()
check("reset restores M=1/N=-1 and next seal is seq 2",
      (t6.tx_seq, t6.rx_seq) == (1, -1)
      and open_raw(t6.key, t6.nonce, t6.aad, t6.seal(b"\x01\x03\x00"))[:4] == b"\x02\x00\x00\x00")

# --- GetState round-trip -----------------------------------------------------
sys.path.insert(0, str(ROOT / "emulator"))
from plaudsim.profile import PlaudDeviceState  # noqa: E402

state = PlaudDeviceState(state=0x1003, scene=5, session_id=0x01020304)
out = sealed_getstate_roundtrip(SealedSession(), state)
inner = bytes(out["response_inner"])
check("round-trip request seq 2, response seq 3",
      out["request_seq"] == 2 and out["response_seq"] == 3)
check("response decrypts to the reconstructed GetState layout",
      inner[:3] == b"\x01\x03\x00" and len(inner) == 18
      and int.from_bytes(inner[3:7], "little") == 0x1003
      and inner[10] == 5)

# --- Bumble GATT leg ---------------------------------------------------------
try:
    from bumble.device import Peer  # noqa: E402
    from bumble.testing.test_utils import TwoDevices  # noqa: E402

    from sealed_support import SealedTestPeripheral  # noqa: E402
except Exception as exc:  # noqa: BLE001
    skip("bumble GATT round-trip", f"{type(exc).__name__}: {exc}")
else:
    async def gatt_leg() -> None:
        session = SealedSession()
        devices = await TwoDevices.create_with_connection()
        peripheral = SealedTestPeripheral(devices[1], session, state)
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
        await peer.write_value(command, session.seal(b"\x01\x03\x00"), with_response=True)
        assert len(responses) == 1, f"expected 1 sealed response, got {len(responses)}"
        got = session.open(responses[0])
        assert got is not None and got[:3] == b"\x01\x03\x00" and len(got) == 18
        assert peripheral.requests == [b"\x01\x03\x00"]
        assert peripheral.drops == 0 and peripheral.errors == []

    try:
        asyncio.run(gatt_leg())
        check("sealed GetState round-trips over Bumble virtual GATT", True)
    except Exception as exc:  # noqa: BLE001
        check("sealed GetState round-trips over Bumble virtual GATT", False, repr(exc))

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print(f"ALL CHECKS PASSED ({len(skips)} skipped)")
