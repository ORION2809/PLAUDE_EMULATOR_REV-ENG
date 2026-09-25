"""Bumble test double: a pv>=20 peripheral speaking sealed GetState (R5-S7).

Test harness only, not emulator product code. It reuses the real ATT stack
(Bumble virtual link), the real GATT UUIDs/roles from plaudsim.profile, the
real GetState layout from PlaudDeviceState, and the synthetic sealed channel
from plaudsim.sealed. The only synthetic behavior is the sealed dispatch
itself, which PlaudPeripheral deliberately does not implement (it refuses
pv>=20 rather than serve cleartext under a modern banner).

Shares one SealedSession with the test client (loopback): the request seals
at seq 2, the response seals at seq 3. See plaudsim.sealed for why this
sharing is a documented simplification (real host/device counters are
independent; the device side is UNKNOWN).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from bumble.device import Connection, Device
from bumble.gatt import Attribute, Characteristic, CharacteristicValue, Service

from plaudsim.filesync import OPCODE_FILE_LIST, OPCODE_SYNC_START
from plaudsim.handshake import (
    CHUNK_SIZE,
    MARKER_FORCE_CLEAR,
    MARKER_PRE_HANDSHAKE,
    MARKER_RSA_MARKER,
    MARKER_SECRET,
    MARKERS,
    SECRET_MAGIC,
    chunk_bytes,
    pack_marker_frame,
    parse_marker_frame,
    reassemble_secret_chunks,
    split_secret_package,
)
from plaudsim.profile import (
    COMMAND_WRITE_UUID,
    DATA_NOTIFY_UUID,
    OPCODE_GET_STATE,
    PLAUD_SERVICE_UUID,
    PlaudDeviceState,
)
from plaudsim.sealed import (
    SYNTHETIC_J,
    SYNTHETIC_K,
    SYNTHETIC_L,
    SYNTHETIC_PORT_VERSION,
    SealedSession,
    open_raw,
    seal_raw,
)
from plaudsim.transfer import (
    FileTable,
    TransferSession,
    parse_file_list_request,
    parse_sync_start_request,
)
from protocol_trace import DEVICE_TO_HOST, HOST_TO_DEVICE, Trace


class SealedTestPeripheral:
    """Sealed peripheral test double: decrypt 2BB1 writes, answer sealed.

    Handles GetState (opcode 3), FileList (opcode 26) and SyncFile
    (opcode 28, HEAD + type-2 DATA + TAIL) using the frozen R3 builders,
    all sealed with the device-side session. `session` here is always the
    DEVICE endpoint: pass a dedicated SealedSession for independent
    counters (R5-S8), or the shared session for the R5-S7 loopback tests.
    """

    def __init__(
        self,
        device: Any,
        session: SealedSession,
        state: PlaudDeviceState | None = None,
        file_table: FileTable | None = None,
        file_bytes: bytes = b"",
        tail_crc: int = 0x1234,
        port_version: int = SYNTHETIC_PORT_VERSION,
    ) -> None:
        self.device = device
        self.session = session
        self.state = state or PlaudDeviceState()
        self.file_table = file_table or FileTable([], port_version=port_version)
        self.file_bytes = bytes(file_bytes)
        self.tail_crc = tail_crc
        self.port_version = port_version
        self.connection: Any = None
        self.requests: list[bytes] = []  # inner plaintext requests accepted
        self.drops = 0  # duplicate/stale/short frames silently dropped
        self.errors: list[str] = []  # AEAD failures + malformed inners
        self.sealed_out = 0  # sealed response frames emitted
        self.trace = Trace()  # capture-only; never influences wire bytes

        self.data_characteristic = Characteristic[bytes](
            DATA_NOTIFY_UUID,
            Characteristic.Properties.NOTIFY | Characteristic.Properties.INDICATE,
            Attribute.READABLE,
        )
        self.command_characteristic = Characteristic[bytes](
            COMMAND_WRITE_UUID,
            Characteristic.Properties.WRITE,
            Attribute.WRITEABLE,
            CharacteristicValue(write=self._on_command_write),
        )
        self.service = Service(
            PLAUD_SERVICE_UUID,
            [self.data_characteristic, self.command_characteristic],
        )
        device.on(Device.EVENT_CONNECTION, self._on_device_connection)

    def install(self) -> None:
        self.device.add_service(self.service)
        live = list(getattr(self.device, "connections", {}).values())
        if live:
            self._on_device_connection(live[0])

    def _on_device_connection(self, connection: Connection) -> None:
        self.connection = connection

    async def _on_command_write(self, connection: Any, value: bytes) -> None:
        event = self.trace.record(HOST_TO_DEVICE, "2BB1", bytes(value), result="received")
        try:
            inner = self.session.open(bytes(value))
        except Exception as exc:  # AEAD failure: log, no response, N untouched
            event.result = f"error:aead_failure:{type(exc).__name__}"
            self.errors.append(f"aead_failure: {type(exc).__name__}")
            return
        if inner is None:  # duplicate/stale/short: silent drop per z$c
            event.result = "dropped:duplicate-or-stale-or-short"
            self.drops += 1
            return
        event.result = "accepted"
        await self._dispatch_sealed(connection, inner)

    async def _dispatch_sealed(self, connection: Any, inner: bytes) -> None:
        if len(inner) < 3 or inner[0] != 1:
            self.errors.append(f"unsupported_inner: {inner.hex()}")
            return
        opcode = int.from_bytes(inner[1:3], "little")
        try:
            if opcode == OPCODE_GET_STATE:
                if len(inner) != 3:
                    raise ValueError("getState takes no payload")
                frames = [self.state.encode()]
            elif opcode == OPCODE_FILE_LIST:  # p2 -> q2 frames via frozen R3 table
                params = parse_file_list_request(inner)
                frames = self.file_table.frames(params["request_stamp"])
            elif opcode == OPCODE_SYNC_START:  # y6 -> HEAD + DATA + TAIL via R3
                params = parse_sync_start_request(inner)
                transfer = TransferSession(
                    file_bytes=self.file_bytes,
                    crc=self.tail_crc,
                    port_version=self.port_version,
                )
                transfer.start(params["session_id"], params["start"], params["end"])
                frames = transfer.frames()
            else:
                raise ValueError(f"unsupported_opcode: {opcode}")
        except ValueError as exc:
            self.errors.append(str(exc))
            return
        self.requests.append(inner)
        for frame in frames:
            opcode = int.from_bytes(frame[1:3], "little") if len(frame) >= 3 and frame[0] == 1 else None
            await self._emit(
                connection,
                self.session.seal(frame),
                parsed={"keyed": True, "opcode": opcode, "inner_len": len(frame)},
                phase="FILESYNC" if opcode in (26, 28, 29, 30) else "SEALED",
            )
            self.sealed_out += 1

    async def _emit(self, connection: Any, payload: bytes, parsed=None, phase=None) -> None:
        event = self.trace.record(DEVICE_TO_HOST, "2BB0", bytes(payload), result="sent")
        if parsed is not None:  # keyed interpretation; raw stays authoritative
            event.parsed = dict(parsed)
        if phase is not None:
            event.phase = phase
        server = self.device.gatt_server
        cccd = server.subscribers.get(connection, {}).get(
            self.data_characteristic.handle, b"\x00\x00"
        )
        if cccd[0] & 0x02:
            await server.indicate_subscriber(connection, self.data_characteristic, payload)
        else:
            await server.notify_subscriber(connection, self.data_characteristic, payload)

    async def _emit_raw(self, connection: Any, payload: bytes) -> None:
        """Pre-key marker emission: notify without sealing (plaintext phase)."""
        await self._emit(connection, payload)


# --- R5-S9 synthetic modern handshake (TEST-ONLY) ---------------------------
# Recovered mechanics (R5-S5/R5-S6, ledger section 4.3): v4 marker chunking
# (count+index, <=100 B), FE11 solicitation, w4 PEM chunking under 0xFE12,
# FE12 reassembly (dedupe/sort/concat frame[4:]), RSA/ECB/PKCS1Padding
# decrypt, J/K/L split with the PLAUD.AI self-check, then per-frame
# ChaCha seal with reset M=1/N=-1. All credential material below is
# deterministic synthetic harness policy, labeled as such.

SYNTHETIC_SN_SIGNATURE = b"SYNTHETIC-SN-SIGNATURE-" + b"0123456789ABCDEF" * 14  # 250 B
SYNTHETIC_DEVICE_TOKEN = "SYNTHETIC-TOKEN-0123456789ABCDEF"


def build_marker_frames(marker: int, payload: bytes) -> list[bytes]:
    """Chunk a pre-key payload exactly as q.n/q.a0 do (R5-S5)."""
    chunks = [payload[i : i + CHUNK_SIZE] for i in range(0, len(payload), CHUNK_SIZE)] or [b""]
    count = len(chunks)
    return [pack_marker_frame(marker, count, index, chunk) for index, chunk in enumerate(chunks)]


def reassemble_sn(frames: list[bytes]) -> bytes:
    """Concatenate v4/w4 chunk payloads in index order (z$c reassembly)."""
    return reassemble_secret_chunks([bytes(f) for f in frames])


def decrypt_secret_package(private_key, frames: list[bytes]) -> dict[str, bytes]:
    """Reassemble FE12 chunks, RSA-decrypt, split J/K/L, self-check (R5-S5).

    Mirrors the SDK: dedupe/sort/concat, RSA/ECB/PKCS1Padding with the
    PKCS8 private key, length>=56 guard, J/K/L slices, then the
    ChaCha open of the remainder must equal b"PLAUD.AI".
    """
    from cryptography.hazmat.primitives.asymmetric import padding

    blob = reassemble_secret_chunks([bytes(f) for f in frames])
    plaintext = private_key.decrypt(blob, padding.PKCS1v15())
    parts = split_secret_package(plaintext)
    if open_raw(parts["chacha_key"], parts["chacha_nonce"], parts["chacha_aad"], parts["sealed_magic"]) != SECRET_MAGIC:
        raise ValueError("synthetic secret self-check failed")
    return parts


class ModernTestPeripheral(SealedTestPeripheral):
    """Test-only pv>=20 peripheral: synthetic pre-key handshake, then sealed.

    Does NOT fork PlaudPeripheral (which correctly refuses pv>=20);
    extends the sealed test double with a pre-key marker phase. Before
    J/K/L installation `self.session` is an unkeyed placeholder that is
    never used for crypto; writes are dispatched on framing (marker vs
    sealed) exactly like z$c's pre/post-key branches.
    """

    def __init__(self, device: Any, *args: Any, **kwargs: Any) -> None:
        profile = kwargs.pop("profile", None)
        self.profile = profile if profile is not None else ModernProfile()
        kwargs.setdefault("port_version", 21)
        super().__init__(device, SealedSession(), *args, **kwargs)
        self.sealed_ready = False
        self.sn_chunks: list[bytes] = []  # held v4 frames (G equivalent)
        self.sn_received: bytes | None = None
        self.v4_frames: list[dict[str, object]] = []  # every v4 frame seen
        self.w4_frames: list[dict[str, object]] = []  # every w4 frame seen
        self.pubkey_chunks: list[bytes] = []
        self.marker_drops = 0  # pre-key duplicate chunks silently skipped

    async def _on_command_write(self, connection: Any, value: bytes) -> None:
        raw = bytes(value)
        event = self.trace.record(HOST_TO_DEVICE, "2BB1", raw, result="received")
        if len(raw) >= 4 and int.from_bytes(raw[0:2], "little") in MARKERS:
            await self._handle_marker(connection, raw, event)
            return
        if not self.sealed_ready:
            event.result = "error:sealed_frame_before_key_install"
            self.errors.append("sealed_frame_before_key_install")
            return
        try:
            inner = self.session.open(raw)
        except Exception as exc:
            event.result = f"error:aead_failure:{type(exc).__name__}"
            self.errors.append(f"aead_failure: {type(exc).__name__}")
            return
        if inner is None:
            event.result = "dropped:duplicate-or-stale-or-short"
            self.drops += 1
            return
        event.result = "accepted"
        opcode = int.from_bytes(inner[1:3], "little") if len(inner) >= 3 and inner[0] == 1 else None
        event.phase = "FILESYNC" if opcode in (26, 28, 29, 30) else "SEALED"
        event.parsed = {"keyed": True, "opcode": opcode, "inner_len": len(inner)}
        await self._dispatch_sealed(connection, inner)

    async def _handle_marker(self, connection: Any, raw: bytes, event=None) -> None:
        def outcome(text: str) -> None:
            if event is not None:
                event.result = text

        try:
            frame = parse_marker_frame(raw)
        except ValueError as exc:
            outcome(f"error:malformed_marker:{exc}")
            self.errors.append(f"malformed_marker: {exc}")
            return
        marker = int(frame["marker"])
        if marker in (MARKER_PRE_HANDSHAKE, MARKER_FORCE_CLEAR):
            self.v4_frames.append(frame)
            if self.sn_received is not None:
                outcome("dropped:batch-already-complete")
                self.marker_drops += 1  # idempotent once the batch completed
                return
            if (
                marker == MARKER_FORCE_CLEAR
                and self.profile.fe20_clear_policy == "index_zero"
                and int(frame["index"]) == 0
            ):
                # PRE_HANDSHAKE_AND_CLEAR resets the held-chunk buffer at the
                # START of the new transfer. Per-frame clearing would make the
                # SDK's own multi-chunk FE20 sends uncompletable, so the clear
                # fires on index 0 only (INFERRED boundary; device internals
                # are UNKNOWN, constrained by the SDK's sending pattern).
                # Policy "never" disables even that (see ModernProfile).
                self.sn_chunks.clear()
            if any(raw == held for held in self.sn_chunks):
                outcome("dropped:duplicate-chunk")
                self.marker_drops += 1  # byte-identical duplicate skipped
            else:
                self.sn_chunks.append(raw)
                outcome("received:chunk-pending")
            count = int(frame["count"])
            if len(self.sn_chunks) == count and self.sn_received is None:
                self.sn_received = reassemble_sn(self.sn_chunks)
                outcome("accepted:batch-complete")
                await self._emit_raw(
                    connection,
                    pack_marker_frame(MARKER_RSA_MARKER, 1, 0, self.profile.fe11_chunk),
                )
        elif marker == MARKER_SECRET:
            self.w4_frames.append(frame)
            if any(raw == held for held in self.pubkey_chunks):
                outcome("dropped:duplicate-chunk")
                self.marker_drops += 1
            else:
                self.pubkey_chunks.append(raw)
                outcome("received:chunk-pending")
            count = int(frame["count"])
            if len(self.pubkey_chunks) == count and not self.sealed_ready:
                outcome("accepted:batch-complete")
                await self._establish_secret(connection)
        else:  # FE11 inbound or unknown marker: never valid toward a device
            outcome(f"error:unexpected_marker:{marker:#x}")
            self.errors.append(f"unexpected_marker: {marker:#x}")

    async def _establish_secret(self, connection: Any) -> None:
        """Decrypt-free device side: build P, RSA-encrypt for the host key."""
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        pem = reassemble_sn(self.pubkey_chunks)  # w4 carries PEM TEXT chunks
        try:
            pubkey = load_pem_public_key(pem)
        except Exception as exc:
            self.errors.append(f"bad_host_pubkey: {type(exc).__name__}")
            return
        rest = seal_raw(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L, SECRET_MAGIC)
        plaintext = SYNTHETIC_J + SYNTHETIC_K + SYNTHETIC_L + rest
        ciphertext = pubkey.encrypt(plaintext, padding.PKCS1v15())
        for index, chunk in enumerate(chunk_bytes(ciphertext)):
            count = (len(ciphertext) + 99) // 100
            await self._emit_raw(
                connection, pack_marker_frame(MARKER_SECRET, count, index, chunk)
            )
        self.session = SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L)
        self.session.tx_seq = self.profile.device_tx_start
        self.sealed_ready = True


# --- R5-S11 policy isolation -------------------------------------------------
# Genuine device-side uncertainty lives here, visibly separated from
# SDK-proven mechanics. SDK-proven behavior (markers, chunking, RSA/J-K-L
# shapes, M/N rules) is deliberately NOT configurable.


class ModernProfile:
    """Explicit synthetic policy for device-side UNKNOWNs (R5-S11).

    HARNESS POLICY throughout -- none of these values is a claim about
    real hardware. Unknown policies raise instead of guessing.
    """

    def __init__(
        self,
        fe20_clear_policy: str = "index_zero",
        fe11_chunk: bytes = b"",
        device_tx_start: int = 1,
    ) -> None:
        if fe20_clear_policy not in ("index_zero", "never"):
            raise ValueError(
                f"unknown fe20_clear_policy {fe20_clear_policy!r}: record it, don't invent it"
            )
        if not 0 <= device_tx_start <= 0xFFFFFFFF:
            raise ValueError(f"device_tx_start out of u32 range: {device_tx_start}")
        self.fe20_clear_policy = fe20_clear_policy
        self.fe11_chunk = bytes(fe11_chunk)
        self.device_tx_start = device_tx_start

    def __repr__(self) -> str:
        return (
            f"ModernProfile(fe20_clear_policy={self.fe20_clear_policy!r}, "
            f"fe11_chunk={self.fe11_chunk!r}, device_tx_start={self.device_tx_start!r})"
        )
