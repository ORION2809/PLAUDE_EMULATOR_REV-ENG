#!/usr/bin/env python3
"""plaud-harness emulator service: one PlaudPeripheral a central can reach
without a radio (the `emulator` service of docker-compose.yml, and what
scripts/local-up.sh starts in the background).

Topology -- HARNESS_POLICY "bridge" mode, the default
------------------------------------------------------

    PlaudPeripheral --Host--> Controller("plaud") ==LocalLink== Controller("central") --HCI--> PLAUD_HCI_TRANSPORT
                                                                                              (tcp-server:0.0.0.0:9000)

Bumble transports carry HCI between a *host* and a *controller*, so whatever
dials the TCP port must play the other role. A central that wants to talk to
the peripheral is a host; it therefore needs a controller on the port. This
process runs a virtual link with two controllers: the peripheral's own, and a
second one whose HCI is exposed on PLAUD_HCI_TRANSPORT. Any Bumble host that
opens the matching client transport (`tcp-client:<host>:9000`) gets an
ordinary controller on the same link and can scan, connect and pull the
recording exactly as the R7 runs did over netsim.

`PLAUD_HCI_MODE=direct` is the R7 rig instead: the peripheral's host sits
directly on PLAUD_HCI_TRANSPORT and expects a *controller* at the other end
(`android-netsim`, `android-emulator`, a `tcp-client` to a `bumble-controllers`
process). Nothing a central can attach to; kept for parity with
r7/pull_capture_peripheral.py.

Environment (every default is HARNESS_POLICY)
----------------------------------------------
    PLAUD_HCI_TRANSPORT   Bumble transport spec         default tcp-server:0.0.0.0:9000
    PLAUD_HCI_MODE        bridge | direct               default bridge
    PLAUD_HEALTH_HOST     health listener bind address  default 0.0.0.0
    PLAUD_HEALTH_PORT     health listener port; 0 = OS-assigned (printed in the READY line),
                          off = no health listener      default 9001
    PLAUD_MEETING_DIR     plaud-harness/meeting/1 dir; serves <dir>/device/recording.ogg
    PLAUD_FILE            an explicit file to serve (wins over PLAUD_MEETING_DIR)
                          fallback when neither is set: tests/fixtures/r6s2_16k_mono.ogg
    PLAUD_SESSION_ID      session id of the served file  default 1700000000 (R7-S13 value)
    PLAUD_DEVICE_NAME     GAP name                        default PlaudNotePro
    PLAUD_DEVICE_ADDRESS  peripheral address              default F0:1A:2B:3C:4D:5E
    PLAUD_BUMBLE_LOG      log level for the bumble logger default WARNING

The health listener answers one JSON line per connection (or a minimal HTTP
response if the client sent a GET/HEAD line first) and closes. It is a
separate port on purpose: Bumble's tcp-server transport binds its sink to the
most recent client, so a healthcheck that connected to the HCI port would
detach a central that was already attached.

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
from typing import Any

HERE = Path(__file__).resolve().parent  # emulator/
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# --- defaults (HARNESS_POLICY) ------------------------------------------------
DEFAULT_HCI_TRANSPORT = "tcp-server:0.0.0.0:9000"
DEFAULT_HCI_MODE = "bridge"
DEFAULT_HEALTH_HOST = "0.0.0.0"
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

logger = logging.getLogger("emulator.serve")


class ConfigError(RuntimeError):
    """A misconfiguration that must stop the service (exit 2), never be papered over."""


def resolve_recording(env: dict[str, str] | None = None) -> tuple[Path, str]:
    """Which file the peripheral serves and why.

    Precedence: PLAUD_FILE, then PLAUD_MEETING_DIR/device/recording.ogg, then the
    R6-S2 fixture. A set-but-missing path is an error, not a silent fallback.
    """
    env = os.environ if env is None else env
    if env.get("PLAUD_FILE"):
        path = Path(env["PLAUD_FILE"]).expanduser()
        if not path.is_file():
            raise ConfigError(f"PLAUD_FILE={path} does not exist")
        return path, "PLAUD_FILE"
    if env.get("PLAUD_MEETING_DIR"):
        mdir = Path(env["PLAUD_MEETING_DIR"]).expanduser()
        path = mdir / "device" / "recording.ogg"
        if not path.is_file():
            raise ConfigError(
                f"PLAUD_MEETING_DIR={mdir} has no device/recording.ogg "
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
        file_path, file_source = resolve_recording(env)
        return cls(
            transport=env.get("PLAUD_HCI_TRANSPORT", DEFAULT_HCI_TRANSPORT),
            mode=mode,
            health_host=env.get("PLAUD_HEALTH_HOST", DEFAULT_HEALTH_HOST),
            health_port=parse_health_port(env.get("PLAUD_HEALTH_PORT", str(DEFAULT_HEALTH_PORT))),
            session_id=int(env.get("PLAUD_SESSION_ID", str(DEFAULT_SESSION_ID)), 0),
            device_name=env.get("PLAUD_DEVICE_NAME", DEFAULT_DEVICE_NAME),
            device_address=env.get("PLAUD_DEVICE_ADDRESS", DEFAULT_DEVICE_ADDRESS),
            bumble_log=env.get("PLAUD_BUMBLE_LOG", "WARNING"),
            file_path=file_path,
            file_source=file_source,
        )


@dataclass
class HealthServer:
    """One JSON status line per connection (HTTP-shaped if the client speaks HTTP)."""

    status: dict[str, Any] = field(default_factory=dict)
    server: asyncio.AbstractServer | None = None
    hci_transport: Any = None  # set in bridge mode; used for the best-effort "client attached" flag

    def snapshot(self) -> dict[str, Any]:
        out = dict(self.status)
        sink = getattr(self.hci_transport, "sink", None)
        out["hci_client_attached"] = (
            getattr(sink, "transport", None) is not None if sink is not None and hasattr(sink, "transport") else None
        )
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


async def build_device(cfg: ServeConfig) -> tuple[Any, Any]:
    """Return (device, hci_transport). The transport is None only if opening failed upstream."""
    from bumble.device import Device, DeviceConfiguration
    from bumble.hci import Address
    from bumble.transport import open_transport

    device_config = DeviceConfiguration(name=cfg.device_name, address=Address(cfg.device_address))

    if cfg.mode == "direct":
        hci = await open_transport(cfg.transport)
        device = Device.from_config_with_hci(device_config, hci.source, hci.sink)
        return device, hci

    from bumble.controller import Controller
    from bumble.host import Host
    from bumble.link import LocalLink
    from bumble.transport.common import AsyncPipeSink

    link = LocalLink()
    own_controller = Controller("plaud", link=link, public_address=cfg.device_address)
    device = Device(config=device_config, host=Host(own_controller, AsyncPipeSink(own_controller)))
    hci = await open_transport(cfg.transport)  # listens now; a central attaches later
    Controller(
        "central",
        host_source=hci.source,
        host_sink=hci.sink,
        link=link,
        public_address=BRIDGE_CONTROLLER_ADDRESS,
    )
    return device, hci


async def serve(cfg: ServeConfig, stop: asyncio.Future | None = None) -> None:
    from plaudsim.profile import PlaudPeripheral

    file_bytes = cfg.file_path.read_bytes()
    file_sha = hashlib.sha256(file_bytes).hexdigest()

    device, hci = await build_device(cfg)
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

    status = {
        "ready": True,
        "service": "plaud-harness emulator",
        "mode": cfg.mode,
        "transport": cfg.transport,
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
        "synthetic": True,
        "started_at": time.time(),
    }
    health = HealthServer(status=status, hci_transport=hci)
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
        if health.server is not None:
            health.server.close()
        try:
            await hci.close()
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
