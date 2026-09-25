"""Plaud/Tinno GATT profile and device model for the emulator.

Scope and honesty boundary
--------------------------
This peripheral emulates a device whose advertised **portVersion is below 20**.
That is not a convenience: `com.plaud.sdk.proto.z` branches the ENTIRE 2BB0/2BB1
channel on it.

  portVersion <  20   control frames travel in cleartext, and the connection
                      goes straight from set_data_notify to the k3 handshake.
  portVersion >= 20   the RSA pre-handshake runs first, and afterwards EVERY
                      frame in both directions is ChaCha20-Poly1305 sealed
                      (z.d outbound, z$c.onCharacteristicChanged inbound).

portVersion is not negotiated on the wire -- it is read out of the manufacturer
specific advertising data (u4.a(ScanResult) -> BleDevice.getPortVersion()), so a
device declares it before a central ever connects. Everything this module puts
on the wire is therefore a faithful emulation of the sub-20 branch and an
INCOMPLETE emulation of the modern one. `PORT_VERSION` makes that explicit and
the peripheral refuses to serve cleartext while claiming a modern device.
"""

from __future__ import annotations

import asyncio

import enum
from dataclasses import dataclass
from struct import pack, unpack_from
from typing import Any

from bumble.device import Connection, Device
from bumble.gatt import (
    GATT_BATTERY_LEVEL_CHARACTERISTIC,
    GATT_BATTERY_SERVICE,
    Attribute,
    Characteristic,
    CharacteristicValue,
    Service,
)

from plaudsim.advertising import ScanFields, build_manufacturer_data
from plaudsim.handshake import (
    HANDSHAKE_OPCODE,
    SSN_OPCODE,
    encode_l3,
    encode_x2,
    parse_handshake_request,
)
from plaudsim.filesync import (
    OPCODE_DELETE_FILE,
    OPCODE_FILE_LIST,
    OPCODE_RESUME,
    OPCODE_STOP_SYNC,
    OPCODE_SYNC_START,
)
from plaudsim.transfer import (
    DEFAULT_EMPTY_PACKAGE_CODE,
    FileTable,
    TransferSession,
    pack_delete_file_response,
    pack_resume_record_response,
    pack_stop_sync_response,
    parse_delete_file_request,
    parse_file_list_request,
    parse_resume_record_request,
    parse_stop_sync_request,
    parse_sync_start_request,
)

# w$d, DIRECT constants.
PLAUD_SERVICE_UUID = "00001910-0000-1000-8000-00805f9b34fb"
DATA_NOTIFY_UUID = "00002BB0-0000-1000-8000-00805f9b34fb"
COMMAND_WRITE_UUID = "00002BB1-0000-1000-8000-00805f9b34fb"

# The cleartext branch in z. Above this the channel is sealed; see the module docstring.
ENCRYPTED_PORT_VERSION = 20
#: What this emulator claims to be. Any value < ENCRYPTED_PORT_VERSION is a
#: faithful cleartext device; 7 also selects the modern file-list stride and the
#: session-bearing type-2 data frame, which is the layout current firmware uses.
PORT_VERSION = 7

OPCODE_GET_STATE = 3        # request y2 -> response z2 / GetStateRsp
OPCODE_SYNC_TIME = 4        # request m7 -> response n7 / TimeSyncRsp
OPCODE_GET_STORAGE = 6      # request a3 -> response n6 / StorageRsp
OPCODE_BATT_STATUS = 9      # request o  -> response p  / BattStatusRsp
OPCODE_HANDSHAKE = HANDSHAKE_OPCODE   # 1: request k3/j3 -> response l3
OPCODE_GET_SSN = SSN_OPCODE           # 2: request w2    -> response x2 / GetSsnRsp
OPCODE_NEW_FEATURE = 138    # device PUSH (DEV_NEW_FEATURE_REQ) -> host l1 FeatureReq
OPCODE_COMMON_SETTINGS = 8  # request q0 CommonSettingsReq -> response r0 CommonSettingsRsp

#: s0$b$a (CommonAction) WIRE values = the enum's int field `a`, NOT its ordinal:
#: READ -> 1, SETTING -> 2 (ALL.txt:49154+). BYTECODE_PROVEN; READ=1 RUNTIME_PROVEN.
COMMON_ACTION_READ = 1
COMMON_ACTION_SET = 2

#: s0$b$b (CommonType): ProGuard kept the enum NAME strings, and the wire value
#: is the third ctor arg (field `a`), which is NOT the ordinal. Recovered from
#: the <clinit> at ALL.txt:22853-23260. BYTECODE_PROVEN. The Swift `CommonType`
#: enum lists the same 21 settings in the same ORDER (ordinal), so the two
#: artifacts cross-reference by ordinal while this table gives the wire byte.
COMMON_TYPE_NAMES: dict[int, str] = {
    1: "BACK_LIGHT_TIME",          # ordinal 0  (Swift LightDuration)
    2: "BACK_LIGHT_BRIGHTNESS",    # ordinal 1  (LightBright)
    3: "LANGUAGE",                 # ordinal 2
    4: "AUTO_DELETE_RECORD_FILE",  # ordinal 3  (AutoClear)
    15: "ENABLE_VAD",              # ordinal 4  (VAD)
    16: "REC_SCENE",               # ordinal 5
    17: "REC_MODE",                # ordinal 6
    18: "VAD_SENSITIVITY",         # ordinal 7
    19: "VPU_GAIN",                # ordinal 8
    32: "BATTERY_MODE",            # ordinal 9
    20: "MIC_GAIN",                # ordinal 10
    21: "WIFI_CHANNEL",            # ordinal 11
    22: "SWITCH_HANDLER_ID",       # ordinal 12 (SwitchHandle)
    23: "AUTO_POWER_OFF",          # ordinal 13
    24: "SAVE_RAW_FILE",           # ordinal 14 (RawWaveEnabled)
    25: "AUTO_RECORD",             # ordinal 15 (RecordingAfterDisConnet)
    26: "AUTO_SYNC",               # ordinal 16 (SyncWhenIdle)
    27: "FIND_MY",                 # ordinal 17 (FindMyState)
    30: "VPU_CLK",                 # ordinal 18
    31: "AUTO_STOP_RECORD",        # ordinal 19 (StopRecordAfterCharging)
    49: "IBEACON_WAKEUP",          # ordinal 20
}
COMMON_TYPE_ENABLE_VAD = 15
COMMON_TYPE_REC_MODE = 17

#: HARNESS_POLICY: the values this synthetic device reports. The SDK reads
#: ENABLE_VAD then REC_MODE right after getState (RUNTIME_PROVEN, R7-S12) and
#: consumes them in q.f (ALL.txt:41224+): ENABLE_VAD -> setVadOpen(v == 1);
#: REC_MODE -> setNcClose(v != 2), i.e. wire 2 = noise-cancelling mode.
DEFAULT_COMMON_SETTINGS: dict[int, int] = {
    COMMON_TYPE_ENABLE_VAD: 1,
    COMMON_TYPE_REC_MODE: 1,
}

#: Bit 3 of the device's feature bitmap. `q$f.a(byte[])` case 138 computes
#: `(payload[0] & 8) != 0` and passes it to `z.a(boolean)`, which sets the
#: static `z.O`. The SDK's own log names it: "NEW_FEATURE-设备WiFi AES支持"
#: ("device WiFi AES support").
#:
#: SCOPE, and this matters: `z.O` is read ONLY through `z.w()`, which has
#: three call sites and all three are Wi-Fi (w7 TX, w7 RX, WifiAgentImpl).
#: The BLE sealed path calls `q5.d`/`q5.b` -- the four-argument
#: ChaCha20-Poly1305 helpers with no algorithm parameter. So this bit does
#: NOT affect BLE: the BLE sealed transport is unconditionally
#: ChaCha20-Poly1305.
FEATURE_BIT_WIFI_AES = 0x08

#: `l1.enPkg()` = packHead + c((byte)(1|2|4|8|16|32|64|128)) -- the host
#: unconditionally advertises all eight feature bits. 4 bytes total.
HOST_FEATURE_BITMAP = 0xFF

#: `q.H()` is `bleDevice.getPortVersion() >= 5`. Below that the SDK subscribes
#: and reads the standard Battery Service (0x180F/0x2A19) during connect; at or
#: above it the "new battery service" path skips 0x180F entirely and battery
#: comes from opcode 9. The emulator publishes 0x180F regardless -- harmless,
#: since a modern client simply never reads it.
NEW_BATTERY_SERVICE_PORT_VERSION = 5

GET_STATE_REQUEST = b"\x01\x03\x00"
GET_STORAGE_REQUEST = b"\x01\x06\x00"


class PlaudLifecycle(enum.Enum):
    """Emulator lifecycle, mapped to states the SDK and templates observe.

    DISCONNECTED  initial and post-disconnect.
    GATT_READY    GATT DB published (peripheral setup).
    CONNECTED     link up; mirrors z's a0.CONNECTED, set in onServicesDiscovered.
    HANDSHAKED    the device answered an opcode-1 handshake with status 0.
    BOUND         the SDK's own notion of a completed bind: handshake status 0,
                  then the battery exchange (portVersion >= 5), then syncTime.
                  `bleBind` fires off the syncTime success callback -- there is
                  no BLE "bind" message at all.
    READY         post-refreshDeviceInfo. Not modelled.

    ON BOUND, AND WHAT IT DOES NOT MEAN. Reaching BOUND here is legitimate, and
    it is not a crypto claim: on the legacy (portVersion < 20) path the
    handshake carries no cryptography whatsoever -- the host sends a token and
    the DEVICE returns a status. Accepting the token is the device's decision,
    and this emulator is the device.

    What BOUND does NOT mean is that anything here could bind to REAL Plaud
    hardware. That direction needs a token, an RSA key pair and an SN signature
    that only Plaud's cloud issues, and none of them exists in this repository.
    The asymmetry is the point: we can be a device to their client, not a client
    to their device.

    A peripheral constructed with `accept_any_token=False` refuses every
    handshake, which is how tests assert that bare command exchanges never
    advance past CONNECTED.
    """

    DISCONNECTED = "disconnected"
    GATT_READY = "gatt_ready"
    CONNECTED = "connected"
    HANDSHAKED = "handshaked"
    BOUND = "bound"
    READY = "ready"


@dataclass(frozen=True)
class PlaudDeviceState:
    """Values for the z2/GetStateRsp response.

    Layout from z2.<init>: state u32le@3, privacyEnable u8@7, keyState u8@8,
    usbState u8@9, scene u8@10, sessionId u32le@11, findMyState u8@15,
    then two further u8 at 16 and 17 that z2 parses but toString does not name.
    """

    state: int = 0
    privacy_enabled: bool = False
    key_state: int = 0
    usb_state: bool = False
    scene: int = 0
    session_id: int = 0
    find_my_state: int = 0
    unnamed_16: int = 0
    unnamed_17: int = 0

    def encode(self) -> bytes:
        return b"\x01\x03\x00" + pack(
            "<IBBBBIBBB",
            self.state,
            int(self.privacy_enabled),
            self.key_state,
            int(self.usb_state),
            self.scene,
            self.session_id,
            self.find_my_state,
            self.unnamed_16,
            self.unnamed_17,
        )


@dataclass(frozen=True)
class PlaudStorageState:
    """Values for the n6/StorageRsp response.

    n6.<init>: free u64le@3, total u64le@11, then `if (len >= 27)` duration
    u64le@19. Names are DIRECT from the n6.toString literal
    "StorageRsp{free=..., total=..., duration=...}". Units are UNKNOWN: no
    formatter in either template app fixes them.
    """

    free: int = 0
    total: int = 0
    duration: int = 0

    def encode(self) -> bytes:
        return b"\x01\x06\x00" + pack("<QQQ", self.free, self.total, self.duration)


@dataclass(frozen=True)
class PlaudBatteryState:
    """Values for the o request / p response pair (opcode 9).

    p.<init>: charging = (u8@3 == 1), level = u8@4. Names are DIRECT from the
    p.toString literal "BattStatusRsp{charging=..., level=...}".

    This message is load-bearing for a real-SDK connect: `q` gates connection
    success on three separate device replies -- handshake status 0, this
    battery exchange (when portVersion >= 5), and syncTime -- each retried a
    few times before the connect is reported as failed. A device that answers
    the handshake but not opcode 9 is reported to the app as a failed connect.

    The device also PUSHES this frame unsolicited to report battery changes.
    """

    charging: bool = False
    level: int = 100

    def encode(self) -> bytes:
        if not 0 <= self.level <= 0xFF:
            raise ValueError(f"level out of u8 range: {self.level}")
        return b"\x01\x09\x00" + bytes([int(self.charging), self.level])


@dataclass(frozen=True)
class PlaudSyncTimeState:
    """Values for the m7 request / n7 response pair.

    Request (m7.enPkg): [01][04 00][u32le stamp][i8 tzHours][i8 tzMinutes].
    m7.a(TimeZone,long) computes `offset = tz.getOffset(now)/60000` then
    `(offset/60, offset%60)`, so BOTH timezone fields are SIGNED and a device
    west of UTC sends negative values (e.g. UTC-04:30 -> -4, -30).

    Response (n7.<init>): stamp u32le@3, timezone u8@7 cast through `(byte)`
    -- so SIGNED -- and, guarded by len >= 9, hasStatistics u8@8.

    NAME CORRECTION: the field at offset 7 is named `timezone` by the
    n7.toString literal "TimeSyncRsp{stamp=%d, timezone=%d, hasStatistics=%s}".
    An earlier reconstruction called it `raw7` and recorded it as unnamed.

    The response stamp is a fixed emulator value and is NOT claimed to echo the
    request: whether the device echoes or reports its own clock is UNKNOWN.
    """

    stamp: int = 0
    tz_hours: int = 0
    tz_mins: int = 0
    timezone: int = 0
    has_statistics: bool = False

    @staticmethod
    def _i8(value: int) -> int:
        if not -128 <= value <= 127:
            raise ValueError(f"timezone field out of int8 range: {value}")
        return value & 0xFF

    def encode_request(self) -> bytes:
        return (
            b"\x01\x04\x00"
            + pack("<I", self.stamp)
            + bytes([self._i8(self.tz_hours), self._i8(self.tz_mins)])
        )

    def encode_response(self) -> bytes:
        return (
            b"\x01\x04\x00"
            + pack("<I", self.stamp)
            + bytes([self._i8(self.timezone), int(self.has_statistics)])
        )


def encode_feature_frame(bitmap: int) -> bytes:
    """A protocol-type-1 opcode-138 feature frame: [01][8A 00][u8 bitmap].

    Both directions share this shape. `l1.enPkg()` is
    `packHead + c((byte)(1|2|4|8|16|32|64|128))`, i.e. a 4-byte frame whose
    single payload byte is 0xFF, and the device push is read as
    `payload = frame[3:]` with `payload[0]` carrying the bitmap.
    """
    if not 0 <= bitmap <= 0xFF:
        raise ValueError(f"feature bitmap out of u8 range: {bitmap}")
    return b"\x01\x8a\x00" + bytes([bitmap])


def parse_feature_request(data: bytes) -> dict[str, object]:
    """Parse an opcode-138 feature frame.

    Mirrors `q$f.a(byte[])` case 138: the SDK requires `length > 3` before it
    strips the 3-byte header, and reads bit 3 of payload[0]. A frame with no
    payload is not an error -- the SDK logs "数据长度不足" and leaves its
    selector untouched -- so this returns `bitmap=None` rather than raising.
    """
    raw = bytes(data)
    if len(raw) < 3 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_NEW_FEATURE:
        raise ValueError(f"not an opcode-138 feature frame: {raw.hex()}")
    if len(raw) <= 3:
        return {"bitmap": None, "payload": b"", "wifi_aes": False}
    payload = raw[3:]
    return {
        "bitmap": payload[0],
        "payload": payload,
        "wifi_aes": bool(payload[0] & FEATURE_BIT_WIFI_AES),
    }


def encode_common_settings_response(type_wire: int, value: int) -> bytes:
    """r0 CommonSettingsRsp: [01][08 00][u16le type @3][u32le value @5] = 9 bytes.

    r0.<init> reads `b = TntBleCommUtils.b(buf, 3)` (16-bit) and
    `c = TntBleCommUtils.d(buf, 5)` (32-bit) (ALL.txt:30994-31030); the SDK
    matches the reply on waiter {8, type+1000} and compares `b` with the
    CommonType WIRE value. BYTECODE_PROVEN layout.
    """
    if not 0 <= type_wire <= 0xFFFF:
        raise ValueError(f"common type out of u16 range: {type_wire}")
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"common value out of u32 range: {value}")
    return b"\x01\x08\x00" + pack("<H", type_wire) + pack("<I", value)


def parse_common_settings_request(data: bytes) -> dict[str, object]:
    """q0 CommonSettingsReq: packHead + c(action)=1 B + a(type)=2 B LE, plus
    a(J)(value)=4 B LE only on the SETTING (4-chunk) path (ALL.txt:59528-59600).

    READ  : 01 08 00 | u8 action=1 | u16le type                 -> 6 bytes
    SETTING: 01 08 00 | u8 action=2 | u16le type | u32le value   -> 10 bytes
    The two READ frames the real SDK sent were 01 08 00 01 0f 00 and
    01 08 00 01 11 00 (ENABLE_VAD, REC_MODE) -- RUNTIME_PROVEN.
    """
    raw = bytes(data)
    if len(raw) < 3 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_COMMON_SETTINGS:
        raise ValueError(f"not an opcode-8 common-settings frame: {raw.hex()}")
    if len(raw) < 6:
        raise ValueError(f"common-settings request too short: {len(raw)}")
    action = raw[3]
    type_wire = unpack_from("<H", raw, 4)[0]
    out: dict[str, object] = {
        "action": action,
        "type": type_wire,
        "name": COMMON_TYPE_NAMES.get(type_wire),
        "value": None,
    }
    if action == COMMON_ACTION_SET:
        if len(raw) < 10:
            raise ValueError(f"common-settings SET too short for a u32 value: {len(raw)}")
        out["value"] = unpack_from("<I", raw, 6)[0]
    elif action != COMMON_ACTION_READ:
        raise ValueError(f"unknown common-settings action: {action}")
    return out


# --- Wi-Fi handoff over BLE (ledger section 7) -------------------------------
# The BLE side of the Wi-Fi fast-transfer flow: the phone asks the pen to raise
# its SoftAP (opcode 10), later asks it to drop it (13), and can read or set
# the pen's device-to-cloud WebSocket profile (16 -> 17). Layouts are
# BYTECODE_PROVEN from the proto request/response classes cited per constant;
# every value the emulator returns is HARNESS_POLICY.
OPCODE_OPEN_WIFI = 10       # req i4 OpenWiFiReq (ALL.txt:70643-70760) -> rsp j4 OpenWiFiRsp (ALL.txt:26526-26599)
OPCODE_CLOSE_WIFI = 13      # req k0, header only (ALL.txt:69681-69697) -> rsp l0 CloseWiFiRsp (ALL.txt:55388-55420)
OPCODE_WEBSOCKET_REQ = 16   # req d3 GET (ALL.txt:49719-49786) / b6 SET (ALL.txt:52659-52788)
OPCODE_WEBSOCKET_RSP = 17   # rsp v2 GetSetWebsocketRsp (ALL.txt:49949-50074)

WEBSOCKET_OP_GET = 1        # d3.enPkg: packHead + c(1) + c(ordinal)
WEBSOCKET_OP_SET = 2        # b6.enPkg: packHead + c(2) + c(ordinal) + NUL-filled buffer

#: d3$a enum: ordinal 0 "unknown", 1 "url", 2 "serToken", 3 "devToken" (DIRECT,
#: ALL.txt:52496-52519). Ordinal 0 is refused by both encoders
#: ("websocket type must be url/serToken/devToken", ALL.txt:49783, :52728) and
#: the Swift `WebsocketType : UInt8 { url, serToken, devToken }`
#: (PlaudBleSDK swiftinterface:714-718) has no `unknown` at all.
WEBSOCKET_TYPE_URL = 1
WEBSOCKET_TYPE_SER_TOKEN = 2
WEBSOCKET_TYPE_DEV_TOKEN = 3
WEBSOCKET_TYPE_NAMES: dict[int, str] = {1: "url", 2: "serToken", 3: "devToken"}
#: b6.enPkg allocates new byte[64] for url ("url length must be < 64") and
#: new byte[16] for the tokens ("token length must be <= 16"), Arrays.fill 0,
#: arraycopy (ALL.txt:52686-52788). v2.<init> scans the same widths
#: (bipush 64 for type 1, else 16; ALL.txt:49965-50029).
WEBSOCKET_VALUE_WIDTH: dict[int, int] = {1: 64, 2: 16, 3: 16}
WEBSOCKET_URL_MAX = 63
WEBSOCKET_TOKEN_MAX = 16

#: i4.<init>: "OpenWiFiReq wifiPass must be 8 ASCII bytes" (ALL.txt:70661-70695);
#: j4.<init>: wifiPass = ASCII bytes [4..12) iff len >= 12 (ALL.txt:26538-26580).
WIFI_PASS_LEN = 8
#: The status the template treats as "device busy streaming" for openWiFi
#: (CLAIM, SyncManager.kt:31-32 and :144-146: "status 4 (busy)"). Harness policy
#: when applied; the mapping itself is a template-app comment, not bytecode.
WIFI_OPEN_STATUS_BUSY = 4

#: HARNESS_POLICY defaults for the WebSocket profile. Synthetic and plainly
#: non-operational; `serToken`/`devToken` are 16 chars, the width the SET
#: encoder allows.
DEFAULT_WEBSOCKET_PROFILE: dict[int, str] = {
    WEBSOCKET_TYPE_URL: "wss://synthetic.invalid/plaudsim",
    WEBSOCKET_TYPE_SER_TOKEN: "SYN-SER-TOKEN-01",
    WEBSOCKET_TYPE_DEV_TOKEN: "SYN-DEV-TOKEN-01",
}


def parse_open_wifi_request(data: bytes) -> dict[str, object]:
    """i4 OpenWiFiReq: [01][0A 00][u8 onOff] (+ 8 ASCII wifiPass bytes when given).

    i4.enPkg (ALL.txt:70697-70760) merges packHead, c(a) [one byte] and, when
    `b` is non-null, its US-ASCII bytes (constructor-checked to be exactly 8).
    The entity-layer `sdk.ble.entity.request.OpenWifiReq` (ALL.txt:131639-131670)
    sends packHead ONLY (3 bytes), so a header-only request is also genuine;
    `on_off` is None in that case (HARNESS_POLICY: treat as "open").
    """
    raw = bytes(data)
    if len(raw) < 3 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_OPEN_WIFI:
        raise ValueError(f"not an opcode-10 openWiFi request: {raw.hex()}")
    if len(raw) == 3:
        return {"on_off": None, "wifi_pass": None}
    if len(raw) == 4:
        return {"on_off": raw[3], "wifi_pass": None}
    if len(raw) == 4 + WIFI_PASS_LEN:
        return {"on_off": raw[3], "wifi_pass": raw[4:].decode("ascii", "replace")}
    raise ValueError(f"openWiFi request has an unrecognised length: {len(raw)}")


def encode_open_wifi_response(status: int, wifi_pass: str | None = None) -> bytes:
    """j4 OpenWiFiRsp: [01][0A 00][u8 status] + optional 8-byte ASCII wifiPass.

    j4.<init> reads status = u8@3 and wifiPass = new String(buf, 4, 8, US_ASCII)
    iff len >= 12 (ALL.txt:26538-26580); the Wi-Fi agent then IGNORES the
    passphrase and derives its own from the serial (IotaStateTracker.txt:
    56-69 -> calculateWifiPassword = takeLast(8)).
    """
    if not 0 <= status <= 0xFF:
        raise ValueError(f"status out of u8 range: {status}")
    frame = b"\x01" + pack("<H", OPCODE_OPEN_WIFI) + bytes([status])
    if wifi_pass is None:
        return frame
    body = wifi_pass.encode("ascii")
    if len(body) != WIFI_PASS_LEN:
        raise ValueError(f"wifiPass must be {WIFI_PASS_LEN} ASCII bytes, got {len(body)}")
    return frame + body


def parse_open_wifi_response(data: bytes) -> dict[str, object]:
    """Host-side mirror of j4.<init> (status u8@3, wifiPass iff len >= 12)."""
    raw = bytes(data)
    if len(raw) < 4 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_OPEN_WIFI:
        raise ValueError(f"not an opcode-10 openWiFi response: {raw.hex()}")
    out: dict[str, object] = {"status": raw[3], "wifi_pass": None}
    if len(raw) >= 4 + WIFI_PASS_LEN:
        out["wifi_pass"] = raw[4 : 4 + WIFI_PASS_LEN].decode("ascii", "replace")
    return out


def parse_close_wifi_request(data: bytes) -> dict[str, int]:
    """k0: header only, [01][0D 00] (ALL.txt:69681-69697)."""
    raw = bytes(data)
    if len(raw) != 3 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_CLOSE_WIFI:
        raise ValueError(f"not a k0 closeWiFi request: {raw.hex()}")
    return {}


def encode_close_wifi_response(status: int) -> bytes:
    """l0 CloseWiFiRsp: [01][0D 00][u8 status] (ALL.txt:55393-55404)."""
    if not 0 <= status <= 0xFF:
        raise ValueError(f"status out of u8 range: {status}")
    return b"\x01" + pack("<H", OPCODE_CLOSE_WIFI) + bytes([status])


def parse_close_wifi_response(data: bytes) -> dict[str, int]:
    raw = bytes(data)
    if len(raw) < 4 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_CLOSE_WIFI:
        raise ValueError(f"not an opcode-13 closeWiFi response: {raw.hex()}")
    return {"status": raw[3]}


def _websocket_width(ws_type: int) -> int:
    if ws_type not in WEBSOCKET_VALUE_WIDTH:
        raise ValueError("websocket type must be url/serToken/devToken")
    return WEBSOCKET_VALUE_WIDTH[ws_type]


def encode_websocket_get_request(ws_type: int) -> bytes:
    """d3.enPkg: [01][10 00][u8 1][u8 ordinal] -- 5 bytes (ALL.txt:49741-49780)."""
    _websocket_width(ws_type)
    return b"\x01" + pack("<H", OPCODE_WEBSOCKET_REQ) + bytes([WEBSOCKET_OP_GET, ws_type])


def encode_websocket_set_request(ws_type: int, value: str) -> bytes:
    """b6.enPkg: [01][10 00][u8 2][u8 ordinal][64 or 16 NUL-filled bytes] (ALL.txt:52686-52788).

    Mirrors the host's own guards: url <= 63 bytes ("url length must be < 64"),
    token <= 16 bytes ("token length must be <= 16"). Uses the platform default
    charset like `String.getBytes()`; ASCII here.
    """
    width = _websocket_width(ws_type)
    body = value.encode("utf-8")
    if ws_type == WEBSOCKET_TYPE_URL and len(body) > WEBSOCKET_URL_MAX:
        raise ValueError("url length must be < 64")
    if ws_type != WEBSOCKET_TYPE_URL and len(body) > WEBSOCKET_TOKEN_MAX:
        raise ValueError("token length must be <= 16")
    return (
        b"\x01"
        + pack("<H", OPCODE_WEBSOCKET_REQ)
        + bytes([WEBSOCKET_OP_SET, ws_type])
        + body.ljust(width, b"\x00")
    )


def parse_websocket_request(data: bytes) -> dict[str, object]:
    """Parse a d3 (GET) or b6 (SET) request as the DEVICE sees it.

    GET is exactly 5 bytes; SET is 5 + width bytes with the value NUL-padded.
    HARNESS_POLICY: the SET value is read up to the first NUL (the host always
    leaves at least one NUL in a url buffer; a 16-byte token may fill it).
    """
    raw = bytes(data)
    if len(raw) < 5 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_WEBSOCKET_REQ:
        raise ValueError(f"not an opcode-16 websocket request: {raw.hex()}")
    op, ws_type = raw[3], raw[4]
    width = _websocket_width(ws_type)
    out: dict[str, object] = {"op": op, "type": ws_type, "name": WEBSOCKET_TYPE_NAMES[ws_type], "value": None}
    if op == WEBSOCKET_OP_GET:
        if len(raw) != 5:
            raise ValueError(f"websocket GET carries no value, got {len(raw)} bytes")
        return out
    if op == WEBSOCKET_OP_SET:
        if len(raw) != 5 + width:
            raise ValueError(f"websocket SET for type {ws_type} must be {5 + width} bytes, got {len(raw)}")
        out["value"] = raw[5:].split(b"\x00", 1)[0].decode("utf-8", "replace")
        return out
    raise ValueError(f"unknown websocket op: {op}")


def encode_websocket_response(ws_type: int, value: str) -> bytes:
    """v2 GetSetWebsocketRsp: [01][11 00][u8 type][value, NUL-padded to 64 or 16].

    v2.<init> (ALL.txt:49954-50029) maps type 1/2/3 to url/serToken/devToken,
    scans from offset 4 for a NUL within the width, and takes bytes[4:nul).
    Padding to the full width is the emulator's choice; a shorter frame is
    legal for the reader only when it contains a NUL before its end.
    """
    width = _websocket_width(ws_type)
    body = value.encode("utf-8")
    if len(body) > width:
        raise ValueError(f"value exceeds the {width}-byte width for type {ws_type}")
    return b"\x01" + pack("<H", OPCODE_WEBSOCKET_RSP) + bytes([ws_type]) + body.ljust(width, b"\x00")


def parse_websocket_response(data: bytes) -> dict[str, object]:
    """Host-side mirror of v2.<init>, quirk included.

    The scan `for (i = 4; i < 4 + width; i++) if (buf[i] == 0) break;` resets
    i to 4 when no NUL is found, and the string is bytes[4 : i) when i != 4,
    else bytes[4 : 4 + width). So an EMPTY value (NUL at offset 4) is read as
    the FULL width of NULs, and a frame shorter than 4 + width with no NUL
    throws in Java (surfaced as ValueError here). Types outside 1..3 leave the
    enum null and use width 16.
    """
    raw = bytes(data)
    if len(raw) < 4 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_WEBSOCKET_RSP:
        raise ValueError(f"not an opcode-17 websocket response: {raw.hex()}")
    ws_type = raw[3]
    width = WEBSOCKET_VALUE_WIDTH.get(ws_type, 16)
    i = 4
    while i < 4 + width:
        if i >= len(raw):
            raise ValueError(f"websocket response shorter than its scan window: {len(raw)}")
        if raw[i] == 0:
            break
        i += 1
    else:
        i = 4
    end = i if i != 4 else 4 + width
    if end > len(raw):
        raise ValueError(f"websocket response shorter than its value width: {len(raw)}")
    return {
        "type": ws_type,
        "name": WEBSOCKET_TYPE_NAMES.get(ws_type),
        "content": raw[4:end].decode("utf-8", "replace"),
    }


def parse_sync_time_request(data: bytes) -> dict[str, int]:
    """Parse a 9-byte m7 request, decoding both timezone fields as int8."""
    raw = bytes(data)
    if len(raw) != 9 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_SYNC_TIME:
        raise ValueError(f"not an m7 syncTime request: {raw.hex()}")

    def i8(b: int) -> int:
        return b - 256 if b > 127 else b

    return {
        "stamp": unpack_from("<I", raw, 3)[0],
        "tz_hours": i8(raw[7]),
        "tz_mins": i8(raw[8]),
    }


class PlaudPeripheral:
    """A Bumble peripheral speaking the recovered cleartext control protocol.

    Dispatch mirrors the device's obligation rather than the host's: the
    request's [u8 protocolType][u16le opcode] header selects the handler, so
    any well-formed request is answered regardless of its payload values. An
    earlier version compared whole requests against fixed byte strings, which
    made syncTime answerable only for one hard-coded timestamp.
    """

    def __init__(
        self,
        device: Any,
        state: PlaudDeviceState | None = None,
        storage: PlaudStorageState | None = None,
        synctime: PlaudSyncTimeState | None = None,
        battery: PlaudBatteryState | None = None,
        file_bytes: bytes = b"",
        file_table: list[dict[str, int]] | None = None,
        tail_crc: int = 0x1234,
        resume_start: int = 0,
        resume_status: int = 0,
        resume_start_time: int = 0,
        port_version: int = PORT_VERSION,
        scan_fields: ScanFields | None = None,
        accept_any_token: bool = True,
        handshake_status: int = 0,
        feature_bitmap: int = 0,
        common_settings: dict[int, int] | None = None,
        l3_timezone: int = 0,
        data_payload_size: int | None = None,
        file_list_per_frame: int | None = None,
        drop_data_offsets: tuple[int, ...] = (),
        empty_package_code: int | None = DEFAULT_EMPTY_PACKAGE_CODE,
        stream_in_task: bool = False,
        response_pacing_s: float = 0.0,
        wifi_open_status: int = 0,
        wifi_close_status: int = 0,
        wifi_busy_status: int = WIFI_OPEN_STATUS_BUSY,
        websocket_profile: dict[int, str] | None = None,
    ) -> None:
        if port_version >= ENCRYPTED_PORT_VERSION:
            raise ValueError(
                f"portVersion {port_version} >= {ENCRYPTED_PORT_VERSION}: the SDK would "
                "ChaCha20-Poly1305-seal every frame on this link, which this peripheral "
                "does not implement. Emulate a cleartext device instead."
            )
        self.device = device
        self.port_version = port_version
        # What this device DECLARES about itself on the air. portVersion lives
        # here and nowhere else: the SDK reads it at scan time and never
        # refreshes it, so the advertisement is what decides whether the link
        # is expected to be encrypted. Keep it consistent with port_version.
        self.scan_fields = scan_fields or ScanFields(
            project_code=881,
            version_type="V",
            version_code=1,
            serial_number="8810000001",
            bind_info=0,
            port_version=port_version,
        )
        self.state = state or PlaudDeviceState()
        self.storage = storage or PlaudStorageState()
        self.synctime = synctime or PlaudSyncTimeState()
        self.battery = battery or PlaudBatteryState()
        self.file_bytes = bytes(file_bytes)
        self.file_table = FileTable(list(file_table or []), port_version=port_version)
        self.file_list_per_frame = file_list_per_frame
        self.tail_crc = tail_crc
        self.resume_start = resume_start
        self.resume_status = resume_status
        self.resume_start_time = resume_start_time
        self.data_payload_size = data_payload_size
        self.drop_data_offsets = tuple(drop_data_offsets)
        # R7-S13: the sentinel that closes a transfer for the genuine client
        # (RUNTIME_PROVEN); the code VALUE is HARNESS_POLICY (real firmware's
        # is UNKNOWN); None reproduces the pre-R7-S13 never-completing sequence.
        self.empty_package_code = empty_package_code
        # R7-S13 §4.4 HARNESS_POLICY: stream a transfer from a cancellable task
        # with inter-frame pacing. Zero-latency inline emission races the real
        # phone's GATT write callback (the tinnotech op-queue registers its
        # [28,29] response bean after the HEAD has already been delivered ->
        # -98/-99), and an in-flight stream must be abandoned when the client
        # re-issues syncFileStart from its cursor or sends stopSync, or the
        # client's gap recovery never converges (run 6 vs 6b). In-process
        # Bumble tests keep the synchronous inline path (default).
        self.stream_in_task = stream_in_task
        self.response_pacing_s = response_pacing_s
        self._stream_task: Any = None
        self.stream_log: list[dict[str, Any]] = []
        # HARNESS POLICY, stated as such: this device accepts any handshake
        # token. Real hardware certainly does not. `handshake_status` lets a
        # test drive the failure path (`s0$d.a(status)` on the client side).
        self.accept_any_token = accept_any_token
        self.handshake_status = handshake_status
        self.handshake_log: list[dict[str, object]] = []
        # HARNESS_POLICY: which capability bits this synthetic device claims.
        # Bit 3 (Wi-Fi AES) is the only bit with a recovered consumer; the
        # other seven are advertised by the host but no SDK reader was found.
        self.feature_bitmap = feature_bitmap
        self.feature_log: list[dict[str, object]] = []
        # HARNESS_POLICY: the CommonSettings values reported for opcode-8 READs
        # (and overwritten by SETTINGs). Types not in the table read as 0.
        self.common_settings: dict[int, int] = dict(
            DEFAULT_COMMON_SETTINGS if common_settings is None else common_settings
        )
        self.settings_log: list[dict[str, object]] = []
        # HARNESS_POLICY: the l3 timezone byte (offset 6). The SDK caches it in
        # q.n via l3.e() and the public facade forwards q.n as bleBind's
        # `protVersion` argument (ALL.txt:44308, :878-961) -- a facade quirk.
        self.l3_timezone = l3_timezone
        # HARNESS_POLICY: the Wi-Fi handoff answers. `wifi_open_status` is what
        # opcode 10 reports when idle; `wifi_busy_status` (CLAIM: 4) when a
        # transfer is mid-stream; `wifi_close_status` answers opcode 13. The
        # WebSocket profile (opcode 16/17) is a synthetic url/serToken/devToken
        # triple that SET overwrites and GET reads back. Nothing here raises a
        # SoftAP; `wifi_hotspot_on` only records what the phone asked for.
        self.wifi_open_status = wifi_open_status
        self.wifi_close_status = wifi_close_status
        self.wifi_busy_status = wifi_busy_status
        self.wifi_hotspot_on = False
        self.websocket_profile: dict[int, str] = dict(
            DEFAULT_WEBSOCKET_PROFILE if websocket_profile is None else websocket_profile
        )
        self.wifi_log: list[dict[str, object]] = []
        self.transfer: TransferSession | None = None
        self.packet_log: list[dict[str, Any]] = []
        self.lifecycle = PlaudLifecycle.DISCONNECTED
        self.lifecycle_log: list[str] = []
        self.connection: Any = None
        device.on(Device.EVENT_CONNECTION, self._on_device_connection)

        # 2BB0 property mask is a VIRTUAL-TEST choice, not a hardware claim.
        # z.a(boolean) writes ENABLE_INDICATION_VALUE or ENABLE_NOTIFICATION_VALUE
        # to the CCCD depending on the discovered properties, so the real client
        # copes with either. Advertising both lets a test drive each path.
        self.data_characteristic = Characteristic[bytes](
            DATA_NOTIFY_UUID,
            Characteristic.Properties.NOTIFY | Characteristic.Properties.INDICATE,
            Attribute.READABLE,
        )
        # WRITE (with response) is a VIRTUAL-TEST choice: z.d calls
        # BluetoothGatt.writeCharacteristic without pinning the write type.
        self.command_characteristic = Characteristic[bytes](
            COMMAND_WRITE_UUID,
            Characteristic.Properties.WRITE,
            Attribute.WRITEABLE,
            CharacteristicValue(write=self._on_command_write),
        )
        self.service = Service(
            PLAUD_SERVICE_UUID,
            [self.data_characteristic, self.command_characteristic],
        )
        # z's pre-handshake reads and subscribes 0x2A19 before 2BB0 on the
        # "old battery service" path, so the emulator publishes it.
        self.battery_characteristic = Characteristic[bytes](
            GATT_BATTERY_LEVEL_CHARACTERISTIC,
            Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
            Attribute.READABLE,
            bytes([self.battery.level & 0xFF]),
        )
        self.battery_service = Service(GATT_BATTERY_SERVICE, [self.battery_characteristic])

    def push_new_feature(self, bitmap: int | None = None, connection: Any = None):
        """Emit the unsolicited DEV_NEW_FEATURE_REQ capability push (opcode 138).

        This is a real device-initiated message the emulator previously did not
        speak. On receipt the SDK (a) forwards the payload to
        `listener.deviceNewFeature`, (b) sets its Wi-Fi AEAD selector from bit
        3, and (c) replies with its own FeatureReq -- so it is a genuine
        round-trip that exercises host logic the harness could not reach before.

        The SDK skips the 3-byte header before reading the bitmap
        (`if (bArr.length > 3) copyOfRange(bArr, 3, len)`), so a frame with no
        payload is logged as "数据长度不足" and leaves the selector untouched.
        """
        value = self.feature_bitmap if bitmap is None else bitmap
        frame = encode_feature_frame(value)
        self.feature_log.append({"direction": "device->host", "bitmap": value})
        target = self._live_connection(connection)
        return self._emit(target, frame)

    def push_battery(self, battery: PlaudBatteryState, connection: Any = None):
        """Emit an unsolicited opcode-9 frame, as the real device does.

        `q$f.a(byte[])` has a push handler for opcode 9 that updates the cached
        charging/level values behind PlaudDeviceAgent.getChargingState(), which
        on Android puts nothing on the wire of its own.
        """
        self.battery = battery
        return self._emit(self._live_connection(connection), battery.encode())

    def _live_connection(self, connection: Any = None) -> Any:
        """The connection to push an unsolicited frame on.

        A peripheral built after the link came up (which the test helpers do)
        never saw the connection event, so fall back to whatever the Bumble
        device currently holds.
        """
        target = connection if connection is not None else self.connection
        if target is None:
            live = list(getattr(self.device, "connections", {}).values())
            target = live[0] if live else None
        if target is None:
            raise RuntimeError("no connection to push on")
        return target

    # --- lifecycle -------------------------------------------------------

    def install(self) -> None:
        self.device.add_service(self.service)
        self.device.add_service(self.battery_service)
        self._set_lifecycle(PlaudLifecycle.GATT_READY)
        # A real peripheral publishes GATT before anyone connects, and the
        # EVENT_CONNECTION handler then takes it to CONNECTED. Some test helpers
        # establish the link first and build the peripheral afterwards, which
        # means that event has already fired and been missed; adopt the live
        # connection so the lifecycle is not stuck at GATT_READY.
        live = list(getattr(self.device, "connections", {}).values())
        if live:
            self._on_device_connection(live[0])

    def manufacturer_data(self) -> bytes:
        """The manufacturer-specific advertising blob this device would emit.

        Built so that `u4.a(ScanResult)` recovers `self.scan_fields`. The
        manufacturer ID itself is whatever key the blob is filed under in the
        scan record; the SDK reads `keyAt(0)` and stores it without ever
        comparing it, so it does not filter on it.
        """
        f = self.scan_fields
        serial = bytes.fromhex(f.serial_number) if f.serial_number else b""
        return build_manufacturer_data(
            project_code=f.project_code,
            version_type=f.version_type,
            version_code=f.version_code,
            serial=serial,
            port_version=f.port_version,
            bind_info=f.bind_info,
        )

    async def advertise(self, name: str | None = None) -> None:
        """Start advertising with the Plaud service UUID and manufacturer data.

        The SDK does NOT filter on the service UUID -- it hands `startScan` an
        empty ScanFilter list and filters in software on the manufacturer data
        -- but advertising the service is what lets ordinary BLE tooling (and
        a Bumble central) find the device at all.
        """
        from bumble.core import AdvertisingData

        self.device.advertising_data = bytes(
            AdvertisingData(
                [
                    (
                        AdvertisingData.COMPLETE_LOCAL_NAME,
                        bytes(name or self.scan_fields.product_name or "Plaud", "utf-8"),
                    ),
                    (
                        AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
                        bytes.fromhex("1019"),  # 0x1910 little-endian
                    ),
                    (
                        AdvertisingData.MANUFACTURER_SPECIFIC_DATA,
                        self.manufacturer_data(),
                    ),
                ]
            )
        )
        await self.device.start_advertising()

    def _set_lifecycle(self, next_state: PlaudLifecycle) -> None:
        self.lifecycle = next_state
        self.lifecycle_log.append(next_state.value)

    def _on_device_connection(self, connection: Connection) -> None:
        self.connection = connection
        self._set_lifecycle(PlaudLifecycle.CONNECTED)
        connection.on(
            Connection.EVENT_DISCONNECTION,
            lambda *_: (
                setattr(self, "connection", None),
                self._set_lifecycle(PlaudLifecycle.DISCONNECTED),
            ),
        )

    # --- command dispatch ------------------------------------------------

    async def _on_command_write(self, connection: Any, value: bytes) -> None:
        request = bytes(value)
        self.packet_log.append({"direction": "request", "bytes": request.hex()})

        if len(request) < 3 or request[0] != 1:
            return self._reject(request, "not_a_type_1_control_frame")
        opcode = unpack_from("<H", request, 1)[0]

        handlers = {
            OPCODE_HANDSHAKE: self._handshake,
            OPCODE_GET_SSN: self._get_ssn,
            OPCODE_GET_STATE: self._get_state,
            OPCODE_GET_STORAGE: self._get_storage,
            OPCODE_SYNC_TIME: self._sync_time,
            OPCODE_BATT_STATUS: self._batt_status,
            OPCODE_SYNC_START: self._start_transfer,
            OPCODE_FILE_LIST: self._serve_file_list,
            OPCODE_RESUME: self._answer_resume,
            OPCODE_STOP_SYNC: self._stop_transfer,
            OPCODE_DELETE_FILE: self._delete_file,
            OPCODE_NEW_FEATURE: self._feature_request,
            OPCODE_COMMON_SETTINGS: self._common_settings,
            OPCODE_OPEN_WIFI: self._open_wifi,
            OPCODE_CLOSE_WIFI: self._close_wifi,
            OPCODE_WEBSOCKET_REQ: self._websocket_profile,
        }
        handler = handlers.get(opcode)
        if handler is None:
            return self._reject(request, "unsupported_opcode")
        if opcode in (OPCODE_SYNC_START, OPCODE_STOP_SYNC):
            self._abort_stream("new_sync_start" if opcode == OPCODE_SYNC_START else "stop_sync")
        try:
            frames = handler(request)
        except ValueError as exc:
            return self._reject(request, f"malformed_request: {exc}")
        if opcode == OPCODE_SYNC_START and self.stream_in_task:
            self._stream_task = asyncio.ensure_future(self._stream(connection, frames))
            return None
        for frame in frames:
            await self._emit(connection, frame)

    # --- streamed transfers (R7-S13 policy, see __init__) -----------------

    def _abort_stream(self, reason: str) -> None:
        task = self._stream_task
        if task is not None and not task.done():
            task.cancel()
            self.stream_log.append({"event": "aborted", "reason": reason})
        self._stream_task = None

    async def _stream(self, connection: Any, frames: list[bytes]) -> None:
        sent = 0
        try:
            for frame in frames:
                await self._emit(connection, frame)
                sent += 1
                if self.response_pacing_s > 0:
                    await asyncio.sleep(self.response_pacing_s)
            self.stream_log.append({"event": "completed", "frames": sent})
        except asyncio.CancelledError:
            self.stream_log.append({"event": "cancelled", "frames_sent": sent, "frames_total": len(frames)})
            raise

    def _reject(self, request: bytes, reason: str) -> None:
        self.packet_log.append(
            {"direction": "error", "reason": reason, "bytes": request.hex()}
        )

    def _handshake(self, request: bytes) -> list[bytes]:
        """k3/j3 (opcode 1) -> l3.

        The layout the client used is selected by the portVersion this device
        ADVERTISED, so the emulator parses with its own value. On the first
        handshake the client registers waiters for both opcode 1 and opcode 2,
        which is why a device may answer either; this one answers l3.
        """
        parsed = parse_handshake_request(request, self.port_version)
        self.handshake_log.append(parsed)
        if not self.accept_any_token:
            raise ValueError("this peripheral is configured to refuse handshakes")
        status = self.handshake_status
        if status == 0:
            self._set_lifecycle(PlaudLifecycle.HANDSHAKED)
        return [
            encode_l3(
                status=status,
                port_version=self.port_version,
                timezone=self.l3_timezone,
                version_type=self.scan_fields.version_type,
                version=self.scan_fields.version_code,
            )
        ]

    def _common_settings(self, request: bytes) -> list[bytes]:
        """q0 CommonSettingsReq (opcode 8) -> r0 CommonSettingsRsp.

        Layout BYTECODE_PROVEN, the READ round-trip RUNTIME_PROVEN (the real
        SDK reads ENABLE_VAD and REC_MODE after getState). The VALUES are
        HARNESS_POLICY; a SETTING stores the new value and echoes it.
        """
        parsed = parse_common_settings_request(request)
        self.settings_log.append(parsed)
        type_wire = int(parsed["type"])
        if parsed["action"] == COMMON_ACTION_SET:
            self.common_settings[type_wire] = int(parsed["value"])
        value = self.common_settings.get(type_wire, 0)
        return [encode_common_settings_response(type_wire, value)]

    def _feature_request(self, request: bytes) -> list[bytes]:
        """l1 FeatureReq (opcode 138, host -> device) -> device acknowledgement.

        The host sends this unprompted after it receives the device's
        capability push, advertising all eight bits (`01 8A 00 FF`). The SDK
        only logs the response, so its payload is unconstrained; the emulator
        echoes its own bitmap, which is the least surprising choice.
        HARNESS_POLICY: the response payload.
        """
        parsed = parse_feature_request(request)
        self.feature_log.append({"direction": "host->device", **parsed})
        return [encode_feature_frame(self.feature_bitmap)]

    def _get_ssn(self, request: bytes) -> list[bytes]:
        """w2 (opcode 2) -> x2 / GetSsnRsp.

        The SSN must match the ADVERTISED serial number: `q` feeds the response
        straight into `checkSn`, and a mismatch raises SN_NOT_MATCH.
        """
        if len(request) != 3:
            raise ValueError(f"getSsn takes no payload, got {len(request)} bytes")
        return [
            encode_x2(
                self.scan_fields.serial_number,
                self.scan_fields.version_type,
                self.scan_fields.version_code,
            )
        ]

    def _get_state(self, request: bytes) -> list[bytes]:
        if len(request) != 3:
            raise ValueError(f"getState takes no payload, got {len(request)} bytes")
        return [self.state.encode()]

    def _get_storage(self, request: bytes) -> list[bytes]:
        if len(request) != 3:
            raise ValueError(f"getStorage takes no payload, got {len(request)} bytes")
        return [self.storage.encode()]

    def _batt_status(self, request: bytes) -> list[bytes]:
        if len(request) != 3:
            raise ValueError(f"battStatus takes no payload, got {len(request)} bytes")
        return [self.battery.encode()]

    def _sync_time(self, request: bytes) -> list[bytes]:
        parse_sync_time_request(request)  # validates shape; values are not echoed
        # The SDK fires bleBind off THIS callback, not off the handshake, so the
        # emulator's own notion of "bound" advances here too.
        if self.lifecycle == PlaudLifecycle.HANDSHAKED:
            self._set_lifecycle(PlaudLifecycle.BOUND)
        return [self.synctime.encode_response()]

    def _start_transfer(self, request: bytes) -> list[bytes]:
        """y6: (re)place the session, then emit HEAD + DATA + TAIL.

        Replacing an active session on a second y6, and emitting the whole
        sequence with no pacing, are HARNESS POLICY.
        """
        params = parse_sync_start_request(request)
        self.transfer = TransferSession(
            file_bytes=self.file_bytes,
            crc=self.tail_crc & 0xFFFF,
            port_version=self.port_version,
            empty_package_code=self.empty_package_code,
            **({"payload_size": self.data_payload_size} if self.data_payload_size else {}),
        )
        self.transfer.start(params["session_id"], params["start"], params["end"])
        return self.transfer.frames(drop_offsets=self.drop_data_offsets)

    def _serve_file_list(self, request: bytes) -> list[bytes]:
        """p2: echo the host's requestStamp and page the whole table.

        `start_session_id` and `flag` are parsed but do not filter the table:
        their semantics are UNKNOWN, and a deterministic full-table answer
        documents that rather than inventing a filter.
        """
        params = parse_file_list_request(request)
        return self.file_table.frames(params["request_stamp"], self.file_list_per_frame)

    def _stop_transfer(self, request: bytes) -> list[bytes]:
        """z6 (opcode 29 INBOUND): abandon the active transfer, ack with a7.

        The SDK sends this of its own accord the moment it sees a DATA offset
        gap, then re-issues syncFile from its cursor. A peripheral that ignores
        it leaves the client waiting on a response opcode 30 that never comes,
        so its recovery path stalls. Note the response opcode is 30, not 29.
        """
        parse_stop_sync_request(request)
        self.transfer = None
        return [pack_stop_sync_response()]

    def _delete_file(self, request: bytes) -> list[bytes]:
        """w6 (opcode 30 INBOUND): drop the entry from the table, ack with x6.

        The answer reports status 0 whether or not the entry existed --
        the device's real status codes are UNKNOWN.
        """
        params = parse_delete_file_request(request)
        self.file_table.entries = [
            e for e in self.file_table.entries if e["session_id"] != params["session_id"]
        ]
        return [
            pack_delete_file_response(
                params["session_id"], 0, port_version=self.port_version
            )
        ]

    # --- Wi-Fi handoff (opcodes 10 / 13 / 16 -> 17) --------------------------------

    def wifi_password(self) -> str:
        """The passphrase the PHONE computes for this serial: takeLast(8)
        (WifiAgentImpl.access$calculateWifiPassword, WifiAgentImpl.txt:1695-1703).
        Reported in the opcode-10 response for symmetry; the Wi-Fi agent discards
        the reported value and recomputes it anyway. HARNESS_POLICY: a serial
        shorter than 8 characters is left-padded with '0' to keep the field 8 bytes."""
        return self.scan_fields.serial_number[-WIFI_PASS_LEN:].rjust(WIFI_PASS_LEN, "0")

    def _open_wifi(self, request: bytes) -> list[bytes]:
        """i4 OpenWiFiReq (opcode 10) -> j4 OpenWiFiRsp.

        BYTECODE_PROVEN layouts; HARNESS_POLICY answers: status
        `wifi_open_status` (0) when idle, `wifi_busy_status` (4, CLAIM from the
        template comments) while a transfer is still streaming, and the 8-byte
        passphrase only on a successful open. `onOff == 0` is treated as a
        request to drop the hotspot (its real meaning is UNKNOWN; the shipped
        driver always sends the boolean it was given).
        """
        parsed = parse_open_wifi_request(request)
        on_off = parsed["on_off"]
        wants_on = True if on_off is None else bool(on_off)
        busy = self.transfer is not None and not self.transfer.done
        status = self.wifi_busy_status if (busy and wants_on) else self.wifi_open_status
        if status == 0:
            self.wifi_hotspot_on = wants_on
        self.wifi_log.append({"opcode": OPCODE_OPEN_WIFI, **parsed, "status": status, "hotspot_on": self.wifi_hotspot_on})
        wifi_pass = self.wifi_password() if (status == 0 and wants_on) else None
        return [encode_open_wifi_response(status, wifi_pass)]

    def _close_wifi(self, request: bytes) -> list[bytes]:
        """k0 (opcode 13, header only) -> l0 CloseWiFiRsp [u8 status].

        NiceBuildSdk.stopWifiTransfer sends this over BLE when BLE is up
        (NiceBuildSdk.txt:3315-3364) and only logs the status
        ("设备热点关闭响应 status=", :1354). HARNESS_POLICY status.
        """
        parse_close_wifi_request(request)
        self.wifi_hotspot_on = False
        self.wifi_log.append({"opcode": OPCODE_CLOSE_WIFI, "status": self.wifi_close_status, "hotspot_on": False})
        return [encode_close_wifi_response(self.wifi_close_status)]

    def _websocket_profile(self, request: bytes) -> list[bytes]:
        """d3 GET / b6 SET (opcode 16) -> v2 GetSetWebsocketRsp (opcode 17).

        GET returns the stored value; SET stores the value and echoes it back
        in the same response layout. That a SET is acknowledged with a type-17
        frame carrying the new value is HARNESS_POLICY: the SDK registers the
        same v2 parser for both (`v2."<init>"` at ALL.txt:40651 and :40692) and
        never inspects the content after a SET.
        """
        parsed = parse_websocket_request(request)
        ws_type = int(parsed["type"])
        if parsed["op"] == WEBSOCKET_OP_SET:
            self.websocket_profile[ws_type] = str(parsed["value"])
        value = self.websocket_profile.get(ws_type, "")
        self.wifi_log.append({"opcode": OPCODE_WEBSOCKET_REQ, **parsed, "stored": value})
        return [encode_websocket_response(ws_type, value)]

    def _answer_resume(self, request: bytes) -> list[bytes]:
        """b5: session and scene are echoed; other fields are synthetic."""
        params = parse_resume_record_request(request)
        return [
            pack_resume_record_response(
                params["session_id"],
                self.resume_start,
                self.resume_status,
                params["scene"],
                self.resume_start_time,
            )
        ]

    # --- transport -------------------------------------------------------

    async def _emit(self, connection: Any, payload: bytes) -> None:
        self.packet_log.append({"direction": "response", "bytes": payload.hex()})
        await self._respond(connection, payload)

    async def _respond(self, connection: Any, response: bytes) -> None:
        # Emulator compatibility behaviour, not a device claim: answer through
        # whichever subscription mode the central selected for 2BB0. Indication
        # (0x02) wins when set; otherwise notify.
        server = self.device.gatt_server
        cccd = server.subscribers.get(connection, {}).get(
            self.data_characteristic.handle, b"\x00\x00"
        )
        if cccd[0] & 0x02:
            await server.indicate_subscriber(connection, self.data_characteristic, response)
        else:
            await server.notify_subscriber(connection, self.data_characteristic, response)


#: Retained so existing callers keep working; the old name said "R1a" long
#: after the peripheral outgrew R1a.
PlaudR1aPeripheral = PlaudPeripheral
