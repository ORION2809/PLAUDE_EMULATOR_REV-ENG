"""Device <-> phone-double sessions over loopback WebSockets.

The device (emulator/plaudsim/wifi_device.py) dials the phone double
(tests/wifi_support.py) exactly as the pen dials the phone's java-websocket
server: the phone binds first, the pen connects, speaks SayHello, and the phone
answers with the Handshake. Ports are ephemeral (port 0) so the tests do not
collide with anything on 8081; the 8081 fact itself is pinned in
test_wifi_javap_pins.py.

Timings in these tests (heartbeat 50 ms, exit timeouts of a few hundred ms,
chunk delays of a few ms) are TEST POLICY that exercise the device's
HARNESS_POLICY timers; none of them is a claim about hardware cadence.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))

from plaudsim.sealed import SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L, SealedSession
from plaudsim.wifi import (
    CIPHERTEXT_TYPE_THRESHOLD,
    MSG_FILE_SYNC_CONTENT,
    MSG_FILE_SYNC_STOP,
    MSG_GET_FILE_LIST,
    MSG_HANDSHAKE,
    MSG_SAY_HELLO,
    MSG_WIFI_CLOSE,
    FileRecord,
    FileSyncContent,
    FileSyncStopRequest,
    WifiSealer,
    looks_encrypted,
)
from plaudsim.wifi_device import WifiDevice, WifiDeviceState, WifiFileStore
from wifi_support import ERROR_HANDSHAKE_FAILED, STATE_READY, PhoneWifiServer

TOKEN = "SYNTHETIC-WIFI-TOKEN-0123456789ABCDEF"[:32]
BIG = bytes(range(256)) * 40          # 10240 bytes, 11 chunks of 1000
SESSION_BIG = 1700000000
SESSION_LOG = 5


def make_store() -> WifiFileStore:
    return WifiFileStore.from_files({SESSION_BIG: BIG, SESSION_LOG: b"log-bytes"})


def make_device(phone: PhoneWifiServer, **kw) -> WifiDevice:
    defaults = dict(
        store=make_store(),
        expected_token=TOKEN,
        chunk_size=1000,
        heartbeat_interval=0.05,
        idle_heartbeats_before_close=3,
        exit_timeout=None,
    )
    defaults.update(kw)
    return WifiDevice("127.0.0.1", phone.port, **defaults)


async def run_device(dev: WifiDevice) -> asyncio.Task[None]:
    task = asyncio.create_task(dev.run())
    await asyncio.wait_for(dev.connected.wait(), 3)
    return task


# --- the full session -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_plaintext_session_handshake_list_sync_delete_extend_close():
    async with PhoneWifiServer(token=TOKEN) as phone:
        dev = make_device(phone)
        task = await run_device(dev)
        await phone.wait_ready(3)

        # Ordering: the pen spoke first; the phone's first frame is the Handshake
        # and it came after the SayHello.
        kinds = [(e["dir"], e.get("type")) for e in phone.log if "type" in e]
        assert kinds[0] == ("in", MSG_SAY_HELLO)
        first_out = next(i for i, k in enumerate(kinds) if k[0] == "out")
        assert kinds[first_out] == ("out", MSG_HANDSHAKE)
        assert phone.hellos[0].sn == dev.serial and phone.hellos[0].p_ver == dev.p_ver
        assert phone.handshakes[0].status == 0 and phone.session_id == dev.session_id
        assert phone.state == STATE_READY and dev.state is WifiDeviceState.HANDSHAKED
        assert dev.log[0]["dir"] == "out" and dev.log[0]["type"] == MSG_SAY_HELLO

        # File list: one frame, offset 0, both entries, u16 scene.
        listing = await phone.get_file_list()
        assert listing.status == 0 and listing.count == 2 and listing.offset == 0
        assert listing.records == [FileRecord(SESSION_BIG, len(BIG), 1), FileRecord(SESSION_LOG, 9, 1)]
        assert sum(1 for e in phone.log if e.get("type") == MSG_GET_FILE_LIST and e["dir"] == "in") == 1

        # Multi-chunk sync, byte-exact reassembly, contiguous monotonic offsets.
        dl = await phone.download(SESSION_BIG, len(BIG))
        assert dl.status is not None and dl.status.status == 0 and dl.status.total == len(BIG)
        assert dl.data == BIG
        assert len(dl.chunks) == 11
        assert [c.offset for c in dl.chunks] == list(range(0, 10240, 1000))
        assert [c.length for c in dl.chunks] == [1000] * 10 + [240]
        assert [c.last for c in dl.chunks] == [False] * 10 + [True]
        assert all(c.session == SESSION_BIG for c in dl.chunks)

        # A range: start=1000, end=2500 -> bytes [1000:2500) in two chunks.
        part = await phone.download(SESSION_BIG, 2500, start=1000)
        assert part.data == BIG[1000:2500]
        assert [c.offset for c in part.chunks] == [1000, 2000]

        # Delete: existing -> 0, unknown -> policy 1, and the table shrinks.
        assert (await phone.delete_file(SESSION_LOG)).status == 0
        assert (await phone.delete_file(424242)).status == 1
        assert [r.session_id for r in (await phone.get_file_list()).records] == [SESSION_BIG]

        assert (await phone.extend_exit_time()).status == 0

        # Then the phone goes quiet: after 3 idle heartbeats the pen announces
        # WifiClose and hangs up; the phone tears its server down.
        before = dev.heartbeats_sent
        await phone.wait_device_closed(3)
        assert len(phone.closes) == 1 and phone.closes[0].reason == "idle_heartbeats"
        assert dev.heartbeats_sent - before <= 3 and dev.idle_heartbeats == 3
        await asyncio.wait_for(task, 3)
        assert dev.state is WifiDeviceState.CLOSED and dev.close_reason == "idle_heartbeats"
        assert dev.state_log == ["idle", "connecting", "connected", "handshaked", "closing", "closed"]
        assert phone.state == "DISCONNECTED"
        # every ping was answered until the close
        assert dev.pongs_received >= dev.heartbeats_sent - 1
        assert not phone.dropped and not phone.errors


@pytest.mark.asyncio
async def test_phone_never_speaks_before_say_hello():
    """With auto_handshake off the phone stays mute forever; the pen keeps
    heartbeating in CONNECTED and never reaches HANDSHAKED."""
    async with PhoneWifiServer(token=TOKEN, auto_handshake=False) as phone:
        dev = make_device(phone, idle_heartbeats_before_close=None)
        task = await run_device(dev)
        await asyncio.sleep(0.3)
        assert dev.state is WifiDeviceState.CONNECTED
        assert not [e for e in phone.log if e["dir"] == "out" and e.get("type") == MSG_HANDSHAKE]
        assert dev.heartbeats_sent >= 3 and not phone.ready.is_set()
        await dev.close("test")
        await asyncio.wait_for(task, 3)


# --- failure modes ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_mismatch_yields_non_zero_status_and_phone_error_1006():
    async with PhoneWifiServer(token="WRONG-TOKEN") as phone:
        dev = make_device(phone, expected_token=TOKEN, idle_heartbeats_before_close=None)
        task = await run_device(dev)
        await asyncio.sleep(0.3)
        assert phone.handshakes and phone.handshakes[0].status == 1
        assert phone.handshakes[0].session == ""
        assert phone.errors == [(ERROR_HANDSHAKE_FAILED, "Handshake failed with status: 1")]
        assert not phone.ready.is_set() and dev.state is WifiDeviceState.CONNECTED
        assert dev.log[-1]["dir"] != "event" or dev.log[-1]["kind"] != "handshake"
        handshake_event = next(e for e in dev.log if e.get("kind") == "handshake")
        assert handshake_event["accepted"] is False
        assert handshake_event["token"] == "WRONG-TOKEN" + "0" * 21
        await dev.close("test")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_unknown_session_sync_fails_with_status_and_no_content():
    async with PhoneWifiServer(token=TOKEN) as phone:
        dev = make_device(phone, idle_heartbeats_before_close=None)
        task = await run_device(dev)
        await phone.wait_ready(3)
        with pytest.raises(RuntimeError, match="File sync failed: status=1"):
            await phone.download(999999, 10)
        assert phone.current_download().chunks == []
        assert not dev.transfer_active
        await dev.close("test")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_stop_mid_transfer_halts_the_stream_and_is_acknowledged():
    big = bytes(range(256)) * 256           # 64 KiB
    store = WifiFileStore.from_files({77: big})
    async with PhoneWifiServer(token=TOKEN) as phone:
        dev = make_device(phone, store=store, chunk_size=512, chunk_delay=0.003, idle_heartbeats_before_close=None)
        task = await run_device(dev)
        await phone.wait_ready(3)

        stop_fut = phone._new_waiter(MSG_FILE_SYNC_STOP)
        sent_stop = False

        async def hook(content):
            nonlocal sent_stop
            if len(phone.current_download().chunks) == 3 and not sent_stop:
                sent_stop = True
                await phone.send(FileSyncStopRequest(77, 1))

        phone.chunk_hook = hook
        transfer = await phone.start_download(77, len(big))
        stop = await asyncio.wait_for(stop_fut, 3)
        assert stop.status == 0
        chunks_at_ack = len(phone.current_download().chunks)
        await asyncio.sleep(0.1)
        assert len(phone.current_download().chunks) == chunks_at_ack, "chunks kept flowing after the stop ack"
        assert chunks_at_ack < len(big) // 512
        assert not transfer.done()
        assert not dev.transfer_active
        ev = next(e for e in dev.log if e.get("kind") == "sync_stop")
        assert ev["stopped_active"] is True
        # the phone's partial buffer is exactly the chunks it saw, in order
        assert phone.current_download().data == big[: 512 * chunks_at_ack]
        phone._transfer_future = None
        await dev.close("test")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_second_file_sync_replaces_the_active_stream():
    big = bytes(range(256)) * 128
    store = WifiFileStore.from_files({1: big, 2: b"second"})
    async with PhoneWifiServer(token=TOKEN) as phone:
        dev = make_device(phone, store=store, chunk_size=256, chunk_delay=0.003, idle_heartbeats_before_close=None)
        task = await run_device(dev)
        await phone.wait_ready(3)
        await phone.start_download(1, len(big))
        await asyncio.sleep(0.02)
        dl = await phone.download(2, 6)
        assert dl.data == b"second"
        assert [c.session for c in dl.chunks] == [2]
        await asyncio.sleep(0.05)
        assert not dev.transfer_active
        # The device cancelled stream 1 before answering FileSync(2) ...
        replaced = next(e for e in dev.log if e.get("kind") == "transfer_replaced")
        assert replaced["session"] == 1 and replaced["by"] == 2
        out = [e for e in dev.log if e["dir"] == "out" and e.get("type") in (MSG_FILE_SYNC_CONTENT, 12)]
        status2 = next(i for i, e in enumerate(out) if e["type"] == 12 and i > 0)
        assert all(e["type"] == MSG_FILE_SYNC_CONTENT for e in out[status2 + 1:]) and len(out) - status2 - 1 == 1
        # ... and whatever chunks of stream 1 were already on the wire were
        # dropped by the phone, which keys transfers by session id, not appended.
        assert all(d == "No transfer context for session: 1" for d in phone.dropped)
        await dev.close("test")
        await asyncio.wait_for(task, 3)


# --- timers ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_timeout_closes_unless_extended():
    async with PhoneWifiServer(token=TOKEN) as phone:
        dev = make_device(phone, heartbeat_interval=None, idle_heartbeats_before_close=None, exit_timeout=0.25)
        task = await run_device(dev)
        await phone.wait_ready(3)
        # keep extending for longer than the timeout: still open
        for _ in range(4):
            await asyncio.sleep(0.1)
            assert (await phone.extend_exit_time()).status == 0
        assert dev.state is WifiDeviceState.HANDSHAKED and not phone.closes
        # stop extending: closes with the timeout reason
        await phone.wait_device_closed(2)
        assert phone.closes[0].reason == "exit_timeout"
        await asyncio.wait_for(task, 3)
        assert dev.close_reason == "exit_timeout"


@pytest.mark.asyncio
async def test_a_request_resets_the_idle_heartbeat_count():
    async with PhoneWifiServer(token=TOKEN) as phone:
        dev = make_device(phone, heartbeat_interval=0.04, idle_heartbeats_before_close=3)
        task = await run_device(dev)
        await phone.wait_ready(3)
        for _ in range(6):
            await asyncio.sleep(0.06)
            await phone.get_file_list()
            assert dev.idle_heartbeats < 3
        assert dev.state is WifiDeviceState.HANDSHAKED
        await phone.wait_device_closed(2)
        await asyncio.wait_for(task, 3)


# --- sealed sessions -----------------------------------------------------------------------


@pytest.mark.parametrize("use_aes", [False, True], ids=["chacha20-poly1305", "aes-gcm"])
@pytest.mark.asyncio
async def test_sealed_session_end_to_end(use_aes):
    phone_sealer = WifiSealer(SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L), use_aes=use_aes)
    dev_sealer = WifiSealer(SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L), use_aes=use_aes)
    async with PhoneWifiServer(token=TOKEN, sealer=phone_sealer) as phone:
        dev = make_device(phone, sealer=dev_sealer)
        task = await run_device(dev)
        await phone.wait_ready(3)
        listing = await phone.get_file_list()
        assert listing.count == 2
        dl = await phone.download(SESSION_BIG, len(BIG))
        assert dl.data == BIG and len(dl.chunks) == 11
        assert (await phone.delete_file(SESSION_LOG)).status == 0
        await phone.wait_device_closed(3)
        await asyncio.wait_for(task, 3)
        # every frame in both directions was sealed and classified as such
        assert all(e["sealed"] for e in dev.log if e["dir"] in ("in", "out"))
        assert all(looks_encrypted(f) for f in phone.raw_frames), "a sealed frame failed the >200 heuristic"
        assert not phone.dropped
        # device seq: 2 (SayHello), 3 (Handshake rsp), ... strictly increasing
        seqs = [e["seq"] for e in dev.log if e["dir"] == "out"]
        assert seqs[0] == 2 and seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert dev_sealer.algorithm == ("AES-GCM" if use_aes else "ChaCha20-Poly1305")


@pytest.mark.asyncio
async def test_key_mismatch_is_an_aead_failure_on_the_phone():
    phone_sealer = WifiSealer(SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L))
    dev_sealer = WifiSealer(SealedSession(b"Z" * 32, SYNTHETIC_K, SYNTHETIC_L))
    async with PhoneWifiServer(token=TOKEN, sealer=phone_sealer) as phone:
        dev = make_device(phone, sealer=dev_sealer, idle_heartbeats_before_close=None)
        task = await run_device(dev)
        await asyncio.sleep(0.2)
        assert phone.dropped and all(d == "decrypt_failed:InvalidTag" for d in phone.dropped)
        assert not phone.hellos and not phone.ready.is_set()
        await dev.close("test")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_sealed_pen_against_keyless_phone_never_handshakes():
    dev_sealer = WifiSealer(SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L))
    async with PhoneWifiServer(token=TOKEN) as phone:
        dev = make_device(phone, sealer=dev_sealer, idle_heartbeats_before_close=None)
        task = await run_device(dev)
        await asyncio.sleep(0.2)
        assert not phone.hellos and phone.dropped   # ciphertext parsed as PDU -> malformed
        assert all(d.startswith("malformed:") or d == "Message too short" for d in phone.dropped)
        await dev.close("test")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_plaintext_pen_is_accepted_by_a_keyed_phone():
    """The phone parses plaintext whenever the heuristic says plaintext, keys or not."""
    phone_sealer = WifiSealer(SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L))
    async with PhoneWifiServer(token=TOKEN, sealer=phone_sealer) as phone:
        dev = make_device(phone, sealer=None, idle_heartbeats_before_close=None)
        task = await run_device(dev)
        # the phone's Handshake is sealed; the keyless pen cannot open it
        await asyncio.sleep(0.2)
        assert phone.hellos and not phone.ready.is_set()
        assert any(e.get("kind") == "ciphertext_without_keys" for e in dev.log)
        await dev.close("test")
        await asyncio.wait_for(task, 3)


def colliding_content_length(sealer: WifiSealer, session: int) -> int:
    """Smallest FileSyncContent data length whose SEALED frame has u16le@4 <= 200.

    The nonce is reused for every frame (as on BLE), so the AEAD keystream is
    constant and sealed byte i == plaintext byte i XOR k[i]; plaintext bytes 4..5
    are PDU bytes 0..1 == totalSize & 0xFFFF. The phone's classifier therefore
    fails for exactly 201 of the 65536 possible low-16-bit sizes, and which
    ones is fixed by (key, nonce).
    """
    probe = WifiSealer(SealedSession(sealer.counters.key, sealer.counters.nonce, sealer.counters.aad), use_aes=sealer.use_aes)
    k = probe._seal_raw(bytes(8))
    k16 = k[4] | k[5] << 8
    collide = {v ^ k16 for v in range(CIPHERTEXT_TYPE_THRESHOLD + 1)}
    n = max(0, min(collide) - 200)
    while True:
        if len(FileSyncContent(session, 0, bytes(n), last=True).encode()) & 0xFFFF in collide:
            return n
        n += 1


@pytest.mark.parametrize("use_aes", [False, True], ids=["chacha20-poly1305", "aes-gcm"])
@pytest.mark.asyncio
async def test_sealed_frame_that_defeats_the_size_heuristic_is_dropped_and_stalls_the_transfer(use_aes):
    """BYTECODE_PROVEN rule (u16le@4 > 200, WifiAgentImpl.txt:850-866) applied to
    ciphertext: a sealed frame whose bytes 4..5 happen to be <= 200 is parsed as
    PLAINTEXT by the phone and lost. With the synthetic keys the colliding sizes
    are fixed, so this is deterministic. The device is not at fault -- it sealed
    a well-formed frame -- yet the phone never sees last=1 and the download
    runs into its request timeout (30 s on the SDK; shortened here)."""
    session = 1700000001
    dev_sealer = WifiSealer(SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L), use_aes=use_aes)
    phone_sealer = WifiSealer(SealedSession(SYNTHETIC_J, SYNTHETIC_K, SYNTHETIC_L), use_aes=use_aes)
    n = colliding_content_length(dev_sealer, session)
    assert CIPHERTEXT_TYPE_THRESHOLD < n < 0x10000
    store = WifiFileStore.from_files({session: bytes(range(256)) * (n // 256) + bytes(n % 256)})
    async with PhoneWifiServer(token=TOKEN, sealer=phone_sealer, request_timeout=0.5) as phone:
        dev = make_device(phone, store=store, sealer=dev_sealer, chunk_size=n, heartbeat_interval=None,
                          idle_heartbeats_before_close=None)
        task = await run_device(dev)
        await phone.wait_ready(3)
        with pytest.raises(asyncio.TimeoutError):
            await phone.download(session, n)
        # the device emitted exactly one sealed content frame (last=1) ...
        sent = [e for e in dev.log if e["dir"] == "out" and e.get("type") == MSG_FILE_SYNC_CONTENT]
        expected_wire = len(FileSyncContent(session, 0, bytes(n), last=True).encode()) + 4 + 16   # [u32 seq] + AEAD tag
        assert len(sent) == 1 and sent[0]["sealed"] and sent[0]["len"] == expected_wire
        # ... whose wire form fails the classifier ...
        offending = [f for f in phone.raw_frames if not looks_encrypted(f)]
        assert len(offending) == 1 and (offending[0][4] | offending[0][5] << 8) <= CIPHERTEXT_TYPE_THRESHOLD
        # ... so the phone parsed ciphertext as a PDU: dropped as malformed, or
        # dispatched as an unknown type <= 200; either way no chunk was stored.
        assert phone.current_download().chunks == []
        assert phone.dropped or phone.unknown
        assert all(t <= CIPHERTEXT_TYPE_THRESHOLD for t in phone.unknown)
        await dev.close("test")
        await asyncio.wait_for(task, 3)


# --- auxiliary verbs ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speed_test_and_device_log():
    async with PhoneWifiServer(token=TOKEN) as phone:
        dev = make_device(phone, idle_heartbeats_before_close=None, device_log=b"SYN-LOG-0123")
        task = await run_device(dev)
        await phone.wait_ready(3)
        packets = await phone.speed_test(pack_size=300, expected_packets=3)
        assert [p.index for p in packets] == [0, 1, 2]
        assert all(len(p.pack_data) == 300 for p in packets)
        log = await phone.get_device_log()
        assert log.offset == 0 and log.data == b"SYN-LOG-0123"
        await dev.close("test")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_malformed_and_unknown_frames_are_logged_not_fatal():
    async with PhoneWifiServer(token=TOKEN) as phone:
        dev = make_device(phone, idle_heartbeats_before_close=None)
        task = await run_device(dev)
        await phone.wait_ready(3)
        await phone.send_raw(b"\x01\x02\x03")                                   # too short
        await phone.send_raw(bytes.fromhex("0a000001" "0300" "ffff") + b"{}")   # negative jsonSize
        await phone.send_raw(bytes.fromhex("0a000001" "3700" "0200") + b"{}")   # type 55: unknown
        await phone.send_raw(bytes.fromhex("0a000001" "1400" "0200") + b"{}")   # type 20: OTA, unsupported
        await asyncio.sleep(0.05)
        kinds = [e.get("kind") for e in dev.log if e["dir"] == "event"]
        assert kinds.count("malformed_pdu") == 2
        assert "unknown_message_type" in kinds and "unsupported_ota" in kinds
        assert (await phone.get_file_list()).count == 2   # still serving
        await dev.close("test")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_connect_failure_is_reported_not_raised():
    dev = WifiDevice("127.0.0.1", 1, store=make_store(), open_timeout=1.0)   # port 1: nothing listens
    await dev.run()
    assert dev.state is WifiDeviceState.FAILED
    assert any(e.get("kind") == "connect_failed" for e in dev.log)
    assert dev.closed.is_set()


def test_wifi_close_type_is_4_and_store_helpers():
    assert MSG_WIFI_CLOSE == 4
    store = make_store()
    assert store.get(SESSION_LOG) == b"log-bytes"
    assert store.delete(SESSION_LOG) is True and store.delete(SESSION_LOG) is False
    assert [r.session_id for r in store.records()] == [SESSION_BIG]
