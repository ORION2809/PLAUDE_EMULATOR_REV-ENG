"""Phone-side test double for the Wi-Fi channel: the java-websocket SERVER.

TEST HARNESS ONLY. This is the PHONE as `sdk.ble.wifi.WifiAgentImpl` drives it
(build/evidence/javap/sdk/ble/wifi/), reduced to the wire behaviour a pen can
observe. Every behaviour below is cited to the phone's bytecode; the values a
test chooses (token, port 0, short timeouts) are test policy.

Recovered phone-side sequence (BYTECODE_PROVEN):
  1. startServer(): `new InetSocketAddress(8081)`, setReuseAddr(true), start();
     onStart(): setConnectionLostTimeout(0), setReuseAddr(true), setTcpNoDelay(true)
     (WebSocketOperation.txt:162-220, WebSocketOperation$startServer$1.txt:15-30).
     The phone then waits up to 30 s for a session id ("Handshake timeout",
     CapricornusStateRelay.txt:46-121, SagittariusProcessorRelay.txt:52-54
     `delay(30000)`).
  2. onOpen: remembers the socket, onClientConnected() (startServer$1.txt:32-67).
     The phone sends NOTHING on its own here.
  3. On a binary frame: `onClientMessage(bytes)` -> handleIncomingMessage:
     len < 8 -> "Message too short"; u16le@4 > 200 with keys -> decrypt;
     then dispatch on the type (WifiAgentImpl.txt:790-1568).
  4. Type 2 SayHello -> if currentSessionId is empty, launch EtaConfiguratorNode
     which queues HandshakeRequest(currentToken) (WifiAgentImpl.txt:1348-1422,
     EtaConfiguratorNode.txt:23-56).
  5. Type 1 HandshakeResponse status 0 -> sessionId = session or token, isActive,
     state READY, onHandshakeCompleted; else onError(1006, ...) (WifiAgentImpl.txt:
     1423-1516). The template then calls getFileList() (SyncManager.kt:219-220).
  6. Type 3 HeartbeatPing -> onDeviceBatteryUpdate + queue HeartbeatPong
     (WifiAgentImpl.txt:1293-1347, ThetaEventRouter.txt:23-68).
  7. Type 4 WifiClose -> cancel transfers, stopServer() (which close()s the client
     socket and stop(1000)s the server), clear keys, DISCONNECTED,
     onWifiTransferStopped (WifiAgentImpl.txt:1187-1292, WebSocketOperation.txt:222-283).
  8. Requests are queued WifiRequestBeans with a 30 s timeout and no response
     callback (all callers pass the 0x0E default mask: WifiRequestBean.txt:37-63,
     e.g. ZetaAsyncManager.txt:69-78); replies are matched by TYPE through
     continuations, not by id. deleteFiles wraps each delete in withTimeout(30000)
     (BetaComponentHandler.txt:137-162).
  9. FileSyncContent bytes are appended in arrival order and the transfer
     completes on last==1 (WifiAgentImpl.txt:1777-2080).

Sealing (when a WifiSealer is given): outbound frames are sealed iff keys are
present (isEncryptionEnabled, WifiAgentImpl.txt:489-503); inbound frames are
opened iff the heuristic fires (WifiAgentImpl.txt:850-878). NOTE the Kotlin
phone performs NO replay check on receive (WifiAgentImpl.txt:2133-2196); pass
`WifiSealer(replay_check=False)` to reproduce that, or leave the default
(strict) to model the proto-path phone (w7.txt:562-1476, `q5.a` at :663).
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from plaudsim.wifi import (
    MESSAGE_NAMES,
    MSG_EXTEND_EXIT_TIME,
    MSG_FILE_DELETE,
    MSG_FILE_SYNC,
    MSG_FILE_SYNC_CONTENT,
    MSG_FILE_SYNC_STOP,
    MSG_GET_DEVICE_LOG,
    MSG_GET_FILE_LIST,
    MSG_HANDSHAKE,
    MSG_HEARTBEAT,
    MSG_SAY_HELLO,
    MSG_SPEED_TEST,
    MSG_TIPS,
    MSG_UNIVERSAL_ERR,
    MSG_WIFI_CLOSE,
    ExtendExitTimeRequest,
    ExtendExitTimeResponse,
    FileDeleteRequest,
    FileDeleteResponse,
    FileListResponse,
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
    SayHello,
    SpeedTestRequest,
    SpeedTestResponse,
    Tips,
    UniversalError,
    WifiClose,
    WifiMessage,
    WifiSealer,
    looks_encrypted,
    parse_pdu,
)

#: IWifiTransferAgent$WifiConnectionState names (DIRECT, IWifiTransferAgent$WifiConnectionState.txt:49-98).
STATE_NONE = "NONE"
STATE_CONNECTING = "CONNECTING"
STATE_CONNECTED = "CONNECTED"
STATE_HANDSHAKING = "HANDSHAKING"
STATE_READY = "READY"
STATE_DISCONNECTED = "DISCONNECTED"
STATE_ERROR = "ERROR"

#: WifiTransferCallback.onError code for a failed handshake (WifiAgentImpl.txt:1505).
ERROR_HANDSHAKE_FAILED = 1006
#: WifiRequestBean default timeout, ms (WifiRequestBean.txt:55).
SDK_REQUEST_TIMEOUT_MS = 30000
#: "Handshake timeout" wait after starting the server, ms (SagittariusProcessorRelay.txt:52).
SDK_HANDSHAKE_TIMEOUT_MS = 30000


@dataclass
class Download:
    """Result of one FileSync exchange as the phone assembled it."""

    session: int
    status: FileSyncStatus | None
    chunks: list[FileSyncContent] = field(default_factory=list)
    data: bytes = b""


class PhoneWifiServer:
    """The phone's WebSocket server, as WifiAgentImpl + WebSocketOperation behave."""

    def __init__(
        self,
        token: str = "SYNTHETIC-WIFI-TOKEN",
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        sealer: WifiSealer | None = None,
        auto_handshake: bool = True,
        auto_pong: bool = True,
        request_timeout: float = 5.0,
        uid: int = 0,
    ) -> None:
        self.token = token
        self.host = host
        self.port = port
        self.sealer = sealer
        self.auto_handshake = auto_handshake
        self.auto_pong = auto_pong
        self.request_timeout = request_timeout
        self.uid = uid

        self.state = STATE_NONE
        self.state_log: list[str] = [self.state]
        self.session_id = ""
        self.ws: Any = None
        self._server: Any = None
        self.log: list[dict[str, Any]] = []
        self.hellos: list[SayHello] = []
        self.handshakes: list[HandshakeResponse] = []
        self.pings: list[HeartbeatPing] = []
        self.closes: list[WifiClose] = []
        self.errors: list[tuple[int, str]] = []
        self.tips: list[Tips] = []
        self.unknown: list[int] = []
        self.dropped: list[str] = []
        self.send_failures: list[int] = []
        self.raw_frames: list[bytes] = []
        self.speed_packets: list[SpeedTestResponse] = []
        self.client_connected = asyncio.Event()
        self.client_disconnected = asyncio.Event()
        self.ready = asyncio.Event()
        self.device_closed = asyncio.Event()
        self._waiters: dict[int, asyncio.Future[Any]] = {}
        self._transfer: Download | None = None
        self._transfer_future: asyncio.Future[Download] | None = None
        self.chunk_hook: Callable[[FileSyncContent], Awaitable[None]] | None = None
        self._send_lock = asyncio.Lock()

    # --- server lifecycle -----------------------------------------------------

    async def __aenter__(self) -> "PhoneWifiServer":
        # ping_interval=None mirrors setConnectionLostTimeout(0); max_size=None
        # because java-websocket imposes no 1 MiB frame cap.
        self._server = await serve(self._handler, self.host, self.port, ping_interval=None, max_size=None)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop_server()

    async def stop_server(self, *, wait: bool = True) -> None:
        """WebSocketOperation.stopServer: close the client socket, then stop the server
        (WebSocketOperation.txt:222-283: `close()` on the client, `stop(1000)` on the server).

        `wait=False` MUST be used when called from inside the connection handler
        (the WifiClose branch of `_on_message`): websockets' `Server.wait_closed()`
        resolves only after every connection handler has returned, so awaiting it
        from the handler itself deadlocks the loop (no timer pending, `select(None)`
        forever). The handler therefore only *schedules* the shutdown; `__aexit__`
        performs the wait once the handler has unwound.
        """
        if self.ws is not None:
            try:
                await self.ws.close()
            except ConnectionClosed:
                pass
        server = self._server
        if server is None:
            return
        server.close()          # idempotent; schedules Server._close()
        if not wait:
            return
        await server.wait_closed()
        self._server = None

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.state_log.append(state)

    async def _handler(self, ws: Any) -> None:
        self.ws = ws
        self._set_state(STATE_CONNECTED)
        self.client_connected.set()
        try:
            async for raw in ws:
                if isinstance(raw, str):
                    self.log.append({"dir": "in", "text": raw})
                    continue
                await self._on_message(bytes(raw))
        except ConnectionClosed:
            pass
        finally:
            self._set_state(STATE_DISCONNECTED)
            self.client_disconnected.set()
            self._fail_pending("client disconnected")

    # --- wire ----------------------------------------------------------------

    async def send(self, message: WifiMessage) -> bytes:
        """WebSocketOperation.sendMessage: a failed send is logged and returns
        false; it never tears down the receive path (WebSocketOperation.txt:
        301-422). So a ConnectionClosed here is recorded, not raised."""
        pdu = message.encode()
        wire = self.sealer.seal(pdu) if self.sealer is not None else pdu
        try:
            async with self._send_lock:
                await self.ws.send(wire)
        except ConnectionClosed:
            self.send_failures.append(message.TYPE)
            return b""
        self.log.append({"dir": "out", "type": message.TYPE, "name": MESSAGE_NAMES.get(message.TYPE), "len": len(wire)})
        return wire

    async def _on_message(self, raw: bytes) -> None:
        self.raw_frames.append(raw)
        if len(raw) < 8:
            self.dropped.append("Message too short")
            return
        if looks_encrypted(raw) and self.sealer is not None:
            try:
                inner = self.sealer.open(raw)
            except Exception as exc:
                self.dropped.append(f"decrypt_failed:{type(exc).__name__}")
                return
            if inner is None:
                self.dropped.append("replay_or_short")
                return
        else:
            inner = raw
        try:
            pdu = parse_pdu(inner)
        except ValueError as exc:
            self.dropped.append(f"malformed:{exc}")
            return
        self.log.append({"dir": "in", "type": pdu.msg_type, "name": pdu.name, "len": len(raw)})
        t = pdu.msg_type
        if t == MSG_SAY_HELLO:
            hello = SayHello.from_pdu(pdu)
            self.hellos.append(hello)
            if self.auto_handshake and not self.session_id:
                self._set_state(STATE_HANDSHAKING)
                await self.send(HandshakeRequest(self.token, int(time.time())))
        elif t == MSG_HANDSHAKE:
            rsp = HandshakeResponse.from_pdu(pdu)
            self.handshakes.append(rsp)
            if rsp.status == 0:
                self.session_id = rsp.session if rsp.session else self.token
                self._set_state(STATE_READY)
                self.ready.set()
            else:
                self.errors.append((ERROR_HANDSHAKE_FAILED, f"Handshake failed with status: {rsp.status}"))
        elif t == MSG_HEARTBEAT:
            ping = HeartbeatPing.from_pdu(pdu)
            self.pings.append(ping)
            if self.auto_pong:
                await self.send(HeartbeatPong(int(time.time())))
        elif t == MSG_WIFI_CLOSE:
            self.closes.append(WifiClose.from_pdu(pdu))
            self._set_state(STATE_DISCONNECTED)
            self.device_closed.set()
            self._fail_pending("device closed wifi")
            # We ARE the connection handler here: schedule the shutdown, never
            # wait for it (see stop_server).
            await self.stop_server(wait=False)
        elif t == MSG_GET_FILE_LIST:
            self._resolve(t, FileListResponse.from_pdu(pdu))
        elif t == MSG_FILE_SYNC:
            status = FileSyncStatus.from_pdu(pdu)
            if self._transfer is not None:
                self._transfer.status = status
                if status.status != 0 and self._transfer_future and not self._transfer_future.done():
                    self._transfer_future.set_exception(RuntimeError(f"File sync failed: status={status.status}"))
        elif t == MSG_FILE_SYNC_CONTENT:
            content = FileSyncContent.from_pdu(pdu)
            # WifiAgentImpl keys transfer contexts by the frame's own session id
            # (LinkedHashMap.get(getSessionId()); null -> Log.w "No transfer
            # context for session:" and return, WifiAgentImpl.txt:1777-1801).
            # A late chunk of a replaced stream is therefore dropped, not
            # appended to the new file.
            if self._transfer is None or content.session != self._transfer.session:
                self.dropped.append(f"No transfer context for session: {content.session}")
            else:
                self._transfer.chunks.append(content)
                if content.data:
                    self._transfer.data += content.data
                if self.chunk_hook is not None:
                    await self.chunk_hook(content)
                if content.last and self._transfer_future and not self._transfer_future.done():
                    self._transfer_future.set_result(self._transfer)
        elif t == MSG_FILE_DELETE:
            self._resolve(t, FileDeleteResponse.from_pdu(pdu))
        elif t == MSG_FILE_SYNC_STOP:
            self._resolve(t, FileSyncStopResponse.from_pdu(pdu))
        elif t == MSG_EXTEND_EXIT_TIME:
            self._resolve(t, ExtendExitTimeResponse.from_pdu(pdu))
        elif t == MSG_SPEED_TEST:
            self.speed_packets.append(SpeedTestResponse.from_pdu(pdu))
            waiter = self._waiters.get(t)
            if waiter is not None and not waiter.done() and len(self.speed_packets) >= waiter.expected:  # type: ignore[attr-defined]
                waiter.set_result(list(self.speed_packets))
        elif t == MSG_GET_DEVICE_LOG:
            self._resolve(t, GetDeviceLogResponse.from_pdu(pdu))
        elif t == MSG_UNIVERSAL_ERR:
            err = UniversalError.from_pdu(pdu)
            self.errors.append((err.code, err.msg))
        elif t == MSG_TIPS:
            self.tips.append(Tips.from_pdu(pdu))
        else:
            self.unknown.append(t)

    # --- waiters ----------------------------------------------------------------

    def _new_waiter(self, msg_type: int) -> asyncio.Future[Any]:
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiters[msg_type] = fut
        return fut

    def _resolve(self, msg_type: int, value: Any) -> None:
        fut = self._waiters.pop(msg_type, None)
        if fut is not None and not fut.done():
            fut.set_result(value)

    def _fail_pending(self, reason: str) -> None:
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._waiters.clear()
        if self._transfer_future is not None and not self._transfer_future.done():
            self._transfer_future.set_exception(RuntimeError(reason))

    async def _request(self, message: WifiMessage, reply_type: int) -> Any:
        fut = self._new_waiter(reply_type)
        await self.send(message)
        return await asyncio.wait_for(fut, self.request_timeout)

    # --- the IWifiTransferAgent verbs -------------------------------------------

    async def wait_ready(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.ready.wait(), timeout)

    async def wait_device_closed(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.device_closed.wait(), timeout)

    async def get_file_list(self) -> FileListResponse:
        """getFileList(): GetFileListRequest(0, 0, false) (ZetaAsyncManager.txt:44-51)."""
        return await self._request(GetFileListRequest(self.uid, 0, False), MSG_GET_FILE_LIST)

    async def start_download(
        self, session_id: int, size: int, scene: int = 1, start: int = 0
    ) -> asyncio.Future[Download]:
        """Send the FileSyncRequest and return the future the transfer completes."""
        self._transfer = Download(session=session_id, status=None)
        self._transfer_future = asyncio.get_running_loop().create_future()
        await self.send(FileSyncRequest(session_id, scene, start, size))
        return self._transfer_future

    async def download(self, session_id: int, size: int, scene: int = 1, start: int = 0) -> Download:
        """downloadFile: FileSyncRequest(session, scene, 0, fileSize) then append
        FileSyncContent bytes until last==1 (EpsilonDataStream.txt:130-143 builds
        the request; WifiAgentImpl.txt:1777-2080 appends)."""
        fut = await self.start_download(session_id, size, scene, start)
        try:
            return await asyncio.wait_for(fut, self.request_timeout)
        finally:
            self._transfer_future = None

    def current_download(self) -> Download | None:
        return self._transfer

    async def delete_file(self, session_id: int, scene: int = 1) -> FileDeleteResponse:
        """deleteFiles(): one FileDeleteRequest per id (BetaComponentHandler.txt:102-137)."""
        return await self._request(FileDeleteRequest(session_id, scene), MSG_FILE_DELETE)

    async def stop_sync(self, session_id: int, scene: int = 1) -> FileSyncStopResponse:
        return await self._request(FileSyncStopRequest(session_id, scene), MSG_FILE_SYNC_STOP)

    async def extend_exit_time(self) -> ExtendExitTimeResponse:
        return await self._request(ExtendExitTimeRequest(), MSG_EXTEND_EXIT_TIME)

    async def speed_test(self, pack_size: int, expected_packets: int) -> list[SpeedTestResponse]:
        self.speed_packets = []
        fut = self._new_waiter(MSG_SPEED_TEST)
        fut.expected = expected_packets  # type: ignore[attr-defined]
        await self.send(SpeedTestRequest(1, pack_size))
        return await asyncio.wait_for(fut, self.request_timeout)

    async def get_device_log(self) -> GetDeviceLogResponse:
        return await self._request(GetDeviceLogRequest(1), MSG_GET_DEVICE_LOG)

    async def send_raw(self, wire: bytes) -> None:
        """Push arbitrary bytes (a test hook for malformed frames)."""
        async with self._send_lock:
            await self.ws.send(wire)
