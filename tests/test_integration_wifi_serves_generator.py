"""Layer 2 -> Layer 1 (Wi-Fi): the generator's device Ogg served by the Wi-Fi
device emulator to the phone double over a loopback WebSocket, byte-exact.

    generator device/recording.ogg
      -> WifiFileStore of emulator/plaudsim/wifi_device.WifiDevice
      -> the device dials tests/wifi_support.PhoneWifiServer (port 0 on 127.0.0.1),
         SayHello -> Handshake -> GetFileList -> FileSync
      -> FileSyncContent chunks appended in arrival order, complete on last == 1
      -> byte-exact vs the file and its sha256 in meeting.json
      -> pipeline.load_audio decodes the received bytes identically

Evidence class: EMULATOR_INTEGRATION_PROVEN for our own components (device
emulator + phone double + generator).  The phone double reproduces the
bytecode-recovered phone behaviour (wifi_support.py cites every step); nothing
here is a claim about real hardware.  Run with ``--timeout``; every await is
also bounded here so a stalled transfer fails instead of hanging.

HARNESS_POLICY: 4096-byte chunks (the emulator default), a 50 ms heartbeat,
no idle/exit timers during the transfer, the synthetic token and session id.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from plaudsim.audio import classify_recording  # noqa: E402
from plaudsim.sealed import SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L, SealedSession  # noqa: E402
from plaudsim.wifi import FileRecord, WifiSealer, looks_encrypted  # noqa: E402
from plaudsim.wifi_device import WifiDevice, WifiDeviceState, WifiFileStore  # noqa: E402
from wifi_support import STATE_READY, PhoneWifiServer  # noqa: E402

from generator.export import DEVICE_FILES  # noqa: E402
from generator.testing import fixture_meeting  # noqa: E402
from pipeline import load_audio  # noqa: E402

pytestmark = pytest.mark.timeout(120)

TOKEN = "SYNTHETIC-WIFI-TOKEN-0123456789ABCDEF"[:32]
SESSION = 1700000000
CHUNK = 4096  # HARNESS_POLICY (emulator default)
WAIT = 10.0   # seconds; every await below is bounded by this or less


@pytest.fixture(scope="module")
def meeting(tmp_path_factory) -> tuple[Path, dict]:
    return fixture_meeting(tmp_path_factory.mktemp("wifi-gen"), "smoke", 3)


@pytest.fixture(scope="module")
def ogg(meeting) -> bytes:
    d, _ = meeting
    return (d / DEVICE_FILES["ogg_opus"]).read_bytes()


def make_device(phone: PhoneWifiServer, data: bytes, **kw) -> WifiDevice:
    defaults = dict(
        store=WifiFileStore.from_files({SESSION: data}),
        expected_token=TOKEN,
        chunk_size=CHUNK,
        heartbeat_interval=0.05,
        idle_heartbeats_before_close=None,
        exit_timeout=None,
    )
    defaults.update(kw)
    return WifiDevice("127.0.0.1", phone.port, **defaults)


async def session(phone: PhoneWifiServer, data: bytes, **kw):
    """Bring the device up to READY; return (device, run task)."""
    dev = make_device(phone, data, **kw)
    task = asyncio.create_task(dev.run())
    await asyncio.wait_for(dev.connected.wait(), WAIT)
    await phone.wait_ready(WAIT)
    assert phone.state == STATE_READY and dev.state is WifiDeviceState.HANDSHAKED
    return dev, task


async def teardown(dev: WifiDevice, task: asyncio.Task) -> None:
    await asyncio.wait_for(dev.close("test"), WAIT)
    await asyncio.wait_for(task, WAIT)


@pytest.mark.asyncio
async def test_generated_ogg_over_wifi_loopback_is_byte_exact(meeting, ogg, tmp_path) -> None:
    d, m = meeting
    async with PhoneWifiServer(token=TOKEN, request_timeout=WAIT) as phone:
        dev, task = await session(phone, ogg)

        listing = await asyncio.wait_for(phone.get_file_list(), WAIT)
        assert listing.status == 0 and listing.records == [FileRecord(SESSION, len(ogg), 1)]

        dl = await asyncio.wait_for(phone.download(SESSION, len(ogg)), WAIT)
        assert dl.status is not None and dl.status.status == 0 and dl.status.total == len(ogg)
        assert dl.data == ogg
        details = m["audio"]["device_details"]["ogg_opus"]
        assert hashlib.sha256(dl.data).hexdigest() == details["sha256"] and len(dl.data) == details["bytes"]
        n_full, tail = divmod(len(ogg), CHUNK)
        assert len(dl.chunks) == n_full + (1 if tail else 0)
        assert [c.offset for c in dl.chunks] == list(range(0, len(ogg), CHUNK))
        assert [c.last for c in dl.chunks] == [False] * (len(dl.chunks) - 1) + [True]
        assert all(c.session == SESSION for c in dl.chunks)
        assert not phone.dropped and not phone.errors

        # the received bytes are the device shape the SDK's own two-test rule expects ...
        assert classify_recording(dl.data)["shape"] == "plain_ogg"
        # ... and the pipeline decodes them exactly like the file on disk
        received = tmp_path / "received.ogg"
        received.write_bytes(dl.data)
        a, b = load_audio(received), load_audio(d / DEVICE_FILES["ogg_opus"])
        assert np.array_equal(a.pcm, b.pcm) and a.sample_rate == 16000
        assert abs(a.duration_s - m["duration_s"]) <= 0.020

        await teardown(dev, task)


@pytest.mark.asyncio
async def test_ranged_resume_returns_the_exact_slice(meeting, ogg) -> None:
    """start=CHUNK, end=3*CHUNK+17 -> bytes [CHUNK : 3*CHUNK+17), the phone's
    resume shape (downloadFile with a non-zero start)."""
    async with PhoneWifiServer(token=TOKEN, request_timeout=WAIT) as phone:
        dev, task = await session(phone, ogg)
        start, end = CHUNK, 3 * CHUNK + 17
        part = await asyncio.wait_for(phone.download(SESSION, end, start=start), WAIT)
        assert part.data == ogg[start:end]
        assert [c.offset for c in part.chunks] == [CHUNK, 2 * CHUNK, 3 * CHUNK]
        assert part.chunks[-1].last
        await teardown(dev, task)


@pytest.mark.asyncio
async def test_generated_ogg_over_a_sealed_wifi_session_is_byte_exact(meeting, ogg) -> None:
    """Same file through the ChaCha20-Poly1305 channel with the synthetic
    session keys: every frame on the wire is ciphertext, the plaintext is
    byte-exact."""
    phone_sealer = WifiSealer(SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L))
    dev_sealer = WifiSealer(SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L))
    async with PhoneWifiServer(token=TOKEN, sealer=phone_sealer, request_timeout=WAIT) as phone:
        dev, task = await session(phone, ogg, sealer=dev_sealer)
        dl = await asyncio.wait_for(phone.download(SESSION, len(ogg)), WAIT)
        assert dl.data == ogg
        assert all(looks_encrypted(f) for f in phone.raw_frames), "a sealed frame failed the >200 heuristic"
        assert not phone.dropped and not phone.errors
        await teardown(dev, task)
