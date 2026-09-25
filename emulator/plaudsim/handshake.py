"""Handshake framing codecs and response parsers (R2).

Source-backed packet machinery WITHOUT authentication semantics. Every layout
below is cited to bytecode in `build/evidence/javap/`; see
`docs/protocol-ledger.md` for the full evidence rows.

TWO DIFFERENT FRAMINGS live on characteristic 2BB0/2BB1, and conflating them
was the single largest error in the previous reconstruction:

1. MARKER FRAMES (pre-handshake, 0xFE10/0xFE11/0xFE12/0xFE20)
       [u16le marker][u8 chunk_count][u8 chunk_index][chunk <=100 B]
   There is NO leading protocol-type byte. v4.enPkg()/w4.enPkg() override
   enPkg() and never call packHead(); the receive side reads the marker with
   `TntBleCommUtils.b(value, 0)` -- a u16 at offset ZERO -- in both
   z$c.onCharacteristicChanged and q.n's response lambda.

2. CONTROL FRAMES (the ordinary request/response protocol)
       [u8 protocolType=1][u16le opcode][payload...]
   Built by l.packHead(); validated by n.<init> which compares the u16le at
   offset 1 against the subclass opcode. NOTE: n.<init> never checks byte 0.

Deliberately ABSENT: k3/j3 request building with real credential values
(the token is an account-issued 32-hex string), RSA/ChaCha key material.
Nothing here can produce a bind.
"""

from __future__ import annotations

from struct import pack, unpack_from

PROTOCOL_TYPE = 1

# w$c.a / w$c.b / w$c.c and w$a.a — DIRECT constants from com/plaud/sdk/proto/w.java.
MARKER_PRE_HANDSHAKE = 0xFE10   # w$c.a  — host->device, v4 with forceClear=false
MARKER_RSA_MARKER = 0xFE11      # w$a.a  — device->host, "send me your RSA public key"
MARKER_SECRET = 0xFE12          # w$c.c / w$a.b — both directions: public key up, secret down
MARKER_FORCE_CLEAR = 0xFE20     # w$c.b  — host->device, v4 with forceClear=true
                                # PlaudDeviceAgent names it PRE_HANDSHAKE_AND_CLEAR.

MARKERS = (MARKER_PRE_HANDSHAKE, MARKER_RSA_MARKER, MARKER_SECRET, MARKER_FORCE_CLEAR)

# q.n / q.a0: `int length = (bytes.length + 99) / 100;` then copyOfRange(i*100, ...).
CHUNK_SIZE = 100
MARKER_HEADER_LEN = 4

HANDSHAKE_OPCODE = 1   # l3 / k3 / j3
SSN_OPCODE = 2         # x2


def chunk_bytes(data: bytes, size: int = CHUNK_SIZE) -> list[bytes]:
    """Split a payload the way q.n/q.a0 do: count = (len + size - 1) // size."""
    if not data:
        return []
    count = (len(data) + size - 1) // size
    return [data[i * size : min((i + 1) * size, len(data))] for i in range(count)]


def pack_marker_frame(marker: int, count: int, index: int, chunk: bytes) -> bytes:
    """Build one v4/w4 marker frame.

    Layout (v4.enPkg / w4.enPkg bytecode): the merge order is
      a(getBleRequestType())  -> u16le marker
      c(this.b)               -> u8, the ctor's SECOND arg
      c(this.a)               -> u8, the ctor's FIRST arg
      this.c                  -> the chunk
    and q.n constructs `new v4(i, length, chunk, forceClear)` while q.a0
    constructs `new w4(i, length, chunk)`, so the second arg is the COUNT and
    the first is the INDEX. Wire order is therefore count, then index.
    """
    if not 0 <= marker <= 0xFFFF:
        raise ValueError(f"marker out of u16 range: {marker:#x}")
    for name, value in (("count", count), ("index", index)):
        if not 0 <= value <= 0xFF:
            raise ValueError(f"{name} out of u8 range: {value}")
    return b"".join((pack("<H", marker), pack("<B", count), pack("<B", index), bytes(chunk)))


def parse_marker_frame(frame: bytes) -> dict[str, object]:
    """Parse one v4/w4 marker frame.

    Mirrors the receive side in z$c.onCharacteristicChanged: marker = u16le@0,
    count = value[2], index = value[3], payload = value[4:]. The SDK's own
    length guard is `value2.length < 4`.
    """
    data = bytes(frame)
    if len(data) < MARKER_HEADER_LEN:
        raise ValueError(f"marker frame too short: {len(data)}")
    return {
        "marker": unpack_from("<H", data, 0)[0],
        "count": data[2],
        "index": data[3],
        "chunk": data[4:],
    }


def reassemble_secret_chunks(frames: list[bytes]) -> bytes:
    """Reassemble 0xFE12 chunks exactly as z$c.onCharacteristicChanged does.

    The SDK: drops a frame byte-identical to one already held, sorts the held
    frames by `bArr[3] & 255` (the index), and concatenates `frame[4:]` once
    `G.size() == I` where I is the count byte of the most recent frame.

    Raises ValueError if the set is not exactly `count` distinct frames --
    the SDK simply waits instead, which is not expressible as a return value.
    """
    seen: list[bytes] = []
    count = None
    for raw in frames:
        data = bytes(raw)
        if len(data) < MARKER_HEADER_LEN:
            raise ValueError(f"marker frame too short: {len(data)}")
        count = data[2]          # z: I = value2[2], overwritten by every frame
        if any(data == held for held in seen):
            continue             # z: "已存在该包，跳过添加"
        seen.append(data)
    if count is None:
        raise ValueError("no frames")
    if len(seen) != count:
        raise ValueError(f"incomplete: {len(seen)} distinct frames, count says {count}")
    seen.sort(key=lambda f: f[3] & 0xFF)
    return b"".join(f[4:] for f in seen)


# --- secret package ------------------------------------------------------
# z$c: after RSA-decrypting the reassembled 0xFE12 payload with
# `userRSAPrivateKey`, the SDK requires len >= 56 and slices:
#     J = plain[0:32]   ChaCha20 key
#     K = plain[32:44]  12-byte nonce (IvParameterSpec)
#     L = plain[44:56]  12-byte associated data (updateAAD)
# then decrypts plain[56:] under (J, K, L) and requires the result to equal
# PlaudEncryptHeader.MAGIC_STRING == "PLAUD.AI".
SECRET_KEY_LEN = 32
SECRET_NONCE_LEN = 12
SECRET_AAD_LEN = 12
SECRET_MIN_LEN = SECRET_KEY_LEN + SECRET_NONCE_LEN + SECRET_AAD_LEN  # 56
SECRET_MAGIC = b"PLAUD.AI"


def split_secret_package(plaintext: bytes) -> dict[str, bytes]:
    """Split an RSA-decrypted secret package into (key, nonce, aad, sealed_magic).

    Pure slicing; performs no crypto. Mirrors the z$c length guard exactly:
    the SDK aborts the handshake when len < 56.
    """
    raw = bytes(plaintext)
    if len(raw) < SECRET_MIN_LEN:
        raise ValueError(f"secret package too short: {len(raw)} < {SECRET_MIN_LEN}")
    return {
        "chacha_key": raw[0:32],
        "chacha_nonce": raw[32:44],
        "chacha_aad": raw[44:56],
        "sealed_magic": raw[56:],
    }


# --- post-handshake ChaCha envelope (portVersion >= 20) ------------------
# z.d(byte[]) outbound:   ChaCha20-Poly1305(J, nonce=K, aad=L) over
#                         [u32le ++M][request bytes], written to 2BB1.
# z$c inbound:            decrypt, require len >= 4, seq = u32le@0, DROP when
#                         seq <= N (N starts at -1), then parse plain[4:].
# The SAME nonce K is used for every message in both directions: that is what
# the shipped SDK does, and an interoperating emulator must match it.
ENVELOPE_SEQ_LEN = 4


def wrap_envelope_plaintext(seq: int, inner: bytes) -> bytes:
    """Build the pre-encryption plaintext: [u32le seq][inner control frame]."""
    if not 0 <= seq <= 0xFFFFFFFF:
        raise ValueError(f"seq out of u32 range: {seq}")
    return pack("<I", seq) + bytes(inner)


def unwrap_envelope_plaintext(plaintext: bytes) -> dict[str, object]:
    """Split a decrypted envelope. Mirrors the `b3.length < 4` guard in z$c."""
    raw = bytes(plaintext)
    if len(raw) < ENVELOPE_SEQ_LEN:
        raise ValueError(f"envelope plaintext too short: {len(raw)}")
    return {"seq": unpack_from("<I", raw, 0)[0], "inner": raw[ENVELOPE_SEQ_LEN:]}


class ReplayWindow:
    """The monotonic receive counter from z$c: `if (N >= d) return; N = d;`.

    z.N is a static int initialised to -1, so the first accepted sequence
    number is 0. Frames that do not strictly increase are DROPPED silently.
    """

    def __init__(self, last: int = -1) -> None:
        self.last = last

    def accept(self, seq: int) -> bool:
        if self.last >= seq:
            return False
        self.last = seq
        return True


def select_handshake_response(data: bytes) -> str:
    """Classify an inbound frame the way the SDK's two receive paths do.

    Marker frames are recognised FIRST, by the u16le at offset 0
    (z$c pre-handshake path, and q.n's `b(bArr2, 0) == 65041` check).
    Control frames are recognised by byte 0 == 1 with the u16le opcode at
    offset 1 -- exactly the `if (a(bArr2,0) == 1) { b2 = b(bArr2,1); ... }`
    chain in q.a's handshake callback, where 1 -> l3, 2 -> x2 and anything
    else is HANDSHAKE_FAIL.

    Returns 'marker', 'l3' or 'x2'.
    """
    raw = bytes(data)
    if len(raw) < 3:
        raise ValueError(f"handshake frame too short: {len(raw)}")
    if unpack_from("<H", raw, 0)[0] in MARKERS:
        if len(raw) < MARKER_HEADER_LEN:
            raise ValueError(f"marker frame too short: {len(raw)}")
        return "marker"
    if raw[0] != PROTOCOL_TYPE:
        raise ValueError(f"not a type-1 control frame and not a marker: {raw[0]:#x}")
    word = unpack_from("<H", raw, 1)[0]
    if word == HANDSHAKE_OPCODE:
        return "l3"
    if word == SSN_OPCODE:
        return "x2"
    raise ValueError(f"unknown handshake opcode: {word:#x}")


# l3 <init> assigns these BEFORE parsing and returns early on short frames,
# so a truncated l3 leaves the Java defaults in place -- not nulls.
L3_DEFAULTS: dict[str, object] = {
    "timezone_min": 0,       # e = 0
    "audio_channel": 1,      # f = 1   <- not 0
    "support_wifi": False,   # g
    "no_ns_agc": False,      # h
    "is_ogg_audio": False,   # i
    "version_type": "V",     # j = "V"
    "version": 0,            # k = 0
}


def parse_l3(data: bytes) -> dict[str, object]:
    """Parse a handshake response (l3, opcode 1).

    Offsets from l3.<init>: status u8@3, portVersion u16le@4, timezone u8@6;
    then a nested guard chain len>=8 -> timezoneMin u8@7, len>=9 ->
    audioChannel u8@8, len>=10 -> supportWifi u8@9, len>=11 -> noNsAgc u8@10,
    len>=12 -> isOggAudio u8@11 and the trailing version block.

    versionType = chr(raw[-4]); version is the LITTLE-ENDIAN u24 built from
    the last three bytes:
        k = (b[len-3]&255) | ((b[len-2]&255)<<8) | ((b[len-1]&255)<<16)
    Names come from the l3.toString() format literal. `version` is rendered as
    "%04d" next to versionType to form versionName (BleDevice.getVersionName),
    so its real range is 0..9999.

    Fields not reached by the guard chain carry the SDK's Java defaults
    (L3_DEFAULTS), because that is what the real client would observe.
    """
    raw = bytes(data)
    if len(raw) < 3:
        raise ValueError(f"l3 response too short: {len(raw)}")
    if raw[0] != PROTOCOL_TYPE or unpack_from("<H", raw, 1)[0] != HANDSHAKE_OPCODE:
        raise ValueError("not an opcode-1 handshake response")
    if len(raw) < 7:
        # b/c/d are unguarded in Java and would throw ArrayIndexOutOfBounds.
        raise ValueError(f"l3 fixed prefix unreadable: {len(raw)}")
    out: dict[str, object] = dict(L3_DEFAULTS)
    out.update(
        {
            "status": raw[3],
            "port_version": unpack_from("<H", raw, 4)[0],
            "timezone": raw[6],
        }
    )
    if len(raw) >= 8:
        out["timezone_min"] = raw[7]
    if len(raw) >= 9:
        out["audio_channel"] = raw[8]
    if len(raw) >= 10:
        out["support_wifi"] = raw[9] == 1
    if len(raw) >= 11:
        out["no_ns_agc"] = raw[10] == 1
    if len(raw) >= 12:
        out["is_ogg_audio"] = raw[11] == 1
        out["version_type"] = chr(raw[-4])
        out["version"] = raw[-3] | (raw[-2] << 8) | (raw[-1] << 16)
    return out


# x2.<init>: the SSN scan runs `for (i = 3; i < 59; i++) if (bArr[i] == 0) break;`
# and resets i to 3 when it falls off the end, so the SSN field occupies bytes
# 3..58 and a NUL at or after offset 59 is never seen.
SSN_SCAN_END = 59
SSN_DEFAULT = "0" * 28
VERSION_NAME_TYPE_OFFSET = 59
VERSION_NAME_CODE_OFFSET = 60


def parse_x2(data: bytes) -> dict[str, object]:
    """Parse an SSN response (x2 / GetSsnRsp, opcode 2).

    ssn  = ASCII from offset 3 up to the first NUL strictly before offset 59;
           when no NUL is found there, the SDK substitutes 28 ASCII zeros.
    versionName = the single char at FIXED offset 59 concatenated with the
           u24le at FIXED offset 60 formatted "%04d" -- e.g. "V0123". It is
           NOT the bytes following the NUL. This matches
           BleDevice.getVersionName(), which builds the same string from the
           advertised versionType and versionCode.
    versionName is absent unless len > 59.
    """
    raw = bytes(data)
    if len(raw) < 3:
        raise ValueError(f"x2 response too short: {len(raw)}")
    if raw[0] != PROTOCOL_TYPE or unpack_from("<H", raw, 1)[0] != SSN_OPCODE:
        raise ValueError("not an opcode-2 SSN response")
    end = 3
    while end < SSN_SCAN_END:
        if end >= len(raw):
            # Java would throw ArrayIndexOutOfBounds here; surface it as an error
            # rather than inventing a shorter scan.
            raise ValueError(f"x2 frame shorter than the SSN scan window: {len(raw)}")
        if raw[end] == 0:
            break
        end += 1
    else:
        end = 3
    ssn = SSN_DEFAULT if end == 3 else raw[3:end].decode("ascii", "replace")

    version_name = None
    if len(raw) > VERSION_NAME_TYPE_OFFSET:
        if len(raw) < VERSION_NAME_CODE_OFFSET + 3:
            raise ValueError(
                f"x2 frame has a versionType byte but no u24 versionCode: {len(raw)}"
            )
        code = (
            raw[VERSION_NAME_CODE_OFFSET]
            | (raw[VERSION_NAME_CODE_OFFSET + 1] << 8)
            | (raw[VERSION_NAME_CODE_OFFSET + 2] << 16)
        )
        version_name = chr(raw[VERSION_NAME_TYPE_OFFSET]) + f"{code:04d}"
    return {"ssn": ssn, "version_name": version_name}


# =========================================================================
# Device side of the LEGACY (portVersion < 20) handshake
# =========================================================================
#
# This is the part of R2 that is genuinely reachable, and it is worth being
# precise about why.
#
# `q.b0()` branches on the ADVERTISED portVersion: `>= 20` runs the RSA
# pre-handshake (`q.n()`), below it goes straight to `q.a()` -- first_handshake.
# And `q.a()`'s only precondition is `!TextUtils.isEmpty(this.g)`, the token
# string the caller passed to `connectBleDevice`. No network call, no key, no
# signature, no cryptography of any kind.
#
# So on the legacy path the handshake is: **the host sends a token, and the
# DEVICE decides whether to accept it.** The secret belongs to the device, not
# to the client.
#
# That asymmetry is the whole story for this project. Binding a *client we
# wrote* to a *real Plaud device* is blocked, because we cannot mint a token the
# device will accept. Binding *Plaud's real client* to a *device we wrote* is
# not blocked at all, because accepting the token is our decision to make.
#
# Accepting any token is therefore HARNESS POLICY, and it is stated as such --
# it is emphatically NOT a claim that real hardware accepts any token.

K3_CONST_AT_3 = 0x02        # hard-coded literal in k3.enPkg; no reader found
K3_STAGE_FIRST = 0
K3_STAGE_SECOND = 1
J3_DEV_TOKEN_LEN = 8


def token_width(port_version: int) -> int:
    """k3 pads/truncates the token to 32 chars when portVersion >= 9, else 16."""
    return 32 if port_version >= 9 else 16


def parse_handshake_request(data: bytes, port_version: int = 7) -> dict[str, object]:
    """Parse a k3 or j3 handshake request as the DEVICE would see it.

    The device knows which layout to expect because `q` uses the portVersion it
    read from *this device's own advertisement* -- so the emulator's advertised
    value selects the layout on both sides.

        [0]     u8    protocolType = 1
        [1..2]  u16le opcode = 1
        [3]     u8    constant 0x02
        [4]     u8    z.h(), stubbed to 0 in the shipped build
        [5]     u8    stage: 0 = k3 (first), 1 = j3 (second)  -- ONLY if pv >= 3
        [6..]   ASCII token, right-padded with '0' to token_width(pv)
    and j3 additionally appends
        [+0..+7] devToken, zero-padded/truncated to 8 bytes
        [+8]     u8 len(userName)
        [+9..]   userName

    Returns the parsed fields plus `is_second`, which distinguishes the two.
    """
    raw = bytes(data)
    if len(raw) < 3:
        raise ValueError(f"handshake request too short: {len(raw)}")
    if raw[0] != PROTOCOL_TYPE or unpack_from("<H", raw, 1)[0] != HANDSHAKE_OPCODE:
        raise ValueError("not an opcode-1 handshake request")
    if len(raw) < 5:
        raise ValueError(f"handshake request too short for its fixed prefix: {len(raw)}")
    if raw[3] != K3_CONST_AT_3:
        raise ValueError(f"expected the constant 0x02 at offset 3, got {raw[3]:#04x}")

    has_stage = port_version >= 3
    stage = raw[5] if has_stage else K3_STAGE_FIRST
    token_at = 6 if has_stage else 5
    width = token_width(port_version)
    if len(raw) < token_at + width:
        raise ValueError(
            f"handshake request too short for a {width}-char token: {len(raw)}"
        )
    token = raw[token_at : token_at + width].decode("ascii", "replace")

    out: dict[str, object] = {
        "agent_value": raw[4],
        "stage": stage,
        "is_second": stage == K3_STAGE_SECOND,
        "token": token,
        "dev_token": None,
        "user_name": None,
    }
    tail = raw[token_at + width :]
    if tail:
        # j3's extra block. The shipped wrappers always append it, because the
        # null short-circuit needs BOTH extra strings to be null and they pass
        # empty strings instead.
        if len(tail) < J3_DEV_TOKEN_LEN + 1:
            raise ValueError(f"handshake tail too short for the j3 block: {len(tail)}")
        out["dev_token"] = tail[:J3_DEV_TOKEN_LEN].rstrip(b"\x00").decode("ascii", "replace")
        name_len = tail[J3_DEV_TOKEN_LEN]
        out["user_name"] = tail[J3_DEV_TOKEN_LEN + 1 : J3_DEV_TOKEN_LEN + 1 + name_len].decode(
            "ascii", "replace"
        )
    return out


# --- host-side k3 construction (R7-S12) ------------------------------------
# The SDK's recovery entry derives the k3 token by a pure LOCAL string transform
# of historicalUserId (ALL.txt:2678-2699): strip a leading "client_user_", then
# drop every '-'. An empty result aborts with bleConnectState(2) and writes
# NOTHING to 2BB1 (RUNTIME_PROVEN, R7-S12 run 2). k3.enPkg then emits
# packHead [01][01 00] + 0x02 + z.h() + (stage iff pv>=3) + the token '0'-padded
# or truncated to token_width(pv) (ALL.txt:87361-87434). u4.b() strips trailing
# 0x00 only, so an ASCII token's 0x30 padding survives. The bytes below were
# matched byte-for-byte against the genuine SDK write captured over netsim
# (RUNTIME_PROVEN + EMULATOR_INTEGRATION_PROVEN, R7-S12 runs 1 and 3).
HISTORICAL_ID_PREFIX = "client_user_"


def normalize_historical_id(historical_id: str) -> str:
    """recoveryConnectBleDevice's token derivation: removePrefix then drop '-'."""
    s = historical_id
    if s.startswith(HISTORICAL_ID_PREFIX):
        s = s[len(HISTORICAL_ID_PREFIX):]
    return s.replace("-", "")


def build_k3(
    token: str,
    port_version: int,
    stage: int = K3_STAGE_FIRST,
    agent_value: int = 0,
) -> bytes:
    """Byte-exact first-handshake (k3) frame as the shipped SDK builds it.

    `agent_value` is z.h(), a hard 0 in this build (ALL.txt:76051-76054). The
    token is truncated to, then right-padded with '0' (0x30) up to,
    token_width(port_version). The j3 tail is not appended here.
    """
    width = token_width(port_version)
    tok = token[:width].ljust(width, "0").encode("ascii")
    head = (
        bytes([PROTOCOL_TYPE])
        + pack("<H", HANDSHAKE_OPCODE)
        + bytes([K3_CONST_AT_3, agent_value & 0xFF])
    )
    if port_version >= 3:
        head += bytes([stage & 0xFF])
    return head + tok


def build_k3_from_historical_id(historical_id: str, port_version: int) -> bytes | None:
    """What recoveryConnectBleDevice(device, historical_id) writes to 2BB1 on a
    portVersion<20 device, or None when the SDK aborts (empty token)."""
    tok = normalize_historical_id(historical_id)
    if not tok:
        return None
    return build_k3(tok, port_version)


def encode_l3(
    status: int = 0,
    port_version: int = 7,
    timezone: int = 0,
    timezone_min: int = 0,
    audio_channel: int = 1,
    support_wifi: bool = False,
    no_ns_agc: bool = False,
    is_ogg_audio: bool = False,
    version_type: str = "V",
    version: int = 1,
) -> bytes:
    """Build a handshake response (l3, opcode 1).

    `status == 0` is success: `q` then caches portVersion/timezone/audioChannel
    and moves on. Any other value becomes `s0$d.a(status)` and fails the connect.

    The trailing version block is read from the frame END, so it must be the
    last four bytes: versionType char then the u24 LITTLE-ENDIAN version.
    """
    if not 0 <= status <= 0xFF:
        raise ValueError(f"status out of u8 range: {status}")
    if not 0 <= version <= 0xFFFFFF:
        raise ValueError(f"version out of u24 range: {version}")
    if len(version_type) != 1:
        raise ValueError("version_type is a single character")
    return (
        b"\x01\x01\x00"
        + bytes([status])
        + pack("<H", port_version)
        + bytes(
            [
                timezone & 0xFF,
                timezone_min & 0xFF,
                audio_channel,
                int(support_wifi),
                int(no_ns_agc),
                int(is_ogg_audio),
            ]
        )
        + version_type.encode("ascii")
        + bytes([version & 0xFF, (version >> 8) & 0xFF, (version >> 16) & 0xFF])
    )


def encode_x2(ssn: str, version_type: str = "V", version_code: int = 1) -> bytes:
    """Build an SSN response (x2 / GetSsnRsp, opcode 2). Always 63 bytes.

    The SSN occupies bytes 3..58 and must be NUL-terminated strictly before
    offset 59 or the SDK substitutes 28 ASCII zeros. versionType sits at the
    FIXED offset 59 and versionCode is the u24le at 60.

    The SDK compares the returned SSN against the ADVERTISED serial number
    (`q.b(String)` -> `r3.a().a(context, serial, ssn, versionName)`), and a
    mismatch raises SN_NOT_MATCH -- so this must agree with what the device
    advertises.
    """
    body = ssn.encode("ascii")
    if len(body) > 55:
        raise ValueError(f"ssn must leave room for a NUL before offset 59: {len(body)}")
    if len(version_type) != 1:
        raise ValueError("version_type is a single character")
    if not 0 <= version_code <= 0xFFFFFF:
        raise ValueError(f"version_code out of u24 range: {version_code}")
    frame = bytearray(63)
    frame[0:3] = b"\x01\x02\x00"
    frame[3 : 3 + len(body)] = body
    frame[3 + len(body)] = 0
    frame[59] = version_type.encode("ascii")[0]
    frame[60:63] = version_code.to_bytes(3, "little")
    return bytes(frame)
