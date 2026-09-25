#!/usr/bin/env python3
"""Readiness probes for the compose topology (HARNESS_POLICY tooling).

The docker-compose.yml healthchecks are deliberately stdlib one-liners so the
images need nothing extra; this module is their twin for docker/job.sh,
scripts/local-up.sh and tests/test_compose_*.py, plus one probe a one-liner
cannot do: attach a Bumble central and see the peripheral advertise.

    python docker/probe.py tcp        HOST:PORT          [--timeout S]
    python docker/probe.py emulator   HOST:PORT          [--timeout S]   health line has "ready": true
    python docker/probe.py mockcloud  URL                [--timeout S]   GET URL/_mock/health -> 200 {"mock": true}
    python docker/probe.py scan       TRANSPORT          [--timeout S] [--address F0:1A:2B:3C:4D:5E]
                                      e.g. tcp-client:emulator:9000 -- a central attaches through the
                                      emulator's bridged controller and must receive the advertisement

Each subcommand polls until success or --timeout, prints one PROBE_OK /
PROBE_FAIL line and exits 0 / 1. Nothing here contacts anything outside the
addresses given on the command line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "emulator",):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

DEFAULT_PERIPHERAL_ADDRESS = "F0:1A:2B:3C:4D:5E"
PROBE_CENTRAL_ADDRESS = "F0:F1:F2:F3:F4:F6"


def split_hostport(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"expected HOST:PORT, got {text!r}")
    return host, int(port)


def poll(fn: Callable[[], Any], timeout: float, interval: float = 0.25) -> tuple[bool, Any, float]:
    """Call fn until it returns a truthy value or raises nothing... (success) within timeout."""
    deadline = time.monotonic() + timeout
    t0 = time.monotonic()
    last: Any = None
    while True:
        try:
            result = fn()
            return True, result, time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001 - every failure is a retry reason
            last = exc
        if time.monotonic() >= deadline:
            return False, last, time.monotonic() - t0
        time.sleep(interval)


# --- probes --------------------------------------------------------------------
def probe_tcp(hostport: str) -> str:
    host, port = split_hostport(hostport)
    with socket.create_connection((host, port), timeout=2):
        return f"{host}:{port} open"


def probe_emulator(hostport: str) -> dict[str, Any]:
    host, port = split_hostport(hostport)
    with socket.create_connection((host, port), timeout=2) as s:
        s.settimeout(3)
        line = s.makefile("rb").readline()
    status = json.loads(line.decode("utf-8"))
    if status.get("ready") is not True:
        raise RuntimeError(f"health line not ready: {status}")
    return status


def probe_mockcloud(url: str) -> dict[str, Any]:
    target = url.rstrip("/") + "/_mock/health"
    with urllib.request.urlopen(target, timeout=2) as resp:  # noqa: S310 - caller-supplied local URL
        if resp.status != 200:
            raise RuntimeError(f"{target} -> {resp.status}")
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("mock") is not True:
        raise RuntimeError(f"{target} did not identify as the mock: {body}")
    return body


async def _scan_once(spec: str, address: str, timeout: float) -> dict[str, Any]:
    from bumble.core import AdvertisingData
    from bumble.device import Device
    from bumble.hci import Address
    from bumble.transport import open_transport

    want = address.upper()
    async with await open_transport(spec) as hci:
        device = Device.with_hci("plaud-harness-probe", Address(PROBE_CENTRAL_ADDRESS), hci.source, hci.sink)
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_advertisement(adv: Any) -> None:
            if adv.address.to_string(False).upper() == want and not found.done():
                found.set_result(adv)

        device.on(device.EVENT_ADVERTISEMENT, on_advertisement)
        await device.power_on()
        await device.start_scanning()
        try:
            adv = await asyncio.wait_for(found, timeout)
        finally:
            try:
                await device.stop_scanning()
            except Exception:  # noqa: BLE001 - teardown only
                pass
        raw = adv.data.get(AdvertisingData.MANUFACTURER_SPECIFIC_DATA, raw=True)
        out: dict[str, Any] = {
            "address": adv.address.to_string(False),
            "rssi": adv.rssi,
            "connectable": adv.is_connectable,
        }
        if raw:
            company = int.from_bytes(raw[:2], "little")
            payload = bytes(raw[2:])
            out["company_id"] = f"0x{company:04X}"
            out["manufacturer_data_hex"] = payload.hex()
            try:
                from plaudsim.advertising import parse_manufacturer_data

                fields = parse_manufacturer_data(payload)
                out["port_version"] = fields.port_version
                out["serial_number"] = fields.serial_number
                out["version_name"] = fields.version_name
            except Exception as exc:  # noqa: BLE001 - decoding is informative, not the probe's verdict
                out["decode_error"] = str(exc)
        return out


def probe_scan(spec: str, address: str, timeout: float) -> dict[str, Any]:
    return asyncio.run(_scan_once(spec, address, timeout))


# --- CLI -----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python docker/probe.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=["tcp", "emulator", "mockcloud", "scan"])
    parser.add_argument("target")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--address", default=DEFAULT_PERIPHERAL_ADDRESS, help="scan: peripheral address to expect")
    args = parser.parse_args(argv)

    if args.kind == "tcp":
        fn = lambda: probe_tcp(args.target)  # noqa: E731
    elif args.kind == "emulator":
        fn = lambda: probe_emulator(args.target)  # noqa: E731
    elif args.kind == "mockcloud":
        fn = lambda: probe_mockcloud(args.target)  # noqa: E731
    else:
        # One scan attempt already waits up to --timeout for the advertisement; the
        # outer poll only re-dials while the TCP port is not accepting yet.
        fn = lambda: probe_scan(args.target, args.address, max(2.0, min(args.timeout, 10.0)))  # noqa: E731

    ok, result, elapsed = poll(fn, args.timeout)
    if ok:
        detail = json.dumps(result, sort_keys=True) if isinstance(result, dict) else str(result)
        print(f"PROBE_OK kind={args.kind} target={args.target} elapsed_s={elapsed:.2f} {detail}", flush=True)
        return 0
    print(f"PROBE_FAIL kind={args.kind} target={args.target} elapsed_s={elapsed:.2f} last_error={result!r}",
          file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
