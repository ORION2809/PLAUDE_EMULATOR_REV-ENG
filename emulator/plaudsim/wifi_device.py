"""Device side of the Wi-Fi bulk-transfer channel: an asyncio WebSocket CLIENT.

WHAT THIS IS. The PEN's half of protocol ledger section 7: it dials the phone's
java-websocket server (`ws://<phone>:8081`), opens with SayHello, answers the
phone's Handshake, keeps the session alive with HeartbeatPing, and serves
GetFileList / FileSync / FileSyncContent / FileDelete / FileSyncStop /
ExtendExitTime / SpeedTest / GetDeviceLog from an in-memory file store, then
announces WifiClose and hangs up.

WHAT THIS IS NOT (scope boundary, deliberate):
* No SoftAP. Real hardware raises a WPA2 hotspot named "Plaud"+last-4-of-SN
  with passphrase last-8-of-SN (WifiAgentImpl.access$calculateWifiName /
  calculateWifiPassword, WifiAgentImpl.txt:1676-1703); the phone joins it
  (WifiConnectionManager) and only then runs the server. This emulator ASSUMES
  IP connectivity already exists and dials whatever host/port it is given.
* No OTA (types 20/21/22): the codecs exist in `wifi.py`, the behaviour is
  logged as unsupported and never answered.
* No real credentials. The handshake token check is HARNESS_POLICY.

EVERY DEVICE-SIDE CHOICE BELOW IS HARNESS_POLICY unless a citation says
otherwise: the SDK dumps show what the PHONE sends and accepts; how the pen
behaves is recovered only where the phone's code constrains it (the pen must
speak first, must deliver FileSyncContent in order, must answer within 30 s,
must send exactly one FileList frame, ...). Timings such as the heartbeat
cadence, the "self-close after ~3 heartbeats" and the "~2 min exit timeout"
are CLAIMS from template-app comments (docs/product-evidence/
agent-findings-2026-09-23.json entries "device self-disconnects the WiFi
session after 3 heartbeats and otherwise times out in ~2-2.5 min"), not
recovered from firmware; they are modelled as configurable policy.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from plaudsim.transfer import FileTable
from plaudsim.wifi import (
    MSG_EXTEND_EXIT_TIME,
    MSG_FILE_DELETE,
    MSG_FILE_SYNC,
    MSG_FILE_SYNC_STOP,
    MSG_GET_DEVICE_LOG,
    MSG_GET_FILE_LIST,
    MSG_HANDSHAKE,
    MSG_HEARTBEAT,
    MSG_OTA_STATUS_SYNC,
    MSG_REQUEST_OTA_PACKAGE,
    MSG_SAY_HELLO,
    MSG_SEND_OTA_FILE_INFO,
    MSG_SPEED_TEST,
    MESSAGE_NAMES,
    MAX_TOTAL_SIZE,
    PDU_HEADER_LEN,
    PDU_VERSION_HOST,
    WS_PORT,
    ExtendExitTimeRequest,
    ExtendExitTimeResponse,
    FileDeleteRequest,
    FileDeleteResponse,
    FileListResponse,
    FileRecord,
    FileSyncContent,
    FileSyncRequest,
    FileSyncStatus,
    FileSyncStopRequest,
    FileSyncStopResponse,
    GetDeviceLogRequest,
    GetDeviceLogResponse,
    GetFileListRequest,
    HandshakeRequest,
    HandshakeResponse,
    HeartbeatPing,
    HeartbeatPong,
    HelloAck,
    Pdu,
    SayHello,
    SpeedTestRequest,
    SpeedTestResponse,
    WifiClose,
    WifiMessage,
    WifiSealer,
    looks_encrypted,
    pad_wifi_token,
    parse_pdu,
)

# HARNESS_POLICY defaults. None of these is a device claim.
DEFAULT_CHUNK_SIZE = 4096          # bytes of file data per FileSyncContent frame
DEFAULT_HEARTBEAT_INTERVAL = 1.0   # seconds between HeartbeatPing frames
DEFAULT_IDLE_HEARTBEATS = 3        # CLAIM "~3 heartbeats" -> then WifiClose
DEFAULT_EXIT_TIMEOUT = 120.0       # CLAIM "~2 min" -> then WifiClose, unless ExtendExitTime
DEFAULT_SESSION_ID = "SYN-WIFI-SESSION-0001"   # synthetic, clearly labelled
DEFAULT_SPEED_TEST_PACKETS = 3
DEFAULT_DEVICE_LOG = b"SYNTHETIC-DEVICE-LOG: plaudsim wifi emulator\n"
DEFAULT_CONNECT_ATTEMPTS = 1       # dial once; the BLE handoff may allow retries
DEFAULT_CONNECT_RETRY_INTERVAL = 0.2

#: Bytes reserved for the FileSyncContent JSON ({"session","offset","length",
#: "last"} with 64-bit values is under 100 bytes).
FILE_SYNC_JSON_RESERVE = 256
#: Largest file slice one FileSyncContent can carry: the PDU's totalSize is a
#: u24 (wifi.MAX_TOTAL_SIZE, BYTECODE_PROVEN envelope) and counts the 8-byte
#: header, the JSON and the tail. DERIVED bound; the chunk size itself is
#: HARNESS_POLICY.
MAX_CHUNK_SIZE = MAX_TOTAL_SIZE - PDU_HEADER_LEN - FILE_SYNC_JSON_RESERVE


class WifiNotConnected(RuntimeError):
    """`WifiDevice.send()` was called while no WebSocket is open."""


class WifiDeviceState(enum.Enum):
    """Device lifecycle. Named after the iOS WiFiConnectStage where one exists
    (PlaudWiFiSDK swiftinterface:170-178: wifiIdle, wifiConnecting,
    wifiConnected, wifiHandshaking, wifiSocketConnected, wifiReady,
    wifiConnectFailed, wifiClosed) -- those are PHONE stages, mirrored here
    from the pen's point of view."""

    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"          # WebSocket open, SayHello sent
    HANDSHAKED = "handshaked"        # phone's Handshake answered with status 0
    CLOSING = "closing"              # WifiClose sent
    CLOSED = "closed"
    FAILED = "failed"


@dataclass
class WifiStatusPolicy:
    """HARNESS_POLICY status codes the pen reports. The only recovered semantics
    are on the PHONE: 0 means success everywhere it is checked, and any other
    HandshakeResponse status surfaces as onError(1006) (WifiAgentImpl.txt:
    1487-1516). Real firmware's non-zero codes are UNKNOWN."""

    handshake_ok: int = 0
    handshake_token_mismatch: int = 1
    file_list_ok: int = 0
    sync_ok: int = 0
    sync_unknown_session: int = 1
    delete_ok: int = 0
    delete_unknown_session: int = 1
    stop_ok: int = 0
    extend_ok: int = 0


@dataclass
class WifiFileStore:
    """In-memory file table + contents backing the Wi-Fi responses.

    `entries` uses the same dict shape as transfer.FileTable ({session_id,
    file_size, scene}) so a BLE table can be reused; the Wi-Fi record has no
    `attribute` byte and it is ignored here.
    """

    entries: list[dict[str, int]] = field(default_factory=list)
    contents: dict[int, bytes] = field(default_factory=dict)

    @classmethod
    def from_files(cls, files: dict[int, bytes], scene: int = 1) -> "WifiFileStore":
        store = cls()
        for session_id, data in files.items():
            store.add(session_id, data, scene)
        return store

    @classmethod
    def from_file_table(cls, table: FileTable, contents: dict[int, bytes]) -> "WifiFileStore":
        for e in table.entries:
            cls._check(e["session_id"], e["file_size"], e.get("scene", 1))
        store = cls(entries=[dict(e) for e in table.entries], contents=dict(contents))
        return store

    @staticmethod
    def _check(session_id: int, size: int, scene: int) -> None:
        """The Wi-Fi record is [u32le sessionId][u32le fileSize][u16le scene]
        (wifi.FileRecord); refuse what it cannot carry instead of failing
        later inside a GetFileList handler."""
        if not 0 <= session_id <= 0xFFFFFFFF:
            raise ValueError(f"session id out of u32 range: {session_id}")
        if not 0 <= size <= 0xFFFFFFFF:
            raise ValueError(f"file size out of u32 range: {size}")
        if not 0 <= scene <= 0xFFFF:
            raise ValueError(f"scene out of u16 range: {scene}")

    def add(self, session_id: int, data: bytes, scene: int = 1) -> None:
        self._check(session_id, len(data), scene)
        self.entries = [e for e in self.entries if e["session_id"] != session_id]
        self.entries.append({"session_id": session_id, "file_size": len(data), "scene": scene})
        self.contents[session_id] = bytes(data)

    def records(self) -> list[FileRecord]:
        return [FileRecord(e["session_id"], e["file_size"], e.get("scene", 1)) for e in self.entries]

    def get(self, session_id: int) -> bytes | None:
        return self.contents.get(session_id)

    def delete(self, session_id: int) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["session_id"] != session_id]
        self.contents.pop(session_id, None)
        return len(self.entries) != before


class WifiDevice:
    """The pen as a WebSocket client. Drive it with `await device.run()`.

    Inbound classification mirrors the phone's own rule (HARNESS_POLICY that
    the pen does the same): a frame whose u16le@4 exceeds 200 is opened with
    the sealer when one is installed; anything else is parsed as plaintext.

    SINGLE-USE (HARNESS_POLICY): one device runs one session. `run()` raises
    on a second call; build a new WifiDevice per session, as the BLE handoff
    does for every accepted OpenWiFi. `close()` is safe in every state: before
    `run()` it ends the session before it starts, while dialling it cancels
    the dial, once connected it announces WifiClose and hangs up.

    Robustness: a request handler that raises is logged (`handler_error`) and
    the session keeps serving; a transfer task that fails is logged
    (`transfer_failed`). Neither kills `run()`.
    """

    def __init__(
        self,
        host: str,
        port: int = WS_PORT,
        *,
        store: WifiFileStore | None = None,
        serial: str = "8810000001",
        version: str = "V0001",
        p_ver: int = 7,
        token: str = "SYNTHETIC-WIFI-TOKEN",
        expected_token: str | None = None,
        sealer: WifiSealer | None = None,
        session_id: str = DEFAULT_SESSION_ID,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_delay: float = 0.0,
        heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
        idle_heartbeats_before_close: int | None = DEFAULT_IDLE_HEARTBEATS,
        exit_timeout: float | None = DEFAULT_EXIT_TIMEOUT,
        battery: int = 100,
        charging: bool = False,
        voltage: int = 4200,
        pdu_version: int = PDU_VERSION_HOST,
        status: WifiStatusPolicy | None = None,
        device_log: bytes = DEFAULT_DEVICE_LOG,
        speed_test_packets: int = DEFAULT_SPEED_TEST_PACKETS,
        open_timeout: float = 5.0,
        connect_attempts: int = DEFAULT_CONNECT_ATTEMPTS,
        connect_retry_interval: float = DEFAULT_CONNECT_RETRY_INTERVAL,
    ) -> None:
        if not 0 < chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError(f"chunk_size must be in 1..{MAX_CHUNK_SIZE} (u24 totalSize), got {chunk_size}")
        self.uri = f"ws://{host}:{port}"
        self.store = store if store is not None else WifiFileStore()
        # SayHello fields. `sn`/`version` are what the phone logs
        # ("Device says hello: SN=..., Version=..., pVer=..."); `token` is
        # HARNESS_POLICY (see wifi.SayHello for the two phone-side checks).
        self.serial = serial
        self.version = version
        self.p_ver = p_ver
        self.token = token
        # HARNESS_POLICY: accept any Handshake token unless one is configured.
        # Real hardware refuses a foreign token (CLAIM, WifiAgentImpl.txt:1400).
        self.expected_token = expected_token
        self.sealer = sealer
        self.session_id = session_id
        self.chunk_size = chunk_size
        self.chunk_delay = chunk_delay
        self.heartbeat_interval = heartbeat_interval
        self.idle_heartbeats_before_close = idle_heartbeats_before_close
        self.exit_timeout = exit_timeout
        self.battery = battery
        self.charging = charging
        self.voltage = voltage
        self.pdu_version = pdu_version
        self.status = status if status is not None else WifiStatusPolicy()
        self.device_log = bytes(device_log)
        self.speed_test_packets = speed_test_packets
        self.open_timeout = open_timeout
        # HARNESS_POLICY: how long the pen keeps trying to reach the phone.
        # Real hardware must wait for the phone to join its SoftAP and start
        # the server; its retry behaviour is UNKNOWN.
        self.connect_attempts = max(1, connect_attempts)
        self.connect_retry_interval = connect_retry_interval

        self.state = WifiDeviceState.IDLE
        self.state_log: list[str] = [self.state.value]
        self.log: list[dict[str, Any]] = []
        self.ws: Any = None
        self.close_reason: str | None = None
        self.heartbeats_sent = 0
        self.pongs_received = 0
        self.idle_heartbeats = 0
        self.transfer_task: asyncio.Task[None] | None = None
        self.transfer_session: int | None = None
        self.transfer_offset = 0
        self.transfer_chunks_sent = 0
        self._exit_deadline: float | None = None
        self._started = False
        self._close_requested = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[Any]] = []
        self.connected = asyncio.Event()
        self.handshaked = asyncio.Event()
        self.closed = asyncio.Event()
        self._handlers: dict[int, Callable[[Pdu], Awaitable[None]]] = {
            MSG_HANDSHAKE: self._on_handshake,
            MSG_SAY_HELLO: self._on_hello_ack,
            MSG_HEARTBEAT: self._on_pong,
            MSG_GET_FILE_LIST: self._on_get_file_list,
            MSG_FILE_SYNC: self._on_file_sync,
            MSG_FILE_SYNC_STOP: self._on_file_sync_stop,
            MSG_FILE_DELETE: self._on_file_delete,
            MSG_EXTEND_EXIT_TIME: self._on_extend_exit_time,
            MSG_SPEED_TEST: self._on_speed_test,
            MSG_GET_DEVICE_LOG: self._on_get_device_log,
            MSG_SEND_OTA_FILE_INFO: self._on_ota,
            MSG_REQUEST_OTA_PACKAGE: self._on_ota,
            MSG_OTA_STATUS_SYNC: self._on_ota,
        }

    # --- lifecycle -------------------------------------------------------------

    def _set_state(self, state: WifiDeviceState) -> None:
        """Advance the lifecycle. CLOSED and FAILED are terminal, and nothing
        but CLOSED/FAILED follows CLOSING -- a late connect or handshake can
        no longer overwrite a close that was already requested."""
        if self.state in (WifiDeviceState.CLOSED, WifiDeviceState.FAILED):
            return
        if self.state is WifiDeviceState.CLOSING and state not in (WifiDeviceState.CLOSED, WifiDeviceState.FAILED):
            return
        self.state = state
        self.state_log.append(state.value)

    def _event(self, kind: str, **fields: Any) -> None:
        self.log.append({"dir": "event", "kind": kind, **fields})

    @property
    def transfer_active(self) -> bool:
        return self.transfer_task is not None and not self.transfer_task.done()

    async def run(self) -> None:
        """Dial the phone, speak SayHello, serve until the socket closes.

        Single-use: raises RuntimeError when called a second time. A dial
        failure is reported (state FAILED, `connect_failed`), not raised.
        """
        if self._started:
            raise RuntimeError("WifiDevice is single-use: run() was already called; build a new device per session")
        self._started = True
        if self._close_requested.is_set():
            # close() came first: the session ended before it began.
            self._event("run_after_close", reason=self.close_reason)
            self.closed.set()
            return
        self._set_state(WifiDeviceState.CONNECTING)
        try:
            ws = await self._dial()
            if ws is None:
                return
            self.ws = ws
            try:
                await self._serve(ws)
            finally:
                await self._cancel_tasks()
                try:
                    await ws.close()
                except (ConnectionClosed, OSError):
                    pass
        finally:
            if self.state is not WifiDeviceState.FAILED:
                self._set_state(WifiDeviceState.CLOSED)
            self.closed.set()

    async def _dial(self) -> Any:
        """Open the WebSocket, retrying per policy; None when close() won the
        race or every attempt failed (state FAILED)."""
        for attempt in range(1, self.connect_attempts + 1):
            if self._close_requested.is_set():
                self._event("dial_cancelled", reason=self.close_reason, attempt=attempt)
                return None
            dial = asyncio.ensure_future(
                connect(
                    self.uri,
                    ping_interval=None,   # mirrors setConnectionLostTimeout(0) on the phone
                    max_size=None,
                    open_timeout=self.open_timeout,
                )
            )
            stop = asyncio.ensure_future(self._close_requested.wait())
            try:
                await asyncio.wait({dial, stop}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                dial.cancel()
                stop.cancel()
                raise
            stop.cancel()
            if self._close_requested.is_set():
                if not dial.done():
                    dial.cancel()
                    await asyncio.wait({dial})
                if not dial.cancelled() and dial.exception() is None:
                    try:
                        await dial.result().close()
                    except (ConnectionClosed, OSError):
                        pass
                self._event("dial_cancelled", reason=self.close_reason, attempt=attempt)
                return None
            exc = dial.exception()
            if exc is None:
                return dial.result()
            if not isinstance(exc, (OSError, WebSocketException)):
                raise exc
            if attempt == self.connect_attempts:
                self._set_state(WifiDeviceState.FAILED)
                self._event("connect_failed", error=repr(exc), attempts=attempt)
                return None
            self._event("connect_retry", error=repr(exc), attempt=attempt)
            try:
                await asyncio.wait_for(self._close_requested.wait(), self.connect_retry_interval)
            except asyncio.TimeoutError:
                pass
        return None

    async def _serve(self, ws: Any) -> None:
        self._set_state(WifiDeviceState.CONNECTED)
        self.connected.set()
        if self._close_requested.is_set():
            return
        self._extend_exit()
        try:
            # The phone sends nothing until it sees SayHello (wifi.SayHello).
            await self.send(SayHello(self.serial, self.version, self.token, self.p_ver))
            if self.heartbeat_interval:
                self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
            if self.exit_timeout:
                self._tasks.append(asyncio.create_task(self._exit_timer()))
            async for raw in ws:
                if isinstance(raw, str):
                    # The phone only logs text frames (WebSocketOperation$startServer$1.txt:118-138).
                    self._event("text_frame_ignored", length=len(raw))
                    continue
                await self._on_frame(bytes(raw))
        except ConnectionClosed as exc:
            self._event("connection_closed", code=getattr(exc, "code", None))

    async def _cancel_tasks(self) -> None:
        tasks = list(self._tasks)
        if self.transfer_task is not None:
            tasks.append(self.transfer_task)
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def close(self, reason: str = "app") -> None:
        """End the session, whatever state it is in.

        Before run(): the device goes straight to CLOSED and a later run()
        returns at once. While dialling: the dial is cancelled (run() ends
        CLOSED without speaking). Once connected: announce WifiClose, then
        close the socket; on WifiClose the phone stops its server and closes
        the client socket (WifiAgentImpl.txt:1187-1292), so the pen's receive
        loop ends either way. The pen-side choices are HARNESS_POLICY.
        """
        if self.state in (WifiDeviceState.CLOSING, WifiDeviceState.CLOSED, WifiDeviceState.FAILED):
            return
        self.close_reason = reason
        self._close_requested.set()
        if self.state is WifiDeviceState.IDLE:
            self._set_state(WifiDeviceState.CLOSED)
            self._event("closed_before_connect", reason=reason)
            self.closed.set()
            return
        self._set_state(WifiDeviceState.CLOSING)
        if self.ws is None:
            return          # still dialling: run() sees the request and hangs up
        if self.transfer_task is not None and not self.transfer_task.done():
            self.transfer_task.cancel()
        try:
            await self.send(WifiClose(reason=reason, status=0))
        except ConnectionClosed:
            pass
        try:
            await self.ws.close()
        except ConnectionClosed:
            pass

    async def wait_handshaked(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.handshaked.wait(), timeout)

    async def wait_closed(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.closed.wait(), timeout)

    # --- transport -------------------------------------------------------------

    async def send(self, message: WifiMessage) -> bytes:
        """Encode, seal when keyed, and send one message. Returns the wire bytes.

        Raises WifiNotConnected when no WebSocket is open. Sealing happens
        under the send lock, so wire order is sequence order and the logged
        `seq` is this frame's own.
        """
        ws = self.ws
        if ws is None:
            raise WifiNotConnected(f"cannot send {MESSAGE_NAMES.get(message.TYPE)}: the WebSocket is not open")
        pdu = message.encode(self.pdu_version)
        async with self._send_lock:
            wire = self.sealer.seal(pdu) if self.sealer is not None else pdu
            seq = self.sealer.tx_seq if self.sealer is not None else None
            await ws.send(wire)
        self.log.append(
            {
                "dir": "out",
                "type": message.TYPE,
                "name": MESSAGE_NAMES.get(message.TYPE),
                "sealed": self.sealer is not None,
                "seq": seq,
                "len": len(wire),
            }
        )
        return wire

    async def _on_frame(self, raw: bytes) -> None:
        if looks_encrypted(raw):
            if self.sealer is None:
                self._event("ciphertext_without_keys", length=len(raw))
                return
            try:
                inner = self.sealer.open(raw)
            except Exception as exc:  # InvalidTag or a length guard
                self._event("aead_failure", error=type(exc).__name__)
                return
            if inner is None:
                self._event("dropped_replay_or_short", rx_seq=self.sealer.rx_seq)
                return
            sealed = True
        else:
            inner = raw
            sealed = False
            if self.sealer is not None:
                # The phone parses plaintext regardless of its keys; so do we.
                self._event("plaintext_while_sealed")
        try:
            pdu = parse_pdu(inner)
        except ValueError as exc:
            self._event("malformed_pdu", error=str(exc))
            return
        self.log.append(
            {"dir": "in", "type": pdu.msg_type, "name": pdu.name, "sealed": sealed, "len": len(raw)}
        )
        if pdu.msg_type != MSG_HEARTBEAT:
            # Any real request means the phone is still busy with us.
            self.idle_heartbeats = 0
        handler = self._handlers.get(pdu.msg_type)
        if handler is None:
            self._event("unknown_message_type", type=pdu.msg_type)
            return
        try:
            await handler(pdu)
        except ConnectionClosed:
            raise
        except Exception as exc:
            # HARNESS_POLICY: log and keep serving; the phone's request runs
            # into its own 30 s timeout (WifiRequestBean.txt:55).
            self._event("handler_error", type=pdu.msg_type, name=pdu.name, error=f"{type(exc).__name__}: {exc}")

    # --- timers ----------------------------------------------------------------

    def _extend_exit(self) -> None:
        if self.exit_timeout:
            self._exit_deadline = time.monotonic() + self.exit_timeout

    async def _heartbeat_loop(self) -> None:
        """HARNESS_POLICY cadence; the CLAIM is only that the pen self-closes
        after ~3 heartbeats once the phone has gone quiet."""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            if self.state in (WifiDeviceState.CLOSING, WifiDeviceState.CLOSED):
                return
            await self.send(HeartbeatPing(self.battery, self.charging, self.voltage))
            self.heartbeats_sent += 1
            if self.transfer_active:
                self.idle_heartbeats = 0
            else:
                self.idle_heartbeats += 1
            if (
                self.idle_heartbeats_before_close
                and self.idle_heartbeats >= self.idle_heartbeats_before_close
            ):
                await self.close(reason="idle_heartbeats")
                return

    async def _exit_timer(self) -> None:
        """HARNESS_POLICY exit timeout, refreshed by ExtendExitTime (CLAIM ~2 min)."""
        while True:
            assert self._exit_deadline is not None
            remaining = self._exit_deadline - time.monotonic()
            if remaining <= 0:
                await self.close(reason="exit_timeout")
                return
            await asyncio.sleep(remaining)

    # --- handlers ----------------------------------------------------------------

    async def _on_handshake(self, pdu: Pdu) -> None:
        req = HandshakeRequest.from_pdu(pdu)
        ok = self.expected_token is None or req.token == pad_wifi_token(self.expected_token)
        self._event("handshake", token=req.token, stamp=req.stamp, accepted=ok)
        if ok:
            if self.state in (WifiDeviceState.CONNECTED, WifiDeviceState.HANDSHAKED):
                self._set_state(WifiDeviceState.HANDSHAKED)
                self.handshaked.set()
            await self.send(HandshakeResponse(self.status.handshake_ok, self.session_id))
        else:
            await self.send(HandshakeResponse(self.status.handshake_token_mismatch, ""))

    async def _on_hello_ack(self, pdu: Pdu) -> None:
        # Dead code on the phone (no HelloAckRequest caller); accepted and logged.
        self._event("hello_ack", token=HelloAck.from_pdu(pdu).token)

    async def _on_pong(self, pdu: Pdu) -> None:
        pong = HeartbeatPong.from_pdu(pdu)
        self.pongs_received += 1
        self._event("pong", stamp=pong.stamp)

    async def _on_get_file_list(self, pdu: Pdu) -> None:
        req = GetFileListRequest.from_pdu(pdu)
        records = self.store.records()
        # One frame, offset 0: the only shape both phone stacks consume whole
        # (wifi.FileListResponse). `start`/`single` are parsed and ignored:
        # every shipped caller sends 0/false and their semantics are UNKNOWN.
        await self.send(
            FileListResponse(
                uid=req.uid, status=self.status.file_list_ok, count=len(records), offset=0, records=records
            )
        )

    async def _on_file_sync(self, pdu: Pdu) -> None:
        req = FileSyncRequest.from_pdu(pdu)
        data = self.store.get(req.session)
        if data is None:
            await self.send(FileSyncStatus(self.status.sync_unknown_session, req.session, 0))
            return
        # HARNESS_POLICY: a second FileSync replaces the active stream. The old
        # stream is cancelled BEFORE the new FileSyncStatus goes out, so no
        # frame of the replaced session is emitted after the status that opens
        # the new one (frames already on the wire can still arrive at the
        # phone; it keys transfer contexts by session and drops the rest,
        # WifiAgentImpl.txt:1777-1800 "No transfer context for session:").
        if self.transfer_task is not None and not self.transfer_task.done():
            self.transfer_task.cancel()
            try:
                await self.transfer_task
            except (asyncio.CancelledError, Exception):
                pass
            self._event("transfer_replaced", session=self.transfer_session, by=req.session)
        await self.send(FileSyncStatus(self.status.sync_ok, req.session, len(data)))
        self.transfer_session = req.session
        self.transfer_task = asyncio.create_task(self._stream_file(req, data))
        self.transfer_task.add_done_callback(functools.partial(self._on_transfer_done, req.session))

    def _on_transfer_done(self, session: int, task: asyncio.Task[None]) -> None:
        """A stream that died of an exception is logged, not lost. The phone
        still only sees its request time out; telling it is out of scope."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._event("transfer_failed", session=session, error=f"{type(exc).__name__}: {exc}")

    async def _stream_file(self, req: FileSyncRequest, data: bytes) -> None:
        """Emit FileSyncContent frames in offset order until `end`, last=1 on the final one.

        HARNESS_POLICY: `end <= 0` or beyond the file means EOF; an empty
        range still produces one empty last=1 frame (the phone accepts that
        terminator, wifi.FileSyncContent).
        """
        start = max(0, req.start)
        end = len(data) if req.end <= 0 or req.end > len(data) else req.end
        offset = start
        self.transfer_offset = offset
        self.transfer_chunks_sent = 0
        if start >= end:
            await self.send(FileSyncContent(req.session, start, b"", last=True))
            self.transfer_chunks_sent = 1
            return
        while offset < end:
            chunk = data[offset : min(offset + self.chunk_size, end)]
            last = offset + len(chunk) >= end
            await self.send(FileSyncContent(req.session, offset, chunk, last=last))
            offset += len(chunk)
            self.transfer_offset = offset
            self.transfer_chunks_sent += 1
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            else:
                await asyncio.sleep(0)
        self._event("transfer_complete", session=req.session, bytes=offset - start)

    async def _on_file_sync_stop(self, pdu: Pdu) -> None:
        req = FileSyncStopRequest.from_pdu(pdu)
        stopped = False
        if self.transfer_task is not None and not self.transfer_task.done():
            self.transfer_task.cancel()
            try:
                await self.transfer_task
            except (asyncio.CancelledError, Exception):
                pass
            stopped = True
        self._event("sync_stop", session=req.session, stopped_active=stopped)
        # HARNESS_POLICY: status 0 whether or not a stream was active.
        await self.send(FileSyncStopResponse(self.status.stop_ok))

    async def _on_file_delete(self, pdu: Pdu) -> None:
        req = FileDeleteRequest.from_pdu(pdu)
        existed = self.store.delete(req.session)
        self._event("delete", session=req.session, existed=existed)
        await self.send(
            FileDeleteResponse(
                req.session,
                self.status.delete_ok if existed else self.status.delete_unknown_session,
            )
        )

    async def _on_extend_exit_time(self, pdu: Pdu) -> None:
        ExtendExitTimeRequest.from_pdu(pdu)
        self._extend_exit()
        self._event("extend_exit_time", deadline_in=self.exit_timeout)
        await self.send(ExtendExitTimeResponse(self.status.extend_ok))

    async def _on_speed_test(self, pdu: Pdu) -> None:
        req = SpeedTestRequest.from_pdu(pdu)
        self._event("speed_test", operate=req.operate, pack_size=req.pack_size)
        if req.operate == 0:
            return
        # HARNESS_POLICY: `speed_test_packets` frames of `pack_size` zero bytes.
        size = max(0, min(req.pack_size, 0xFFFF))
        for index in range(self.speed_test_packets):
            await self.send(SpeedTestResponse(index, bytes(size)))
            await asyncio.sleep(0)

    async def _on_get_device_log(self, pdu: Pdu) -> None:
        req = GetDeviceLogRequest.from_pdu(pdu)
        self._event("get_device_log", operate=req.operate)
        if req.operate == 0:
            return
        await self.send(GetDeviceLogResponse(0, self.device_log))

    async def _on_ota(self, pdu: Pdu) -> None:
        # Out of scope: parsed by wifi.py codecs, never acted on.
        self._event("unsupported_ota", type=pdu.msg_type)


# --- the BLE handoff: build a WifiDevice from a PlaudPeripheral ------------------------


def store_from_peripheral(peripheral: Any) -> WifiFileStore:
    """The Wi-Fi file table a BLE peripheral would serve: one record per BLE
    file-table entry, with the bytes its y6 would stream
    (`peripheral.file_bytes_for(session_id)`) and the entry's scene.
    HARNESS_POLICY: the two transports serve the same recordings; entries the
    peripheral has no content for are left out."""
    store = WifiFileStore()
    for entry in peripheral.file_table.entries:
        sid = entry["session_id"]
        try:
            data = peripheral.file_bytes_for(sid)
        except KeyError:
            continue
        store.add(sid, data, entry.get("scene", 1))
    return store


def phone_dialer(
    host: str,
    port: int = WS_PORT,
    *,
    store: WifiFileStore | None = None,
    **device_kwargs: Any,
) -> Callable[[Any], "WifiDevice"]:
    """A `PlaudPeripheral(wifi_device_factory=...)` factory: every accepted
    BLE OpenWiFi builds a NEW WifiDevice that dials `ws://host:port`.

    HARNESS_POLICY, all of it: the real pen raises a SoftAP and dials the
    phone that joined it (ledger section 7); here the phone's address is
    configured and IP connectivity is assumed. SayHello carries the BLE
    identity -- `sn` = the advertised serial, `version` = versionType +
    4-digit versionCode, `pVer` = the BLE portVersion (that pVer equals the
    portVersion is UNKNOWN, docs/wifi-transport.md section 7). The file store
    defaults to `store_from_peripheral`. `device_kwargs` go to WifiDevice and
    win over these defaults.
    """

    def factory(peripheral: Any) -> WifiDevice:
        fields = peripheral.scan_fields
        kwargs: dict[str, Any] = {
            "serial": fields.serial_number,
            "version": f"{fields.version_type}{int(fields.version_code):04d}",
            "p_ver": peripheral.port_version,
        }
        kwargs.update(device_kwargs)
        return WifiDevice(
            host,
            port,
            store=store if store is not None else store_from_peripheral(peripheral),
            **kwargs,
        )

    return factory


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_CONNECT_ATTEMPTS",
    "DEFAULT_CONNECT_RETRY_INTERVAL",
    "DEFAULT_DEVICE_LOG",
    "DEFAULT_EXIT_TIMEOUT",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_IDLE_HEARTBEATS",
    "DEFAULT_SESSION_ID",
    "DEFAULT_SPEED_TEST_PACKETS",
    "MAX_CHUNK_SIZE",
    "WifiDevice",
    "WifiDeviceState",
    "WifiFileStore",
    "WifiNotConnected",
    "WifiStatusPolicy",
    "phone_dialer",
    "store_from_peripheral",
]
