"""R7-S12: the genuine official SDK's k3 write, replayed as a golden test.

Provenance. On 2026-09-23 the real `plaud-sdk.aar` (sha256 041a6f88...) ran on
an API-34 AVD and, through `recoveryConnectBleDevice(device, historicalUserId)`
with SYNTHETIC identifiers, wrote its first_handshake (k3) frame to 1910/2BB1 of
`emulator/plaudsim` attached over Bumble's android-netsim transport. The
peripheral recorded every 2BB1 write (r7/r7-s12-k3-capture-*.json); the SDK's
own logcat lines are in r7/r7-s12-logcat-*.log.

What this proves, and what it does not:
  * k3 construction is byte-exact vs the bytecode model (build_k3):
    SDK_PROVEN + BYTECODE_PROVEN + RUNTIME_PROVEN + EMULATOR_INTEGRATION_PROVEN.
  * The token is the LOCALLY-normalised historicalUserId, '0'-padded (run 1)
    or truncated (run 3) to token_width(pv). No cloud call is involved.
  * An empty token makes the SDK abort and write NOTHING (run 2 = falsifier):
    the empty-token guard, not a cloud guard, is the only gate.
  * It is NOT evidence about real Plaud hardware, and NOT authentication: the
    device that accepted the token is ours (HARNESS_POLICY accept_any_token).

The captures are evidence files; if they are absent the tests skip rather
than fabricate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "emulator"))

from plaudsim.handshake import (  # noqa: E402
    build_k3,
    build_k3_from_historical_id,
    normalize_historical_id,
    parse_handshake_request,
    token_width,
)

R7 = ROOT / "r7"
RUN1 = R7 / "r7-s12-k3-capture-run1.json"
RUN3 = R7 / "r7-s12-k3-capture-run3-truncate.json"
RUN2_LOGCAT = R7 / "r7-s12-logcat-run2-negative.log"
RUN2_CAPTURE = R7 / "r7-s12-k3-capture-run2-negative.json"
RUN1_LOGCAT = R7 / "r7-s12-logcat-run1.log"

PV = 7  # what the capture peripheral advertised


def _capture(path: Path) -> dict:
    if not path.exists():
        pytest.skip(f"runtime evidence file missing: {path.name}")
    return json.loads(path.read_text())


def _k3_writes(cap: dict) -> list[bytes]:
    return [bytes.fromhex(w["hex"]) for w in cap["writes"] if w.get("opcode") == 1]


# --- the pure model ---------------------------------------------------------

def test_normalisation_is_prefix_strip_then_dehyphen():
    assert normalize_historical_id("SYNTH-HIST-0001") == "SYNTHHIST0001"
    assert normalize_historical_id("client_user_ab-cd") == "abcd"
    assert normalize_historical_id("client_user_") == ""
    # only a LEADING prefix is stripped, and every '-' goes
    assert normalize_historical_id("x-client_user_-y") == "xclient_user_y"


def test_build_k3_layout_pv7_and_pv20():
    f7 = build_k3("SYNTHHIST0001", 7)
    assert f7.hex() == "01010002000053594e54484849535430303031303030"
    assert len(f7) == 6 + token_width(7) == 22
    f20 = build_k3("SYNTHHIST0001", 20)
    assert len(f20) == 6 + 32
    assert f20[:19] == f7[:19] and f20[19:] == b"0" * 19
    # stage byte is absent below pv 3
    assert len(build_k3("ab", 2)) == 5 + 16 and build_k3("ab", 2)[3:5] == b"\x02\x00"


def test_empty_token_means_no_frame():
    assert build_k3_from_historical_id("", PV) is None
    assert build_k3_from_historical_id("client_user_", PV) is None
    assert build_k3_from_historical_id("---", PV) is None


# --- golden replay against the genuine SDK captures ------------------------

def test_run1_padding_is_byte_exact_with_the_sdk():
    cap = _capture(RUN1)
    assert cap["port_version"] == PV
    k3s = _k3_writes(cap)
    assert len(k3s) == 1, "exactly one k3 per connect"
    got = k3s[0]
    assert got == build_k3_from_historical_id("SYNTH-HIST-0001", PV)
    assert got.hex() == "01010002000053594e54484849535430303031303030"
    parsed = parse_handshake_request(got, PV)
    assert parsed == {
        "agent_value": 0, "stage": 0, "is_second": False,
        "token": "SYNTHHIST0001000", "dev_token": None, "user_name": None,
    }


def test_run3_truncation_is_byte_exact_with_the_sdk():
    cap = _capture(RUN3)
    got = _k3_writes(cap)[0]
    hid = "client_user_0123456789abcdef0123456789ABCDEF"
    assert got == build_k3_from_historical_id(hid, PV)
    # 32-char token truncated to the pv<9 width of 16; nothing padded
    assert got[6:] == b"0123456789abcdef"
    assert parse_handshake_request(got, PV)["token"] == "0123456789abcdef"


def test_run2_empty_id_writes_nothing_and_sdk_aborts():
    if not RUN2_LOGCAT.exists():
        pytest.skip("run2 logcat missing")
    log = RUN2_LOGCAT.read_text(encoding="utf-8", errors="replace")
    # the SDK's own abort line (ALL.txt:2703) and the predicted state callback
    assert "historicalUserId 为空，中止" in log
    assert "K3CAP_CONNECT_STATE state=2" in log
    assert "stage=\"first_handshake\"" not in log
    if RUN2_CAPTURE.exists():
        assert _k3_writes(json.loads(RUN2_CAPTURE.read_text())) == []


def test_run1_sdk_log_names_the_normalised_token_and_legacy_path():
    if not RUN1_LOGCAT.exists():
        pytest.skip("run1 logcat missing")
    log = RUN1_LOGCAT.read_text(encoding="utf-8", errors="replace")
    assert "handshakeToken=SYNTHHIST0001" in log
    assert "protVersion=7" in log
    # legacy path: straight to first_handshake, accepted, then bind
    for stage in ("stage=first_handshake", "detail=old_protocol_ok", "stage=sync_time"):
        assert stage in log
    assert "K3CAP_BIND" in log and "status=0" in log
    # force-clear is inert below pv 20: no marker frame is written
    for w in _capture(RUN1)["writes"]:
        assert w["hex"][:2] == "01", "only type-1 control frames were written"


def test_run1_post_handshake_sequence_is_the_sdk_connect_order():
    cap = _capture(RUN1)
    ops = [w.get("opcode") for w in cap["writes"]]
    # k3, syncTime, battStatus, getState, then the two CommonSettings reads
    assert ops == [1, 4, 9, 3, 8, 8]
    reads = [bytes.fromhex(w["hex"]) for w in cap["writes"] if w.get("opcode") == 8]
    assert reads == [bytes.fromhex("010800010f00"), bytes.fromhex("010800011100")]
