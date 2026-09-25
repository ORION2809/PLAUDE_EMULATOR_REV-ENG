"""Recording/file-sync codecs and parsers (R3).

Pure structure recovered from the shipped AAR; nothing here sends, receives or
touches lifecycle state. Every layout is cited to bytecode in
`build/evidence/javap/`; see `docs/protocol-ledger.md`.

THREE protocol types appear on 2BB0 and they are NOT interchangeable
(conflating them was the largest R3 error in the previous reconstruction):

  type 1  control frames   [u8 1][u16le opcode][...]   -- s6 (28), t6 (29), q2 (26), c5 (22)
  type 2  FILE DATA        the bulk recording stream   -- parse_file_data_frame
  type 4  BLE RATE TEST    throughput probe, opcode 101 -- parse_rate_test_frame

The type-2 data layout DEPENDS ON portVersion (q.m, set from l3.portVersion):
portVersion >= 7 carries a session id that older firmware omits.
"""

from __future__ import annotations

from struct import pack, unpack_from

PROTOCOL_TYPE = 1
FILE_DATA_TYPE = 2
RATE_TEST_TYPE = 4

# Opcodes, DIRECT from each class's getBleRequestType()/a() constant.
OPCODE_SYNC_START = 28   # y6 request   -> s6 response (same opcode)
OPCODE_SYNC_HEAD = 28    # s6 SyncFileHeadRsp
OPCODE_SYNC_TAIL = 29    # t6 SyncFileTailRsp
OPCODE_FILE_LIST = 26    # p2 request -> q2 GetRecSessionsRsp
OPCODE_RESUME = 22       # b5 request -> c5 RecordResumeRsp
OPCODE_STOP_SYNC = 29    # z6 request -> a7 SyncRecFileStopRsp, which is opcode 30
OPCODE_STOP_SYNC_RSP = 30
OPCODE_DELETE_FILE = 30  # w6 request -> x6 SyncRecFileDelRsp, which is opcode 31
OPCODE_DELETE_FILE_RSP = 31
OPCODE_RATE_TEST = 101   # x request  -> type-4 frames

# Opcode 29 means DIFFERENT things in the two directions: inbound to the device
# it is "stop syncing" (z6), outbound from it it is the transfer tail (t6).
# Opcode 30 likewise: inbound "delete file" (w6), outbound "stop acknowledged"
# (a7). The request and response opcode spaces are separate tables (w$c and
# w$a respectively), and they are NOT symmetric.

# q2.a([B,I) selects the entry stride from the format selector, which q passes
# as this.m == l3.portVersion.
FILE_ENTRY_STRIDES = {"legacy": 8, "mid": 9, "modern": 10}
FILE_LIST_ENTRY_OFFSET = 11   # q2.a: `int i2 = 11;`
EMPTY_PACKAGE_OFFSET = 0xFFFFFFFF   # w.a == 4294967295L


def file_entry_stride(port_version: int) -> int:
    """q2.a's switch on the format selector: 1 -> 8, 2..6 -> 9, else -> 10."""
    if port_version == 1:
        return 8
    if 2 <= port_version <= 6:
        return 9
    return 10


# --- requests ------------------------------------------------------------


def pack_sync_start(session_id: int, start: int, end: int) -> bytes:
    """y6 syncFile request: [01][1C 00][u32le sessionId][u32le start][u32le end].

    y6.enPkg = packHead() + a(this.a) + a(this.b) + a(this.c), and the only
    construction site is q.a(long,long,long,boolean,...) which is reached from
    PlaudDeviceAgent.syncFile(sessionId, start, end). 15 bytes.
    """
    for name, value in (("session_id", session_id), ("start", start), ("end", end)):
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(f"{name} out of u32 range: {value}")
    return b"\x01\x1c\x00" + pack("<III", session_id, start, end)


def pack_file_list_request(request_stamp: int, start_session_id: int, flag: bool = False) -> bytes:
    """p2 getFileList request: [01][1A 00][u32le requestStamp][u32le startSessionId][u8 flag].

    NAMING CORRECTION: the first field is NOT a recording session id. q builds
    `new p2(c2.e(), j, z)` where c2 is the s5 singleton and `s5.e()` returns
    `this.a`, set in s5.a(long,int) to `System.currentTimeMillis() / 1000`.
    s5.a(byte[]) then gates every inbound q2 frame on
    `TntBleCommUtils.d(bArr, 3) == e()`, so the device MUST echo this stamp at
    offset 3 of every file-list response.

    `start_session_id` is PlaudDeviceAgent.getFileList(startSessionId).
    `flag` reaches p2 as the boolean `z` from q.a(long, boolean, ...); its
    meaning is UNKNOWN (no consumer found). 12 bytes.
    """
    for name, value in (("request_stamp", request_stamp), ("start_session_id", start_session_id)):
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(f"{name} out of u32 range: {value}")
    return b"\x01\x1a\x00" + pack("<II", request_stamp, start_session_id) + pack("<B", int(bool(flag)))


def pack_resume_record_request(session_id: int, scene: int) -> bytes:
    """b5 resumeRecord request: [01][16 00][u32le sessionId][u8 scene].

    b5.enPkg emits the payload only when `this.a >= 0`; a negative session id
    yields the bare 3-byte head. Reached from
    PlaudDeviceAgent.resumeRecord(sessionId). 8 bytes.
    """
    if not 0 <= session_id <= 0xFFFFFFFF:
        raise ValueError(f"session_id out of u32 range: {session_id}")
    if not 0 <= scene <= 0xFF:
        raise ValueError(f"scene out of u8 range: {scene}")
    return b"\x01\x16\x00" + pack("<I", session_id) + pack("<B", scene)


# --- control responses ---------------------------------------------------


def parse_sync_head(data: bytes) -> dict[str, object]:
    """s6 SyncFileHeadRsp{sessionId u32le@3, status u8@7}. 8 bytes.

    q$a.a treats `status > 0` as failure: it tears down the opcode-29 handler
    before forwarding the response. status == 0 is the success path.
    """
    raw = bytes(data)
    if len(raw) < 8:
        raise ValueError(f"s6 response too short: {len(raw)}")
    if raw[0] != PROTOCOL_TYPE or unpack_from("<H", raw, 1)[0] != OPCODE_SYNC_HEAD:
        raise ValueError("not an opcode-28 sync-head response")
    return {"session_id": unpack_from("<I", raw, 3)[0], "status": raw[7]}


def parse_sync_tail(data: bytes) -> dict[str, object]:
    """t6 SyncFileTailRsp{sessionId u32le@3, crc u16le@7}. 9 bytes.

    The crc is forwarded to the caller and NEVER compared inside the SDK: no
    tntGetCrc/tntGetFileCrc call site consumes it. Treat it as opaque.
    """
    raw = bytes(data)
    if len(raw) < 9:
        raise ValueError(f"t6 response too short: {len(raw)}")
    if raw[0] != PROTOCOL_TYPE or unpack_from("<H", raw, 1)[0] != OPCODE_SYNC_TAIL:
        raise ValueError("not an opcode-29 sync-tail response")
    return {"session_id": unpack_from("<I", raw, 3)[0], "crc": unpack_from("<H", raw, 7)[0]}


def parse_resume_record_response(data: bytes) -> dict[str, object]:
    """c5 RecordResumeRsp: sessionId u32le@3, start u32le@7, status u8@11,
    then guarded by len>=17: scene u8@12, startTime u32le@13. 17 bytes full."""
    raw = bytes(data)
    if len(raw) < 12:
        raise ValueError(f"c5 response too short: {len(raw)}")
    if raw[0] != PROTOCOL_TYPE or unpack_from("<H", raw, 1)[0] != OPCODE_RESUME:
        raise ValueError("not an opcode-22 resume response")
    out: dict[str, object] = {
        "session_id": unpack_from("<I", raw, 3)[0],
        "start": unpack_from("<I", raw, 7)[0],
        "status": raw[11],
        "scene": 0,       # c5 field defaults, assigned before the guard
        "start_time": 0,
    }
    if len(raw) >= 17:
        out["scene"] = raw[12]
        out["start_time"] = unpack_from("<I", raw, 13)[0]
    return out


def parse_file_entry(entry: bytes, port_version: int = 7) -> dict[str, object]:
    """One file-list entry, stride selected by portVersion.

    From q2.a's three loops:
      stride 8  (pv == 1):   [u32le sessionId][u32le fileSize]
                             -> BleFile(sessionId, fileSize, attribute=0, scene=0)
      stride 9  (pv 2..6):   [u32le sessionId][u32le fileSize][u8 @+8]
                             -> BleFile(sessionId, fileSize, attribute=@+8, scene=0)
      stride 10 (otherwise): [u32le sessionId][u32le fileSize][u8 @+8][u8 @+9]
                             -> BleFile(sessionId, fileSize, attribute=@+9, scene=@+8)

    ORDER CORRECTION: in the 10-byte form the SDK calls
    `new BleFile(d5, d6, a3, a2)` where a2 is the byte at +8 and a3 the byte at
    +9, and BleFile's 4-arg ctor is (sessionId, fileSize, attribute, scene).
    So +8 is SCENE and +9 is ATTRIBUTE -- the opposite of the obvious reading.
    BleFile.isMusic() is `scene == 4`.

    NOTE the 9-byte form leaves scene at 0, NOT at BleFile's 3-arg default of 1.
    """
    raw = bytes(entry)
    stride = file_entry_stride(port_version)
    if len(raw) < stride:
        raise ValueError(f"file entry too short for stride {stride}: {len(raw)}")
    out: dict[str, object] = {
        "session_id": unpack_from("<I", raw, 0)[0],
        "file_size": unpack_from("<I", raw, 4)[0],
        "attribute": 0,
        "scene": 0,
    }
    if stride == 9:
        out["attribute"] = raw[8]
    elif stride == 10:
        out["scene"] = raw[8]
        out["attribute"] = raw[9]
    return out


def parse_file_list_frame(data: bytes, port_version: int = 7) -> dict[str, object]:
    """q2 GetRecSessionsRsp frame.

    Header, from q2.<init> and q2.a([B,I):
        [0]      u8   protocolType = 1
        [1..2]   u16le opcode = 26
        [3..6]   u32le requestStamp      -- checked by s5.a against s5.e()
        [7..8]   u16le totals            -- q2.<init>: b(bArr, 7)
        [9..10]  u16le frameStartIndex   -- q2.a: `if (b(bArr,9) == c.size())`
        [11..]   entries, stride from portVersion

    OFFSET CORRECTION: entries begin at 11, not 9. q2.a hard-codes `i2 = 11`
    and computes the entry count as `(bArr.length - 11) / stride`.

    frameStartIndex is a RESUMPTION GATE, not decoration: a frame whose value
    does not equal the number of entries accumulated so far is silently
    ignored, which is how the SDK rejects duplicates and out-of-order frames.
    q2.<init> never reads offsets 3..6; the requestStamp attribution comes
    from s5.a(byte[]).
    """
    raw = bytes(data)
    if len(raw) < 9:
        raise ValueError(f"file-list frame too short for totals: {len(raw)}")
    if raw[0] != PROTOCOL_TYPE or unpack_from("<H", raw, 1)[0] != OPCODE_FILE_LIST:
        raise ValueError("not an opcode-26 file-list response")
    out: dict[str, object] = {
        "request_stamp": unpack_from("<I", raw, 3)[0],
        "totals": unpack_from("<H", raw, 7)[0],
        "frame_start_index": None,
        "entries": [],
    }
    if len(raw) < FILE_LIST_ENTRY_OFFSET:
        # q2.a's own guard: `if (bArr.length < 11) return;` -- header only.
        return out
    out["frame_start_index"] = unpack_from("<H", raw, 9)[0]
    stride = file_entry_stride(port_version)
    count = (len(raw) - FILE_LIST_ENTRY_OFFSET) // stride
    out["entries"] = [
        parse_file_entry(
            raw[FILE_LIST_ENTRY_OFFSET + i * stride : FILE_LIST_ENTRY_OFFSET + (i + 1) * stride],
            port_version,
        )
        for i in range(count)
    ]
    return out


# --- bulk data -----------------------------------------------------------


def parse_file_data_frame(data: bytes, port_version: int = 7) -> dict[str, object]:
    """Protocol-type-2 file-data frame, mirroring q$a.a([B) exactly.

    q$a.a sets `int i = 1;` then, only when `q.m >= 7`, reads a session id at
    offset 1 and sets `i = 5`. Everything after is expressed in terms of i:

        offset  = u32le @ i          (i == 5 modern, 1 legacy)
        length  = u8    @ i + 4      (9 modern, 5 legacy)
        payload = bytes [i+5 : i+5+length], clamped to the frame end
        cursor += len(payload)

    so the wire layouts are
        portVersion >= 7: [u8 2][u32le sessionId][u32le offset][u8 len][payload]
        portVersion  < 7: [u8 2][u32le offset][u8 len][payload]

    THE SENTINEL: when offset == 0xFFFFFFFF the frame is EMPTY_PACKAGE and the
    SDK instead reads a code byte at `i + 5` -- offset 10 modern, 6 legacy --
    i.e. it SKIPS the byte that would otherwise hold the length. That
    asymmetry is in the bytecode; it is reproduced here, not smoothed over.
    code == 1 means "do not restart"; any other code triggers a restart from
    the cursor when a packet loss stop is in progress.

    Session gating (modern only): a frame whose session id differs from the
    requested one is dropped with "---sessionId miss match---". Legacy frames
    carry no session id at all, so they cannot be gated.
    """
    raw = bytes(data)
    if len(raw) < 1 or raw[0] != FILE_DATA_TYPE:
        raise ValueError(
            f"not a type-2 file-data frame: {raw[:1].hex() if raw else 'empty'}"
        )
    modern = port_version >= 7
    i = 5 if modern else 1
    out: dict[str, object] = {
        "frame_type": FILE_DATA_TYPE,
        "port_version_branch": "modern" if modern else "legacy",
        "session_id": None,
        "offset": None,
        "length": None,
        "payload": b"",
        "empty_package": False,
        "code": None,
    }
    if modern:
        if len(raw) < 5:
            raise ValueError(f"file-data frame too short for session id: {len(raw)}")
        out["session_id"] = unpack_from("<I", raw, 1)[0]
    if len(raw) < i + 4:
        raise ValueError(f"file-data frame too short for offset: {len(raw)}")
    offset = unpack_from("<I", raw, i)[0]
    out["offset"] = offset
    if offset == EMPTY_PACKAGE_OFFSET:
        if len(raw) < i + 6:
            raise ValueError(f"EMPTY_PACKAGE frame too short for code: {len(raw)}")
        out["empty_package"] = True
        out["code"] = raw[i + 5]
        return out
    if len(raw) < i + 5:
        raise ValueError(f"file-data frame too short for length byte: {len(raw)}")
    declared = raw[i + 4]
    start = i + 5
    length = declared if declared + start <= len(raw) else len(raw) - start
    out["length"] = declared
    out["payload"] = raw[start : start + max(0, length)]
    return out


def parse_rate_test_frame(data: bytes) -> dict[str, object]:
    """Protocol-type-4 frame from the BLE THROUGHPUT TEST (request x, opcode 101).

    ATTRIBUTION CORRECTION: this frame is NOT part of file sync. Its only
    parser is the notify lambda inside q.a(int, c$b, v3), the handler for
    `new x(i).enPkg()` on opcode 101 -- PlaudDeviceAgent has no public
    rate-test entry point and the default payload size is 80.

    The parser really does read two different widths at the SAME offset:
        long d2 = TntBleCommUtils.d(bArr2, 1);   // u32le @1
        int  a2 = TntBleCommUtils.a(bArr2, 1);   // u8    @1
    and then copies `bArr2[2 : 2+min(a2, len-2)]`. Because the payload starts
    at 2, only the u8 reading is self-consistent; the u32 read is almost
    certainly a copy-paste remnant of the file-sync handler, where the two
    reads are at i and i+4. The overlap is preserved verbatim: both values are
    returned and NEITHER is given a semantic name here.
    """
    raw = bytes(data)
    if len(raw) < 2:
        raise ValueError(f"rate-test frame too short: {len(raw)}")
    if raw[0] != RATE_TEST_TYPE:
        raise ValueError(f"not a type-4 rate-test frame: {raw[0]:#x}")
    u32_at_1 = unpack_from("<I", raw, 1)[0] if len(raw) >= 5 else None
    u8_at_1 = raw[1]
    length = min(u8_at_1, len(raw) - 2)
    return {
        "u32_at_1": u32_at_1,
        "u8_at_1": u8_at_1,
        "payload": raw[2 : 2 + length],
    }


def pack_stop_sync_request() -> bytes:
    """z6 stopSyncFile request: [01][1D 00], no payload. 3 bytes.

    The SDK sends this on its own whenever it detects a DATA offset gap, then
    re-issues syncFile from its cursor. An emulator that ignores it strands the
    client's recovery path.
    """
    return b"\x01\x1d\x00"


def parse_stop_sync_response(data: bytes) -> dict[str, object]:
    """a7 SyncRecFileStopRsp: header only, opcode 30. `toString` is literally
    "SyncRecFileStopRsp{}" -- the class parses no fields at all."""
    raw = bytes(data)
    if len(raw) < 3:
        raise ValueError(f"a7 response too short: {len(raw)}")
    if raw[0] != PROTOCOL_TYPE or unpack_from("<H", raw, 1)[0] != OPCODE_STOP_SYNC_RSP:
        raise ValueError("not an opcode-30 stop-sync response")
    return {}


def pack_delete_file_request(session_id: int) -> bytes:
    """w6 deleteFile request: [01][1E 00][u32le sessionId]. 7 bytes."""
    if not 0 <= session_id <= 0xFFFFFFFF:
        raise ValueError(f"session_id out of u32 range: {session_id}")
    return b"\x01\x1e\x00" + pack("<I", session_id)


def parse_delete_file_response(data: bytes, port_version: int = 7) -> dict[str, object]:
    """x6 SyncRecFileDelRsp, opcode 31 -- another portVersion-dependent layout.

    x6.<init>(byte[], int) takes the portVersion as its second argument:
        pv >= 7:  sessionId u32le@3, status u8@7   (8 bytes)
        pv <  7:  status u8@3                      (4 bytes), no session id
    """
    raw = bytes(data)
    if len(raw) < 4:
        raise ValueError(f"x6 response too short: {len(raw)}")
    if raw[0] != PROTOCOL_TYPE or unpack_from("<H", raw, 1)[0] != OPCODE_DELETE_FILE_RSP:
        raise ValueError("not an opcode-31 delete-file response")
    if port_version >= 7:
        if len(raw) < 8:
            raise ValueError(f"x6 modern response too short: {len(raw)}")
        return {"session_id": unpack_from("<I", raw, 3)[0], "status": raw[7]}
    return {"session_id": None, "status": raw[3]}
