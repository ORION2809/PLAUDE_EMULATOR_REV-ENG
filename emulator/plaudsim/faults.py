"""Fault injection for the Plaud emulator (V2: "survives a fault-injection
suite without corrupting a transfer").

Everything in this module is HARNESS_POLICY unless a comment says otherwise.
A *fault* is something the harness does to the emulator's OUTBOUND frame
stream -- drop, duplicate, reorder, truncate, corrupt, mislabel, interleave,
cut the link -- and never a claim about what real Plaud firmware does. The
point of the exercise is the other side of the link: the reconstructed SDK
receiver (`tests/fault_support.py`) must either reassemble the served file
byte-for-byte or report a detected failure, and never accept wrong bytes
silently.

Layering, kept deliberately thin:

    FaultInjector      pure: classifies each outbound frame and applies the
                       matching faults; no Bumble, no lifecycle
    FaultyPeripheral   PlaudPeripheral subclass that routes every emitted
                       frame through the injector, and adds the few device
                       behaviours the matrix needs that the base peripheral
                       leaves open (MTU-fitted frames, per-session file
                       content, a device that does NOT abandon its stream)

`profile.py`, `transfer.py` and `filesync.py` are NOT modified; every hook
here is a subclass override of a method the base class already exposes
(`_emit`, `_on_command_write`, `_start_transfer`, `_serve_file_list`,
`_stop_transfer`, `_delete_file`, `_abort_stream`, `_stream`). Task-mode
streaming and inter-frame pacing come from the base class's own R7-S13
parameters (`stream_in_task`, `response_pacing_s`).
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass, field
from struct import pack, unpack_from
from typing import Any

from plaudsim.filesync import (
    EMPTY_PACKAGE_OFFSET,
    FILE_DATA_TYPE,
    OPCODE_FILE_LIST,
    OPCODE_SYNC_HEAD,
    OPCODE_SYNC_START,
    OPCODE_SYNC_TAIL,
    PROTOCOL_TYPE,
)
from plaudsim.profile import PlaudPeripheral
from plaudsim.transfer import (
    MAX_DATA_PAYLOAD_SIZE,
    pack_empty_package_frame,
    parse_delete_file_request,
    parse_sync_start_request,
)

# --- frame classification ------------------------------------------------

#: Outbound frame kinds the injector can target. The classification mirrors
#: how the SDK's dispatcher tells frames apart (z$c.onCharacteristicChanged,
#: ALL.txt:72280+): byte 0 is the protocol type; for type 1 the u16le opcode
#: at offset 1 selects the handler; type 2 is bulk file data, and within type
#: 2 the offset 0xFFFFFFFF marks the EMPTY_PACKAGE sentinel (q$a.a,
#: ALL.txt:69024-69026) that closes a transfer (R7-S13, RUNTIME_PROVEN).
class Kind(str, enum.Enum):
    HEAD = "head"            # type 1, opcode 28 (s6)
    DATA = "data"            # type 2, a real payload frame (q$a.a bulk path)
    EMPTY = "empty_package"  # type 2, offset 0xFFFFFFFF (q$a.a finish/restart branch)
    TAIL = "tail"            # type 1, opcode 29 (t6)
    FILE_LIST = "file_list"  # type 1, opcode 26 (q2)
    OTHER = "other"          # anything else the peripheral emits
    ANY = "any"              # wildcard for Fault.target only


class Stream(str, enum.Enum):
    """What a burst of outbound frames belongs to (for per-stream ordinals)."""

    TRANSFER = "transfer"
    FILE_LIST = "file_list"
    CONTROL = "control"


@dataclass(frozen=True)
class FrameInfo:
    kind: Kind
    opcode: int | None
    session_id: int | None
    offset: int | None        # DATA only; EMPTY_PACKAGE_OFFSET for the sentinel
    index: int                # ordinal of this KIND within the current stream
    stream: int               # ordinal of the stream since the injector was built
    stream_kind: Stream


#: Modern (portVersion >= 7) type-2 header: [u8 2][u32 sid][u32 off][u8 len].
#: Legacy (< 7): [u8 2][u32 off][u8 len]. Layout BYTECODE_PROVEN (q$a.a sets
#: i = 5 iff q.m >= 7, ALL.txt:68975-68998); see filesync.parse_file_data_frame.
def data_header_len(port_version: int) -> int:
    return 10 if port_version >= 7 else 6


def classify_frame(frame: bytes, port_version: int) -> tuple[Kind, int | None, int | None, int | None]:
    """(kind, opcode, session_id, offset) for one outbound frame."""
    raw = bytes(frame)
    if not raw:
        return Kind.OTHER, None, None, None
    if raw[0] == FILE_DATA_TYPE:
        modern = port_version >= 7
        sid = unpack_from("<I", raw, 1)[0] if modern and len(raw) >= 5 else None
        i = 5 if modern else 1
        off = unpack_from("<I", raw, i)[0] if len(raw) >= i + 4 else None
        return (Kind.EMPTY if off == EMPTY_PACKAGE_OFFSET else Kind.DATA), None, sid, off
    if raw[0] == PROTOCOL_TYPE and len(raw) >= 3:
        opcode = unpack_from("<H", raw, 1)[0]
        sid = unpack_from("<I", raw, 3)[0] if opcode in (OPCODE_SYNC_HEAD, OPCODE_SYNC_TAIL) and len(raw) >= 7 else None
        if opcode == OPCODE_SYNC_HEAD:
            return Kind.HEAD, opcode, sid, None
        if opcode == OPCODE_SYNC_TAIL:
            return Kind.TAIL, opcode, sid, None
        if opcode == OPCODE_FILE_LIST:
            return Kind.FILE_LIST, opcode, None, None
        return Kind.OTHER, opcode, None, None
    return Kind.OTHER, None, None, None


# --- faults ----------------------------------------------------------------


class FaultKind(str, enum.Enum):
    DROP = "drop"                          # frame never leaves the device
    DUPLICATE = "duplicate"                # frame sent twice back-to-back
    REORDER = "reorder"                    # frame swapped with the NEXT frame of its stream
    TRUNCATE = "truncate"                  # frame cut to `param` bytes
    CORRUPT = "corrupt"                    # byte `param[0]` XORed with `param[1]`
    WRONG_SESSION = "wrong_session"        # type-2 session id rewritten to `param`
    HEAD_STATUS = "head_status"            # HEAD status byte rewritten to `param`
    INTERLEAVE_BEFORE = "interleave_before"  # `param` (bytes) emitted before the frame
    INTERLEAVE_AFTER = "interleave_after"    # `param` (bytes) emitted after the frame
    EMPTY_PACKAGE_BEFORE = "empty_package_before"  # EMPTY_PACKAGE(code=param) before the frame
    EMPTY_PACKAGE_AFTER = "empty_package_after"    # EMPTY_PACKAGE(code=param) after the frame
    DISCONNECT_AFTER = "disconnect_after"  # frame is sent, then the device drops the link
    CUT_AFTER = "cut_after"                # frame is sent, then the device goes silent for this stream
    CUT_BEFORE = "cut_before"              # the device goes silent starting with this frame


class LinkAction(enum.Enum):
    DISCONNECT = "disconnect"
    CUT = "cut"


@dataclass
class Fault:
    """One injected fault. HARNESS_POLICY by definition.

    kind      what to do (FaultKind)
    target    which frame kind it applies to (Kind; ANY for wildcard). The
              EMPTY_PACKAGE sentinel is Kind.EMPTY, not Kind.DATA, so a
              DATA fault never touches the completion frame by accident.
    offsets   DATA offsets it applies to (None = every DATA frame)
    indices   ordinals within the stream of that kind (None = any). For a
              file-list page this is the page number; for TAIL/HEAD/EMPTY it
              is 0.
    streams   stream ordinals the fault is active in (None = any stream)
    once      fire at most once per matching key (offset for DATA, else
              index). A one-shot fault is what lets the matrix separate "the
              client recovers" from "the device is permanently broken".
    param     kind-specific parameter (see FaultKind)
    """

    kind: FaultKind
    target: Kind = Kind.DATA
    offsets: tuple[int, ...] | None = None
    indices: tuple[int, ...] | None = None
    streams: tuple[int, ...] | None = None
    opcodes: tuple[int, ...] | None = None   # type-1 opcodes (for target OTHER/ANY)
    once: bool = True
    param: Any = None
    label: str = ""
    fired: list[tuple[int, int, int | None]] = field(default_factory=list)

    def matches(self, info: FrameInfo) -> bool:
        if self.target is not Kind.ANY and info.kind is not self.target:
            return False
        if self.streams is not None and info.stream not in self.streams:
            return False
        if self.opcodes is not None and info.opcode not in self.opcodes:
            return False
        if self.offsets is not None and (info.kind is not Kind.DATA or info.offset not in self.offsets):
            return False
        if self.indices is not None and info.index not in self.indices:
            return False
        if self.once:
            key = info.offset if info.kind is Kind.DATA else info.index
            if any(f[2] == key for f in self.fired):
                return False
        return True

    def note(self, info: FrameInfo) -> None:
        key = info.offset if info.kind is Kind.DATA else info.index
        self.fired.append((info.stream, info.index, key))


class FaultInjector:
    """Pure stream transformer: frames in, (frames | link actions) out.

    `begin_stream()` must be called at the start of every burst the peripheral
    produces (a transfer, a file-list answer) so per-kind ordinals restart.
    """

    def __init__(self, faults: tuple[Fault, ...] | list[Fault] = (), port_version: int = 7) -> None:
        self.faults = list(faults)
        self.port_version = port_version
        self.stream = -1
        self.stream_kind = Stream.CONTROL
        self._counts: dict[Kind, int] = {}
        self._held: tuple[bytes, FrameInfo] | None = None
        self.log: list[dict[str, Any]] = []

    def begin_stream(self, stream_kind: Stream) -> None:
        if self._held is not None:
            # A reorder target that was the last frame of its stream has no
            # partner to swap with; it is dropped and that is recorded.
            self.log.append({"event": "held_frame_discarded", "info": self._held[1]})
            self._held = None
        self.stream += 1
        self.stream_kind = stream_kind
        self._counts = {}

    def classify(self, frame: bytes) -> FrameInfo:
        kind, opcode, sid, off = classify_frame(frame, self.port_version)
        idx = self._counts.get(kind, 0)
        self._counts[kind] = idx + 1
        return FrameInfo(kind, opcode, sid, off, idx, self.stream, self.stream_kind)

    def process(self, frame: bytes) -> list[bytes | LinkAction]:
        info = self.classify(frame)
        out: list[bytes | LinkAction] = [bytes(frame)]
        held_before = self._held
        for fault in self.faults:
            if not fault.matches(info):
                continue
            fault.note(info)
            self.log.append({"event": "fault", "kind": fault.kind.value, "label": fault.label, "info": info})
            out = self._apply(fault, info, out)
            if not out or any(isinstance(x, LinkAction) for x in out):
                break
        held_now = self._held is not None and self._held is not held_before
        if held_before is not None and not held_now:
            # The frame held back by a REORDER goes out right after its successor.
            self._held = None
            frames = [x for x in out if not isinstance(x, LinkAction)]
            actions = [x for x in out if isinstance(x, LinkAction)]
            out = frames + [held_before[0]] + actions
        return out

    def _apply(self, fault: Fault, info: FrameInfo, out: list[bytes | LinkAction]) -> list[bytes | LinkAction]:
        frames = [x for x in out if isinstance(x, (bytes, bytearray))]
        actions = [x for x in out if isinstance(x, LinkAction)]
        if not frames:
            return out
        head, rest = bytes(frames[0]), [bytes(x) for x in frames[1:]]
        k = fault.kind
        if k is FaultKind.DROP:
            return rest + actions
        if k is FaultKind.DUPLICATE:
            return [head, head] + rest + actions
        if k is FaultKind.REORDER:
            self._held = (head, info)
            return rest + actions
        if k is FaultKind.TRUNCATE:
            return [head[: int(fault.param)]] + rest + actions
        if k is FaultKind.CORRUPT:
            at, mask = fault.param
            b = bytearray(head)
            if at < len(b):
                b[at] ^= mask
            return [bytes(b)] + rest + actions
        if k is FaultKind.WRONG_SESSION:
            if info.kind in (Kind.DATA, Kind.EMPTY) and self.port_version >= 7 and len(head) >= 5:
                head = head[:1] + pack("<I", int(fault.param)) + head[5:]
            return [head] + rest + actions
        if k is FaultKind.HEAD_STATUS:
            if info.kind is Kind.HEAD and len(head) >= 8:
                head = head[:7] + bytes([int(fault.param) & 0xFF]) + head[8:]
            return [head] + rest + actions
        if k is FaultKind.INTERLEAVE_BEFORE:
            return [bytes(fault.param), head] + rest + actions
        if k is FaultKind.INTERLEAVE_AFTER:
            return [head, bytes(fault.param)] + rest + actions
        if k is FaultKind.EMPTY_PACKAGE_BEFORE:
            sid = info.session_id if info.session_id is not None else 0
            return [pack_empty_package_frame(int(fault.param), sid, self.port_version), head] + rest + actions
        if k is FaultKind.EMPTY_PACKAGE_AFTER:
            sid = info.session_id if info.session_id is not None else 0
            return [head, pack_empty_package_frame(int(fault.param), sid, self.port_version)] + rest + actions
        if k is FaultKind.DISCONNECT_AFTER:
            return [head] + rest + [LinkAction.DISCONNECT]
        if k is FaultKind.CUT_AFTER:
            return [head] + rest + [LinkAction.CUT]
        if k is FaultKind.CUT_BEFORE:
            return [LinkAction.CUT]
        raise ValueError(f"unknown fault kind {k}")


# --- the peripheral ---------------------------------------------------------


class FaultyPeripheral(PlaudPeripheral):
    """PlaudPeripheral whose outbound frames pass through a FaultInjector.

    Emission mode (the base class's R7-S13 machinery, selected by `pace`):

    pace = None   inline: HEAD, DATA..., EMPTY_PACKAGE, TAIL are emitted inside
                  the y6 write handler, before the ATT write response. The
                  whole stream is therefore queued at the central before its
                  client can react, which is the in-process equivalent of
                  R7-S13 run 6 ("the emulator kept draining the old stream"):
                  nothing can be abandoned. Fine for cells without recovery;
                  for gap cells it IS the run-6 device.
    pace = float  `stream_in_task=True, response_pacing_s=pace`: the base
                  class streams from a cancellable task with that inter-frame
                  gap, answers the y6 write immediately, and aborts the task
                  on a new y6 or a z6 (`PlaudPeripheral._abort_stream`) --
                  the behaviour under which the genuine SDK's gap recovery
                  converged with exactly one restart (runs 6b/6c/6d).

    Extra device behaviours, all HARNESS_POLICY (real firmware: UNKNOWN):

    abandon_stream_on_restart
                          False: a new y6 does NOT cancel the running stream,
                          so both drain concurrently (the run-6 device in
                          paced form). Default True (runs 6b-10).
    cancel_stream_on_stop a z6 stop cancels a running stream (default True).
    cancel_stream_on_delete
                          a w6 delete of the session being streamed cancels
                          the stream. What a real device does when the file
                          under an active sync is deleted is UNKNOWN.
    fit_payload_to_mtu    size each DATA frame to the connection's ATT MTU
                          (payload = att_mtu - 3 - header). Nothing in the SDK
                          pins the device's frame size (ledger S12); the base
                          peripheral's fixed 32 bytes overflows a 23-byte MTU
                          and Bumble's GATT server then truncates the
                          notification (gatt_server.py:425-426), which is the
                          "unsized" fault the matrix also exercises.
    files                 {session_id: bytes}: per-session content, so two
                          files can be synced back to back. The base serves
                          one buffer for every session.
    """

    def __init__(
        self,
        device: Any,
        *,
        faults: tuple[Fault, ...] | list[Fault] = (),
        pace: float | None = None,
        fit_payload_to_mtu: bool = False,
        files: dict[int, bytes] | None = None,
        abandon_stream_on_restart: bool = True,
        cancel_stream_on_stop: bool = True,
        cancel_stream_on_delete: bool = True,
        **kwargs: Any,
    ) -> None:
        if pace is not None:
            kwargs.setdefault("stream_in_task", True)
            kwargs.setdefault("response_pacing_s", float(pace))
        super().__init__(device, **kwargs)
        self.injector = FaultInjector(faults, port_version=self.port_version)
        self.pace = pace
        self.fit_payload_to_mtu = fit_payload_to_mtu
        self.files = dict(files or {})
        self.abandon_stream_on_restart = abandon_stream_on_restart
        self.cancel_stream_on_stop = cancel_stream_on_stop
        self.cancel_stream_on_delete = cancel_stream_on_delete
        self._suppressed = False
        self._orphans: list[Any] = []                # streams deliberately left running
        self.emitted: list[dict[str, Any]] = []     # what actually left the device

    # --- request entry -----------------------------------------------------

    async def _on_command_write(self, connection: Any, value: bytes) -> None:
        self._suppressed = False                     # a CUT silences one stream only
        return await super()._on_command_write(connection, value)

    # --- stream lifecycle (base hooks) ---------------------------------------

    def _abort_stream(self, reason: str) -> None:
        """Base hook, called by PlaudPeripheral before y6 ("new_sync_start")
        and z6 ("stop_sync") are handled, and by this class for w6."""
        if reason == "new_sync_start" and not self.abandon_stream_on_restart:
            task = self._stream_task
            if task is not None and not task.done():
                self._orphans.append(task)
                self.stream_log.append({"event": "not_abandoned", "reason": reason})
            self._stream_task = None
            return
        if reason == "stop_sync" and not self.cancel_stream_on_stop:
            task = self._stream_task
            if task is not None and not task.done():
                self._orphans.append(task)
                self.stream_log.append({"event": "not_abandoned", "reason": reason})
            self._stream_task = None
            return
        super()._abort_stream(reason)

    async def _stream(self, connection: Any, frames: list[bytes]) -> None:
        try:
            await super()._stream(connection, frames)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a dead link mid-stream is an expected outcome
            self.stream_log.append({"event": "error", "error": type(exc).__name__})

    # --- handlers with policy hooks -------------------------------------------

    def _start_transfer(self, request: bytes) -> list[bytes]:
        params = parse_sync_start_request(request)
        if params["session_id"] in self.files:
            self.file_bytes = self.files[params["session_id"]]
        if self.fit_payload_to_mtu:
            conn = self._live_connection()
            mtu = int(getattr(conn, "att_mtu", 23))
            # HARNESS_POLICY: fit one notification. ATT_HANDLE_VALUE_NTF has a
            # 3-byte header, so att_mtu - 3 bytes of value fit in one PDU.
            self.data_payload_size = max(1, min(MAX_DATA_PAYLOAD_SIZE, mtu - 3 - data_header_len(self.port_version)))
        self.injector.begin_stream(Stream.TRANSFER)
        return super()._start_transfer(request)

    def _serve_file_list(self, request: bytes) -> list[bytes]:
        self.injector.begin_stream(Stream.FILE_LIST)
        return super()._serve_file_list(request)

    def _stop_transfer(self, request: bytes) -> list[bytes]:
        # The base already aborted the stream (or this class kept it, see
        # _abort_stream) before dispatching here.
        return super()._stop_transfer(request)

    def _delete_file(self, request: bytes) -> list[bytes]:
        params = parse_delete_file_request(request)
        active = self.transfer
        if (
            self.cancel_stream_on_delete
            and active is not None
            and active.session_id == params["session_id"]
        ):
            super()._abort_stream("delete_of_streaming_session")
            self.transfer = None
        return super()._delete_file(request)

    # --- transport ---------------------------------------------------------

    async def _emit(self, connection: Any, payload: bytes) -> None:
        if self._suppressed:
            self.emitted.append({"suppressed": True, "bytes": bytes(payload).hex()})
            return
        for item in self.injector.process(bytes(payload)):
            if item is LinkAction.DISCONNECT:
                self._suppressed = True
                self.stream_log.append({"event": "link_dropped_by_device"})
                await connection.disconnect()
                return
            if item is LinkAction.CUT:
                self._suppressed = True
                self.stream_log.append({"event": "device_went_silent"})
                return
            kind, opcode, sid, off = classify_frame(item, self.port_version)
            self.emitted.append({"kind": kind.value, "opcode": opcode, "offset": off, "len": len(item)})
            await super()._emit(connection, item)

    def stream_events(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.stream_log if e.get("event") == event]


__all__ = [
    "Fault",
    "FaultInjector",
    "FaultKind",
    "FaultyPeripheral",
    "FrameInfo",
    "Kind",
    "LinkAction",
    "Stream",
    "classify_frame",
    "data_header_len",
]
