"""Evidence-conformance pins for the Wi-Fi channel: read the javap dumps.

Like tests/test_r7_transport_scope.py and test_r7_s12_common_settings.py, these
read build/evidence/javap/ so that a future edit of the emulator cannot quietly
change a wire constant. They skip when the evidence tree is absent.

Two phone-side stacks exist (see emulator/plaudsim/wifi.py); both are pinned:
  * sdk.ble.wifi.WifiMessageProcessor$*  (build/evidence/javap/sdk/ble/wifi/)
  * com.plaud.sdk.proto k/m subclasses    (build/evidence/javap/ALL.txt blocks)
"""

from __future__ import annotations

import bisect
import re
import sys
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "emulator"))

from plaudsim import profile as prof  # noqa: E402
from plaudsim import wifi  # noqa: E402

EVIDENCE = ROOT / "build/evidence/javap"
WIFI = EVIDENCE / "sdk/ble/wifi"
ALL = EVIDENCE / "ALL.txt"

pytestmark = pytest.mark.skipif(not ALL.is_file(), reason="no decompiled evidence; run ./scripts/build-evidence.sh")


def _read(name: str) -> str:
    path = WIFI / name
    if not path.is_file():
        pytest.skip(f"missing {path}")
    return path.read_text(errors="replace")


@lru_cache(maxsize=1)
def _all_lines() -> list[str]:
    return ALL.read_text(errors="replace").split("\n")


@lru_cache(maxsize=1)
def _class_starts() -> list[int]:
    return [i for i, l in enumerate(_all_lines()) if l.startswith("Compiled from")]


def all_class_block(class_name: str) -> str:
    """The javap block of one class in ALL.txt (from 'Compiled from' to the next)."""
    lines = _all_lines()
    header = f"class {class_name} "
    for i, l in enumerate(lines):
        if header in l and l.startswith("public"):
            starts = _class_starts()
            k = bisect.bisect_right(starts, i) - 1
            end = starts[k + 1] if k + 1 < len(starts) else len(lines)
            return "\n".join(lines[starts[k]:end])
    raise AssertionError(f"class {class_name} not in ALL.txt")


_CONST = re.compile(r"(?:bipush|sipush)\s+(\d+)|iconst_(\d)")


def literal_after(text: str, method_sig: str) -> int:
    """The integer constant pushed right after `method_sig` (e.g. 'public int c();')."""
    i = text.index(method_sig)
    m = _CONST.search(text, i, i + 200)
    assert m, f"no constant after {method_sig}"
    return int(m.group(1) or m.group(2))


# --- Kotlin stack: type numbers -------------------------------------------------------

KOTLIN_TYPES = {
    "WifiMessageProcessor$UniversalErrorResponse.txt": ("getResponseType", wifi.MSG_UNIVERSAL_ERR),
    "WifiMessageProcessor$HandshakeRequest.txt": ("getRequestType", wifi.MSG_HANDSHAKE),
    "WifiMessageProcessor$HandshakeResponse.txt": ("getResponseType", wifi.MSG_HANDSHAKE),
    "WifiMessageProcessor$HelloAckRequest.txt": ("getRequestType", wifi.MSG_SAY_HELLO),
    "WifiMessageProcessor$SayHelloResponse.txt": ("getResponseType", wifi.MSG_SAY_HELLO),
    "WifiMessageProcessor$HeartbeatPongRequest.txt": ("getRequestType", wifi.MSG_HEARTBEAT),
    "WifiMessageProcessor$HeartbeatPingResponse.txt": ("getResponseType", wifi.MSG_HEARTBEAT),
    "WifiMessageProcessor$WifiCloseResponse.txt": ("getResponseType", wifi.MSG_WIFI_CLOSE),
    "WifiMessageProcessor$GetFileListRequest.txt": ("getRequestType", wifi.MSG_GET_FILE_LIST),
    "WifiMessageProcessor$FileListResponse.txt": ("getResponseType", wifi.MSG_GET_FILE_LIST),
    "WifiMessageProcessor$FileSyncRequest.txt": ("getRequestType", wifi.MSG_FILE_SYNC),
    "WifiMessageProcessor$FileSyncStatusResponse.txt": ("getResponseType", wifi.MSG_FILE_SYNC),
    "WifiMessageProcessor$FileSyncContentResponse.txt": ("getResponseType", wifi.MSG_FILE_SYNC_CONTENT),
    "WifiMessageProcessor$FileDeleteRequest.txt": ("getRequestType", wifi.MSG_FILE_DELETE),
    "WifiMessageProcessor$FileDeleteResponse.txt": ("getResponseType", wifi.MSG_FILE_DELETE),
}


@pytest.mark.parametrize("name,expect", list(KOTLIN_TYPES.items()), ids=list(KOTLIN_TYPES))
def test_kotlin_message_type_literals(name, expect):
    method, value = expect
    assert literal_after(_read(name), f"public int {method}();") == value


KOTLIN_KEYS = {
    "WifiMessageProcessor$HandshakeRequest.txt": ["token", "stamp"],
    "WifiMessageProcessor$HandshakeResponse.txt": ["status", "session"],
    "WifiMessageProcessor$SayHelloResponse.txt": ["sn", "version", "token", "pVer"],
    "WifiMessageProcessor$HeartbeatPingResponse.txt": ["battery", "charging", "voltage"],
    "WifiMessageProcessor$HeartbeatPongRequest.txt": ["stamp"],
    "WifiMessageProcessor$WifiCloseResponse.txt": ["reason"],
    "WifiMessageProcessor$UniversalErrorResponse.txt": ["code", "msg"],
    "WifiMessageProcessor$GetFileListRequest.txt": ["uid", "start", "single"],
    "WifiMessageProcessor$FileListResponse.txt": ["status", "count", "uid", "offset"],
    "WifiMessageProcessor$FileSyncRequest.txt": ["session", "scene", "start", "end"],
    "WifiMessageProcessor$FileSyncStatusResponse.txt": ["status", "session", "total"],
    "WifiMessageProcessor$FileSyncContentResponse.txt": ["session", "offset", "length", "last"],
    "WifiMessageProcessor$FileDeleteRequest.txt": ["session", "scene"],
    "WifiMessageProcessor$FileDeleteResponse.txt": ["session", "status"],
}


@pytest.mark.parametrize("name,keys", list(KOTLIN_KEYS.items()), ids=list(KOTLIN_KEYS))
def test_kotlin_json_keys_are_string_literals_in_the_class(name, keys):
    text = _read(name)
    for key in keys:
        assert re.search(rf"// String {re.escape(key)}\n", text), f"{key} not in {name}"


def test_kotlin_message_classes_emit_every_key_the_phone_reads():
    """The device-side encoders must produce at least the keys each parser reads."""
    assert set(wifi.HandshakeResponse(0, "s").to_json()) >= {"status", "session"}
    assert set(wifi.SayHello().to_json()) >= {"sn", "version", "token", "pVer"}
    assert set(wifi.HeartbeatPing().to_json()) >= {"battery", "charging", "voltage", "volume"}
    assert set(wifi.WifiClose().to_json()) >= {"reason", "status"}
    assert set(wifi.UniversalError().to_json()) >= {"code", "msg", "cmd", "status"}
    assert set(wifi.FileListResponse().to_json()) == {"uid", "status", "count", "offset"}
    assert set(wifi.FileSyncStatus().to_json()) == {"status", "session", "total"}
    assert set(wifi.FileSyncContent().to_json()) == {"session", "offset", "length", "last"}
    assert set(wifi.FileDeleteResponse().to_json()) == {"session", "status"}


# --- Kotlin stack: envelope, guards, heuristic, timings --------------------------------


def test_request_envelope_is_u24_size_u8_version1_s16_type_s16_jsonsize():
    text = _read("WifiMessageProcessor$BaseWifiRequest.txt")
    # allocate(len+8) little-endian, three size bytes, then put((byte)1), putShort(type), putShort(len)
    assert "bipush        8" in text and "ByteOrder.LITTLE_ENDIAN" in text
    assert re.search(r"iconst_1\n\s+\d+: invokevirtual #\d+\s+// Method java/nio/ByteBuffer.put:\(B\)", text)
    assert text.count("ByteBuffer.putShort:(S)") == 2
    assert wifi.PDU_VERSION_HOST == 1 and wifi.PDU_HEADER_LEN == 8


def test_response_guards_are_the_sdk_strings():
    text = _read("WifiMessageProcessor$BaseWifiResponse.txt")
    for s in ("Data too short for header", "Type mismatch: expected", "Data too short for JSON content"):
        assert s in text
    assert re.search(r"bipush        8\n\s+\d+: if_icmplt", text)  # len < 8 guard


def test_ciphertext_heuristic_is_u16_at_4_greater_than_200():
    text = _read("WifiAgentImpl.txt")
    m = re.search(
        r"iconst_4\n\s+\d+: baload\n\s+\d+: sipush\s+255\n\s+\d+: iand\n\s+\d+: aload_1\n\s+\d+: iconst_5\n"
        r"\s+\d+: baload\n\s+\d+: sipush\s+255\n\s+\d+: iand\n\s+\d+: bipush\s+8\n\s+\d+: ishl\n\s+\d+: ior\n"
        r"\s+\d+: dup\n\s+\d+: istore\s+18\n\s+\d+: sipush\s+200\n\s+\d+: if_icmple",
        text,
    )
    assert m, "the >200 classifier"
    assert wifi.CIPHERTEXT_TYPE_THRESHOLD == 200
    assert "String Message too short:" in text


def test_server_port_and_socket_options():
    assert "sipush        8081" in _read("WebSocketOperation.txt")
    start = _read("WebSocketOperation$startServer$1.txt")
    assert re.search(r"iconst_0\n\s+\d+: invokevirtual #\d+\s+// Method org/java_websocket/server/WebSocketServer.setConnectionLostTimeout", start)
    assert "setReuseAddr" in start and "setTcpNoDelay" in start
    assert wifi.WS_PORT == 8081


def test_request_timeout_30s_and_handshake_wait_30s():
    assert "long 30000l" in _read("WifiRequestBean.txt")
    relay = EVIDENCE / "com/plaud/sdk/internalimpl/SagittariusProcessorRelay.txt"
    if relay.is_file():
        assert "long 30000l" in relay.read_text(errors="replace")
    assert "String Handshake timeout" in (EVIDENCE / "com/plaud/sdk/internalimpl/CapricornusStateRelay.txt").read_text(errors="replace")


def test_handshake_token_is_padded_to_32_with_zero():
    text = _read("WifiMessageProcessor$HandshakeRequest.txt")
    assert re.search(r"bipush        32\n\s+\d+: bipush        48\n\s+\d+: invokestatic\s+#\d+\s+// Method kotlin/text/StringsKt.padEnd", text)
    assert wifi.WIFI_TOKEN_WIDTH == 32 and wifi.WIFI_TOKEN_PAD == "0"


def test_file_list_records_are_10_bytes_and_named_from_the_session_timestamp():
    text = _read("WifiMessageProcessor$FileListResponse.txt")
    assert re.search(r"bipush        10\n\s+\d+: iadd", text)
    assert "String yyyy-MM-dd_HH-mm-ss" in text and "String .opus" in text and "long 100l" in text
    assert "String log" in text
    assert wifi.FILE_RECORD_LEN == 10


def test_phone_sends_handshake_only_from_the_say_hello_handler():
    """Both stacks construct their HandshakeRequest inside the SayHello branch."""
    kotlin = _read("WifiAgentImpl.txt")
    hello_case = kotlin.index("SayHelloResponse.\"<init>\"")
    eta = kotlin.index("EtaConfiguratorNode.\"<init>\"")
    assert hello_case < eta < hello_case + 12000
    assert "HandshakeRequest.\"<init>\"" in _read("EtaConfiguratorNode.txt")
    x7 = all_class_block("com.plaud.sdk.proto.x7")
    m3_ctor = 'Method com/plaud/sdk/proto/m3."<init>"'
    assert x7.count(m3_ctor) == 1
    say_hello_handler = x7.index("public void a(com.plaud.sdk.proto.p5);")
    assert say_hello_handler < x7.index(m3_ctor)


def test_close_teardown_and_error_code():
    text = _read("WifiAgentImpl.txt")
    assert "sipush        1006" in text and "String Handshake failed with status:" in text
    assert "String Device closed WiFi" in text
    # WifiClose handler calls stopServer and reports DISCONNECTED
    i = text.index("WifiCloseResponse.\"<init>\"")
    window = text[i:i + 9000]
    assert "WebSocketOperation.stopServer" in window and "WifiConnectionState.DISCONNECTED" in window


def test_kotlin_receive_stores_sequence_without_replay_check():
    """`WiFi received seq=..., expected > N` is logged, then recvSeq is simply set."""
    text = _read("WifiAgentImpl.txt")
    i = text.index("String WiFi received seq=")
    window = text[i:i + 6000]
    seq_log = window.index("String , expected >")
    store = window.index("putfield      #279", seq_log)
    between = window[seq_log:store]
    assert "if_icmp" not in between, "a comparison would mean a replay check exists"


# --- proto stack: the full type/key table -----------------------------------------

PROTO_TABLE = [
    # class, base, type, keys
    ("com.plaud.sdk.proto.m3", "k", 1, ["token", "stamp"]),
    ("com.plaud.sdk.proto.n3", "m", 1, ["status"]),
    ("com.plaud.sdk.proto.p5", "m", 2, ["token", "pVer"]),
    ("com.plaud.sdk.proto.p3", "k", 3, []),
    ("com.plaud.sdk.proto.o3", "m", 3, ["volume", "voltage", "charging"]),
    ("com.plaud.sdk.proto.z7", "m", 4, ["status"]),
    ("com.plaud.sdk.proto.o7", "m", 5, ["tips"]),
    ("com.plaud.sdk.proto.r7", "m", 0, ["cmd", "status"]),
    ("com.plaud.sdk.proto.h2", "k", 11, ["uid", "start", "single"]),
    ("com.plaud.sdk.proto.i2", "m", 11, ["uid", "status", "count", "offset"]),
    ("com.plaud.sdk.proto.v1", "k", 12, ["session", "scene", "start", "end"]),
    ("com.plaud.sdk.proto.w1", "m", 12, ["session", "status"]),
    ("com.plaud.sdk.proto.u1", "m", 13, ["session", "offset", "length", "last"]),
    ("com.plaud.sdk.proto.q1", "k", 14, ["session", "scene"]),
    ("com.plaud.sdk.proto.r1", "m", 14, ["session", "status"]),
    ("com.plaud.sdk.proto.x1", "k", 15, ["session", "scene"]),
    ("com.plaud.sdk.proto.y1", "m", 15, ["status"]),
    ("com.plaud.sdk.proto.h1", "k", 16, []),
    ("com.plaud.sdk.proto.i1", "m", 16, ["status"]),
    ("com.plaud.sdk.proto.i6", "k", 20, ["filesize", "ver", "crc", "uid"]),
    ("com.plaud.sdk.proto.r5", "k", 21, ["start", "len", "last", "uid"]),
    ("com.plaud.sdk.proto.a8", "m", 21, ["start", "end", "payload_size", "uid", "send_rate_pps"]),
    ("com.plaud.sdk.proto.b8", "m", 22, ["status", "uid"]),
    ("com.plaud.sdk.proto.e6", "k", 100, ["operate", "pack_size"]),
    ("com.plaud.sdk.proto.f6", "m", 100, ["index"]),
    ("com.plaud.sdk.proto.d2", "k", 101, ["operate"]),
    ("com.plaud.sdk.proto.e2", "m", 101, ["offset", "length"]),
]


@pytest.mark.parametrize("cls,base,typ,keys", PROTO_TABLE, ids=[r[0].split(".")[-1] for r in PROTO_TABLE])
def test_proto_class_type_and_keys(cls, base, typ, keys):
    block = all_class_block(cls)
    assert f"extends com.plaud.sdk.proto.{base} " in block.split("\n")[1] + " "
    sig = "public int c();" if base == "k" else "public int a();"
    assert literal_after(block, sig) == typ
    for key in keys:
        assert re.search(rf"// String {re.escape(key)}\n", block), f"{key} not in {cls}"


def test_proto_envelope_and_tail_sizing():
    k = all_class_block("com.plaud.sdk.proto.k")
    # pack u24 size at 0, u8 version (x7.t) at 3, u16 type at 4, u16 jsonLen at 6
    assert "TntBleCommUtils.c:([BIJ)I" in k and "Field com/plaud/sdk/proto/x7.t:I" in k
    x7 = all_class_block("com.plaud.sdk.proto.x7")
    assert "public static int t = 1;" in x7
    m = all_class_block("com.plaud.sdk.proto.m")
    assert "TntBleCommUtils.c:([BI)J" in m  # u24 at 0
    assert "String  Mismatch data length" in m
    i2 = all_class_block("com.plaud.sdk.proto.i2")
    assert "long 8l" in i2 and "TntBleCommUtils.b:([BI)I" in i2   # tail = size - jsonLen - 8; u16 scene


def test_proto_w7_send_and_receive_semantics():
    w7 = (EVIDENCE / "com/plaud/sdk/proto/w7.txt").read_text(errors="replace")
    assert "sipush        8081" in w7
    # seq = z.p()+1 then z.e(seq); AES via q5.e when z.w() else ChaCha via q5.d
    assert re.search(r"z\.p:\(\)I\n\s+\d+: iconst_1\n\s+\d+: iadd", w7)
    assert "q5.e:([B[B[B[B)[B" in w7 and "q5.d:([B[B[B[B)[B" in w7
    assert "String AES-GCM" in w7 and "String ChaCha20-Poly1305" in w7
    # receive: q5.a(..., boolean) then u32 seq compared with the rx counter
    assert "q5.a:([B[B[B[BZ)[B" in w7
    assert "String 881" in w7
    # the proto server also disables the connection-lost timer
    assert re.search(r"iconst_0\n\s+\d+: invokevirtual #\d+\s+// Method org/java_websocket/server/WebSocketServer.setConnectionLostTimeout", w7)


def test_aes_gcm_parameters_in_q5():
    q5 = (EVIDENCE / "com/plaud/sdk/proto/q5.txt").read_text(errors="replace")
    assert 'String AES/GCM/NoPadding' in q5 and "sipush        128" in q5 and "GCMParameterSpec" in q5
    assert "Key must be 32 bytes" in q5 and "Nonce must be 12 bytes" in q5
    assert wifi.AEAD_TAG_LEN == 16


# --- BLE handoff opcodes 10 / 13 / 16 / 17 -------------------------------------------


def test_ble_open_wifi_request_and_response_layouts():
    i4 = all_class_block("com.plaud.sdk.proto.i4")
    assert literal_after(i4, "public int getBleRequestType();") == prof.OPCODE_OPEN_WIFI == 10
    assert "String OpenWiFiReq wifiPass must be 8 ASCII bytes, got" in i4
    assert "TntBleCommUtils.c:(I)[B" in i4  # c(mode) -> one byte (not on/off: R7-S14)
    j4 = all_class_block("com.plaud.sdk.proto.j4")
    assert literal_after(j4, "public int a();") == 10
    assert re.search(r"bipush        12\n\s+\d+: if_icmplt", j4)          # wifiPass iff len >= 12
    assert re.search(r"iconst_4\n\s+\d+: bipush        8\n", j4)         # String(buf, 4, 8, US_ASCII)
    assert "String OpenWiFiRsp{status=%d, wifiPass=%s}" in j4
    assert prof.WIFI_PASS_LEN == 8


def test_ble_close_wifi_layouts():
    k0 = all_class_block("com.plaud.sdk.proto.k0")
    assert literal_after(k0, "public int getBleRequestType();") == prof.OPCODE_CLOSE_WIFI == 13
    assert "enPkg" not in k0  # header only
    l0 = all_class_block("com.plaud.sdk.proto.l0")
    assert literal_after(l0, "public int a();") == 13
    assert "String CloseWiFiRsp{status=%d}" in l0
    assert re.search(r"iconst_3\n\s+\d+: invokevirtual #\d+\s+// Method com/tinnotech/penblesdk/utils/TntBleCommUtils\.a:\(\[BI\)I", l0)


def test_ble_websocket_profile_layouts_and_enum():
    enum = all_class_block("com.plaud.sdk.proto.d3$a")
    for ordinal, name in ((0, "unknown"), (1, "url"), (2, "serToken"), (3, "devToken")):
        assert re.search(rf"String {name}\n\s+\d+: iconst_{ordinal}\n", enum), name
    assert prof.WEBSOCKET_TYPE_NAMES == {1: "url", 2: "serToken", 3: "devToken"}
    d3 = all_class_block("com.plaud.sdk.proto.d3")
    assert literal_after(d3, "public int getBleRequestType();") == prof.OPCODE_WEBSOCKET_REQ == 16
    assert re.search(r"iconst_1\n\s+\d+: invokevirtual #\d+\s+// Method com/tinnotech/penblesdk/utils/TntBleCommUtils\.c:\(I\)\[B", d3)
    b6 = all_class_block("com.plaud.sdk.proto.b6")
    assert literal_after(b6, "public int getBleRequestType();") == 16
    assert re.search(r"iconst_2\n\s+\d+: invokevirtual #\d+\s+// Method com/tinnotech/penblesdk/utils/TntBleCommUtils\.c:\(I\)\[B", b6)
    assert "String token length must be <= 16" in b6 and "String url length must be < 64" in b6
    assert re.search(r"bipush        16\n\s+\d+: newarray       byte", b6)
    assert re.search(r"bipush        64\n\s+\d+: newarray       byte", b6)
    assert prof.WEBSOCKET_VALUE_WIDTH == {1: 64, 2: 16, 3: 16}
    v2 = all_class_block("com.plaud.sdk.proto.v2")
    assert literal_after(v2, "public int a();") == prof.OPCODE_WEBSOCKET_RSP == 17
    assert re.search(r"bipush        16\n\s+\d+: istore        4", v2)   # default width 16
    assert re.search(r"bipush        64\n\s+\d+: istore        4", v2)   # url width 64
    assert "String GetSetWebsocketRsp{type=%d, content=%s}" in v2


def test_wifi_name_and_password_derive_from_the_serial():
    text = _read("WifiAgentImpl.txt")
    i = text.index("access$calculateWifiName")
    assert "String Plaud" in text[i:i + 1500]
    assert re.search(r"iconst_4\n\s+\d+: invokestatic\s+#\d+\s+// Method kotlin/text/StringsKt.takeLast", text[i:i + 1500])
    j = text.index("access$calculateWifiPassword")
    assert re.search(r"bipush        8\n\s+\d+: invokestatic\s+#\d+\s+// Method kotlin/text/StringsKt.takeLast", text[j:j + 800])


# --- citations that point at line numbers must point at the bytecode (review T9) ------------

_EPSILON_CITE = re.compile(r"EpsilonDataStream\.txt:(\d+)-(\d+)")


def test_file_sync_start_end_citations_point_at_the_constructor_call():
    """`FileSyncRequest(session, scene, start=0, end=fileSize)` is built at
    EpsilonDataStream.txt file lines 130-143 (`iconst_0 ... getFileSize; l2i
    ... invokespecial FileSyncRequest.<init>(JIII)V`). An earlier citation
    quoted the bytecode OFFSETS 205-235 as if they were file lines."""
    sys.path.insert(0, str(ROOT / "tests"))
    from wifi_support import PhoneWifiServer

    text = _read("EpsilonDataStream.txt").split("\n")
    docs = {
        "wifi.FileSyncRequest": wifi.FileSyncRequest.__doc__ or "",
        "PhoneWifiServer.download": PhoneWifiServer.download.__doc__ or "",
    }
    for where, doc in docs.items():
        cites = _EPSILON_CITE.findall(doc)
        assert cites, f"{where} cites no EpsilonDataStream.txt line range"
        for lo, hi in cites:
            block = "\n".join(text[int(lo) - 1 : int(hi)])
            assert "iconst_0" in block and "getFileSize" in block and "l2i" in block, (where, lo, hi)
            assert 'FileSyncRequest."<init>":(JIII)V' in block, (where, lo, hi)
