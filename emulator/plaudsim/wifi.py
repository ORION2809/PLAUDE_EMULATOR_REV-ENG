"""Pure codecs for the Wi-Fi bulk-transfer channel (protocol ledger section 7).

Nothing here does I/O. `wifi_device.py` drives these codecs over a WebSocket;
`tests/wifi_support.py` is the phone-side double.

ROLES (SOURCE-DERIVED, ledger section 7): the PEN raises a SoftAP, the PHONE
runs a java-websocket SERVER on TCP 8081 (WebSocketOperation.txt:180-181,
proto/w7.txt:1859) and the PEN dials in as the WebSocket CLIENT. Every
application message is one BINARY WebSocket frame carrying one PDU.

PDU ENVELOPE (BYTECODE_PROVEN, both phone-side encoders/decoders agree):

    [0..2]   u24le totalSize      = 8 + jsonSize (+ tail on the pen side)
    [3]      u8    pduVersion     host stamps 1 (BaseWifiRequest.encode,
                                  WifiMessageProcessor$BaseWifiRequest.txt:73-75;
                                  x7.t = 1, ALL.txt:23380-ish "public static int t = 1")
    [4..5]   s16le messageType    (putShort; read back with getShort)
    [6..7]   s16le jsonSize
    [8..]    UTF-8 JSON, then an optional raw binary tail

  Encoder: WifiMessageProcessor$BaseWifiRequest.txt:33-85 and proto k.d()
  (ALL.txt:29670-29752). Decoder: WifiMessageProcessor$BaseWifiResponse.txt:
  77-233 ("Data too short for header" when len < 8, "Type mismatch" when the
  s16 at 4 differs from the expected type, "Data too short for JSON content"
  when len < 8 + jsonSize; a JSON parse failure is logged and leaves the
  object null, so every getter returns its default). proto m.a(byte[])
  (ALL.txt:35270-35394) reads the same fields (u24@0, u8@3, u16@4, u16@6).

TWO PHONE-SIDE IMPLEMENTATIONS. The shipped SDK carries two independent
phone-side stacks for this channel: `sdk.ble.wifi.WifiAgentImpl` (Kotlin, the
one `PlaudDeviceAgent.getWifiAgent()` hands to the template) and
`com.plaud.sdk.proto.x7`/`w7` (the older Java one). They agree on the
envelope and the type numbers but NOT on every JSON key the pen is expected
to send (see the per-message docstrings). Where they differ, the device
codecs here EMIT BOTH spellings (HARNESS_POLICY: superset), so either phone
decodes the frame; which spelling real firmware emits is UNKNOWN.

MESSAGE TYPES (BYTECODE_PROVEN): the per-class `getRequestType`/
`getResponseType` literals under build/evidence/javap/sdk/ble/wifi/ and the
`c()`/`a()` literals of the proto k/m subclasses (ALL.txt line ranges in
`MESSAGE_EVIDENCE`). Types 5, 15, 16, 20, 21, 22, 100 and 101 exist ONLY on
the proto path in this build (WebSocketOperation.sendMessage names 15 as
FILE_SYNC_STOP but the Kotlin stack has no request class for it).

CIPHERTEXT HEURISTIC (BYTECODE_PROVEN, WifiAgentImpl.txt:850-866): an inbound
frame is treated as sealed when `u16le at raw offset 4` exceeds 200 AND the
agent holds keys; otherwise it is parsed as plaintext. Every legitimate type
is <= 101. `looks_encrypted` reproduces the numeric test only; whether the
pen classifies inbound frames the same way is UNKNOWN.

SEALING (BYTECODE_PROVEN, ledger 4.3 + tests/test_r7_transport_scope.py):
on portVersion >= 20 the whole PDU is AEAD-sealed exactly like the BLE
channel -- `[u32le seq][PDU]` under (z.J, z.K, z.L) -- with AES-GCM iff
capability bit 3 (opcode 138) else ChaCha20-Poly1305 (proto/w7.txt:31-464, the send path `a(e8)`:
z.p()+1 at :151, then q5.e (AES) at :227 or q5.d (ChaCha) at :236; WifiAgentImpl
access$encryptWifiMessage, WifiAgentImpl.txt:505-698). `WifiSealer` wraps
`sealed.SealedSession` for both AEADs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from struct import pack, unpack_from
from typing import Any, ClassVar

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from plaudsim.sealed import SealedSession, open_raw, seal_raw

# --- transport constants -----------------------------------------------------

#: WebSocketOperation.startServer: `new InetSocketAddress(8081)`
#: (WebSocketOperation.txt:180-181); proto w7.c() likewise (w7.txt:1859).
WS_PORT = 8081

PDU_HEADER_LEN = 8
#: What both host encoders stamp into byte 3. The proto stack overwrites its
#: static `x7.t` with the pduVersion of the pen's SayHello (ALL.txt:25403-25405,
#: `m.b()` -> `putstatic x7.t`), so the pen's value is adopted, never checked.
PDU_VERSION_HOST = 1
MAX_TOTAL_SIZE = 0xFFFFFF
#: WifiAgentImpl.access$handleIncomingMessage: `sipush 200; if_icmple` on the
#: u16le at offset 4 (WifiAgentImpl.txt:850-866).
CIPHERTEXT_TYPE_THRESHOLD = 200

# Message types. Evidence per entry in MESSAGE_EVIDENCE below.
MSG_UNIVERSAL_ERR = 0
MSG_HANDSHAKE = 1
MSG_SAY_HELLO = 2
MSG_HEARTBEAT = 3
MSG_WIFI_CLOSE = 4
MSG_TIPS = 5
MSG_GET_FILE_LIST = 11
MSG_FILE_SYNC = 12
MSG_FILE_SYNC_CONTENT = 13
MSG_FILE_DELETE = 14
MSG_FILE_SYNC_STOP = 15
MSG_EXTEND_EXIT_TIME = 16
MSG_SEND_OTA_FILE_INFO = 20
MSG_REQUEST_OTA_PACKAGE = 21
MSG_OTA_STATUS_SYNC = 22
MSG_SPEED_TEST = 100
MSG_GET_DEVICE_LOG = 101

MESSAGE_NAMES: dict[int, str] = {
    MSG_UNIVERSAL_ERR: "UniversalErr",
    MSG_HANDSHAKE: "Handshake",
    MSG_SAY_HELLO: "SayHello",
    MSG_HEARTBEAT: "Heartbeat",
    MSG_WIFI_CLOSE: "WifiClose",
    MSG_TIPS: "Tips",
    MSG_GET_FILE_LIST: "GetFileList",
    MSG_FILE_SYNC: "FileSync",
    MSG_FILE_SYNC_CONTENT: "FileSyncContent",
    MSG_FILE_DELETE: "FileDelete",
    MSG_FILE_SYNC_STOP: "FileSyncStop",
    MSG_EXTEND_EXIT_TIME: "ExtendExitTime",
    MSG_SEND_OTA_FILE_INFO: "SendOTAFileInfo",
    MSG_REQUEST_OTA_PACKAGE: "RequestOTAPackage",
    MSG_OTA_STATUS_SYNC: "OTAStatusSync",
    MSG_SPEED_TEST: "SpeedTest",
    MSG_GET_DEVICE_LOG: "GetDeviceLog",
}

#: Where each type number is pinned. "kotlin" = build/evidence/javap/sdk/ble/
#: wifi/<class>.txt (the literal after getRequestType/getResponseType);
#: "proto" = build/evidence/javap/ALL.txt class block of the k (request) or m
#: (response) subclass whose c()/a() returns the number.
MESSAGE_EVIDENCE: dict[int, dict[str, str]] = {
    0: {"kotlin": "WifiMessageProcessor$UniversalErrorResponse.txt:15", "proto": "r7 ALL.txt:50094-50176"},
    1: {"kotlin": "WifiMessageProcessor$HandshakeRequest.txt:30 / $HandshakeResponse.txt:15",
        "proto": "m3 ALL.txt:67603-67705 / n3 ALL.txt:26332-26388"},
    2: {"kotlin": "WifiMessageProcessor$SayHelloResponse.txt:15 / $HelloAckRequest.txt:22",
        "proto": "p5 ALL.txt:56494-56583"},
    3: {"kotlin": "WifiMessageProcessor$HeartbeatPingResponse.txt:15 / $HeartbeatPongRequest.txt:19",
        "proto": "o3 ALL.txt:63481-63597 / p3 ALL.txt:50681"},
    4: {"kotlin": "WifiMessageProcessor$WifiCloseResponse.txt:15", "proto": "z7 ALL.txt:36998-37054"},
    5: {"proto": "o7 ALL.txt:68773-68829"},
    11: {"kotlin": "WifiMessageProcessor$GetFileListRequest.txt:46 / $FileListResponse.txt:20",
         "proto": "h2 ALL.txt:22288-22372 / i2 ALL.txt:64526-64813"},
    12: {"kotlin": "WifiMessageProcessor$FileSyncRequest.txt:30 / $FileSyncStatusResponse.txt:15",
         "proto": "v1 ALL.txt:29460 / w1 ALL.txt:59105"},
    13: {"kotlin": "WifiMessageProcessor$FileSyncContentResponse.txt:15", "proto": "u1 ALL.txt:69699-69862"},
    14: {"kotlin": "WifiMessageProcessor$FileDeleteRequest.txt:35 / $FileDeleteResponse.txt:15",
         "proto": "q1 ALL.txt:81456-81523 / r1 ALL.txt:55435-55517"},
    15: {"kotlin": "WebSocketOperation.txt:353-367 (name FILE_SYNC_STOP only)",
         "proto": "x1 ALL.txt:34415-34482 / y1 ALL.txt:70241-70297"},
    16: {"proto": "h1 ALL.txt:38632 / i1 ALL.txt:79531-79587"},
    20: {"proto": "i6 ALL.txt:65413-65509"},
    21: {"proto": "r5 ALL.txt:38480 / a8 ALL.txt:32501"},
    22: {"proto": "b8 ALL.txt:59312"},
    100: {"proto": "e6 ALL.txt:67710-67775 / f6 ALL.txt:23276-23375"},
    101: {"proto": "d2 ALL.txt:22798-22847 / e2 ALL.txt:64992-65103"},
}


# --- envelope ------------------------------------------------------------------


@dataclass(frozen=True)
class Pdu:
    """One parsed PDU. `tail` is everything after the JSON."""

    total_size: int
    pdu_version: int
    msg_type: int
    json_size: int
    json_bytes: bytes
    tail: bytes

    @property
    def json(self) -> dict[str, Any] | None:
        """The JSON object, or None when it does not parse.

        BaseWifiResponse catches JSONException and leaves `jsonObject` null
        (WifiMessageProcessor$BaseWifiResponse.txt:197-206), after which every
        getter returns its default; mirroring that here keeps the defaults in
        the message classes honest.
        """
        try:
            obj = json.loads(self.json_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    @property
    def name(self) -> str:
        return MESSAGE_NAMES.get(self.msg_type, f"type{self.msg_type}")


def encode_json(obj: dict[str, Any]) -> bytes:
    """Compact UTF-8 JSON in insertion order.

    org.json.JSONObject.toString() emits `{"k":v,"k2":v2}` with no whitespace
    and in insertion order, which is what the host puts on the wire; this is
    byte-identical for the value types the SDK uses (int/long/String).
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def pack_pdu(
    msg_type: int,
    payload: dict[str, Any] | bytes,
    tail: bytes = b"",
    pdu_version: int = PDU_VERSION_HOST,
) -> bytes:
    """Build one PDU: header + JSON + optional binary tail.

    totalSize counts the tail (the proto parsers size the tail as
    `pduSize - jsonSize - 8`: i2.b() ALL.txt:64669-64681, f6 ALL.txt:23303-23314),
    so it must be the full frame length.
    """
    if not -0x8000 <= msg_type <= 0x7FFF:
        raise ValueError(f"messageType out of s16 range: {msg_type}")
    if not 0 <= pdu_version <= 0xFF:
        raise ValueError(f"pduVersion out of u8 range: {pdu_version}")
    body = encode_json(payload) if isinstance(payload, dict) else bytes(payload)
    if len(body) > 0x7FFF:
        raise ValueError(f"jsonSize out of s16 range: {len(body)}")
    total = PDU_HEADER_LEN + len(body) + len(tail)
    if total > MAX_TOTAL_SIZE:
        raise ValueError(f"totalSize out of u24 range: {total}")
    head = (
        bytes([total & 0xFF, (total >> 8) & 0xFF, (total >> 16) & 0xFF, pdu_version])
        + pack("<h", msg_type)
        + pack("<h", len(body))
    )
    return head + body + bytes(tail)


def parse_pdu(raw: bytes, strict_total: bool = False) -> Pdu:
    """Split a PDU the way BaseWifiResponse.process_item_data does.

    Raises ValueError with the SDK's own messages on the two length guards.
    `strict_total=True` additionally requires totalSize == len(raw); the
    Kotlin parser never checks it, the proto tail-carrying parsers implicitly
    do (a mismatch throws inside their arraycopy).
    """
    data = bytes(raw)
    if len(data) < PDU_HEADER_LEN:
        raise ValueError("Data too short for header")
    total = data[0] | (data[1] << 8) | (data[2] << 16)
    version = data[3]
    msg_type = unpack_from("<h", data, 4)[0]
    json_size = unpack_from("<h", data, 6)[0]
    if json_size < 0 or len(data) < PDU_HEADER_LEN + json_size:
        raise ValueError("Data too short for JSON content")
    if strict_total and total != len(data):
        raise ValueError(f"totalSize {total} != frame length {len(data)}")
    return Pdu(
        total_size=total,
        pdu_version=version,
        msg_type=msg_type,
        json_size=json_size,
        json_bytes=data[PDU_HEADER_LEN : PDU_HEADER_LEN + json_size],
        tail=data[PDU_HEADER_LEN + json_size :],
    )


def looks_encrypted(raw: bytes) -> bool:
    """The WifiAgentImpl inbound classifier, numeric part only.

    `handleIncomingMessage` returns early on frames shorter than 8 bytes
    ("Message too short"), then reads `(b[4]&255) | (b[5]&255)<<8` and takes
    the decrypt branch iff that value > 200 (WifiAgentImpl.txt:830-866). The
    SDK additionally requires its three key arrays to be non-null; that is
    the caller's business.
    """
    data = bytes(raw)
    if len(data) < PDU_HEADER_LEN:
        return False
    return unpack_from("<H", data, 4)[0] > CIPHERTEXT_TYPE_THRESHOLD


# --- handshake token ---------------------------------------------------------

WIFI_TOKEN_WIDTH = 32
WIFI_TOKEN_PAD = "0"


def pad_wifi_token(token: str) -> str:
    """`StringsKt.padEnd(token, 32, '0')` (HandshakeRequest.txt:17-19, HelloAckRequest.txt:14-16).

    Same pad character and width as the BLE k3 token (handshake.build_k3,
    token_width(pv>=9) == 32), but UNLIKE k3 nothing truncates: padEnd leaves
    a longer string alone, and the proto m3 encoder (ALL.txt:67613-67645) only
    appends zeros when len < 32. So the two rules are identical for tokens of
    32 characters or fewer and diverge above that.
    """
    return token.ljust(WIFI_TOKEN_WIDTH, WIFI_TOKEN_PAD)


# --- message classes -------------------------------------------------------------


def _opt_int(obj: dict[str, Any] | None, key: str, default: int) -> int:
    """org.json optInt: Number/numeric-String coerce, anything else -> default."""
    if obj is None or key not in obj:
        return default
    value = obj[key]
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _opt_str(obj: dict[str, Any] | None, key: str, default: str = "") -> str:
    """org.json optString: any value is stringified, absent -> default."""
    if obj is None or key not in obj or obj[key] is None:
        return default
    value = obj[key]
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class WifiMessage:
    """Base for one message in one direction. Subclasses set TYPE/HAS_TAIL."""

    TYPE: ClassVar[int] = -1
    HAS_TAIL: ClassVar[bool] = False

    def to_json(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def tail(self) -> bytes:
        return b""

    def encode(self, pdu_version: int = PDU_VERSION_HOST) -> bytes:
        return pack_pdu(self.TYPE, self.to_json(), self.tail(), pdu_version)

    @classmethod
    def check_type(cls, pdu: Pdu) -> None:
        if pdu.msg_type != cls.TYPE:
            raise ValueError(f"Type mismatch: expected {cls.TYPE}, got {pdu.msg_type}")


# 0 UniversalErr (device -> phone)


@dataclass
class UniversalError(WifiMessage):
    """Type 0. Kotlin reads {"code": optInt -1, "msg": optString ""}
    (UniversalErrorResponse.txt:26-53); proto r7 reads {"cmd", "status"}
    (ALL.txt:50110-50133). HARNESS_POLICY: the device emits all four keys.
    On receipt WifiAgentImpl calls onError(code, msg) (WifiAgentImpl.txt:1517-1553)."""

    TYPE: ClassVar[int] = MSG_UNIVERSAL_ERR
    code: int = -1
    msg: str = ""
    cmd: int = 0
    status: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "msg": self.msg, "cmd": self.cmd, "status": self.status}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "UniversalError":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "code", -1), _opt_str(j, "msg"), _opt_int(j, "cmd", 0), _opt_int(j, "status", 0))


# 1 Handshake


@dataclass
class HandshakeRequest(WifiMessage):
    """Type 1, phone -> pen: {"token": padEnd(token,32,'0'), "stamp": unix seconds}
    (HandshakeRequest.txt:13-25 and :44-54; proto m3 ALL.txt:67662-67687 puts
    stamp as an int). The token is the BLE handshake token
    (NiceBuildSdk.resolveHandshakeToken, WifiAgentImpl.txt:2278-2307)."""

    TYPE: ClassVar[int] = MSG_HANDSHAKE
    token: str = ""
    stamp: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"token": pad_wifi_token(self.token), "stamp": int(self.stamp)}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "HandshakeRequest":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_str(j, "token"), _opt_int(j, "stamp", 0))


@dataclass
class HandshakeResponse(WifiMessage):
    """Type 1, pen -> phone: {"status": optInt -1, "session": optString ""}
    (HandshakeResponse.txt:26-53). status 0 -> READY, sessionId = session or
    (when empty) the phone's own token (WifiAgentImpl.txt:1429-1441); any
    other status -> onError(1006, "Handshake failed with status: N")
    (WifiAgentImpl.txt:1487-1516). proto n3 reads only "status"."""

    TYPE: ClassVar[int] = MSG_HANDSHAKE
    status: int = -1
    session: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "session": self.session}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "HandshakeResponse":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "status", -1), _opt_str(j, "session"))


# 2 SayHello (device -> phone) / HelloAck (phone -> device, dead code)


@dataclass
class SayHello(WifiMessage):
    """Type 2, pen -> phone: {"sn","version","token" (optString ""), "pVer" (optInt -1)}
    (SayHelloResponse.txt:26-97). proto p5 reads "token" and "pVer" only
    (ALL.txt:56508-56540).

    What the phone does with it (BYTECODE_PROVEN): this is the message that
    TRIGGERS the phone's HandshakeRequest -- WifiAgentImpl case 2 launches
    EtaConfiguratorNode when currentSessionId is empty (WifiAgentImpl.txt:
    1405-1422) and proto x7.a(p5) builds m3 (ALL.txt:25365-25436). Nothing
    else sends a Handshake, so the pen must speak first. The Kotlin path
    compares "token" with its own handshake token and only LOGS a mismatch
    ("握手会被设备拒绝" = "the device will refuse the handshake", CLAIM about
    the pen); the proto path compares upper(md5hex(x7.l)) with "token" and
    proceeds only on a match ("SN 验证" = SN verification, ALL.txt:25409-25440,
    s7.c -> md5 at proto/s7.txt:346-374). Which value real firmware sends is
    UNKNOWN; HARNESS_POLICY: `token` echoes the configured handshake token."""

    TYPE: ClassVar[int] = MSG_SAY_HELLO
    sn: str = ""
    version: str = ""
    token: str = ""
    p_ver: int = -1

    def to_json(self) -> dict[str, Any]:
        return {"sn": self.sn, "version": self.version, "token": self.token, "pVer": self.p_ver}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "SayHello":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_str(j, "sn"), _opt_str(j, "version"), _opt_str(j, "token"), _opt_int(j, "pVer", -1))


@dataclass
class HelloAck(WifiMessage):
    """Type 2, phone -> pen: {"token": padEnd(token,32,'0')} (HelloAckRequest.txt).
    No caller constructs HelloAckRequest in this build (grep of ALL.txt for
    `HelloAckRequest."<init>"` is empty), so a pen must NOT wait for it."""

    TYPE: ClassVar[int] = MSG_SAY_HELLO
    token: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"token": pad_wifi_token(self.token)}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "HelloAck":
        cls.check_type(pdu)
        return cls(_opt_str(pdu.json, "token"))


# 3 Heartbeat


@dataclass
class HeartbeatPing(WifiMessage):
    """Type 3, pen -> phone. Kotlin reads {"battery": optInt -1, "charging": optInt==1,
    "voltage": optInt -1} (HeartbeatPingResponse.txt:26-64) and answers with a
    HeartbeatPong via ThetaEventRouter (WifiAgentImpl.txt:1334-1347); proto o3
    reads {"volume", "voltage", "charging"} (ALL.txt:63494-63544). HARNESS_POLICY:
    the pen emits `battery` AND `volume` with the same value. `charging` must be
    the INT 1/0 -- org.json optInt does not coerce a JSON boolean."""

    TYPE: ClassVar[int] = MSG_HEARTBEAT
    battery: int = -1
    charging: bool = False
    voltage: int = -1

    def to_json(self) -> dict[str, Any]:
        return {
            "battery": self.battery,
            "volume": self.battery,
            "charging": 1 if self.charging else 0,
            "voltage": self.voltage,
        }

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "HeartbeatPing":
        cls.check_type(pdu)
        j = pdu.json
        battery = _opt_int(j, "battery", _opt_int(j, "volume", -1))
        return cls(battery, _opt_int(j, "charging", 0) == 1, _opt_int(j, "voltage", -1))


@dataclass
class HeartbeatPong(WifiMessage):
    """Type 3, phone -> pen: {"stamp": unix seconds} (HeartbeatPongRequest.txt:10-14,
    :30-35); proto p3 sends `{}` (ALL.txt:50681, no keys). A pen must accept both."""

    TYPE: ClassVar[int] = MSG_HEARTBEAT
    stamp: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {} if self.stamp is None else {"stamp": int(self.stamp)}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "HeartbeatPong":
        cls.check_type(pdu)
        j = pdu.json
        return cls(None if j is None or "stamp" not in j else _opt_int(j, "stamp", 0))


# 4 WifiClose (device -> phone)


@dataclass
class WifiClose(WifiMessage):
    """Type 4, pen -> phone. Kotlin reads {"reason": optString ""} (WifiCloseResponse.txt:
    18-38) and then tears everything down: cancels transfers, stopServer(),
    clears keys, DISCONNECTED, onWifiTransferStopped (WifiAgentImpl.txt:
    1187-1292). proto z7 reads {"status"} (ALL.txt:37007-37023). HARNESS_POLICY:
    emit both."""

    TYPE: ClassVar[int] = MSG_WIFI_CLOSE
    reason: str = ""
    status: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"reason": self.reason, "status": self.status}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "WifiClose":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_str(j, "reason"), _opt_int(j, "status", 0))


# 5 Tips (device -> phone, proto path only)


@dataclass
class Tips(WifiMessage):
    """Type 5, pen -> phone: {"tips": int} (proto o7 ALL.txt:68782-68798); the
    iOS delegate exposes it as wifiTips(_ tips: Int). Meaning of the values:
    UNKNOWN. The Kotlin stack has no handler (type 5 falls into "Unknown
    message type")."""

    TYPE: ClassVar[int] = MSG_TIPS
    tips: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"tips": self.tips}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "Tips":
        cls.check_type(pdu)
        return cls(_opt_int(pdu.json, "tips", 0))


# 11 GetFileList


@dataclass
class GetFileListRequest(WifiMessage):
    """Type 11, phone -> pen: {"uid": long, "start": long, "single": int 0/1}
    (GetFileListRequest.txt:49-79 -- note `single` is put with put(String,int),
    so it is an INT; proto h2 identical, ALL.txt:22314-22350). Every caller in
    this build sends (0, 0, false) (ZetaAsyncManager.txt:44-51,
    GammaServiceUnit.txt:127-134)."""

    TYPE: ClassVar[int] = MSG_GET_FILE_LIST
    uid: int = 0
    start: int = 0
    single: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"uid": self.uid, "start": self.start, "single": 1 if self.single else 0}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "GetFileListRequest":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "uid", 0), _opt_int(j, "start", 0), _opt_int(j, "single", 0) != 0)


FILE_RECORD_LEN = 10


@dataclass(frozen=True)
class FileRecord:
    """One 10-byte file-list record: [u32le sessionId][u32le fileSize][u16le scene]
    (FileListResponse.getSessions, FileListResponse.txt:117-218; proto i2.b(),
    ALL.txt:64697-64734). Distinct from the BLE 10-byte record, whose trailing
    field is [u8 scene][u8 attribute] (ledger 5.6)."""

    session_id: int
    file_size: int
    scene: int = 1

    def pack(self) -> bytes:
        return pack("<IIH", self.session_id, self.file_size, self.scene)


def pack_file_records(records: list[FileRecord]) -> bytes:
    return b"".join(r.pack() for r in records)


def parse_file_records(tail: bytes) -> list[FileRecord]:
    """FileListResponse.getSessions loop: `while (i + 10 <= len)` -- a trailing
    partial record is silently ignored (FileListResponse.txt:117-123). (The
    proto i2.b() loop is `i < len` and would throw on a partial record.)"""
    data = bytes(tail)
    out: list[FileRecord] = []
    i = 0
    while i + FILE_RECORD_LEN <= len(data):
        sid, size, scene = unpack_from("<IIH", data, i)
        out.append(FileRecord(sid, size, scene))
        i += FILE_RECORD_LEN
    return out


def file_name_for_session(session_id: int, tz: tzinfo = timezone.utc) -> str:
    """The name WifiAgentImpl derives for a listed session
    (FileListResponse.txt:219-256): "log<id>" when sessionId < 100, else
    `SimpleDateFormat("yyyy-MM-dd_HH-mm-ss").format(sessionId * 1000)` + ".opus"
    in the phone's default zone -- i.e. the session id IS a unix timestamp."""
    if session_id < 100:
        return f"log{session_id}"
    return datetime.fromtimestamp(session_id, tz).strftime("%Y-%m-%d_%H-%M-%S") + ".opus"


@dataclass
class FileListResponse(WifiMessage):
    """Type 11, pen -> phone: {"uid": optLong 0, "status": optInt -1, "count": optInt 0,
    "offset": optInt 0} + a tail of 10-byte records (FileListResponse.txt).

    Consumption differs between the two phone stacks and that constrains the
    pen: WifiAgentImpl resumes its continuation on the FIRST type-11 frame
    (fileListContinuation is nulled at WifiAgentImpl.txt:1173-1186), so a
    paged list would lose every page after the first; proto i2.b() appends a
    frame's records only when `offset == records accumulated so far`
    (ALL.txt:64653-64666). A single frame with offset 0 satisfies both, which
    is what the emulator sends (HARNESS_POLICY). status != 0 -> empty list
    (WifiAgentImpl.txt:1164-1171)."""

    TYPE: ClassVar[int] = MSG_GET_FILE_LIST
    HAS_TAIL: ClassVar[bool] = True
    uid: int = 0
    status: int = -1
    count: int = 0
    offset: int = 0
    records: list[FileRecord] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"uid": self.uid, "status": self.status, "count": self.count, "offset": self.offset}

    def tail(self) -> bytes:
        return pack_file_records(self.records)

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "FileListResponse":
        cls.check_type(pdu)
        j = pdu.json
        return cls(
            _opt_int(j, "uid", 0),
            _opt_int(j, "status", -1),
            _opt_int(j, "count", 0),
            _opt_int(j, "offset", 0),
            parse_file_records(pdu.tail),
        )


# 12 FileSync


@dataclass
class FileSyncRequest(WifiMessage):
    """Type 12, phone -> pen: {"session": long, "scene": int, "start": int, "end": int}
    (FileSyncRequest.txt:33-69; proto v1 same keys). WifiAgentImpl sends
    start=0, end=fileSize (EpsilonDataStream.txt:205-235: `iconst_0` then
    `getFileSize l2i`); the iOS `appSyncFile(_:_:_:_:)` defaults end to 0, whose
    meaning is UNKNOWN (HARNESS_POLICY in the device: end <= 0 means EOF)."""

    TYPE: ClassVar[int] = MSG_FILE_SYNC
    session: int = 0
    scene: int = 1
    start: int = 0
    end: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"session": self.session, "scene": self.scene, "start": self.start, "end": self.end}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "FileSyncRequest":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "session", 0), _opt_int(j, "scene", 1), _opt_int(j, "start", 0), _opt_int(j, "end", 0))


@dataclass
class FileSyncStatus(WifiMessage):
    """Type 12, pen -> phone: {"status": optInt -1, "session": optLong 0, "total": optLong 0}
    (FileSyncStatusResponse.txt). status != 0 fails the transfer with
    "File sync failed: status=N"; status 0 with total > 0 sets the transfer's
    expected size (WifiAgentImpl.txt:1048-1143). proto w1 reads session/status."""

    TYPE: ClassVar[int] = MSG_FILE_SYNC
    status: int = -1
    session: int = 0
    total: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "session": self.session, "total": self.total}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "FileSyncStatus":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "status", -1), _opt_int(j, "session", 0), _opt_int(j, "total", 0))


# 13 FileSyncContent (device -> phone)


@dataclass
class FileSyncContent(WifiMessage):
    """Type 13, pen -> phone: {"session": optLong 0, "offset": optLong 0, "length": optInt 0,
    "last": optInt 0 (==1 means finished)} + `length` raw bytes at 8+jsonSize
    (FileSyncContentResponse.txt; proto u1 ALL.txt:69716-69792).

    Phone-side facts a pen must respect: getFileData returns the tail ONLY when
    its length equals "length" and is > 0, else an empty array
    (FileSyncContentResponse.txt:81-115); the bytes are APPENDED to the output
    stream in arrival order -- "offset" is parsed but never used for placement
    (WifiAgentImpl.process_item_data(FileSyncContentResponse, byte[]),
    WifiAgentImpl.txt:1777-2080) -- so delivery must be in order; an empty
    frame with last=1 is accepted as the terminator (ibid. :1811-1815 jumps
    straight to the isFinished check)."""

    TYPE: ClassVar[int] = MSG_FILE_SYNC_CONTENT
    HAS_TAIL: ClassVar[bool] = True
    session: int = 0
    offset: int = 0
    data: bytes = b""
    last: bool = False

    @property
    def length(self) -> int:
        return len(self.data)

    def to_json(self) -> dict[str, Any]:
        return {"session": self.session, "offset": self.offset, "length": self.length, "last": 1 if self.last else 0}

    def tail(self) -> bytes:
        return bytes(self.data)

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "FileSyncContent":
        cls.check_type(pdu)
        j = pdu.json
        declared = _opt_int(j, "length", 0)
        # getFileData: tail accepted iff len(tail) > 0 and len(tail) == length
        data = pdu.tail if (len(pdu.tail) > 0 and len(pdu.tail) == declared) else b""
        return cls(_opt_int(j, "session", 0), _opt_int(j, "offset", 0), data, _opt_int(j, "last", 0) == 1)


# 14 FileDelete


@dataclass
class FileDeleteRequest(WifiMessage):
    """Type 14, phone -> pen: {"session": long, "scene": int (default 1)}
    (FileDeleteRequest.txt:19-31, :38-60; proto q1 same). Sent one per session
    by deleteFiles with a 30 s withTimeout (BetaComponentHandler.txt:102-162)."""

    TYPE: ClassVar[int] = MSG_FILE_DELETE
    session: int = 0
    scene: int = 1

    def to_json(self) -> dict[str, Any]:
        return {"session": self.session, "scene": self.scene}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "FileDeleteRequest":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "session", 0), _opt_int(j, "scene", 1))


@dataclass
class FileDeleteResponse(WifiMessage):
    """Type 14, pen -> phone: {"session": optLong 0, "status": optInt -1}
    (FileDeleteResponse.txt:18-47). status 0 = deleted, anything else counts as
    a failure in the batch summary (BetaComponentHandler.txt:170-204)."""

    TYPE: ClassVar[int] = MSG_FILE_DELETE
    session: int = 0
    status: int = -1

    def to_json(self) -> dict[str, Any]:
        return {"session": self.session, "status": self.status}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "FileDeleteResponse":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "session", 0), _opt_int(j, "status", -1))


# 15 FileSyncStop (proto path only)


@dataclass
class FileSyncStopRequest(WifiMessage):
    """Type 15, phone -> pen: {"session": long, "scene": int} (proto x1 ALL.txt:
    34437-34463; iOS appStopSyncFile(sessionId, scene)). The Kotlin stack only
    names the type (WebSocketOperation.txt:353-367, "FILE_SYNC_STOP")."""

    TYPE: ClassVar[int] = MSG_FILE_SYNC_STOP
    session: int = 0
    scene: int = 1

    def to_json(self) -> dict[str, Any]:
        return {"session": self.session, "scene": self.scene}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "FileSyncStopRequest":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "session", 0), _opt_int(j, "scene", 1))


@dataclass
class FileSyncStopResponse(WifiMessage):
    """Type 15, pen -> phone: {"status": int} (proto y1 ALL.txt:70250-70266)."""

    TYPE: ClassVar[int] = MSG_FILE_SYNC_STOP
    status: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "FileSyncStopResponse":
        cls.check_type(pdu)
        return cls(_opt_int(pdu.json, "status", -1))


# 16 ExtendExitTime (proto path only)


@dataclass
class ExtendExitTimeRequest(WifiMessage):
    """Type 16, phone -> pen: `{}` (proto h1 ALL.txt:38632 puts no keys; iOS
    appExtendWifiExitTime())."""

    TYPE: ClassVar[int] = MSG_EXTEND_EXIT_TIME

    def to_json(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "ExtendExitTimeRequest":
        cls.check_type(pdu)
        return cls()


@dataclass
class ExtendExitTimeResponse(WifiMessage):
    """Type 16, pen -> phone: {"status": int} (proto i1 ALL.txt:79540-79556)."""

    TYPE: ClassVar[int] = MSG_EXTEND_EXIT_TIME
    status: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "ExtendExitTimeResponse":
        cls.check_type(pdu)
        return cls(_opt_int(pdu.json, "status", -1))


# 20/21/22 OTA (proto path only) -- codecs only, no device behaviour.


@dataclass
class SendOtaFileInfoRequest(WifiMessage):
    """Type 20, phone -> pen: {"filesize","ver","crc","uid"} (proto i6 ALL.txt:
    65444-65487; the class logs itself as "SpeedTestReq.getPkgJson", a copy-paste
    artefact in the SDK). iOS startPushOTA(uid, fileSize, crc:, toVersion)."""

    TYPE: ClassVar[int] = MSG_SEND_OTA_FILE_INFO
    filesize: int = 0
    ver: int = 0
    crc: int = 0
    uid: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"filesize": self.filesize, "ver": self.ver, "crc": self.crc, "uid": self.uid}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "SendOtaFileInfoRequest":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "filesize", 0), _opt_int(j, "ver", 0), _opt_int(j, "crc", 0), _opt_int(j, "uid", 0))


@dataclass
class RequestOtaPackage(WifiMessage):
    """Type 21, pen -> phone: {"start","end","payload_size","uid","send_rate_pps"}
    (proto a8 ALL.txt:32501; iOS penRequestOTAData(start:end:payloadSize:uid:sendRatePPS:))."""

    TYPE: ClassVar[int] = MSG_REQUEST_OTA_PACKAGE
    start: int = 0
    end: int = 0
    payload_size: int = 0
    uid: int = 0
    send_rate_pps: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "payload_size": self.payload_size,
            "uid": self.uid,
            "send_rate_pps": self.send_rate_pps,
        }

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "RequestOtaPackage":
        cls.check_type(pdu)
        j = pdu.json
        return cls(
            _opt_int(j, "start", 0),
            _opt_int(j, "end", 0),
            _opt_int(j, "payload_size", 0),
            _opt_int(j, "uid", 0),
            _opt_int(j, "send_rate_pps", 0),
        )


@dataclass
class OtaPackage(WifiMessage):
    """Type 21, phone -> pen: {"start","len","last","uid"} + binary tail (proto r5
    ALL.txt:38480; iOS sendFilePackToPen(type:start:len:last:uid:binData:))."""

    TYPE: ClassVar[int] = MSG_REQUEST_OTA_PACKAGE
    HAS_TAIL: ClassVar[bool] = True
    start: int = 0
    last: bool = False
    uid: int = 0
    data: bytes = b""

    def to_json(self) -> dict[str, Any]:
        return {"start": self.start, "len": len(self.data), "last": 1 if self.last else 0, "uid": self.uid}

    def tail(self) -> bytes:
        return bytes(self.data)

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "OtaPackage":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "start", 0), _opt_int(j, "last", 0) == 1, _opt_int(j, "uid", 0), pdu.tail)


@dataclass
class OtaStatusSync(WifiMessage):
    """Type 22, pen -> phone: {"status","uid"} (proto b8 ALL.txt:59312; iOS wifiOTAStatus)."""

    TYPE: ClassVar[int] = MSG_OTA_STATUS_SYNC
    status: int = 0
    uid: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "uid": self.uid}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "OtaStatusSync":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "status", 0), _opt_int(j, "uid", 0))


# 100 SpeedTest (proto path only)


@dataclass
class SpeedTestRequest(WifiMessage):
    """Type 100, phone -> pen: {"operate": int (the boolean onOff), "pack_size": int}
    (proto e6 ALL.txt:67715-67757; iOS appWiFiRate(onOff, packSize))."""

    TYPE: ClassVar[int] = MSG_SPEED_TEST
    operate: int = 1
    pack_size: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"operate": self.operate, "pack_size": self.pack_size}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "SpeedTestRequest":
        cls.check_type(pdu)
        j = pdu.json
        return cls(_opt_int(j, "operate", 0), _opt_int(j, "pack_size", 0))


@dataclass
class SpeedTestResponse(WifiMessage):
    """Type 100, pen -> phone: {"index": int} + a tail of (pduSize - jsonSize - 8)
    bytes (proto f6 ALL.txt:23287-23332). The tail size is derived from
    totalSize, so totalSize must be exact."""

    TYPE: ClassVar[int] = MSG_SPEED_TEST
    HAS_TAIL: ClassVar[bool] = True
    index: int = 0
    pack_data: bytes = b""

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index}

    def tail(self) -> bytes:
        return bytes(self.pack_data)

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "SpeedTestResponse":
        cls.check_type(pdu)
        expected = pdu.total_size - pdu.json_size - PDU_HEADER_LEN
        if expected != len(pdu.tail):
            raise ValueError(f"SpeedTest tail {len(pdu.tail)} != totalSize-derived {expected}")
        return cls(_opt_int(pdu.json, "index", 0), pdu.tail)


# 101 GetDeviceLog (proto path only)


@dataclass
class GetDeviceLogRequest(WifiMessage):
    """Type 101, phone -> pen: {"operate": int (boolean begin)} (proto d2 ALL.txt:
    22815-22831; iOS appGetLogs(begin))."""

    TYPE: ClassVar[int] = MSG_GET_DEVICE_LOG
    operate: int = 1

    def to_json(self) -> dict[str, Any]:
        return {"operate": self.operate}

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "GetDeviceLogRequest":
        cls.check_type(pdu)
        return cls(_opt_int(pdu.json, "operate", 0))


@dataclass
class GetDeviceLogResponse(WifiMessage):
    """Type 101, pen -> phone: {"offset": int, "length": int} + `length` bytes of
    log at 8+jsonSize (proto e2 ALL.txt:65005-65055; the arraycopy throws when
    the frame is shorter than 8+jsonSize+length)."""

    TYPE: ClassVar[int] = MSG_GET_DEVICE_LOG
    HAS_TAIL: ClassVar[bool] = True
    offset: int = 0
    data: bytes = b""

    def to_json(self) -> dict[str, Any]:
        return {"offset": self.offset, "length": len(self.data)}

    def tail(self) -> bytes:
        return bytes(self.data)

    @classmethod
    def from_pdu(cls, pdu: Pdu) -> "GetDeviceLogResponse":
        cls.check_type(pdu)
        j = pdu.json
        length = _opt_int(j, "length", 0)
        if len(pdu.tail) < length:
            raise ValueError(f"GetDeviceLog tail {len(pdu.tail)} shorter than length {length}")
        return cls(_opt_int(j, "offset", 0), pdu.tail[:length])


#: What the PHONE parses for each type it receives (device -> phone).
DEVICE_TO_PHONE: dict[int, type[WifiMessage]] = {
    MSG_UNIVERSAL_ERR: UniversalError,
    MSG_HANDSHAKE: HandshakeResponse,
    MSG_SAY_HELLO: SayHello,
    MSG_HEARTBEAT: HeartbeatPing,
    MSG_WIFI_CLOSE: WifiClose,
    MSG_TIPS: Tips,
    MSG_GET_FILE_LIST: FileListResponse,
    MSG_FILE_SYNC: FileSyncStatus,
    MSG_FILE_SYNC_CONTENT: FileSyncContent,
    MSG_FILE_DELETE: FileDeleteResponse,
    MSG_FILE_SYNC_STOP: FileSyncStopResponse,
    MSG_EXTEND_EXIT_TIME: ExtendExitTimeResponse,
    MSG_REQUEST_OTA_PACKAGE: RequestOtaPackage,
    MSG_OTA_STATUS_SYNC: OtaStatusSync,
    MSG_SPEED_TEST: SpeedTestResponse,
    MSG_GET_DEVICE_LOG: GetDeviceLogResponse,
}

#: What the PEN parses for each type it receives (phone -> device).
PHONE_TO_DEVICE: dict[int, type[WifiMessage]] = {
    MSG_HANDSHAKE: HandshakeRequest,
    MSG_SAY_HELLO: HelloAck,
    MSG_HEARTBEAT: HeartbeatPong,
    MSG_GET_FILE_LIST: GetFileListRequest,
    MSG_FILE_SYNC: FileSyncRequest,
    MSG_FILE_DELETE: FileDeleteRequest,
    MSG_FILE_SYNC_STOP: FileSyncStopRequest,
    MSG_EXTEND_EXIT_TIME: ExtendExitTimeRequest,
    MSG_SEND_OTA_FILE_INFO: SendOtaFileInfoRequest,
    MSG_REQUEST_OTA_PACKAGE: OtaPackage,
    MSG_SPEED_TEST: SpeedTestRequest,
    MSG_GET_DEVICE_LOG: GetDeviceLogRequest,
}


def decode_for_phone(raw: bytes) -> WifiMessage:
    """Parse a device -> phone frame into its message class (phone-side mirror)."""
    pdu = parse_pdu(raw)
    cls = DEVICE_TO_PHONE.get(pdu.msg_type)
    if cls is None:
        raise ValueError(f"Unknown message type: {pdu.msg_type}")
    return cls.from_pdu(pdu)


def decode_for_device(raw: bytes) -> WifiMessage:
    """Parse a phone -> device frame into its message class (device side)."""
    pdu = parse_pdu(raw)
    cls = PHONE_TO_DEVICE.get(pdu.msg_type)
    if cls is None:
        raise ValueError(f"Unknown message type: {pdu.msg_type}")
    return cls.from_pdu(pdu)


# --- sealing -----------------------------------------------------------------------

AEAD_TAG_LEN = 16


class WifiSealer:
    """AEAD wrapper for the Wi-Fi channel over `sealed.SealedSession` counters.

    Recovered (BYTECODE_PROVEN):
    * plaintext = [u32le seq][PDU]; seq is the pre-incremented TX counter
      (proto w7 send path `a(e8)`, w7.txt:31-464: `z.p()+1` :151; Kotlin
      encryptWifiMessage: `sendSeq++` where sendSeq was copied from z.M,
      WifiAgentImpl.txt:205-206 and :534-539 -- NOTE that path uses the value
      BEFORE incrementing, so the two phone stacks differ by one);
    * AES-GCM (`AES/GCM/NoPadding`, GCMParameterSpec(128, iv), AAD via
      updateAAD: proto/q5.txt:459-489) iff z.w() -- capability bit 3 -- else
      ChaCha20-Poly1305 (q5.txt:76-98 seal, :177-239 open with useAes flag);
      the same (key, nonce, aad) triple as BLE (z.J/z.K/z.L), nonce REUSED
      per frame exactly as on BLE;
    * receive on proto w7 (`c(byte[])`, w7.txt:562-1476; `q5.a(...,boolean)`
      at :663): len >= 4, seq = u32le@0 as int, drop when `rx >= seq`, else
      set; receive on Kotlin WifiAgentImpl:
      NO replay check -- it logs "expected > recvSeq" and then unconditionally
      stores the sequence (WifiAgentImpl.txt:2133-2196);
    * counters: shared with the BLE statics z.M/z.N on every model except
      serial prefix "881", which uses w7's private m/n (tests/test_r7_transport_scope.py).

    HARNESS_POLICY: `counters` is any SealedSession; pass the BLE session's
    to continue its counters (non-881) or a fresh one (881). The device's own
    TX start is UNKNOWN (SealedSession's reset gives 2 for the first frame).
    `replay_check=False` reproduces the Kotlin phone's permissive receive.
    """

    def __init__(
        self,
        counters: SealedSession | None = None,
        use_aes: bool = False,
        replay_check: bool = True,
    ) -> None:
        self.counters = counters if counters is not None else SealedSession()
        self.use_aes = bool(use_aes)
        self.replay_check = bool(replay_check)
        self.seal_calls = 0

    @property
    def algorithm(self) -> str:
        # The SDK's own log names (WifiAgentImpl.txt:280-284).
        return "AES-GCM" if self.use_aes else "ChaCha20-Poly1305"

    @property
    def tx_seq(self) -> int:
        return self.counters.tx_seq

    @property
    def rx_seq(self) -> int:
        return self.counters.rx_seq

    def _seal_raw(self, plaintext: bytes) -> bytes:
        c = self.counters
        if self.use_aes:
            return AESGCM(c.key).encrypt(c.nonce, plaintext, c.aad or None)
        return seal_raw(c.key, c.nonce, c.aad, plaintext)

    def _open_raw(self, wire: bytes) -> bytes:
        c = self.counters
        if len(wire) < AEAD_TAG_LEN:
            raise ValueError("Encrypted data must be at least 16 bytes")
        if self.use_aes:
            return AESGCM(c.key).decrypt(c.nonce, bytes(wire), c.aad or None)
        return open_raw(c.key, c.nonce, c.aad, wire)

    def seal(self, pdu: bytes) -> bytes:
        """M += 1, then AEAD([u32le M][pdu]). Re-sends must copy the bytes."""
        c = self.counters
        c.tx_seq = _to_signed((c.tx_seq + 1) & 0xFFFFFFFF)
        self.seal_calls += 1
        return self._seal_raw(pack("<I", c.tx_seq & 0xFFFFFFFF) + bytes(pdu))

    def open(self, wire: bytes) -> bytes | None:
        """Open one inbound wire frame; None = silently skipped (short or replayed)."""
        plaintext = self._open_raw(bytes(wire))
        if len(plaintext) < 4:
            return None
        seq = _to_signed(unpack_from("<I", plaintext, 0)[0])
        if self.replay_check and self.counters.rx_seq >= seq:
            return None
        self.counters.rx_seq = seq
        return plaintext[4:]


def _to_signed(value_u32: int) -> int:
    return value_u32 - (1 << 32) if value_u32 >= (1 << 31) else value_u32


__all__ = [
    "WS_PORT", "PDU_HEADER_LEN", "PDU_VERSION_HOST", "MAX_TOTAL_SIZE", "CIPHERTEXT_TYPE_THRESHOLD",
    "MSG_UNIVERSAL_ERR", "MSG_HANDSHAKE", "MSG_SAY_HELLO", "MSG_HEARTBEAT", "MSG_WIFI_CLOSE", "MSG_TIPS",
    "MSG_GET_FILE_LIST", "MSG_FILE_SYNC", "MSG_FILE_SYNC_CONTENT", "MSG_FILE_DELETE", "MSG_FILE_SYNC_STOP",
    "MSG_EXTEND_EXIT_TIME", "MSG_SEND_OTA_FILE_INFO", "MSG_REQUEST_OTA_PACKAGE", "MSG_OTA_STATUS_SYNC",
    "MSG_SPEED_TEST", "MSG_GET_DEVICE_LOG", "MESSAGE_NAMES", "MESSAGE_EVIDENCE",
    "Pdu", "encode_json", "pack_pdu", "parse_pdu", "looks_encrypted",
    "WIFI_TOKEN_WIDTH", "WIFI_TOKEN_PAD", "pad_wifi_token",
    "WifiMessage", "UniversalError", "HandshakeRequest", "HandshakeResponse", "SayHello", "HelloAck",
    "HeartbeatPing", "HeartbeatPong", "WifiClose", "Tips", "GetFileListRequest", "FILE_RECORD_LEN",
    "FileRecord", "pack_file_records", "parse_file_records", "file_name_for_session", "FileListResponse",
    "FileSyncRequest", "FileSyncStatus", "FileSyncContent", "FileDeleteRequest", "FileDeleteResponse",
    "FileSyncStopRequest", "FileSyncStopResponse", "ExtendExitTimeRequest", "ExtendExitTimeResponse",
    "SendOtaFileInfoRequest", "RequestOtaPackage", "OtaPackage", "OtaStatusSync",
    "SpeedTestRequest", "SpeedTestResponse", "GetDeviceLogRequest", "GetDeviceLogResponse",
    "DEVICE_TO_PHONE", "PHONE_TO_DEVICE", "decode_for_phone", "decode_for_device",
    "AEAD_TAG_LEN", "WifiSealer",
]
