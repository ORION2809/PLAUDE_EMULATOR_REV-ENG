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

Every hook here is a subclass override of a method the base class exposes
(`_emit`, `_on_command_write`, `_start_transfer`, `_serve_file_list`,
`_delete_file`, `_abort_stream`, `_on_disconnection`, `file_bytes_for`,
`transfer_streaming`). Task-mode streaming, inter-frame pacing, stream
abort on disconnection and stream error logging come from the base class
(`stream_in_task`, `response_pacing_s`, `PlaudPeripheral._stream`).
"""

from __future__ import annotations

import contextvars
import enum
import weakref
from dataclasses import dataclass, field
from struct import pack, unpack_from
from typing import Any

from plaudsim.filesync import (
    EMPTY_PACKAGE_OFFSET,
    FILE_DATA_TYPE,
    OPCODE_FILE_LIST,
    OPCODE_SYNC_HEAD,
    OPCODE_SYNC_TAIL,
    PROTOCOL_TYPE,
)
from plaudsim.profile import PlaudPeripheral
from plaudsim.transfer import (
    MAX_DATA_PAYLOAD_SIZE,
    pack_empty_package_frame,
    pack_sync_head,
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


@dataclass
class _StreamState:
    """Per-stream classification state: the stream's ordinal and kind, the
    per-kind frame counts, and a frame held back by a REORDER."""

    ordinal: int
    kind: Stream
    counts: dict[Kind, int] = field(default_factory=dict)
    held: tuple[bytes, FrameInfo] | None = None


class FaultInjector:
    """Pure stream transformer: frames in, (frames | link actions) out.

    `begin_stream()` must be called at the start of every burst the peripheral
    produces (a transfer, a file-list answer) so per-kind ordinals restart.

    Streams can overlap (a device that does not abandon a stream keeps an
    orphan emitting while the next one runs), so the per-stream state is
    keyed: `begin_stream(kind, key=k)` opens a stream under key `k`, and
    `process(frame, key=k)` classifies against THAT stream's ordinal and
    counts. A key that never began a stream (a control answer) gets fresh
    counts under the current stream ordinal. Without a key everything shares
    one default stream, which is the pure, single-stream use.
    """

    def __init__(self, faults: tuple[Fault, ...] | list[Fault] = (), port_version: int = 7) -> None:
        self.faults = list(faults)
        self.port_version = port_version
        self.stream = -1
        self.stream_kind = Stream.CONTROL
        self._default = _StreamState(-1, Stream.CONTROL)
        self._keyed: "weakref.WeakKeyDictionary[Any, _StreamState]" = weakref.WeakKeyDictionary()
        self.log: list[dict[str, Any]] = []

    def begin_stream(self, stream_kind: Stream, key: Any = None) -> int:
        previous = self._default if key is None else self._keyed.get(key)
        if previous is not None and previous.held is not None:
            # A reorder target that was the last frame of its stream has no
            # partner to swap with; it is dropped and that is recorded.
            self.log.append({"event": "held_frame_discarded", "info": previous.held[1]})
        self.stream += 1
        self.stream_kind = stream_kind
        state = _StreamState(self.stream, stream_kind)
        if key is None:
            self._default = state
        else:
            self._keyed[key] = state
        return self.stream

    def _state(self, key: Any) -> _StreamState:
        if key is None:
            return self._default
        state = self._keyed.get(key)
        if state is None:
            state = _StreamState(self.stream, Stream.CONTROL)
            self._keyed[key] = state
        return state

    def classify(self, frame: bytes, key: Any = None) -> FrameInfo:
        state = self._state(key)
        kind, opcode, sid, off = classify_frame(frame, self.port_version)
        idx = state.counts.get(kind, 0)
        state.counts[kind] = idx + 1
        return FrameInfo(kind, opcode, sid, off, idx, state.ordinal, state.kind)

    def process(self, frame: bytes, key: Any = None) -> list[bytes | LinkAction]:
        state = self._state(key)
        info = self.classify(frame, key)
        out: list[bytes | LinkAction] = [bytes(frame)]
        held_before = state.held
        for fault in self.faults:
            if not fault.matches(info):
                continue
            fault.note(info)
            self.log.append({"event": "fault", "kind": fault.kind.value, "label": fault.label, "info": info})
            out = self._apply(fault, info, out, state)
            if not out or any(isinstance(x, LinkAction) for x in out):
                break
        held_now = state.held is not None and state.held is not held_before
        if held_before is not None and not held_now:
            # The frame held back by a REORDER goes out right after its successor.
            state.held = None
            frames = [x for x in out if not isinstance(x, LinkAction)]
            actions = [x for x in out if isinstance(x, LinkAction)]
            out = frames + [held_before[0]] + actions
        return out

    def _apply(
        self, fault: Fault, info: FrameInfo, out: list[bytes | LinkAction], state: _StreamState
    ) -> list[bytes | LinkAction]:
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
            state.held = (head, info)
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


class _Scope:
    """Identity of one emission context: a write's own answers, or one stream.

    `FaultyPeripheral` keeps the current scope in a ContextVar. Every command
    write starts a fresh one; a y6 or a file-list answer replaces it with a
    stream scope. asyncio copies the context when it creates a task, so a
    task-mode stream keeps ITS scope for its whole life, whatever the client
    writes meanwhile. A CUT/DISCONNECT silences the scope it fired in, and the
    injector keys its per-stream ordinals by scope.
    """

    __slots__ = ("label", "__weakref__")

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"<scope {self.label} {id(self):#x}>"


#: Frames emitted outside any command write (unsolicited pushes) share this scope.
_UNSCOPED = _Scope("unscoped")
_EMIT_SCOPE: contextvars.ContextVar[_Scope] = contextvars.ContextVar("plaudsim_fault_scope", default=_UNSCOPED)


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

    Fault scope: a CUT or DISCONNECT silences the stream (or the write's own
    answers) it fired in -- and only that one. Other writes are answered
    normally, and they do NOT revive the silenced stream. Fault ordinals
    (`Fault.indices`, `Fault.streams`) are counted per stream, so an orphan
    stream keeps its own numbering while the next one runs.

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
                          one buffer for every session. With `files` (or
                          `file_revisions`) given, a y6 for a session in
                          neither is REFUSED: HEAD with status
                          `unknown_session_head_status` (default 1) and
                          nothing else, instead of serving some other file.
    file_revisions        {session_id: [rev0, rev1, ...]}: the n-th y6 for the
                          session serves revision n (the last one repeats) --
                          the file changing under an active sync. A CONTENT
                          fault: a restart then splices two revisions at the
                          right offsets, which the SDK cannot detect (review
                          T10; see docs/v2-fault-matrix.md).
    """

    def __init__(
        self,
        device: Any,
        *,
        faults: tuple[Fault, ...] | list[Fault] = (),
        pace: float | None = None,
        fit_payload_to_mtu: bool = False,
        files: dict[int, bytes] | None = None,
        file_revisions: dict[int, list[bytes]] | None = None,
        unknown_session_head_status: int = 1,
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
        self.file_revisions = {sid: [bytes(r) for r in revs] for sid, revs in (file_revisions or {}).items()}
        if any(not revs for revs in self.file_revisions.values()):
            raise ValueError("file_revisions needs at least one revision per session")
        self.unknown_session_head_status = unknown_session_head_status
        self.abandon_stream_on_restart = abandon_stream_on_restart
        self.cancel_stream_on_stop = cancel_stream_on_stop
        self.cancel_stream_on_delete = cancel_stream_on_delete
        self._silenced: "weakref.WeakSet[_Scope]" = weakref.WeakSet()
        self._orphans: list[Any] = []                # streams deliberately left running
        self._orphan_links: dict[Any, Any] = {}
        self._sync_starts: dict[int, int] = {}
        self.emitted: list[dict[str, Any]] = []     # what actually left the device

    # --- request entry -----------------------------------------------------

    async def _on_command_write(self, connection: Any, value: bytes) -> None:
        # A fresh scope for this write's own answers. A stream it starts gets
        # its own scope in _start_transfer / _serve_file_list.
        _EMIT_SCOPE.set(_Scope("write"))
        self._silenced.discard(_UNSCOPED)            # a push-level CUT lasts until the next write
        return await super()._on_command_write(connection, value)

    # --- stream lifecycle (base hooks) ---------------------------------------

    @property
    def transfer_streaming(self) -> bool:
        return super().transfer_streaming or any(not t.done() for t in self._orphans)

    def _orphan(self, reason: str) -> None:
        task = self._stream_task
        if task is not None and not task.done():
            self._orphans.append(task)
            self._orphan_links[task] = self._stream_connection
            self.stream_log.append({"event": "not_abandoned", "reason": reason})
        self._stream_task = None
        self._stream_connection = None

    def _abort_stream(self, reason: str) -> None:
        """Base hook, called by PlaudPeripheral after a y6 ("new_sync_start")
        or z6 ("stop_sync") parsed and on disconnection, and by this class for w6."""
        if reason == "new_sync_start" and not self.abandon_stream_on_restart:
            return self._orphan(reason)
        if reason == "stop_sync" and not self.cancel_stream_on_stop:
            return self._orphan(reason)
        super()._abort_stream(reason)

    def _on_disconnection(self, connection: Any) -> None:
        super()._on_disconnection(connection)
        for task in list(self._orphans):
            if self._orphan_links.get(task) is connection and not task.done():
                task.cancel()
                self.stream_log.append({"event": "aborted", "reason": "disconnected", "orphan": True})

    # --- handlers with policy hooks -------------------------------------------

    def _knows_session(self, session_id: int) -> bool:
        if session_id in self.file_revisions or session_id in self.files:
            return True
        return not self.files and not self.file_revisions

    def file_bytes_for(self, session_id: int) -> bytes:
        revisions = self.file_revisions.get(session_id)
        if revisions:
            n = max(self._sync_starts.get(session_id, 0), 1) - 1
            return revisions[min(n, len(revisions) - 1)]
        if session_id in self.files:
            return self.files[session_id]
        if self.files or self.file_revisions:
            raise KeyError(f"session {session_id} is not on this device")
        return self.file_bytes

    def _start_transfer(self, request: bytes) -> list[bytes]:
        params = parse_sync_start_request(request)
        sid = params["session_id"]
        scope = _Scope(f"transfer:{sid}")
        _EMIT_SCOPE.set(scope)
        self.injector.begin_stream(Stream.TRANSFER, key=scope)
        if not self._knows_session(sid):
            # HARNESS_POLICY: refuse, visibly. R8: a HEAD status > 0 is a
            # failed head for the SDK; what real firmware answers is UNKNOWN.
            self.transfer = None
            self.stream_log.append({"event": "unknown_session", "session_id": sid})
            return [pack_sync_head(sid, self.unknown_session_head_status)]
        self._sync_starts[sid] = self._sync_starts.get(sid, 0) + 1
        if self.fit_payload_to_mtu:
            conn = self._live_connection()
            mtu = int(getattr(conn, "att_mtu", 23))
            # HARNESS_POLICY: fit one notification. ATT_HANDLE_VALUE_NTF has a
            # 3-byte header, so att_mtu - 3 bytes of value fit in one PDU.
            self.data_payload_size = max(1, min(MAX_DATA_PAYLOAD_SIZE, mtu - 3 - data_header_len(self.port_version)))
        return super()._start_transfer(request)

    def _serve_file_list(self, request: bytes) -> list[bytes]:
        scope = _Scope("file_list")
        _EMIT_SCOPE.set(scope)
        self.injector.begin_stream(Stream.FILE_LIST, key=scope)
        return super()._serve_file_list(request)

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

    async def _emit(self, connection: Any, payload: bytes) -> bool:
        scope = _EMIT_SCOPE.get()
        if scope in self._silenced:
            self.emitted.append({"suppressed": True, "bytes": bytes(payload).hex()})
            return False
        for item in self.injector.process(bytes(payload), key=scope):
            if item is LinkAction.DISCONNECT:
                self._silenced.add(scope)
                self.stream_log.append({"event": "link_dropped_by_device"})
                await connection.disconnect()
                return False
            if item is LinkAction.CUT:
                self._silenced.add(scope)
                self.stream_log.append({"event": "device_went_silent"})
                return False
            kind, opcode, sid, off = classify_frame(item, self.port_version)
            delivered = await super()._emit(connection, item)
            self.emitted.append(
                {"kind": kind.value, "opcode": opcode, "offset": off, "len": len(item), "delivered": delivered}
            )
        return True

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
