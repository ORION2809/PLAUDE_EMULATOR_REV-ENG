"""R7-S13 pull-capture peripheral: frozen PlaudPeripheral (portVersion 7,
legacy) on the netsim transport, serving a REAL Ogg/Opus recording fixture so
the genuine SDK can pull it, and mirroring every host->device write (syncFile
starts with their offsets, stopSync, deleteFile) to a JSON capture file.

Environment knobs (all HARNESS_POLICY, documented in the run report):
    PULLCAP_FILE         path of the file to serve (default <repo>/tests/fixtures/r6s2_16k_mono.ogg)
    PULLCAP_SESSION_ID   session id of the served file (default 0x6553F000 = 1700000000)
    PULLCAP_DROP_OFFSET  if set, the DATA frame starting at this offset is omitted
                         from the FIRST transfer only (the R3 gap experiment);
                         subsequent syncFile starts are served intact.
    PULLCAP_PAYLOAD      DATA payload size per frame (default: profile default)
    PULLCAP_TAIL_CRC     value of the TAIL u16 field (default 0xBEEF; run 2 uses 0)
    PULLCAP_EMPTY_CODE   if set, emit an EMPTY_PACKAGE frame with this code byte
    PULLCAP_EMPTY_POS    before_tail (default) | after_tail | only
    PULLCAP_ABORT_ON_RESTART  1 (default): stream from a cancellable task; a new syncFileStart/stopSync aborts the old stream
    PULLCAP_PACING_S     inter-frame pacing for the streamed path (default 0.004)
    PULLCAP_CAPTURE      capture JSON path (default <repo>/r7/pull-capture.json)
    K3CAP_BUMBLE_LOG     bumble log level (default WARNING)

NOTHING here is authentication: the token inside k3 is a synthetic identifier
the app was told to use; the peripheral is ours; no Plaud infrastructure is
touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# The repository root is derived from this file's location (<root>/r7/<file>),
# so the script runs from any checkout; nothing machine-specific is hard-coded.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "reference" / "upstream" / "bumble"))

from bumble import logging as bumble_logging

bumble_logging.setup_basic_logging(os.environ.get("K3CAP_BUMBLE_LOG", "WARNING"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("R7S13")

from bumble.core import AdvertisingData
from bumble import data_types
from bumble.device import Device, DeviceConfiguration
from bumble.hci import Address
from bumble.transport import open_transport

from plaudsim.filesync import OPCODE_STOP_SYNC, OPCODE_SYNC_START, OPCODE_DELETE_FILE, OPCODE_FILE_LIST
from plaudsim.profile import (
    PlaudBatteryState,
    PlaudDeviceState,
    PlaudPeripheral,
    PlaudStorageState,
    PlaudSyncTimeState,
)
from plaudsim.transfer import parse_sync_start_request, parse_stop_sync_request
from plaudsim.transfer import pack_empty_package_frame

FILE_PATH = os.environ.get("PULLCAP_FILE", str(ROOT / "tests" / "fixtures" / "r6s2_16k_mono.ogg"))
SESSION_ID = int(os.environ.get("PULLCAP_SESSION_ID", str(1700000000)))
DROP_OFFSET = os.environ.get("PULLCAP_DROP_OFFSET")
PAYLOAD = os.environ.get("PULLCAP_PAYLOAD")
CAPTURE_PATH = os.environ.get("PULLCAP_CAPTURE", str(ROOT / "r7" / "pull-capture.json"))
TAIL_CRC = int(os.environ.get("PULLCAP_TAIL_CRC", "0xBEEF"), 0)  # run 2 experiment: 0 vs 0xBEEF
EMPTY_CODE = os.environ.get("PULLCAP_EMPTY_CODE")        # run 3: emit EMPTY_PACKAGE(code) ...
EMPTY_POS = os.environ.get("PULLCAP_EMPTY_POS", "before_tail")  # ... before_tail | after_tail | only (no TAIL)

FILE_BYTES = open(FILE_PATH, "rb").read()
FILE_SHA = hashlib.sha256(FILE_BYTES).hexdigest()
CAPTURES: list[dict[str, Any]] = []
STATS: dict[str, Any] = {
    "file": FILE_PATH, "file_len": len(FILE_BYTES), "file_sha256": FILE_SHA,
    "session_id": SESSION_ID, "drop_offset": int(DROP_OFFSET) if DROP_OFFSET else None,
    "tail_crc": TAIL_CRC,
    "sync_starts": [], "stop_syncs": 0, "data_frames_emitted": 0,
}


def _dump() -> None:
    with open(CAPTURE_PATH, "w") as fh:
        json.dump({"stats": STATS, "writes": CAPTURES}, fh, indent=1)


PACING_S = float(os.environ.get("PULLCAP_PACING_S", "0.004"))   # HARNESS_POLICY inter-frame pacing
ABORT_ON_RESTART = os.environ.get("PULLCAP_ABORT_ON_RESTART", "1") == "1"


class PullCapturingPeripheral(PlaudPeripheral):
    """Capture-only subclass.

    Runs 1-10 used an experimental override that streamed transfers from a
    cancellable task with pacing; R7-S13 landed exactly that in the shipped
    `PlaudPeripheral` (`stream_in_task`, `response_pacing_s`, and the
    EMPTY_PACKAGE default in `TransferSession`). From run 11 on this subclass
    only records writes and served frames, so the runs exercise the emulator
    as tests do.
    """

    async def _on_command_write(self, connection: Any, value: bytes) -> None:
        raw = bytes(value)
        await self._record_write(raw)
        await super()._on_command_write(connection, value)

    async def _record_write(self, raw: bytes) -> None:
        rec: dict[str, Any] = {"t": time.time(), "char": "2BB1", "len": len(raw), "hex": raw.hex()}
        try:
            if len(raw) >= 3 and raw[0] == 1:
                opcode = int.from_bytes(raw[1:3], "little")
                rec["opcode"] = opcode
                if opcode == OPCODE_SYNC_START:
                    p = parse_sync_start_request(raw)
                    rec["sync_start"] = p
                    STATS["sync_starts"].append({"t": rec["t"], **p})
                    logger.info("PULLCAP_SYNC_START session=%d start=%d end=%d (n=%d)",
                                p["session_id"], p["start"], p["end"], len(STATS["sync_starts"]))
                elif opcode == OPCODE_STOP_SYNC:
                    STATS["stop_syncs"] += 1
                    logger.info("PULLCAP_STOP_SYNC n=%d", STATS["stop_syncs"])
                elif opcode == OPCODE_FILE_LIST:
                    logger.info("PULLCAP_FILE_LIST_REQ")
                elif opcode == OPCODE_DELETE_FILE:
                    logger.info("PULLCAP_DELETE_REQ")
        except Exception as exc:  # diagnostic only
            rec["parse_error"] = str(exc)
        CAPTURES.append(rec)
        _dump()

    def _start_transfer(self, request: bytes) -> list[bytes]:
        frames = super()._start_transfer(request)
        if EMPTY_CODE is not None and EMPTY_POS != "before_tail":
            # Runs 3-9 experiments (position/code). The shipped default is now
            # EMPTY_PACKAGE(code 0) before TAIL (transfer.DEFAULT_EMPTY_PACKAGE_CODE);
            # "after_tail"/"only" rebuild the sequence for the negative controls.
            empty = pack_empty_package_frame(int(EMPTY_CODE, 0), self.transfer.session_id, self.port_version)
            base = [f for f in frames if not (f[0] == 2 and f[5:9] == b"\xff\xff\xff\xff")]
            if EMPTY_POS == "after_tail":
                frames = base + [empty]
            elif EMPTY_POS == "only":
                frames = base[:-1] + [empty]
            STATS["empty_package"] = {"code": int(EMPTY_CODE, 0), "pos": EMPTY_POS, "hex": empty.hex()}
        n_data = max(0, len(frames) - 2)
        STATS["data_frames_emitted"] += n_data
        dropped = list(self.drop_data_offsets)
        logger.info("PULLCAP_SERVE frames=%d data=%d dropped_offsets=%s payload=%s",
                    len(frames), n_data, dropped, self.transfer.payload_size if self.transfer else None)
        # The gap experiment applies to the first transfer only: once the SDK
        # restarts from its cursor the file must arrive intact.
        if self.drop_data_offsets:
            self.drop_data_offsets = ()
            STATS["drop_applied_on_sync_start_n"] = len(STATS["sync_starts"])
        _dump()
        return frames


async def main() -> None:
    spec = sys.argv[1] if len(sys.argv) > 1 else "android-netsim"
    async with await open_transport(spec) as hci:
        config = DeviceConfiguration(name="PlaudNotePro", address=Address("F0:1A:2B:3C:4D:5E"))
        device = Device.from_config_with_hci(config, hci.source, hci.sink)
        kwargs: dict[str, Any] = {}
        if PAYLOAD:
            kwargs["data_payload_size"] = int(PAYLOAD)
        if DROP_OFFSET:
            kwargs["drop_data_offsets"] = (int(DROP_OFFSET),)
        if EMPTY_CODE is not None and EMPTY_POS == "before_tail":
            kwargs["empty_package_code"] = int(EMPTY_CODE, 0)
        if EMPTY_CODE is not None and EMPTY_POS in ("after_tail", "only"):
            kwargs["empty_package_code"] = None   # rebuilt by _start_transfer for the negative controls
        if os.environ.get("PULLCAP_NO_EMPTY") == "1":
            kwargs["empty_package_code"] = None   # run-9 control: the pre-R7-S13 sequence
        # for_real_sdk(): the R7-S13 policy these runs established (task
        # streaming, 4 ms pacing), which profile.REAL_SDK_* now packages. The
        # PULLCAP_ABORT_ON_RESTART / PULLCAP_PACING_S knobs below are passed
        # explicitly and win; their defaults equal the for_real_sdk() values,
        # and PULLCAP_ABORT_ON_RESTART=0 still gives the inline, unpaced path.
        peripheral = PullCapturingPeripheral.for_real_sdk(
            device,
            state=PlaudDeviceState(state=0x1001, privacy_enabled=True, key_state=1, scene=4, session_id=SESSION_ID),
            storage=PlaudStorageState(free=8589934592, total=17179869184, duration=36000),
            synctime=PlaudSyncTimeState(stamp=1700000000),
            battery=PlaudBatteryState(charging=False, level=87),
            file_bytes=FILE_BYTES,
            file_table=[{"session_id": SESSION_ID, "file_size": len(FILE_BYTES), "scene": 2, "attribute": 1}],
            tail_crc=TAIL_CRC,  # HARNESS_POLICY value; whether the SDK gates on it is the run-2 question
            l3_timezone=5,
            stream_in_task=ABORT_ON_RESTART,
            response_pacing_s=PACING_S if ABORT_ON_RESTART else 0.0,
            **kwargs,
        )
        peripheral.install()
        await device.power_on()
        device.advertising_data = bytes(AdvertisingData([
            data_types.Flags(0x06),
            data_types.ManufacturerSpecificData(0xFFFF, peripheral.manufacturer_data()),
        ]))
        await device.start_advertising(auto_restart=True)
        logger.info("PULLCAP_PERIPHERAL_READY pv=%s file=%s len=%d sha256=%s session=%d drop=%s payload=%s",
                    peripheral.port_version, FILE_PATH, len(FILE_BYTES), FILE_SHA, SESSION_ID, DROP_OFFSET, PAYLOAD)
        _dump()
        await asyncio.get_event_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("PULLCAP_STOP writes=%d", len(CAPTURES))
