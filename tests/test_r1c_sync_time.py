"""R1c: the syncTime exchange over a Bumble virtual link.

Request: m7, 9 bytes, `01 04 00` + u32le unix seconds + int8 tzHours + int8
tzMinutes. Both timezone fields are SIGNED: m7.a(TimeZone,long) computes
`offset = tz.getOffset(now) / 60000` then `(offset / 60, offset % 60)` with
Java integer division, which truncates toward zero and keeps the dividend's
sign -- so a device west of UTC sends negative hours AND negative minutes.

Response: n7, `01 04 00` + u32le stamp + int8 timezone + u8 hasStatistics.
The field at offset 7 is named `timezone` by n7's own toString literal; an
earlier reconstruction recorded it as an unnamed `raw7`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "reference/upstream/bumble"))
sys.path.insert(0, str(Path(__file__).parents[1] / "emulator"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

from support import connect_like_the_sdk, load_fixture

from plaudsim.profile import PlaudPeripheral, PlaudSyncTimeState, parse_sync_time_request

FX = load_fixture("post-bind-sync-time.json")
DET = FX["response"]["emulator_deterministic"]
EXPECTED_RESPONSE = bytes.fromhex(DET["example_hex"])
EXPECTED_REQUEST = bytes(FX["request"]["example_bytes"])
assert len(EXPECTED_RESPONSE) == 9 and len(EXPECTED_REQUEST) == 9

R1C_STATE = PlaudSyncTimeState(
    stamp=1700000000,
    tz_hours=0,
    tz_mins=0,
    timezone=0,
    has_statistics=False,
)
assert R1C_STATE.stamp == DET["stamp"]["value"]
assert R1C_STATE.encode_request() == EXPECTED_REQUEST
assert R1C_STATE.encode_response() == EXPECTED_RESPONSE


async def run_exchange(prefer_notify: bool, request: bytes = EXPECTED_REQUEST):
    devices, peripheral, peer, data, command = await connect_like_the_sdk(
        lambda device: PlaudPeripheral(device, synctime=R1C_STATE), "post-bind-sync-time.json"
    )
    responses: list[bytes] = []
    await peer.subscribe(data, responses.append, prefer_notify=prefer_notify)
    await peer.write_value(command, request, with_response=True)
    return peripheral, responses


@pytest.mark.asyncio
@pytest.mark.parametrize("prefer_notify", [True, False], ids=["notify", "indicate"])
async def test_sync_time_exchange(prefer_notify: bool) -> None:
    peripheral, responses = await run_exchange(prefer_notify)

    assert responses == [EXPECTED_RESPONSE]
    assert responses[0][:3] == b"\x01\x04\x00"
    assert int.from_bytes(responses[0][3:7], "little") == R1C_STATE.stamp
    assert responses[0][7] == R1C_STATE.timezone
    assert responses[0][8] == int(R1C_STATE.has_statistics)


@pytest.mark.asyncio
async def test_any_wall_clock_is_answered_not_just_the_fixture_value() -> None:
    """The device answers a well-formed opcode-4 request whatever timestamp it
    carries. An earlier emulator compared the whole request against one fixed
    byte string, so a real SDK -- which sends the current wall clock -- would
    have been silently ignored."""
    other = PlaudSyncTimeState(stamp=1893456000, tz_hours=5, tz_mins=30).encode_request()
    assert other != EXPECTED_REQUEST
    peripheral, responses = await run_exchange(True, other)
    assert len(responses) == 1
    assert responses[0] == EXPECTED_RESPONSE


def test_timezone_fields_are_signed_int8() -> None:
    """India (UTC+5:30) -> (5, 30). Newfoundland (UTC-3:30) -> (-3, -30):
    Java's `/` and `%` truncate toward zero, so BOTH components go negative."""
    india = PlaudSyncTimeState(stamp=1, tz_hours=5, tz_mins=30).encode_request()
    assert india[7:9] == bytes([5, 30])
    assert parse_sync_time_request(india) == {"stamp": 1, "tz_hours": 5, "tz_mins": 30}

    newfoundland = PlaudSyncTimeState(stamp=1, tz_hours=-3, tz_mins=-30).encode_request()
    assert newfoundland[7:9] == bytes([0xFD, 0xE2])
    assert parse_sync_time_request(newfoundland) == {
        "stamp": 1, "tz_hours": -3, "tz_mins": -30
    }
    # An unsigned reading would report 253 and 226.
    assert newfoundland[7] == 253 and newfoundland[8] == 226

    with pytest.raises(ValueError):
        PlaudSyncTimeState(stamp=1, tz_hours=200).encode_request()


def test_response_timezone_is_signed_too() -> None:
    """n7.<init> casts through `(byte)`, so offset 7 is int8 in the response."""
    frame = PlaudSyncTimeState(stamp=1, timezone=-5).encode_response()
    assert frame[7] == 0xFB


def test_request_parser_rejects_the_wrong_shapes() -> None:
    for bad in (
        b"\x01\x04\x00" + bytes(4),            # 7 bytes: missing both tz fields
        b"\x01\x04\x00" + bytes(6),            # 9 bytes but... actually 9; keep shape checks below
    )[:1]:
        with pytest.raises(ValueError):
            parse_sync_time_request(bad)
    with pytest.raises(ValueError):
        parse_sync_time_request(b"\x01\x06\x00" + bytes(6))      # opcode 6, not 4
    with pytest.raises(ValueError):
        parse_sync_time_request(b"\x02\x04\x00" + bytes(6))      # protocol type 2
    with pytest.raises(ValueError):
        parse_sync_time_request(b"\x01\x04\x00" + bytes(7))      # 10 bytes
