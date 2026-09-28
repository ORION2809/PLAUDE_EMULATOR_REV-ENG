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
                                      [--company-id 0xFFFF] [--port-version 7]
                                      e.g. tcp-client:emulator:9000 -- a central attaches through the
                                      emulator's bridged controller and must receive a PlaudPeripheral
                                      advertisement: manufacturer data under --company-id that
                                      plaudsim.advertising decodes to --port-version

Each subcommand retries until success or --timeout and prints one PROBE_OK /
PROBE_FAIL line; exit 0 / 1. The whole run is bounded by --timeout (plus a
second or two of teardown): every scan attempt -- opening the transport, the
host's HCI_Reset, starting the scan, waiting for the advertisement -- runs under
one deadline, because Bumble's Host.reset waits for its Command Complete with
no timeout of its own (reference/upstream/bumble/bumble/host.py, send_sync_command
with response_timeout=None). An advertisement from the expected address that is
not a PlaudPeripheral is a verdict, not a reason to retry. Nothing here contacts
anything outside the addresses given on the command line.
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
#: HARNESS_POLICY: what emulator/serve.py advertises (company id as in
#: r7/pull_capture_peripheral.py; portVersion 7 = cleartext PlaudPeripheral).
DEFAULT_COMPANY_ID = 0xFFFF
DEFAULT_PORT_VERSION = 7
#: one scan attempt never waits longer than this, so a slow first dial is retried
MAX_ATTEMPT_S = 10.0


class ProbeVerdict(RuntimeError):
    """A definitive negative answer: retrying cannot change it."""


def split_hostport(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"expected HOST:PORT, got {text!r}")
    return host, int(port)


def poll(fn: Callable[[float], Any], timeout: float, interval: float = 0.25) -> tuple[bool, Any, float]:
    """Call fn(remaining_s) until it returns (success), raises ProbeVerdict (final
    failure) or the deadline passes. Every other exception is a retry reason."""
    t0 = time.monotonic()
    deadline = t0 + timeout
    last: Any = None
    while True:
        remaining = deadline - time.monotonic()
        try:
            result = fn(max(remaining, 0.5))
            return True, result, time.monotonic() - t0
        except ProbeVerdict as exc:
            return False, exc, time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001 - every other failure is a retry reason
            last = exc
        if time.monotonic() + interval >= deadline:
            return False, last, time.monotonic() - t0
        time.sleep(interval)


# --- probes --------------------------------------------------------------------
def probe_tcp(hostport: str, budget: float = 2.0) -> str:
    host, port = split_hostport(hostport)
    with socket.create_connection((host, port), timeout=min(2.0, budget)):
        return f"{host}:{port} open"


def probe_emulator(hostport: str, budget: float = 3.0) -> dict[str, Any]:
    host, port = split_hostport(hostport)
    with socket.create_connection((host, port), timeout=min(2.0, budget)) as s:
        s.settimeout(min(3.0, budget))
        line = s.makefile("rb").readline()
    status = json.loads(line.decode("utf-8"))
    if status.get("ready") is not True:
        raise RuntimeError(f"health line not ready: {status}")
    return status


def probe_mockcloud(url: str, budget: float = 2.0) -> dict[str, Any]:
    target = url.rstrip("/") + "/_mock/health"
    with urllib.request.urlopen(target, timeout=min(2.0, budget)) as resp:  # noqa: S310 - caller-supplied local URL
        if resp.status != 200:
            raise RuntimeError(f"{target} -> {resp.status}")
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("mock") is not True:
        raise RuntimeError(f"{target} did not identify as the mock: {body}")
    return body


def describe_advertisement(adv: Any) -> dict[str, Any]:
    """Decode what the probe saw (informative fields; the verdict is check_advertisement)."""
    from bumble.core import AdvertisingData

    out: dict[str, Any] = {
        "address": adv.address.to_string(False),
        "rssi": adv.rssi,
        "connectable": adv.is_connectable,
    }
    raw = adv.data.get(AdvertisingData.MANUFACTURER_SPECIFIC_DATA, raw=True)
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
        except Exception as exc:  # noqa: BLE001 - reported, and check_advertisement fails on it
            out["decode_error"] = str(exc)
    return out


def check_advertisement(detail: dict[str, Any], company_id: int, port_version: int) -> str | None:
    """None when `detail` is a PlaudPeripheral advertisement as expected, else why not."""
    if "company_id" not in detail:
        return "no manufacturer-specific data in the advertisement (not a PlaudPeripheral)"
    if detail["company_id"] != f"0x{company_id:04X}":
        return f"manufacturer company id {detail['company_id']} != expected 0x{company_id:04X}"
    if "decode_error" in detail:
        return f"manufacturer data does not decode as a Plaud advertisement: {detail['decode_error']}"
    if detail.get("port_version") != port_version:
        return f"port_version {detail.get('port_version')} != expected {port_version}"
    return None


async def _scan_once(spec: str, address: str, company_id: int, port_version: int) -> dict[str, Any]:
    from bumble.device import Device
    from bumble.hci import Address
    from bumble.transport import open_transport

    want = address.upper()
    async with await open_transport(spec) as hci:
        device = Device.with_hci("plaud-harness-probe", Address(PROBE_CENTRAL_ADDRESS), hci.source, hci.sink)
        found: asyncio.Future = asyncio.get_running_loop().create_future()
        wrong: list[str] = []

        def on_advertisement(adv: Any) -> None:
            if found.done() or adv.address.to_string(False).upper() != want:
                return
            detail = describe_advertisement(adv)
            why = check_advertisement(detail, company_id, port_version)
            if why is None:
                found.set_result(detail)
            else:
                wrong.append(f"{why}; saw {json.dumps(detail, sort_keys=True)}")
                found.set_exception(ProbeVerdict(wrong[-1]))

        device.on(device.EVENT_ADVERTISEMENT, on_advertisement)
        await device.power_on()
        await device.start_scanning()
        try:
            return await found
        finally:
            try:
                await asyncio.wait_for(device.stop_scanning(), 1.0)
            except Exception:  # noqa: BLE001 - teardown only
                pass


def probe_scan(spec: str, address: str, budget: float, company_id: int = DEFAULT_COMPANY_ID,
               port_version: int = DEFAULT_PORT_VERSION) -> dict[str, Any]:
    """One bounded attempt: everything from dialling to the verdict within `budget` s."""
    attempt = max(0.5, min(budget, MAX_ATTEMPT_S))

    async def bounded() -> dict[str, Any]:
        try:
            return await asyncio.wait_for(_scan_once(spec, address, company_id, port_version), attempt)
        except asyncio.TimeoutError:
            raise TimeoutError(f"no advertisement from {address} within {attempt:.1f}s "
                               "(transport open, HCI reset, scan start and the advertisement included)") from None

    return asyncio.run(bounded())


# --- CLI -----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python docker/probe.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=["tcp", "emulator", "mockcloud", "scan"])
    parser.add_argument("target")
    parser.add_argument("--timeout", type=float, default=20.0, help="bound on the whole probe, retries included")
    parser.add_argument("--address", default=DEFAULT_PERIPHERAL_ADDRESS, help="scan: peripheral address to expect")
    parser.add_argument("--company-id", type=lambda s: int(s, 0), default=DEFAULT_COMPANY_ID,
                        help="scan: manufacturer company id to expect (default 0xFFFF)")
    parser.add_argument("--port-version", type=int, default=DEFAULT_PORT_VERSION,
                        help="scan: portVersion the manufacturer data must decode to (default 7)")
    args = parser.parse_args(argv)

    if args.kind == "tcp":
        fn = lambda budget: probe_tcp(args.target, budget)  # noqa: E731
    elif args.kind == "emulator":
        fn = lambda budget: probe_emulator(args.target, budget)  # noqa: E731
    elif args.kind == "mockcloud":
        fn = lambda budget: probe_mockcloud(args.target, budget)  # noqa: E731
    else:
        fn = lambda budget: probe_scan(args.target, args.address, budget, args.company_id,  # noqa: E731
                                       args.port_version)

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
