"""R7-S12: the new emulator behaviour asserted against the MECHANICAL digest.

`docs/evidence-digest.json` is extracted from javap by
`scripts/extract_evidence_digest.py` (and regenerated+diffed by
tests/test_evidence_conformance.py). Asserting the opcode-8 codec and the k3
builder against *that* file, rather than against literals typed into the
tests, keeps these claims independent of the emulator that makes them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "emulator"))

from plaudsim.handshake import K3_CONST_AT_3, build_k3, token_width  # noqa: E402
from plaudsim.profile import (  # noqa: E402
    COMMON_ACTION_READ,
    COMMON_ACTION_SET,
    COMMON_TYPE_NAMES,
    OPCODE_COMMON_SETTINGS,
    encode_common_settings_response,
    parse_common_settings_request,
)

DIGEST = json.loads((ROOT / "docs/evidence-digest.json").read_text())


def test_digest_carries_the_enum_tables():
    assert DIGEST["extractor_version"] >= 3
    assert set(DIGEST["enums"]) == {"s0$b$a", "s0$b$b"}


def test_common_action_wire_values_come_from_the_digest():
    act = DIGEST["enums"]["s0$b$a"]
    assert act["READ"]["wire"] == COMMON_ACTION_READ == 1
    assert act["SETTING"]["wire"] == COMMON_ACTION_SET == 2
    assert act["READ"]["ordinal"] == 0 and act["SETTING"]["ordinal"] == 1


def test_common_type_table_equals_the_digest_wire_map():
    types = DIGEST["enums"]["s0$b$b"]
    assert len(types) == 21
    wire_to_name = {v["wire"]: name for name, v in types.items()}
    assert wire_to_name == COMMON_TYPE_NAMES
    # the ordinal order is the Swift CommonType case order; the wire is not it
    by_ordinal = [name for name, _ in sorted(types.items(), key=lambda kv: kv[1]["ordinal"])]
    assert by_ordinal[4] == "ENABLE_VAD" and types["ENABLE_VAD"]["wire"] == 15
    assert by_ordinal[6] == "REC_MODE" and types["REC_MODE"]["wire"] == 17
    assert any(v["wire"] != v["ordinal"] for v in types.values())


def test_q0_request_widths_match_enpkg():
    q0 = DIGEST["classes"]["q0"]
    assert q0.get("request_opcode") == OPCODE_COMMON_SETTINGS == 8
    assert q0.get("protocol_type") == 1
    widths = [c["width_bits"] for c in q0["enpkg_calls"]]
    assert widths == [8, 16, 32]  # action, type, value
    read = parse_common_settings_request(b"\x01\x08\x00\x01\x0f\x00")
    assert read == {"action": 1, "type": 15, "name": "ENABLE_VAD", "value": None}
    setf = b"\x01\x08\x00\x02\x0f\x00" + (7).to_bytes(4, "little")
    assert parse_common_settings_request(setf)["value"] == 7


def test_r0_response_lands_on_the_digest_parse_offsets():
    r0 = DIGEST["classes"]["r0"]
    assert r0["response_opcode"] == 8
    assert r0["tostring"].startswith("CommonSettingsRsp{type=")
    readers = {(r["offset"], r["width_bits"]) for r in r0["init_readers"]}
    assert readers == {(3, 16), (5, 32)}
    frame = encode_common_settings_response(0x1234, 0xDEADBEEF)
    assert int.from_bytes(frame[3:5], "little") == 0x1234
    assert int.from_bytes(frame[5:9], "little") == 0xDEADBEEF
    assert len(frame) == 9


def test_k3_builder_agrees_with_the_digest_k3_entry():
    k3 = DIGEST["classes"].get("k3")
    if not k3:
        pytest.skip("k3 not in digest classes")
    assert k3.get("request_opcode") == 1 and k3.get("protocol_type") == 1
    lits = set(k3.get("enpkg_structure", {}).get("int_literals", []))
    # the 0x02 constant and both token-width branches are literal in enPkg
    assert {K3_CONST_AT_3, 16, 32} <= lits
    f = build_k3("SYNTHHIST0001", 7)
    assert f[0] == 1 and f[1:3] == b"\x01\x00" and f[3] == K3_CONST_AT_3
    assert len(f) == 6 + token_width(7)
