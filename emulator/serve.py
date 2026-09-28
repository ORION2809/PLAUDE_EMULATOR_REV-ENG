#!/usr/bin/env python3
"""plaud-harness emulator service: one PlaudPeripheral a central can reach
without a radio (the `emulator` service of docker-compose.yml, and what
scripts/local-up.sh starts in the background).

Topology -- HARNESS_POLICY "bridge" mode, the default
------------------------------------------------------

    PlaudPeripheral --Host--> Controller("plaud") ==LocalLink== Controller("central-1") --HCI--> TCP client 1
                                                            ==LocalLink== Controller("central-2") --HCI--> TCP client 2
                                                                          (one per client of tcp-server:<host>:9000)

Bumble transports carry HCI between a *host* and a *controller*, so whatever
dials the TCP port must play the other role. A central that wants to talk to
the peripheral is a host; it therefore needs a controller on the port. This
process runs a virtual link with the peripheral's own controller and, for every
TCP client of PLAUD_HCI_TRANSPORT, a fresh controller of its own. Any Bumble
host that opens the matching client transport (`tcp-client:<host>:9000`) gets
an ordinary controller on the same link and can scan, connect and pull the
recording exactly as the R7 runs did over netsim.

Per-client controllers are HARNESS_POLICY (review finding C1). Bumble's own
tcp-server transport shares ONE packet parser and ONE sink across every client
(reference/upstream/bumble/bumble/transport/tcp_server.py:100-103), and a
Controller's HCI_Reset handler resets nothing
(reference/upstream/bumble/bumble/controller.py:2031-2038). A central that left
without an LE disconnect kept the peripheral connected (so it never advertised
again, controller.py:740), one that left while scanning left le_scan_enable set
(every later LE_Set_Scan_Parameters -> COMMAND_DISALLOWED, controller.py:2764),
and one that died mid-packet desynchronised the shared parser. Now, when a
client's TCP connection ends, its controller sends LL_TERMINATE_IND for every LE
link it holds (the peripheral sees an ordinary disconnection and its
auto-restart advertiser resumes) and is removed from the link; the next client
starts from a fresh controller and a fresh parser.

`PLAUD_HCI_MODE=direct` is the R7 rig instead: the peripheral's host sits
directly on PLAUD_HCI_TRANSPORT and expects a *controller* at the other end
(`android-netsim`, `android-emulator`, a `tcp-client` to a `bumble-controllers`
process). Nothing a central can attach to; kept for parity with
r7/pull_capture_peripheral.py. A bridge-mode transport other than
`tcp-server:` (e.g. `pty`) falls back to one shared controller on that
transport, without the per-client isolation.

Environment (every default is HARNESS_POLICY)
----------------------------------------------
    PLAUD_HCI_TRANSPORT   Bumble transport spec         default tcp-server:127.0.0.1:9000
    PLAUD_HCI_MODE        bridge | direct               default bridge
    PLAUD_HEALTH_HOST     health listener bind address  default 127.0.0.1
    PLAUD_HEALTH_PORT     health listener port; 0 = OS-assigned (printed in the READY line),
                          off = no health listener      default 9001
    PLAUD_MEETING_DIR     plaud-harness/meeting/1 dir; serves its primary device recording
                          (meeting.json audio.device_primary, else audio.device.ogg_opus,
                          else <dir>/device/recording.ogg)
    PLAUD_FILE            an explicit file to serve (wins over PLAUD_MEETING_DIR)
                          fallback when neither is set (or both are empty): tests/fixtures/r6s2_16k_mono.ogg
    PLAUD_SESSION_ID      session id of the served file  default 1700000000 (R7-S13 value)
    PLAUD_DEVICE_NAME     GAP name                        default PlaudNotePro
    PLAUD_DEVICE_ADDRESS  peripheral address              default F0:1A:2B:3C:4D:5E
    PLAUD_BUMBLE_LOG      log level for the bumble logger default WARNING

The defaults bind loopback only (review finding C12): nothing here is
authentication, and the health line names the served file and its sha256.
docker-compose.yml and docker/Dockerfile.emulator set 0.0.0.0 explicitly, which
a container needs.

The health listener answers one JSON line per connection (or a minimal HTTP
response if the client sent a GET/HEAD line first) and closes. It is a
separate port so that a healthcheck never becomes an HCI client. Besides the
static configuration it reports `hci_clients` (attached TCP clients),
`peripheral_connections`, `advertising`, `pid`, and `ready`, which turns false
(with a `fault`) when the peripheral has been connected with no HCI client
attached, or neither connected nor advertising, for more than STALE_FAULT_S --
states in which no new central can find it.

What is emulated is exactly what `plaudsim.profile.PlaudPeripheral` emulates:
a portVersion-7 (cleartext) device. The transfer runs from a cancellable task
with 4 ms inter-frame pacing (`stream_in_task=True, response_pacing_s=0.004`),
the R7-S13 policy that the genuine SDK was observed to pull byte-exact.
Nothing here is authentication and no Plaud host is contacted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent  # emulator/
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# --- defaults (HARNESS_POLICY) ------------------------------------------------
DEFAULT_HCI_TRANSPORT = "tcp-server:127.0.0.1:9000"
DEFAULT_HCI_MODE = "bridge"
DEFAULT_HEALTH_HOST = "127.0.0.1"
DEFAULT_HEALTH_PORT = 9001
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "r6s2_16k_mono.ogg"
DEFAULT_SESSION_ID = 1700000000  # R7-S13 pull_capture_peripheral value
DEFAULT_DEVICE_NAME = "PlaudNotePro"
DEFAULT_DEVICE_ADDRESS = "F0:1A:2B:3C:4D:5E"
BRIDGE_CONTROLLER_ADDRESS = "F0:F1:F2:F3:F4:F5"
MANUFACTURER_COMPANY_ID = 0xFFFF  # as in r7/pull_capture_peripheral.py
PORT_VERSION = 7
STREAM_IN_TASK = True  # R7-S13 policy
RESPONSE_PACING_S = 0.004  # R7-S13 policy
FILE_TABLE_SCENE = 2  # R7-S13 file-table entry values
FILE_TABLE_ATTRIBUTE = 1
READY_MARKER = "EMULATOR_READY"
STALE_FAULT_S = 5.0  # a fault state must persist this long before health says ready:false
WATCHDOG_PERIOD_S = 0.5

logger = logging.getLogger("emulator.serve")


class ConfigError(RuntimeError):
    """A misconfiguration that must stop the service (exit 2), never be papered over."""


LEGACY_MEETING_RECORDING = "device/recording.ogg"


def meeting_primary_recording(mdir: Path) -> str:
    """Relative path of a meeting dir's primary device recording.

    Mirrors generator.contract.device_primary_path (not imported: the emulator
    does not depend on the generator): meeting.json audio.device_primary, else
    audio.device["ogg_opus"], else -- no meeting.json, or a meeting.json with
    neither -- device/recording.ogg. Never the first audio.device entry: the
    generator writes keys sorted, so on a 0.2.0 meeting that is e2ee_ogg
    (device/recording_e2ee.bin, ciphertext). A device_primary naming no entry,
    an unreadable meeting.json, or a path that is absolute or leaves the
    meeting dir is a ConfigError.
    """
    meeting_json = mdir / "meeting.json"
    if not meeting_json.is_file():
        return LEGACY_MEETING_RECORDING
    try:
        meeting = json.loads(meeting_json.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"PLAUD_MEETING_DIR={mdir}: meeting.json is not readable JSON: {exc}") from exc
    audio = meeting.get("audio") if isinstance(meeting, dict) else None
    if not isinstance(audio, dict):
        audio = {}
    device = audio.get("device")
    if not isinstance(device, dict):
        device = {}
    primary = audio.get("device_primary")
    if primary:
        if not isinstance(primary, str) or primary not in device:
            raise ConfigError(
                f"PLAUD_MEETING_DIR={mdir}: meeting.json audio.device_primary {primary!r} names no audio.device entry"
            )
        rel = device[primary]
    elif "ogg_opus" in device:
        rel = device["ogg_opus"]
    else:
        return LEGACY_MEETING_RECORDING
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        raise ConfigError(f"PLAUD_MEETING_DIR={mdir}: meeting.json device path {rel!r} must be a relative path")
    root = mdir.resolve()
    target = (mdir / rel).resolve()
    if target != root and root not in target.parents:
        raise ConfigError(f"PLAUD_MEETING_DIR={mdir}: meeting.json device path {rel!r} resolves outside the meeting dir")
    return rel


def resolve_recording(env: dict[str, str] | None = None) -> tuple[Path, str]:
    """Which file the peripheral serves and why.

    Precedence: PLAUD_FILE, then PLAUD_MEETING_DIR's primary device recording
    (meeting_primary_recording: audio.device_primary, else audio.device.ogg_opus,
    else device/recording.ogg), then the R6-S2 fixture. A set-but-missing path is
    an error, not a silent fallback; an empty value (what docker compose
    substitutes for an unset variable) is unset.
    """
    env = os.environ if env is None else env
    if env.get("PLAUD_FILE"):
        path = Path(env["PLAUD_FILE"]).expanduser()
        if not path.is_file():
            raise ConfigError(f"PLAUD_FILE={path} does not exist")
        return path, "PLAUD_FILE"
    if env.get("PLAUD_MEETING_DIR"):
        mdir = Path(env["PLAUD_MEETING_DIR"]).expanduser()
        rel = meeting_primary_recording(mdir)
        path = mdir / rel
        if not path.is_file():
            raise ConfigError(
                f"PLAUD_MEETING_DIR={mdir} has no {rel} "
                "(expected a plaud-harness/meeting/1 directory written by `python -m generator`)"
            )
        return path, "PLAUD_MEETING_DIR"
    if not DEFAULT_FIXTURE.is_file():
        raise ConfigError(f"fixture {DEFAULT_FIXTURE} missing and no PLAUD_FILE/PLAUD_MEETING_DIR given")
    return DEFAULT_FIXTURE, "fixture"


def parse_health_port(raw: str) -> int:
    """'off'/'none'/'' -> -1 (disabled); otherwise an int, 0 meaning OS-assigned."""
    text = raw.strip().lower()
    if text in ("off", "none", "disabled", ""):
        return -1
    port = int(text, 0)
    if port < 0 or port > 65535:
        raise ConfigError(f"PLAUD_HEALTH_PORT={raw!r}: expected 0..65535 or 'off'")
    return port


def parse_tcp_server_spec(spec: str) -> tuple[str | None, int] | None:
    """`tcp-server:<host>:<port>` -> (host or None for every interface, port); else None.

    Same syntax as Bumble's tcp-server transport, including `_` for "all interfaces"
    (reference/upstream/bumble/bumble/transport/tcp_server.py:33-46)."""
    prefix = "tcp-server:"
    if not spec.startswith(prefix):
        return None
    host, sep, port = spec[len(prefix):].rpartition(":")
    if not sep or not port.isdigit():
        raise ConfigError(f"PLAUD_HCI_TRANSPORT={spec!r}: expected tcp-server:<host>:<port>")
    return (None if host in ("", "_") else host), int(port)


@dataclass
class ServeConfig:
    transport: str = DEFAULT_HCI_TRANSPORT
    mode: str = DEFAULT_HCI_MODE
    health_host: str = DEFAULT_HEALTH_HOST
    health_port: int = DEFAULT_HEALTH_PORT
    session_id: int = DEFAULT_SESSION_ID
    device_name: str = DEFAULT_DEVICE_NAME
    device_address: str = DEFAULT_DEVICE_ADDRESS
    bumble_log: str = "WARNING"
    file_path: Path = DEFAULT_FIXTURE
    file_source: str = "fixture"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ServeConfig":
        env = os.environ if env is None else env
        mode = env.get("PLAUD_HCI_MODE", DEFAULT_HCI_MODE).strip().lower()
        if mode not in ("bridge", "direct"):
            raise ConfigError(f"PLAUD_HCI_MODE={mode!r}: expected bridge or direct")
        transport = env.get("PLAUD_HCI_TRANSPORT") or DEFAULT_HCI_TRANSPORT
        if mode == "bridge":
            parse_tcp_server_spec(transport)  # validates the tcp-server form early
        file_path, file_source = resolve_recording(env)
        return cls(
            transport=transport,
            mode=mode,
            health_host=env.get("PLAUD_HEALTH_HOST") or DEFAULT_HEALTH_HOST,
            health_port=parse_health_port(env.get("PLAUD_HEALTH_PORT", str(DEFAULT_HEALTH_PORT))),
            session_id=int(env.get("PLAUD_SESSION_ID", str(DEFAULT_SESSION_ID)), 0),
            device_name=env.get("PLAUD_DEVICE_NAME", DEFAULT_DEVICE_NAME),
            device_address=env.get("PLAUD_DEVICE_ADDRESS", DEFAULT_DEVICE_ADDRESS),
            bumble_log=env.get("PLAUD_BUMBLE_LOG", "WARNING"),
            file_path=file_path,
            file_source=file_source,
        )


# --- bridge: one controller per HCI client -----------------------------------------
def release_controller(controller: Any, link: Any) -> int:
    """Undo everything a departed host left on its controller; returns LE links closed.

    Each LE link gets an LL_TERMINATE_IND (reason 0x13, remote user terminated), the
    same PDU Controller.on_hci_disconnect_command sends (controller.py:1683-1719), so the
    peer controller reports an ordinary HCI disconnection to its host. Scanning,
    connecting and advertising state is cleared and the controller leaves the link."""
    from bumble import hci, ll

    released = 0
    reason = hci.HCI_ErrorCode.REMOTE_USER_TERMINATED_CONNECTION_ERROR
    for connection in list(controller.le_connections.values()):
        try:
            connection.send_ll_control_pdu(ll.TerminateInd(reason))
            released += 1
        except Exception as exc:  # noqa: BLE001 - the peer may already be gone
            logger.warning("BRIDGE_TERMINATE_FAILED %s: %r", connection.peer_address, exc)
    controller.le_connections.clear()
    controller.le_scan_enable = False
    controller.pending_le_connection = None
    controller.le_legacy_advertiser.stop()
    for advertising_set in controller.advertising_sets.values():
        advertising_set.stop()
    controller.host = None
    if controller in link.controllers:
        link.remove_controller(controller)
    return released


class _ClientSink:
    """Controller -> host bytes for one TCP client; drops them once the client is gone
    (Controller.send_hci_packet schedules writes with call_soon, controller.py:553-559)."""

    def __init__(self, transport: asyncio.WriteTransport) -> None:
        self.transport: asyncio.WriteTransport | None = transport

    def on_packet(self, packet: bytes) -> None:
        transport = self.transport
        if transport is not None and not transport.is_closing():
            transport.write(packet)


@dataclass
class BridgeClient:
    number: int
    peer: str
    transport: asyncio.Transport
    source: Any
    sink: _ClientSink
    controller: Any
    attached_at: float


class _BridgeProtocol(asyncio.Protocol):
    def __init__(self, bridge: "BridgeHciServer") -> None:
        self.bridge = bridge
        self.client: BridgeClient | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.client = self.bridge.attach(transport)  # type: ignore[arg-type]

    def data_received(self, data: bytes) -> None:
        client = self.client
        if client is None:
            return
        try:
            client.source.data_received(data)
        except Exception as exc:  # noqa: BLE001 - one bad client must not take the service down
            logger.warning("BRIDGE_CLIENT_ERROR n=%d peer=%s %r; closing it", client.number, client.peer, exc)
            client.transport.close()

    def eof_received(self) -> bool:
        return False  # close our side too; connection_lost follows

    def connection_lost(self, exc: Exception | None) -> None:
        if self.client is not None:
            self.bridge.detach(self.client)


class BridgeHciServer:
    """TCP server that gives every client its own virtual controller on `link`."""

    def __init__(self, link: Any, controller_address: str = BRIDGE_CONTROLLER_ADDRESS) -> None:
        self.link = link
        self.controller_address = controller_address
        self.clients: dict[int, BridgeClient] = {}
        self.accepted = 0
        self.server: asyncio.AbstractServer | None = None

    async def start(self, host: str | None, port: int) -> tuple[str, int]:
        loop = asyncio.get_running_loop()
        self.server = await loop.create_server(lambda: _BridgeProtocol(self), host, port)
        sock = self.server.sockets[0].getsockname()
        return sock[0], sock[1]

    def attach(self, transport: asyncio.Transport) -> BridgeClient:
        from bumble.controller import Controller
        from bumble.transport.common import StreamPacketSource

        self.accepted += 1
        number = self.accepted
        peer = transport.get_extra_info("peername")
        source = StreamPacketSource()
        sink = _ClientSink(transport)
        controller = Controller(
            f"central-{number}",
            host_source=source,
            host_sink=sink,
            link=self.link,
            public_address=self.controller_address,
        )
        client = BridgeClient(number, f"{peer}", transport, source, sink, controller, time.time())
        self.clients[number] = client
        logger.info("BRIDGE_CLIENT_ATTACHED n=%d peer=%s clients=%d", number, client.peer, len(self.clients))
        return client

    def detach(self, client: BridgeClient) -> None:
        if self.clients.pop(client.number, None) is None:
            return
        client.sink.transport = None
        released = release_controller(client.controller, self.link)
        logger.info("BRIDGE_CLIENT_DETACHED n=%d peer=%s le_links_released=%d clients=%d",
                    client.number, client.peer, released, len(self.clients))

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
        for client in list(self.clients.values()):
            client.transport.close()
            self.detach(client)
        if self.server is not None:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except (asyncio.TimeoutError, Exception):  # pragma: no cover - shutdown best effort
                pass


# --- health ------------------------------------------------------------------------
@dataclass
class HealthServer:
    """One JSON status line per connection (HTTP-shaped if the client speaks HTTP)."""

    status: dict[str, Any] = field(default_factory=dict)
    server: asyncio.AbstractServer | None = None
    live: Callable[[], dict[str, Any]] | None = None  # dynamic fields, merged over `status`

    def snapshot(self) -> dict[str, Any]:
        out = dict(self.status)
        if self.live is not None:
            out.update(self.live())
        out["now"] = time.time()
        return out

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                head = await asyncio.wait_for(reader.read(512), timeout=0.25)
            except asyncio.TimeoutError:
                head = b""
            body = (json.dumps(self.snapshot(), sort_keys=True) + "\n").encode()
            if head[:4] in (b"GET ", b"HEAD"):
                writer.write(
                    b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + (body if head[:4] == b"GET " else b"")
                )
            else:
                writer.write(body)
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # pragma: no cover - best effort
                pass

    async def start(self, host: str, port: int) -> tuple[str, int]:
        self.server = await asyncio.start_server(self._handle, host, port)
        sock = self.server.sockets[0].getsockname()
        return sock[0], sock[1]


class Liveness:
    """Samples the peripheral every WATCHDOG_PERIOD_S and derives `ready`.

    A fault is a state in which no new central can find the peripheral: connected
    while no HCI client is attached (bridge mode only -- in direct mode the clients
    are not ours to count), or neither connected nor advertising. It must persist for
    STALE_FAULT_S before `ready` turns false, so the brief window between a
    disconnection and the advertiser's restart is not reported."""

    def __init__(self, device: Any, own_controller: Any | None, bridge: BridgeHciServer | None) -> None:
        self.device = device
        self.own_controller = own_controller
        self.bridge = bridge
        self.fault: str | None = None
        self.fault_since: float | None = None

    def sample(self) -> dict[str, Any]:
        connections = len(getattr(self.device, "connections", {}) or {})
        if self.own_controller is not None:
            ctrl = self.own_controller
            advertising = bool(ctrl.le_legacy_advertiser.enabled) or any(
                s.enabled for s in ctrl.advertising_sets.values())
        else:
            advertising = bool(getattr(self.device, "is_advertising", False)) and connections == 0
        clients = len(self.bridge.clients) if self.bridge is not None else None
        fault = None
        if connections and clients == 0:
            fault = "peripheral connected with no HCI client attached"
        elif not connections and not advertising:
            fault = "peripheral neither connected nor advertising"
        return {"peripheral_connections": connections, "advertising": advertising, "hci_clients": clients,
                "fault_now": fault}

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        fault = self.sample()["fault_now"]
        if fault is None:
            self.fault, self.fault_since = None, None
        elif fault != self.fault:
            self.fault, self.fault_since = fault, now

    def live(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        s = self.sample()
        s.pop("fault_now")
        out: dict[str, Any] = dict(s)
        out["hci_client_attached"] = (s["hci_clients"] > 0) if s["hci_clients"] is not None else None
        if self.bridge is not None:
            out["hci_clients_total"] = self.bridge.accepted
        stale = self.fault is not None and self.fault_since is not None and now - self.fault_since > STALE_FAULT_S
        out["ready"] = not stale
        if stale:
            out["fault"] = f"{self.fault} for {now - self.fault_since:.1f}s"
        return out

    async def run(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(WATCHDOG_PERIOD_S)


def _advertising_payload(peripheral: Any) -> bytes:
    from bumble import data_types
    from bumble.core import AdvertisingData

    return bytes(
        AdvertisingData(
            [
                data_types.Flags(0x06),
                data_types.ManufacturerSpecificData(MANUFACTURER_COMPANY_ID, peripheral.manufacturer_data()),
            ]
        )
    )


def _scan_response_payload(name: str) -> bytes:
    from bumble import data_types
    from bumble.core import AdvertisingData

    return bytes(AdvertisingData([data_types.CompleteLocalName(name)]))


@dataclass
class Built:
    device: Any
    closers: list[Callable[[], Any]]
    own_controller: Any | None = None
    bridge: BridgeHciServer | None = None
    listening: str | None = None


async def build_device(cfg: ServeConfig) -> Built:
    """The peripheral's Device plus whatever serves HCI to the outside world."""
    from bumble.device import Device, DeviceConfiguration
    from bumble.hci import Address
    from bumble.transport import open_transport

    device_config = DeviceConfiguration(name=cfg.device_name, address=Address(cfg.device_address))

    if cfg.mode == "direct":
        hci = await open_transport(cfg.transport)
        device = Device.from_config_with_hci(device_config, hci.source, hci.sink)
        return Built(device, [hci.close])

    from bumble.controller import Controller
    from bumble.host import Host
    from bumble.link import LocalLink
    from bumble.transport.common import AsyncPipeSink

    link = LocalLink()
    own_controller = Controller("plaud", link=link, public_address=cfg.device_address)
    device = Device(config=device_config, host=Host(own_controller, AsyncPipeSink(own_controller)))
    tcp = parse_tcp_server_spec(cfg.transport)
    if tcp is not None:
        bridge = BridgeHciServer(link)
        host, port = await bridge.start(*tcp)  # listens now; centrals attach later
        return Built(device, [bridge.close], own_controller, bridge, f"{host}:{port}")
    # Any other transport: one shared controller on it (no per-client isolation).
    hci = await open_transport(cfg.transport)
    Controller("central", host_source=hci.source, host_sink=hci.sink, link=link,
               public_address=BRIDGE_CONTROLLER_ADDRESS)
    return Built(device, [hci.close], own_controller)


async def serve(cfg: ServeConfig, stop: asyncio.Future | None = None) -> None:
    from plaudsim.profile import PlaudPeripheral

    file_bytes = cfg.file_path.read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()

    built = await build_device(cfg)
    device = built.device
    peripheral = PlaudPeripheral(
        device,
        file_bytes=file_bytes,
        file_table=[
            {
                "session_id": cfg.session_id,
                "file_size": len(file_bytes),
                "scene": FILE_TABLE_SCENE,
                "attribute": FILE_TABLE_ATTRIBUTE,
            }
        ],
        port_version=PORT_VERSION,
        stream_in_task=STREAM_IN_TASK,
        response_pacing_s=RESPONSE_PACING_S,
    )
    peripheral.install()
    await device.power_on()
    device.advertising_data = _advertising_payload(peripheral)
    device.scan_response_data = _scan_response_payload(cfg.device_name)
    await device.start_advertising(auto_restart=True)

    liveness = Liveness(device, built.own_controller, built.bridge)
    watchdog = asyncio.get_running_loop().create_task(liveness.run())
    status = {
        "service": "plaud-harness emulator",
        "mode": cfg.mode,
        "transport": cfg.transport,
        "hci_listening": built.listening,
        "port_version": peripheral.port_version,
        "device_name": cfg.device_name,
        "device_address": cfg.device_address,
        "file": str(cfg.file_path),
        "file_source": cfg.file_source,
        "file_bytes": len(file_bytes),
        "file_sha256": file_sha,
        "session_id": cfg.session_id,
        "stream_in_task": STREAM_IN_TASK,
        "response_pacing_s": RESPONSE_PACING_S,
        "stale_fault_s": STALE_FAULT_S,
        "synthetic": True,
        "pid": os.getpid(),
        "started_at": time.time(),
    }
    health = HealthServer(status=status, live=liveness.live)
    health_addr = "off"
    if cfg.health_port >= 0:  # -1 (PLAUD_HEALTH_PORT=off) disables; 0 lets the OS pick
        host, port = await health.start(cfg.health_host, cfg.health_port)
        health_addr = f"{host}:{port}"
        status["health"] = health_addr

    line = (
        f"{READY_MARKER} mode={cfg.mode} transport={cfg.transport} health={health_addr} "
        f"pv={peripheral.port_version} name={cfg.device_name} address={cfg.device_address} "
        f"file={cfg.file_path} source={cfg.file_source} bytes={len(file_bytes)} sha256={file_sha} "
        f"session={cfg.session_id} stream_in_task={STREAM_IN_TASK} pacing_s={RESPONSE_PACING_S}"
    )
    print(line, flush=True)
    logger.info(line)

    stop = stop or asyncio.get_running_loop().create_future()
    try:
        await stop
    finally:
        watchdog.cancel()
        if health.server is not None:
            health.server.close()
        for close in built.closers:
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # pragma: no cover - shutdown best effort
                pass


async def _main_async(cfg: ServeConfig) -> int:
    loop = asyncio.get_running_loop()
    stop: asyncio.Future = loop.create_future()

    def _request_stop(signame: str) -> None:
        if not stop.done():
            logger.info("EMULATOR_STOP signal=%s", signame)
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop, sig.name)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-POSIX loops
            pass
    await serve(cfg, stop)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(name)s %(message)s")
    try:
        cfg = ServeConfig.from_env()
    except (ConfigError, ValueError) as exc:
        print(f"EMULATOR_CONFIG_ERROR {exc}", file=sys.stderr, flush=True)
        return 2
    logging.getLogger("bumble").setLevel(cfg.bumble_log.upper())
    try:
        return asyncio.run(_main_async(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
