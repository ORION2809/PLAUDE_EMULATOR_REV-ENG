"""Host-side model of the Plaud SDK's file-transfer receiver, and the drivers
that run it over a Bumble link, for the V2 fault matrix.

WHAT THIS MIRRORS, AND FROM WHERE
---------------------------------
The Android SDK's receiver for one `syncFile` is the inner class
`com.plaud.sdk.proto.q$a` (build/evidence/javap/ALL.txt:68831-69574; jadx
form: build/evidence/jadx-out/sources/com/plaud/sdk/proto/q.java:88-330).
Its fields, as used below (names DIRECT from the bytecode):

    a  long     the host cursor: the next offset it will accept   (`cursor`)
    b  boolean  a loss recovery has been kicked off               (`resend_pending`)
    c  boolean  this receiver has handed over to a restart        (`stopped`)
    d  long     stall-timer budget, 5000 ms                        (`timer_budget`)
    e  executor the running stall timer                            (`timer_task`)
    g  long     the `start` this receiver was created with         (`start`)
    h  boolean  isResend (stored, never read)
    i  long     sessionId;  j long end;  o the owning `q`
    n  v3       the data sink: receiveVoiceData(bytes, offset) / finish(code)

Shared, q-level state: `q.H` ("recovery in flight", read through q.j at
ALL.txt:45087-45091, written through q.d at :45113-45119) and `q.m`
(portVersion, q.l at :45093-45097). The request registry is `z.t`
(z.a([I[B..) registers, build/evidence/javap/com/plaud/sdk/proto/z.txt:2203;
z.a(int) looks up NEWEST-FIRST and evicts older matches, ALL.txt:75489-75535,
jadx z.java:1565-1580).

The rules, each BYTECODE_PROVEN at the cited lines of q$a.a(byte[])
(ALL.txt:68962-69395; jadx q.java:183-330):

  R1  type-2 frame, modern layout: session id u32@1 must equal `i`, else the
      frame is dropped ("---sessionId miss match---", :68987-69001) with NO
      other effect.
  R2  offset u32@i; offset == 0xFFFFFFFF is EMPTY_PACKAGE (:69024-69026) and
      the code byte is read at i+5 (:69031-69035). With H false: ignored when
      cursor == start or b is set, else `v3.finish(code)` (:69100-69108;
      jadx q.java:257-266). With H true: ignored when c or code == 1; else
      c = true, timer cancelled, restart from the cursor
      ("---isPacketLossStopSync---", :69041-69099).
      >>> finish(code) is THE completion of a transfer: bleDataComplete on the
      raw path, "download complete" on the export path. RUNTIME_PROVEN by
      R7-S13 (r7/r7-s13-recording-pull.md S3.4, runs 3, 4b, 8): the genuine
      SDK completed ONLY when this frame arrived after the last DATA frame
      and before the TAIL; HEAD.DATA.TAIL alone never completed (runs 1, 2,
      9); EMPTY after TAIL never completed (run 7, see R12); codes 0 and 1
      both completed on the normal path (runs 4b, 8).
  R3  diff = offset - cursor. diff == 0 -> accept (:69026, :69120-69122).
  R4  diff != 0 and H -> arm/extend the 5 s timer and return (:69123-69128).
  R5  diff < 0 -> return, silently (:69129-69131). Duplicates and frames from
      behind the cursor are dropped without any recovery.
  R6  diff > 0, H false, b false -> b = true and the "fileSyncLossPkgStop"
      runnable (:69132-69182; body q$a.f at :69396-69418): q.k writes z6
      stopSync (q.k sets H = false first, ALL.txt:46662-46665, then registers
      response opcode 30 and writes `new z6().enPkg()`, :46666-46689); then
      H = true (:69413-69415); then a() arms the timer (:69417). The a7 ack's
      callback (:69420-69441): if c return; cancel timer; c = true; restart
      from the cursor. diff > 0 with b already set -> return (:69132-69135).
      RUNTIME (R7-S13 S4.2, runs 6b/6c/6d): the z6 goes on the wire 0-35 ms
      after "start resend ...", and y6(cursor) follows the a7 ~35 ms after
      the gap -- NOT after the 5 s timer. The timer is the stall path (R10).
  R7  accept: b = false; len u8@(i+4); clamp `len` to the frame end
      (:69196-69214); copy; cursor += len (:69216-69225); receiveVoiceData
      (payload, offset) (:69230).
  R8  HEAD (type 1, opcode 28, :69238-69289): parse s6; `failed` when the
      parse throws or status > 0, in which case z.c(29) marks the state;
      the head callback fires iff (!b || failed).
  R9  TAIL (type 1, opcode 29, :69295-69395): if !H and cursor != start ->
      z.c(29) and the tail callback with the parsed t6 (bleSyncFileTail).
      >>> That callback is NOT completion: nothing in it touches the sink
      (v3) and the transfer request stays pending (RUNTIME: runs 1/1b/2 --
      bleSyncFileTail fired, no bleDataComplete, export stuck at 99 %, the
      op-queue later reported -98 on the pending [28,29] request). Otherwise:
      if c return; c = true; cancel timer; restart from the cursor. A TAIL
      with no progress is therefore ALWAYS a restart.
  R10 the stall timer (q$a.b(JJ..) at :69491-69557, the Runnable stored in
      q$a.f at :68906-68919): counts d down from 5000 in 10 ms steps; on
      expiry, if H and the executor is alive and !c -> c = true and restart
      from the cursor. a() (:68928-68950) sets d = 5000 and starts the
      executor only if none is running -- i.e. it arms OR extends. It is
      armed ONLY by R4 and R6: a TAIL without an EMPTY_PACKAGE arms nothing,
      so q$a itself never times such a transfer out.
  R11 restart == q.a(JJJZ..) (ALL.txt:39870-40135; q.txt:1220-1263): H = false;
      register {28, 29}; write y6(sessionId, cursor, end); a NEW q$a with
      a = g = cursor, b = c = false. z.z() is a no-op in this build
      (jadx z.java:399-400).
  R12 dispatch (z$c.onCharacteristicChanged cleartext path, ALL.txt:72280+;
      jadx z.java:619-745): type 2 -> the registration for opcode 29
      ("File Sync Callback is Null" when none, ALL.txt:72342, z.java:642-646);
      type 1 -> the registration for its opcode; opcodes
      {11,19,26,28,35,50,52,61,143} are sticky, every other response opcode
      (29 TAIL, 30 a7 included) removes its registration before the callback
      runs (z.java:700-712). CONSEQUENCE: the TAIL unregisters the receiver,
      so an EMPTY_PACKAGE that arrives AFTER the TAIL has no receiver and
      cannot finish -- which is what run 7 observed.
  R13 the TAIL crc is parsed (t6) and forwarded; no SDK call site compares it
      (ledger S5.8; RUNTIME: 0xBEEF and 0 behave identically, runs 1b/2).
      TntBleCommUtils.readInt is native, so what a frame shorter than its
      header does inside the SDK is UNKNOWN; the model reports such a frame
      as `undecodable` instead of guessing.

HARNESS_POLICY (things the SDK does not fix, chosen here and labelled):

  P1  `stall_timeout`: the SDK's constant is 5.0 s (R10); tests may scale it
      down for speed. Whatever value is used, the *mechanism* is R10's.
  P2  `idle_timeout`: the SDK's receiver has NO quiescence timer of its own
      (R10's timer is armed only by R4/R6). A transfer that simply stops
      (device went silent) or that ended with a TAIL but no EMPTY_PACKAGE
      would wait forever at q$a level; any give-up is app-level (the
      tinnotech op-queue's -98 in R7-S13 run 1b) and its timing is UNKNOWN.
      The driver reports `stalled` (no TAIL seen) or `stalled_after_tail`
      (TAIL callback fired, no finish) after `idle_timeout` seconds without
      a frame so a test terminates.
  P3  `max_restarts`: q$a restarts without bound. The driver caps restarts
      and reports `restart_cap_exceeded` so a livelock terminates.
  P4  `expected_size`: at R2 completion the driver compares the cursor with
      the file-list `fileSize` (BleFile.fileSize, ledger S5.6) and reports
      `finish_short` when data is missing. The SDK does not do this; whether
      the consumer app does is UNKNOWN. Without it a lost last frame would
      be a silently short file, which is exactly what V2 must never accept.
  P5  app-level resume: after `finish_short`, `link_lost` or `app_stopped`
      the matrix re-issues syncFile(sessionId, cursor, 0) through the public
      entry point (PlaudDeviceAgent.syncFile(sessionId, start, end), ledger
      S5.8) -- the SDK's own primitive, driven by harness policy.
  P6  the data sink writes each accepted payload at its offset and REFUSES
      an offset that is not the sink length: by R3 that cannot happen, so a
      violation is a model bug, not a device outcome.
  P7  post-finish drain: after finish(code) the receiver is still registered
      (nothing in R2 unregisters it), so the driver keeps dispatching for up
      to `idle_timeout` more seconds until the TAIL callback (R9 + R12
      unregistration) is seen. Bookkeeping actions in that window are
      recorded; a recovery action (restart, loss_recovery, a7) is recorded
      in `post_finish` and NOT executed, so a device that misbehaves after
      completion cannot drag the driver into a second transfer.
  P8  not modelled: everything above q$a's sink. Run 6 (R7-S13) showed the
      genuine export layer producing a CORRUPTED file (fixture[0:4800] ||
      fixture[3200:11271]) when the device kept draining an abandoned stream
      and the -98/-99 request retries re-issued the download while the
      writer appended. q$a only ever accepts DATA at its cursor, so this
      model's sink is always a prefix of the served file; the corruption
      lives in the export writer + op-queue retry, which this model does not
      contain. The matrix therefore reports the run-6 signature as
      `corruption_risk` instead of claiming byte-exactness for the real SDK.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from struct import unpack_from
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from bumble.device import Connection, Peer
from bumble.testing.test_utils import Devices, TwoDevices

from support import SDK_REQUESTED_MTU, discover, load_fixture, role_uuids

from plaudsim.filesync import (
    EMPTY_PACKAGE_OFFSET,
    FILE_DATA_TYPE,
    OPCODE_DELETE_FILE_RSP,
    OPCODE_FILE_LIST,
    OPCODE_STOP_SYNC_RSP,
    OPCODE_SYNC_HEAD,
    OPCODE_SYNC_TAIL,
    PROTOCOL_TYPE,
    pack_delete_file_request,
    pack_file_list_request,
    pack_stop_sync_request,
    pack_sync_start,
    parse_sync_head,
    parse_sync_tail,
)
from plaudsim.transfer import FileListAccumulator

SERVICE_UUID, DATA_UUID, COMMAND_UUID = role_uuids(load_fixture("post-bind-get-storage.json"))

#: q$a.d initialiser and a() re-arm value: `ldc2_w 5000l` (ALL.txt:68906, :68930).
SDK_STALL_TIMEOUT_S = 5.0
#: q$a.b(JJ..) counts the budget down in `Thread.sleep(10)` steps (ALL.txt:69498-69510).
SDK_STALL_TICK_S = 0.010
#: z$c's sticky response opcodes (jadx z.java:700-708; z$c.txt:1222 lookupswitch).
STICKY_OPCODES = frozenset({11, 19, 26, 28, 35, 50, 52, 61, 143})


class ReceiverUndecodable(Exception):
    """A frame too short for the SDK's fixed-offset reads (R13: UNKNOWN)."""


@dataclass
class Action:
    kind: str
    receiver: "SdkReceiver | None" = None
    offset: int | None = None
    payload: bytes = b""
    value: Any = None


@dataclass
class Registration:
    opcodes: frozenset[int]
    handler: Callable[[bytes], list[Action]]
    label: str


class SdkHost:
    """q-level and z-level state shared by successive receivers."""

    def __init__(self, port_version: int = 7) -> None:
        self.port_version = port_version           # q.m
        self.recovery_in_flight = False            # q.H
        self.state: int | None = None              # z.n via z.c(int)
        self.registrations: list[Registration] = []  # z.t
        self.log: list[str] = []

    # z.a([I[B..): append a registration (z.txt:2203)
    def register(self, opcodes: set[int], handler: Callable[[bytes], list[Action]], label: str) -> Registration:
        reg = Registration(frozenset(opcodes), handler, label)
        self.registrations.append(reg)
        return reg

    # z.a(int): newest match wins, older matches are evicted (ALL.txt:75489-75535)
    def lookup(self, opcode: int) -> Registration | None:
        found: Registration | None = None
        for reg in reversed(list(self.registrations)):
            if opcode in reg.opcodes:
                if found is None:
                    found = reg
                else:
                    self.registrations.remove(reg)
                    self.log.append(f"registry: evicted older {reg.label}")
        return found

    # z$c.onCharacteristicChanged, cleartext branch (R12)
    def dispatch(self, raw: bytes) -> list[Action]:
        if not raw:
            return [Action("other", value="empty")]
        t = raw[0]
        if t == FILE_DATA_TYPE:
            reg = self.lookup(OPCODE_SYNC_TAIL)
            if reg is None:
                self.log.append("File Sync Callback is Null")
                return [Action("dropped_no_receiver", offset=None)]
            return reg.handler(raw)
        if t == PROTOCOL_TYPE and len(raw) >= 3:
            opcode = unpack_from("<H", raw, 1)[0]
            reg = self.lookup(opcode)
            if reg is None:
                return [Action("other", value=opcode)]
            if opcode not in STICKY_OPCODES:
                self.registrations.remove(reg)
            return reg.handler(raw)
        return [Action("other", value=f"type {t}")]


class SdkReceiver:
    """One q$a instance (R1-R10)."""

    def __init__(self, host: SdkHost, session_id: int, start: int, end: int, is_resend: bool) -> None:
        self.host = host
        self.cursor = start                 # a
        self.resend_pending = False         # b
        self.stopped = False                # c
        self.timer_budget = SDK_STALL_TIMEOUT_S  # d
        self.timer_task: asyncio.Task[None] | None = None  # e
        self.start = start                  # g
        self.is_resend = is_resend          # h
        self.session_id = session_id        # i
        self.end = end                      # j
        self.timer_expiries: "asyncio.Queue[Any] | None" = None
        self.stall_timeout = SDK_STALL_TIMEOUT_S
        self.events: list[str] = []

    # q$a.a(): arm or extend (R10)
    def arm_timer(self) -> None:
        self.timer_budget = self.stall_timeout
        if self.timer_task is None or self.timer_task.done():
            self.timer_task = asyncio.get_running_loop().create_task(self._countdown())
            self.events.append("timer_armed")
        else:
            self.events.append("timer_extended")

    # q$a.b(): shutdown (R10)
    def cancel_timer(self) -> None:
        if self.timer_task is not None:
            self.timer_task.cancel()
            self.timer_task = None
            self.events.append("timer_cancelled")

    async def _countdown(self) -> None:
        # q$a.b(JJ..): while d >= 0 { sleep 10; d -= 10; bail if executor gone }
        tick = SDK_STALL_TICK_S
        while self.timer_budget >= 0:
            await asyncio.sleep(tick)
            self.timer_budget -= tick
        if self.host.recovery_in_flight and not self.stopped and self.timer_expiries is not None:
            self.timer_expiries.put_nowait(("timer", self))

    def _restart(self, why: str) -> list[Action]:
        self.stopped = True
        self.cancel_timer()
        self.events.append(f"restart:{why}")
        return [Action("restart", receiver=self, offset=self.cursor, value=why)]

    def on_frame(self, raw: bytes) -> list[Action]:
        t = raw[0]
        if t == PROTOCOL_TYPE:
            opcode = unpack_from("<H", raw, 1)[0]
            if opcode == OPCODE_SYNC_HEAD:                      # R8
                try:
                    s6: dict[str, Any] | None = parse_sync_head(raw)
                except ValueError:
                    s6 = None
                failed = s6 is None or int(s6["status"]) > 0
                if failed:
                    self.host.state = 29
                if not self.resend_pending or failed:
                    return [Action("head", receiver=self, value=s6, payload=b"failed" if failed else b"")]
                return [Action("head_suppressed", receiver=self, value=s6)]
            if opcode == OPCODE_SYNC_TAIL:                      # R9
                if not self.host.recovery_in_flight and self.cursor != self.start:
                    self.host.state = 29
                    try:
                        t6: dict[str, Any] | None = parse_sync_tail(raw)
                    except ValueError:
                        t6 = None
                    self.events.append("tail_callback")
                    return [Action("tail", receiver=self, offset=self.cursor, value=t6)]
                if self.stopped:
                    return [Action("tail_ignored_stopped", receiver=self)]
                return self._restart("tail_without_progress" if self.cursor == self.start else "tail_while_recovering")
            return [Action("other", value=opcode)]
        if t != FILE_DATA_TYPE:
            return [Action("other", value=f"type {t}")]

        i = 1
        if self.host.port_version >= 7:                         # R1
            if len(raw) < 5:
                raise ReceiverUndecodable(f"{len(raw)}-byte type-2 frame: no session id")
            sid = unpack_from("<I", raw, 1)[0]
            if sid != self.session_id:
                self.events.append("session_mismatch")
                return [Action("session_mismatch", receiver=self, value=sid)]
            i = 5
        if len(raw) < i + 4:
            raise ReceiverUndecodable(f"{len(raw)}-byte type-2 frame: no offset")
        offset = unpack_from("<I", raw, i)[0]
        if offset == EMPTY_PACKAGE_OFFSET:                      # R2
            if len(raw) < i + 6:
                raise ReceiverUndecodable(f"{len(raw)}-byte EMPTY_PACKAGE: no code")
            code = raw[i + 5]
            if not self.host.recovery_in_flight:
                if self.cursor == self.start or self.resend_pending:
                    return [Action("empty_package_ignored", receiver=self, value=code)]
                self.events.append(f"finish:{code}")
                return [Action("finish", receiver=self, offset=self.cursor, value=code)]
            if self.stopped or code == 1:
                return [Action("empty_package_ignored", receiver=self, value=code)]
            return self._restart("empty_package_while_recovering")
        diff = offset - self.cursor
        if diff != 0:
            if self.host.recovery_in_flight:                    # R4
                self.arm_timer()
                return [Action("gap_while_recovering", receiver=self, offset=offset)]
            if diff < 0:                                        # R5
                self.events.append("dropped_behind_cursor")
                return [Action("dropped_behind_cursor", receiver=self, offset=offset)]
            if self.resend_pending:                             # R6, b already set
                return [Action("gap_ignored_pending", receiver=self, offset=offset)]
            self.resend_pending = True
            self.events.append(f"gap:{self.cursor}->{offset}")
            return [Action("loss_recovery", receiver=self, offset=offset)]
        if len(raw) < i + 5:
            raise ReceiverUndecodable(f"{len(raw)}-byte type-2 frame: no length byte")
        self.resend_pending = False                             # R7
        declared = raw[i + 4]
        i4 = i + 5
        length = declared if declared + i4 <= len(raw) else len(raw) - i4
        payload = raw[i4 : i4 + length]
        self.cursor += length
        return [Action("data", receiver=self, offset=offset, payload=payload, value=declared)]


@dataclass
class TransferResult:
    session_id: int
    data: bytes
    complete: bool                 # v3.finish(code) fired (R2) -- bleDataComplete
    failure: str | None            # None, or why the driver stopped
    cursor: int
    restarts: int                  # SDK-level syncFileStart re-issues (R6/R9/R2/R10)
    stop_syncs: int                # z6 writes (R6)
    timer_restarts: int            # restarts that came from R10
    tail_seen: bool                # the R9 tail callback fired (bleSyncFileTail)
    tail_crc: int | None
    head_statuses: list[int]
    finish_codes: list[int]
    post_finish: list[str]         # recovery actions seen after finish, not executed (P7)
    session_mismatches: int
    dropped_behind: int
    dropped_no_receiver: int
    other_frames: int
    data_frames: int
    events: list[str]
    elapsed_s: float


class SdkTransferDriver:
    """Runs SdkHost/SdkReceiver against a live Bumble peer (P1-P7)."""

    def __init__(
        self,
        peer: Peer,
        data_char: Any,
        command_char: Any,
        connection: Connection | None = None,
        *,
        port_version: int = 7,
        stall_timeout: float = SDK_STALL_TIMEOUT_S,   # P1
        idle_timeout: float = 1.0,                     # P2
        max_restarts: int = 12,                        # P3
        prefer_notify: bool = True,
    ) -> None:
        self.peer = peer
        self.data_char = data_char
        self.command_char = command_char
        self.connection = connection
        self.port_version = port_version
        self.stall_timeout = stall_timeout
        self.idle_timeout = idle_timeout
        self.max_restarts = max_restarts
        self.prefer_notify = prefer_notify
        self.inbox: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.frames: list[bytes] = []
        self.link_lost = False
        self.host = SdkHost(port_version)

    async def attach(self) -> None:
        await self.peer.subscribe(self.data_char, self._on_value, prefer_notify=self.prefer_notify)
        if self.connection is not None:
            self.connection.on(Connection.EVENT_DISCONNECTION, self._on_disconnect)

    def _on_value(self, value: Any) -> None:
        raw = bytes(value)
        self.frames.append(raw)
        self.inbox.put_nowait(("frame", raw))

    def _on_disconnect(self, *_: Any) -> None:
        self.link_lost = True
        self.inbox.put_nowait(("disconnected", None))

    async def write(self, payload: bytes) -> None:
        await self.peer.write_value(self.command_char, payload, with_response=True)

    async def get_state(self) -> bytes | None:
        """Control-plane probe used by the second-central cell."""
        await self.write(b"\x01\x03\x00")
        try:
            kind, raw = await asyncio.wait_for(self.inbox.get(), self.idle_timeout)
        except asyncio.TimeoutError:
            return None
        return raw if kind == "frame" else None

    # --- q.a(JJJZ..): syncFileStart (R11) --------------------------------------

    async def _sync_file_start(self, st: "_Run", cursor: int, is_resend: bool) -> str | None:
        if is_resend:
            st.restarts += 1
            if st.restarts > self.max_restarts:
                return "restart_cap_exceeded"                  # P3
        self.host.recovery_in_flight = False
        receiver = SdkReceiver(self.host, st.session_id, cursor, st.end, is_resend)
        receiver.stall_timeout = self.stall_timeout
        receiver.timer_expiries = self.inbox
        st.receiver = receiver
        st.receivers.append(receiver)
        self.host.register({OPCODE_SYNC_HEAD, OPCODE_SYNC_TAIL}, receiver.on_frame, f"syncFile#{st.restarts}")
        try:
            await self.write(pack_sync_start(st.session_id, cursor, st.end))
        except Exception as exc:                               # the link died under the write
            return f"write_failed:{type(exc).__name__}"
        return None

    # --- the fileSyncLossPkgStop runnable (R6) ----------------------------------

    async def _loss_recovery(self, st: "_Run", receiver: SdkReceiver) -> str | None:
        self.host.recovery_in_flight = False                   # q.k, first thing

        def on_a7(raw: bytes) -> list[Action]:                 # the a7 callback lambda
            return [Action("a7", receiver=receiver)]

        self.host.register({OPCODE_STOP_SYNC_RSP}, on_a7, "stopSync")
        st.stop_syncs += 1
        try:
            await self.write(pack_stop_sync_request())         # z6 on the wire, synchronously on the gap
        except Exception as exc:
            return f"write_failed:{type(exc).__name__}"
        self.host.recovery_in_flight = True
        receiver.arm_timer()
        return None

    async def sync_file(
        self,
        session_id: int,
        start: int = 0,
        end: int = 0,
        *,
        expected_size: int | None = None,                      # P4
        sink: bytearray | None = None,
        on_progress: Callable[[int], Awaitable[bool]] | None = None,
    ) -> TransferResult:
        st = _Run(session_id, start, end, sink if sink is not None else bytearray())
        t0 = time.monotonic()
        # HARNESS_POLICY: a new syncFile call starts from an empty inbox. The
        # only items that can be pending here are a timer expiry from a run the
        # driver already gave up on, or frames that arrived after `stalled` was
        # declared; the SDK would drop the latter ("File Sync Callback is
        # Null", R12) because the old registration is gone.
        while not self.inbox.empty():
            self.inbox.get_nowait()
        failure = await self._sync_file_start(st, start, is_resend=False)
        while failure is None and not st.complete:
            try:
                kind, item = await asyncio.wait_for(self.inbox.get(), self.idle_timeout)
            except asyncio.TimeoutError:
                # P2: nothing in q$a times this out. `stalled_after_tail` is
                # the HEAD.DATA.TAIL-without-EMPTY_PACKAGE case of R7-S13 runs
                # 1/2/9 (request pending forever at SDK level).
                failure = "stalled_after_tail" if st.tail_seen else "stalled"
                break
            if kind == "disconnected":
                failure = "link_lost"
                break
            if kind == "timer":                                 # R10 expiry
                receiver = item
                if receiver.timer_task is None or receiver.stopped or not self.host.recovery_in_flight:
                    continue
                st.timer_restarts += 1
                receiver.stopped = True
                receiver.cancel_timer()
                failure = await self._sync_file_start(st, receiver.cursor, is_resend=True)
                continue
            raw = item
            try:
                actions = self.host.dispatch(raw)
            except ReceiverUndecodable as exc:
                failure = f"undecodable:{exc}"                  # R13
                break
            for act in actions:
                failure = await self._execute(st, act)
                if failure is not None or st.complete:
                    break
            if on_progress is not None and failure is None and not st.complete:
                if await on_progress(st.receiver.cursor if st.receiver else 0):
                    failure = await self._app_stop(st)
        if failure is None and st.complete and not st.tail_seen and not self.link_lost:
            await self._drain_after_finish(st)                  # P7
        if failure is None and st.complete and expected_size is not None and st.receiver is not None:
            if st.receiver.cursor < expected_size:
                failure = "finish_short"                        # P4
        for r in st.receivers:
            r.cancel_timer()
        receiver = st.receiver
        return TransferResult(
            session_id=session_id,
            data=bytes(st.sink),
            complete=st.complete,
            failure=failure,
            cursor=receiver.cursor if receiver else start,
            restarts=st.restarts,
            stop_syncs=st.stop_syncs,
            timer_restarts=st.timer_restarts,
            tail_seen=st.tail_seen,
            tail_crc=st.tail_crc,
            head_statuses=st.head_statuses,
            finish_codes=st.finish_codes,
            post_finish=st.post_finish,
            session_mismatches=st.session_mismatches,
            dropped_behind=st.dropped_behind,
            dropped_no_receiver=st.dropped_no_receiver,
            other_frames=st.other_frames,
            data_frames=st.data_frames,
            events=[e for r in st.receivers for e in r.events] + self.host.log,
            elapsed_s=time.monotonic() - t0,
        )

    async def _drain_after_finish(self, st: "_Run") -> None:
        """P7: keep dispatching after finish(code) until the TAIL callback or
        `idle_timeout`. The receiver is still registered at this point (R2
        unregisters nothing; R9+R12 do), exactly as in the SDK, but recovery
        actions are recorded rather than executed."""
        deadline = time.monotonic() + self.idle_timeout
        while not st.tail_seen:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                kind, item = await asyncio.wait_for(self.inbox.get(), remaining)
            except asyncio.TimeoutError:
                break
            if kind == "disconnected":
                st.post_finish.append("link_lost")
                break
            if kind != "frame":
                continue
            try:
                actions = self.host.dispatch(item)
            except ReceiverUndecodable as exc:
                st.post_finish.append(f"undecodable:{exc}")
                continue
            for act in actions:
                if act.kind in ("loss_recovery", "a7", "restart"):
                    st.post_finish.append(act.kind)
                    continue
                await self._execute(st, act)

    async def _app_stop(self, st: "_Run") -> str:
        """HARNESS_POLICY (P5): an app-initiated stop. The SDK's own stop
        primitive is q.k (R6); here the harness drives it from outside the
        receiver, then waits for the a7 so the emulator's stream is provably
        cancelled before the caller resumes."""
        self.host.register({OPCODE_STOP_SYNC_RSP}, lambda raw: [Action("a7_app")], "appStop")
        st.stop_syncs += 1
        await self.write(pack_stop_sync_request())
        deadline = time.monotonic() + self.idle_timeout
        while time.monotonic() < deadline:
            try:
                kind, item = await asyncio.wait_for(self.inbox.get(), self.idle_timeout)
            except asyncio.TimeoutError:
                break
            if kind == "frame":
                acts = self.host.dispatch(item)
                if any(a.kind == "a7_app" for a in acts):
                    return "app_stopped"
                for a in acts:                                  # data still in flight is accepted (R3)
                    if a.kind == "data":
                        self._sink_write(st, a)
        return "app_stopped_no_ack"

    def _sink_write(self, st: "_Run", act: Action) -> None:
        if act.offset != len(st.sink):                          # P6
            raise AssertionError(f"model bug: accepted offset {act.offset} at sink length {len(st.sink)}")
        st.sink.extend(act.payload)
        st.data_frames += 1

    async def _execute(self, st: "_Run", act: Action) -> str | None:
        k = act.kind
        if k == "data":
            self._sink_write(st, act)
        elif k == "loss_recovery":
            return await self._loss_recovery(st, act.receiver)
        elif k == "a7":
            r = act.receiver
            if not r.stopped:
                r.cancel_timer()
                r.stopped = True
                r.events.append("restart:a7")
                return await self._sync_file_start(st, r.cursor, is_resend=True)
        elif k == "restart":
            return await self._sync_file_start(st, act.receiver.cursor, is_resend=True)
        elif k == "finish":                                     # R2: THE completion
            st.finish_codes.append(int(act.value))
            st.complete = True
        elif k == "tail":                                       # R9: callback only
            st.tail_seen = True
            st.tail_crc = None if act.value is None else int(act.value["crc"])
        elif k == "head":
            st.head_statuses.append(-1 if act.value is None else int(act.value["status"]))
        elif k == "session_mismatch":
            st.session_mismatches += 1
        elif k == "dropped_behind_cursor":
            st.dropped_behind += 1
        elif k == "dropped_no_receiver":
            st.dropped_no_receiver += 1
        elif k == "other":
            st.other_frames += 1
        return None

    # --- file list (s5 + q2 mirror) --------------------------------------------

    async def get_file_list(self, request_stamp: int, start_session_id: int = 0) -> tuple[FileListAccumulator, list[str]]:
        acc = FileListAccumulator(request_stamp=request_stamp, port_version=self.port_version)
        verdicts: list[str] = []
        await self.write(pack_file_list_request(request_stamp, start_session_id))
        while not acc.complete:
            try:
                kind, raw = await asyncio.wait_for(self.inbox.get(), self.idle_timeout)
            except asyncio.TimeoutError:
                verdicts.append("stalled")
                break
            if kind != "frame":
                verdicts.append(kind)
                break
            if raw[0] == PROTOCOL_TYPE and unpack_from("<H", raw, 1)[0] == OPCODE_FILE_LIST:
                verdicts.append(acc.ingest_frame(raw))
            else:
                verdicts.append("other")
        return acc, verdicts

    async def delete_file(self, session_id: int) -> bool:
        await self.write(pack_delete_file_request(session_id))
        try:
            kind, raw = await asyncio.wait_for(self.inbox.get(), self.idle_timeout)
        except asyncio.TimeoutError:
            return False
        return kind == "frame" and raw[0] == PROTOCOL_TYPE and unpack_from("<H", raw, 1)[0] == OPCODE_DELETE_FILE_RSP


@dataclass
class _Run:
    session_id: int
    start: int
    end: int
    sink: bytearray
    receiver: SdkReceiver | None = None
    receivers: list[SdkReceiver] = field(default_factory=list)
    complete: bool = False
    tail_seen: bool = False
    restarts: int = 0
    stop_syncs: int = 0
    timer_restarts: int = 0
    tail_crc: int | None = None
    head_statuses: list[int] = field(default_factory=list)
    finish_codes: list[int] = field(default_factory=list)
    post_finish: list[str] = field(default_factory=list)
    session_mismatches: int = 0
    dropped_behind: int = 0
    dropped_no_receiver: int = 0
    other_frames: int = 0
    data_frames: int = 0


# --- app-level resume (P5) -------------------------------------------------

RESUMABLE = ("finish_short", "link_lost", "app_stopped")


@dataclass
class ResumeTrace:
    results: list[TransferResult]

    @property
    def final(self) -> TransferResult:
        return self.results[-1]

    @property
    def resumes(self) -> int:
        return len(self.results) - 1


async def sync_with_app_resume(
    make_driver: Callable[[], Awaitable[SdkTransferDriver]],
    session_id: int,
    expected_size: int,
    *,
    max_resumes: int = 3,
) -> ResumeTrace:
    """Drive syncFile and, on a resumable failure, re-issue it from the cursor
    (the public syncFile(sessionId, start, end) entry) through whatever
    driver `make_driver` hands back (the same one, or one on a new link)."""
    sink = bytearray()
    driver = await make_driver()
    trace = ResumeTrace([await driver.sync_file(session_id, 0, 0, expected_size=expected_size, sink=sink)])
    while trace.final.failure in RESUMABLE and trace.resumes < max_resumes and trace.final.cursor < expected_size:
        driver = await make_driver()
        trace.results.append(
            await driver.sync_file(session_id, trace.final.cursor, 0, expected_size=expected_size, sink=sink)
        )
    return trace


# --- link helpers -----------------------------------------------------------


@dataclass
class Link:
    devices: Devices
    peripheral: Any
    peer: Peer
    data: Any
    command: Any
    connection: Connection
    central_index: int = 0


async def bring_up(
    peripheral_factory: Callable[[Any], Any],
    *,
    mtu: int | None = SDK_REQUESTED_MTU,
) -> Link:
    """Link + MTU (the SDK's order, see tests/support.py) + discovery."""
    devices = await TwoDevices.create_with_connection()
    peripheral = peripheral_factory(devices[1])
    peripheral.install()
    connection = devices.connections[0]
    peer = Peer(connection)
    if mtu is not None:
        await peer.request_mtu(mtu)
    peer, data, command = await discover(devices, SERVICE_UUID, DATA_UUID, COMMAND_UUID)
    return Link(devices, peripheral, peer, data, command, connection)


async def reconnect(link: Link, *, mtu: int | None = SDK_REQUESTED_MTU) -> Link:
    """After a disconnection: peripheral advertises again, the central connects,
    MTU + discovery are redone (a new ATT bearer has no subscriptions)."""
    devices = link.devices
    central, peripheral_device = devices[0], devices[1]
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    peripheral_device.once(peripheral_device.EVENT_CONNECTION, fut.set_result)
    await peripheral_device.start_advertising(advertising_interval_min=1.0)
    await central.connect(peripheral_device.random_address)
    await asyncio.wait_for(fut, 5)
    await asyncio.sleep(0)
    connection = devices.connections[0]
    peer = Peer(connection)
    if mtu is not None:
        await peer.request_mtu(mtu)
    peer, data, command = await discover(devices, SERVICE_UUID, DATA_UUID, COMMAND_UUID)
    return Link(devices, link.peripheral, peer, data, command, connection)


async def bring_up_two_centrals(peripheral_factory: Callable[[Any], Any], *, mtu: int = SDK_REQUESTED_MTU) -> tuple[Link, Callable[[], Awaitable[Link]]]:
    """Device 1 is the peripheral; device 0 connects now; the returned coroutine
    connects device 2 later (the peripheral re-advertises while connected)."""
    devices = Devices(3)
    for d in devices.devices:
        await d.power_on()
    per = devices[1]
    peripheral = peripheral_factory(per)
    peripheral.install()

    async def connect(index: int) -> Link:
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        per.once(per.EVENT_CONNECTION, fut.set_result)
        await per.start_advertising(advertising_interval_min=1.0)
        await devices[index].connect(per.random_address)
        await asyncio.wait_for(fut, 5)
        await asyncio.sleep(0)
        connection = devices.connections[index]
        peer = Peer(connection)
        await peer.request_mtu(mtu)
        await peer.discover_services()
        await peer.discover_characteristics()
        await peer.discover_descriptors()
        service = next(s for s in peer.services if str(s.uuid).lower() == SERVICE_UUID)
        data = next(c for c in service.characteristics if str(c.uuid).lower() == DATA_UUID)
        command = next(c for c in service.characteristics if str(c.uuid).lower() == COMMAND_UUID)
        return Link(devices, peripheral, peer, data, command, connection, central_index=index)

    first = await connect(0)
    return first, lambda: connect(2)


async def driver_for(link: Link, **kwargs: Any) -> SdkTransferDriver:
    d = SdkTransferDriver(link.peer, link.data, link.command, link.connection, **kwargs)
    await d.attach()
    return d


# --- outcome vocabulary -----------------------------------------------------

RECOVERED = "recovered"                     # byte-exact through the SDK's own paths (finish fired)
RECOVERED_BY_APP_RESUME = "recovered_by_app_resume"   # byte-exact only after P4/P5
DETECTED = "detected"                       # the model reported failure; bytes are a prefix
#: The run-6 signature (R7-S13): the client restarted more than once because
#: the device kept serving a stream it should have abandoned. The receiver
#: model's sink is still a prefix (P6), but the genuine SDK's export layer
#: produced a corrupted file under exactly this trace (P8), so the matrix
#: refuses to call it "recovered".
CORRUPTION_RISK = "corruption_risk"
NOT_TESTABLE = "n-a"


def outcome_of(result: TransferResult, served: bytes, app_resumes: int = 0) -> str:
    """The V2 invariant, as a function. Raises on silent corruption."""
    if not served.startswith(result.data):
        raise AssertionError("SILENT CORRUPTION: accepted bytes are not a prefix of the served file")
    if result.complete and result.failure is None and result.data == served:
        return RECOVERED if app_resumes == 0 else RECOVERED_BY_APP_RESUME
    if result.failure is not None:
        return DETECTED
    raise AssertionError(f"neither byte-exact nor detected: {result}")


def run6_signature(result: TransferResult) -> bool:
    """True when the trace carries the R7-S13 run-6 shape: more than one
    SDK-level restart, at least one of them caused by a frame of the stream
    the client had already abandoned (its EMPTY_PACKAGE while H was set, or
    its TAIL landing on the fresh receiver)."""
    stale = ("restart:empty_package_while_recovering", "restart:tail_without_progress", "restart:tail_while_recovering")
    return result.restarts >= 2 and any(e in stale for e in result.events)
