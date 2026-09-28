"""Synthetic/test-only modern sealed transport (R5-S7).

TEST-ONLY POST-SECRET FIXTURE. Nothing here authenticates, and nothing here
touches Plaud credentials, the backend, or the live FE10/FE20/FE11/FE12
exchange. This module represents the channel state *after* a successful
secret establishment, with deterministic synthetic key material, so the
post-secret protocol machinery recovered in R5-S5/R5-S6 can be exercised
locally.

Recovered (SDK does this; see docs/protocol-ledger.md section 4.3 and
r5-s6/README.md): ChaCha20-Poly1305 over [u32le seq][frame] with a fixed
key J, fixed nonce K and fixed AAD L; M pre-incremented per seal call from
a reset value of 1 (first sealed frame carries seq 2); N accept-and-set
from -1 with silent drop when N >= seq; AEAD failure propagates without
advancing N; the same nonce is reused for every frame in both directions.

Synthetic (this module chooses this; the SDK does not fix it): the J/K/L
values below, the declared port version 21, and -- for the R5-S7 GetState
helper only -- the loopback sharing of one SealedSession for both sides
(sequences 2 then 3). R5-S8 adds SealedLink, the two-independent-endpoint
model (M_host/N_host plus M_device/N_device); on a real link the host and
the device hold independent counters and the device-side start is UNKNOWN
(R5-S6), so neither sharing nor any particular device start is a protocol
claim.
"""

from __future__ import annotations

from struct import pack, unpack_from

try:  # cryptography >= ~42 exposes aead at the top level
    from cryptography.hazmat.primitives.aead import ChaCha20Poly1305
except ImportError:  # older builds keep it under ciphers.aead
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from cryptography.exceptions import InvalidTag

# --- deterministic synthetic fixture -------------------------------------
# Plainly non-operational ASCII, fixed lengths enforced below. These stand in
# for J/K/L *after* secret establishment; they are not derived from, and can
# never be mistaken for, Plaud-issued material.
SYNTHETIC_J = b"SYNTHETIC-J-KEY-0123456789ABCDEF"  # 32-byte ChaCha20 key
SYNTHETIC_K = b"SYN-TEST-NON"  # 12-byte nonce, reused every frame like the SDK
SYNTHETIC_L = b"SYN-TEST-AAD"  # 12-byte associated data
assert len(SYNTHETIC_J) == 32
assert len(SYNTHETIC_K) == 12
assert len(SYNTHETIC_L) == 12

#: Port version the sealed tests declare. >= 20 selects the SDK's sealed
#: branch (ledger section 4.1); 21 is the smallest modern value.
SYNTHETIC_PORT_VERSION = 21

_U32_MOD = 1 << 32


def _check_key_nonce(key: bytes, nonce: bytes) -> None:
    # Mirrors the q5.d/q5.b gates (same error strings as the SDK).
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes")
    if len(nonce) != 12:
        raise ValueError("Nonce must be 12 bytes")


def seal_raw(key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
    """One ChaCha20-Poly1305 seal exactly as q5.d performs it.

    `plaintext` is the full AEAD input (for transport frames this is
    [u32le seq][frame], built by the caller). Empty AAD is passed as None,
    matching the SDK's `updateAAD only when aad non-empty` shape.
    """
    _check_key_nonce(key, nonce)
    return ChaCha20Poly1305(key).encrypt(nonce, bytes(plaintext), bytes(aad) or None)


def open_raw(key: bytes, nonce: bytes, aad: bytes, wire: bytes) -> bytes:
    """One ChaCha20-Poly1305 open exactly as q5.b performs it.

    Enforces the SDK's ciphertext>=16 gate (Poly1305 tag width). AEAD
    failures raise InvalidTag to the caller, mirroring the SDK's rethrow out
    of the GATT callback rather than a silent drop.
    """
    _check_key_nonce(key, nonce)
    raw = bytes(wire)
    if len(raw) < 16:
        raise ValueError("Encrypted data must be at least 16 bytes")
    return ChaCha20Poly1305(key).decrypt(nonce, raw, bytes(aad) or None)


def _to_signed(value_u32: int) -> int:
    # The SDK narrows the u32 sequence with l2i and compares as a signed int.
    return value_u32 - _U32_MOD if value_u32 >= (1 << 31) else value_u32


class SealedSession:
    """One endpoint's M/N state over fixed synthetic J/K/L.

    `tx_seq` is the SDK's M (host TX counter): reset to 1, pre-incremented
    once per seal call, wrapping as a signed 32-bit int (practically
    unreachable; implemented for exactness). `rx_seq` is the SDK's N
    (receive counter): reset to -1, accept-and-set on strictly greater
    sequences only. `seal_calls` counts seal invocations so tests can prove
    a retry path performed no re-seal.
    """

    def __init__(
        self,
        key: bytes = SYNTHETIC_J,
        nonce: bytes = SYNTHETIC_K,
        aad: bytes = SYNTHETIC_L,
    ) -> None:
        _check_key_nonce(key, nonce)
        self.key = bytes(key)
        self.nonce = bytes(nonce)
        self.aad = bytes(aad)
        self.seal_calls = 0
        self.reset()

    def reset(self) -> None:
        """The z.y() equivalent: M -> 1, N -> -1 (keys are fixture-fixed)."""
        self.tx_seq = 1
        self.rx_seq = -1

    def seal(self, frame: bytes) -> bytes:
        """Seal one logical frame: M += 1, then encrypt [u32le M][frame].

        The sequence is part of the AEAD plaintext (R5-S6 stack-accounting
        proof), not an adjacent header. The returned buffer is complete;
        re-sending it is a plain byte copy and must not call this again.
        """
        self.tx_seq = _to_signed((self.tx_seq + 1) & 0xFFFFFFFF)
        self.seal_calls += 1
        plaintext = pack("<I", self.tx_seq & 0xFFFFFFFF) + bytes(frame)
        return seal_raw(self.key, self.nonce, self.aad, plaintext)

    def open(self, wire: bytes) -> bytes | None:
        """Open one inbound wire frame.

        Returns the inner plaintext frame on acceptance, None when the frame
        is skipped: a short decrypted payload (SDK: decrypt-invalid skip) or
        a duplicate/stale sequence (SDK: silent `N >= seq` drop). In both
        skip cases the receive counter is untouched. An AEAD authentication
        failure raises InvalidTag, also leaving the counter untouched.
        """
        plaintext = open_raw(self.key, self.nonce, self.aad, wire)
        if len(plaintext) < 4:
            return None
        seq = _to_signed(unpack_from("<I", plaintext, 0)[0])
        if self.rx_seq >= seq:
            return None
        self.rx_seq = seq
        return plaintext[4:]


GET_STATE_REQUEST = b"\x01\x03\x00"


class SealedLink:
    """Two independent sealed endpoints sharing synthetic J/K/L (R5-S8).

    TEST-ONLY HARNESS POLICY. `host` seals requests with M_host and opens
    responses with N_host; `device` opens requests with N_device and seals
    responses with M_device. Each side resets to M=1/N=-1 per R5-S6, so the
    first sealed frame in *each direction* carries seq 2 -- the device
    response does NOT take seq 3 merely because the request took seq 2.
    The real device-side counter start is UNKNOWN; independence itself is
    the recovered property (separate static M/N roles in z/z$c), the exact
    device start value is not claimed.
    """

    def __init__(
        self,
        key: bytes = SYNTHETIC_J,
        nonce: bytes = SYNTHETIC_K,
        aad: bytes = SYNTHETIC_L,
    ) -> None:
        self.host = SealedSession(key, nonce, aad)
        self.device = SealedSession(key, nonce, aad)

    def reset(self) -> None:
        """Reset both endpoints, equivalent to z.y() on each side."""
        self.host.reset()
        self.device.reset()


def sealed_getstate_roundtrip(session: SealedSession, state) -> dict[str, object]:
    """End-to-end sealed GetState exchange on one shared loopback session.

    Shared-session use is documented in the module docstring: it yields the
    request at seq 2 and the response at seq 3, exercising TX increment and
    RX accept-and-set in both directions with a single M/N pair. `state` is
    a PlaudDeviceState (imported lazily so this module stays Bumble-free).
    """
    from plaudsim.profile import OPCODE_GET_STATE

    request_wire = session.seal(GET_STATE_REQUEST)
    request_seq = session.tx_seq
    request_inner = session.open(request_wire)
    if request_inner is None:
        raise AssertionError("own request was not accepted by the RX path")
    if len(request_inner) != 3 or unpack_from("<H", request_inner, 1)[0] != OPCODE_GET_STATE:
        raise ValueError(f"not a GetState request after decrypt: {request_inner.hex()}")

    response_inner_expected = state.encode()
    response_wire = session.seal(response_inner_expected)
    response_seq = session.tx_seq
    response_inner = session.open(response_wire)
    if response_inner is None:
        raise AssertionError("own response was not accepted by the RX path")
    return {
        "request_seq": request_seq,
        "request_wire": request_wire,
        "response_seq": response_seq,
        "response_wire": response_wire,
        "response_inner": response_inner,
    }


# --- R5-S8 two-endpoint application round-trips (test support) -------------
# These wire the FROZEN R3 application framing (transfer.py / filesync.py,
# unchanged) through two independent sealed endpoints. Sealing is an outer
# transport layer: every application byte layout below is inherited from R3,
# every sequencing property from R5-S6/R5-S7, and only the endpoint pairing
# plus the synthetic fixture are harness policy.


def sealed_filelist_roundtrip(
    link: SealedLink,
    file_table,
    request_stamp: int,
    start_session_id: int = 0,
    flag: bool = False,
    port_version: int = SYNTHETIC_PORT_VERSION,
) -> dict[str, object]:
    """Sealed FileList exchange over independent endpoints.

    Host seals p2 (M_host 1->2); device opens (N_device -1->2), runs the
    existing FileTable paging, and seals each q2 frame with M_device;
    host opens each with N_host and ingests them with the existing
    FileListAccumulator. Returns seqs, wires, parsed frames and entries.
    """
    from plaudsim.filesync import pack_file_list_request, parse_file_list_frame
    from plaudsim.transfer import FileListAccumulator, parse_file_list_request

    request = pack_file_list_request(request_stamp, start_session_id, flag)
    request_wire = link.host.seal(request)
    request_seq = link.host.tx_seq      # seal pre-increments; this is the sequence it sealed
    inner = link.device.open(request_wire)
    if inner != request:
        raise AssertionError("device did not accept the sealed FileList request")
    params = parse_file_list_request(inner)
    frames = file_table.frames(params["request_stamp"])
    wires, response_seqs = _seal_all(link.device, frames)
    inners = []
    for w in wires:
        opened = link.host.open(w)
        if opened is None:
            raise AssertionError("host dropped a sealed FileList response")
        inners.append(opened)
    parsed_frames = [parse_file_list_frame(f, port_version) for f in inners]
    accumulator = FileListAccumulator(port_version=port_version)
    for f in inners:
        if accumulator.ingest_frame(f) != "accepted":
            raise AssertionError("accumulator rejected a sealed FileList frame")
    if not accumulator.complete:
        raise AssertionError("accumulator did not complete on sealed frames")
    return {
        "request_seq": request_seq,
        "request_wire": request_wire,
        "response_seqs": response_seqs,
        "response_wires": wires,
        "parsed_frames": parsed_frames,
        "entries": accumulator.entries,
    }


def sealed_sync_roundtrip(
    link: SealedLink,
    file_bytes: bytes,
    session_id: int,
    crc: int = 0x1234,
    port_version: int = SYNTHETIC_PORT_VERSION,
    start: int = 0,
    end: int = 0,
) -> dict[str, object]:
    """Sealed SyncFile exchange over independent endpoints.

    Host seals y6; device opens, runs the existing TransferSession, and
    seals HEAD + type-2 DATA + TAIL each with M_device; host opens each
    and parses with the existing R3 parsers. Verifies the reassembled
    payload equals the synthetic file bytes from `start`.
    """
    from plaudsim.filesync import (
        pack_sync_start,
        parse_file_data_frame,
        parse_sync_head,
        parse_sync_tail,
    )
    from plaudsim.transfer import TransferSession, parse_sync_start_request

    request = pack_sync_start(session_id, start, end)
    request_wire = link.host.seal(request)
    request_seq = link.host.tx_seq
    inner = link.device.open(request_wire)
    if inner != request:
        raise AssertionError("device did not accept the sealed SyncFile request")
    params = parse_sync_start_request(inner)
    session = TransferSession(
        file_bytes=bytes(file_bytes), crc=crc, port_version=port_version
    )
    session.start(params["session_id"], params["start"], params["end"])
    frames = session.frames()
    wires, response_seqs = _seal_all(link.device, frames)
    inners = []
    for w in wires:
        opened = link.host.open(w)
        if opened is None:
            raise AssertionError("host dropped a sealed transfer frame")
        inners.append(opened)
    from plaudsim.filesync import EMPTY_PACKAGE_OFFSET

    head = parse_sync_head(inners[0])
    tail = parse_sync_tail(inners[-1])
    # R7-S13: the sentinel that closes the transfer sits before the TAIL; it is
    # not payload. Sealing covers it like any other frame.
    datas = [
        d for d in (parse_file_data_frame(f, port_version) for f in inners[1:-1])
        if d["offset"] != EMPTY_PACKAGE_OFFSET
    ]
    # Each chunk is written at its ABSOLUTE offset into a file-sized buffer
    # and the chunks must tile [start, len(file)) exactly: a wrong offset,
    # a gap, an overlap or a chunk past the end fails.
    reassembled = bytearray(len(file_bytes))
    cursor = start
    for d in datas:
        off, payload = d["offset"], d["payload"]
        if off != cursor or off + len(payload) > len(file_bytes):
            raise AssertionError(f"DATA at offset {off} (+{len(payload)}) does not continue at {cursor}")
        reassembled[off : off + len(payload)] = payload
        cursor = off + len(payload)
    if cursor != len(file_bytes) or bytes(reassembled[start:]) != bytes(file_bytes)[start:]:
        raise AssertionError("transfer payload did not survive sealing")
    return {
        "request_seq": request_seq,
        "request_wire": request_wire,
        "response_seqs": response_seqs,
        "response_wires": wires,
        "head": head,
        "datas": datas,
        "tail": tail,
        "payload": bytes(reassembled[start:]),
    }


def _seal_all(session: "SealedSession", frames) -> tuple[list[bytes], list[int]]:
    """Seal each frame and record the sequence number actually sealed."""
    wires: list[bytes] = []
    seqs: list[int] = []
    for f in frames:
        wires.append(session.seal(f))
        seqs.append(session.tx_seq)
    return wires, seqs
