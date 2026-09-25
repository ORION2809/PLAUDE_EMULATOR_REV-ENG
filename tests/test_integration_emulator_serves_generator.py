"""Layer 2 -> Layer 1 -> pipeline: the generator's device Ogg served by the BLE
emulator over Bumble's virtual link, pulled the way the Android SDK pulls,
and then fed to the pipeline as if it had come off a device.

    generator device/recording.ogg
      -> PlaudPeripheral file table (Bumble TwoDevices, MTU 255, real ATT discovery)
      -> getFileList, syncFileStart -> HEAD, DATA..., EMPTY_PACKAGE, TAIL   (R7-S13)
      -> reassembled on the central side, EMPTY_PACKAGE sentinel skipped
      -> byte-exact vs the file and its sha256 in meeting.json
      -> pipeline.load_audio decodes the pulled bytes identically to the file
      -> "energy-vad-cluster" (the model-free system that runs here) diarizes it

Evidence class: EMULATOR_INTEGRATION_PROVEN for our own components.  Nothing
here is a claim about real Plaud hardware or the real SDK (the real SDK's
completion on this exact sequence is R7-S13 runtime evidence, pinned in
tests/test_r7_s13_transfer_close.py).

HARNESS_POLICY, stated here and in docs/integration.md:
  * DATA payload 240 bytes (u8 length field; under the SDK's 255-byte MTU).
  * Speaker-count tolerance for the model-free diarizer.  Its clustering
    threshold (2.5) was tuned on pulse-train voices, not on the generator's
    formant voices; on this meeting it merges both speakers into one cluster
    when no speaker count is given.  With the count hint the pipeline supports
    (``num_speakers``) it recovers exactly two.  The test therefore asserts:
    exact count WITH the hint, |error| <= 1 WITHOUT it, no missed reference
    speech (miss rate <= 0.02; measured 0.0), and DER below the 0.5 that a
    two-speaker coin flip would score (measured 0.294).  These are the
    properties provable today; a real-speech claim needs re-tuning (docs/pipeline.md).
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from struct import pack

import numpy as np
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "reference/upstream/bumble"))
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from support import connect_like_the_sdk  # noqa: E402

from plaudsim.audio import classify_recording  # noqa: E402
from plaudsim.filesync import (  # noqa: E402
    EMPTY_PACKAGE_OFFSET,
    parse_file_data_frame,
    parse_file_list_frame,
    parse_sync_head,
    parse_sync_tail,
)
from plaudsim.profile import PlaudPeripheral  # noqa: E402

from evals import load_meeting, score_meeting  # noqa: E402
from evals.io import hypothesis_from_dict  # noqa: E402
from generator.export import DEVICE_FILES  # noqa: E402
from generator.testing import fixture_meeting  # noqa: E402
from pipeline import PipelineConfig, get_pipeline, load_audio  # noqa: E402

pytestmark = pytest.mark.timeout(120)

SESSION = 0x0655A1B0
TAIL_CRC = 0x0BEE
PAYLOAD = 240  # HARNESS_POLICY, see module docstring
#: HARNESS_POLICY tolerances for the model-free diarizer (see module docstring).
SPEAKER_COUNT_TOLERANCE_WITHOUT_HINT = 1
MAX_MISS_RATE = 0.02
MAX_DER_WITH_HINT = 0.5


@pytest.fixture(scope="module")
def meeting(tmp_path_factory) -> tuple[Path, dict]:
    return fixture_meeting(tmp_path_factory.mktemp("ble-gen"), "smoke", 3)


@pytest.fixture(scope="module")
def ogg(meeting) -> bytes:
    d, _ = meeting
    return (d / DEVICE_FILES["ogg_opus"]).read_bytes()


def reassemble(frames: list[bytes], port_version: int) -> bytes:
    """DATA frames -> file bytes; the caller has already cut HEAD/EMPTY/TAIL off."""
    body = bytearray()
    cursor = 0
    for frame in frames:
        parsed = parse_file_data_frame(frame, port_version=port_version)
        assert parsed["session_id"] == SESSION
        assert parsed["offset"] != EMPTY_PACKAGE_OFFSET, "the sentinel must not be reassembled"
        assert parsed["offset"] == cursor, "offset gap: the SDK would resend from its cursor"
        body += parsed["payload"]
        cursor += len(parsed["payload"])
    return bytes(body)


async def pull_like_the_sdk(file_bytes: bytes) -> tuple[bytes, PlaudPeripheral, list[bytes]]:
    """List, then pull SESSION over the virtual GATT link; return (bytes, peripheral, transfer frames)."""
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(
            device,
            file_bytes=file_bytes,
            file_table=[{"session_id": SESSION, "file_size": len(file_bytes), "scene": 2, "attribute": 1}],
            tail_crc=TAIL_CRC,
            data_payload_size=PAYLOAD,
        )
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=True)

    await peer.write_value(command, b"\x01\x1a\x00" + pack("<II", 0x655A1B00, 0) + b"\x00", with_response=True)
    listing = parse_file_list_frame(responses[-1], port_version=peripheral.port_version)
    assert listing["totals"] == 1
    entry = listing["entries"][0]
    assert entry["session_id"] == SESSION and entry["file_size"] == len(file_bytes)

    n = len(responses)
    await peer.write_value(command, b"\x01\x1c\x00" + pack("<III", SESSION, 0, 0), with_response=True)
    expected = 3 + -(-len(file_bytes) // PAYLOAD)  # HEAD + DATA + EMPTY_PACKAGE + TAIL
    for _ in range(4000):
        if len(responses) - n >= expected:
            break
        await asyncio.sleep(0.005)
    transfer = responses[n:]
    assert len(transfer) == expected, f"got {len(transfer)} frames, expected {expected}"
    assert parse_sync_head(transfer[0]) == {"session_id": SESSION, "status": 0}
    assert transfer[-2][0] == 2 and transfer[-2][5:9] == b"\xff\xff\xff\xff", "EMPTY_PACKAGE before TAIL (R7-S13)"
    assert parse_sync_tail(transfer[-1]) == {"session_id": SESSION, "crc": TAIL_CRC}
    body = reassemble(transfer[1:-2], peripheral.port_version)
    await devices.connections[0].disconnect()
    return body, peripheral, transfer


# --- byte-exact over the air ------------------------------------------------------


@pytest.mark.asyncio
async def test_generated_ogg_pulled_over_bumble_is_byte_exact(meeting, ogg) -> None:
    d, m = meeting
    body, peripheral, transfer = await pull_like_the_sdk(ogg)
    assert body == ogg
    details = m["audio"]["device_details"]["ogg_opus"]
    assert hashlib.sha256(body).hexdigest() == details["sha256"] and len(body) == details["bytes"]
    assert classify_recording(body)["shape"] == "plain_ogg" == details["shape"]
    # every DATA frame but the last carries the full policy payload
    sizes = [len(parse_file_data_frame(f, port_version=peripheral.port_version)["payload"]) for f in transfer[1:-2]]
    assert set(sizes[:-1]) == {PAYLOAD} and 0 < sizes[-1] <= PAYLOAD
    assert not [e for e in peripheral.packet_log if e["direction"] == "error"], "no rejected request"


# --- the pulled bytes are what the pipeline sees --------------------------------------


@pytest.mark.asyncio
async def test_pulled_bytes_decode_identically_and_diarize_within_tolerance(meeting, ogg, tmp_path) -> None:
    d, m = meeting
    body, _, _ = await pull_like_the_sdk(ogg)
    pulled = tmp_path / "pulled" / "recording.ogg"   # suffix routes the loader to PyAV
    pulled.parent.mkdir()
    pulled.write_bytes(body)

    # 1. the pipeline's loader decodes the pulled bytes exactly as the file on disk
    a = load_audio(pulled)
    b = load_audio(d / DEVICE_FILES["ogg_opus"])
    assert a.container == b.container == "pyav" and a.source_sample_rate == 48000
    assert a.sample_rate == b.sample_rate == m["sample_rate"] == 16000
    assert np.array_equal(a.pcm, b.pcm), "same bytes in, same PCM out"
    assert abs(a.duration_s - m["duration_s"]) <= 0.020

    # 2. the model-free system, WITH the speaker-count hint: exact count
    n_ref = len(m["speakers"])
    hinted = get_pipeline("energy-vad-cluster", PipelineConfig(params={"num_speakers": n_ref}))
    assert hinted.is_system_under_test is True
    hyp = hinted.run(pulled, d)
    assert hyp.meeting_id == m["meeting_id"]
    assert all(s["text"] == "" for s in hyp.segments), "a diarization-only system emits no words"
    assert len(hyp.speakers) == n_ref == hyp.extra["diarization"]["n_clusters"]
    report = score_meeting(load_meeting(d), hypothesis_from_dict(hyp.to_dict()))
    assert report.speaker_count.abs_error == 0
    assert report.der.miss / report.der.total <= MAX_MISS_RATE, "the VAD must not lose reference speech"
    assert report.der.der < MAX_DER_WITH_HINT, f"DER {report.der.der:.3f}"
    assert report.der.der > 0.0, "a model-free clusterer is not an oracle; the zero would be suspicious"

    # 3. WITHOUT the hint: the documented threshold merges the formant voices
    auto = get_pipeline("energy-vad-cluster").run(pulled, d)
    assert abs(len(auto.speakers) - n_ref) <= SPEAKER_COUNT_TOLERANCE_WITHOUT_HINT, auto.speakers
    auto_report = score_meeting(load_meeting(d), hypothesis_from_dict(auto.to_dict()))
    assert auto_report.der.miss / auto_report.der.total <= MAX_MISS_RATE

    # 4. the hypothesis from the pulled bytes equals the one from the on-disk file
    on_disk = get_pipeline("energy-vad-cluster", PipelineConfig(params={"num_speakers": n_ref})).run(
        d / DEVICE_FILES["ogg_opus"], d
    )
    assert on_disk.segments == hyp.segments
