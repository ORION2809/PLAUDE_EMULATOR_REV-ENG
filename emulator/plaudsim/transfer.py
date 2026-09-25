"""Device-side transfer/file-list state for the R3 emulator.

Pure protocol logic: no Bumble, no lifecycle transitions, no authentication.
`profile.py` drives these objects from 2BB1 writes and emits the produced
frames through the notify/indicate path.

Recovered behaviour is cited to bytecode; everything the sources do NOT fix is
labelled HARNESS POLICY and is settable by the caller so a test can vary it.

HARNESS POLICY (explicitly NOT device claims):
* DATA payload size per frame. Nothing in the SDK pins it; the length field is
  a u8, so the protocol ceiling is 255 and the practical one is the ATT MTU.
* The HEAD status byte, the TAIL crc, the file table and the file bytes are
  caller-supplied synthetic values.
* Emitting HEAD, every DATA frame and TAIL back-to-back with no pacing.
* Replacing the active session when a second y6 arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from struct import pack

from plaudsim.filesync import (
    EMPTY_PACKAGE_OFFSET,
    FILE_DATA_TYPE,
    FILE_LIST_ENTRY_OFFSET,
    OPCODE_FILE_LIST,
    OPCODE_RESUME,
    OPCODE_SYNC_HEAD,
    OPCODE_SYNC_START,
    OPCODE_SYNC_TAIL,
    OPCODE_DELETE_FILE,
    OPCODE_DELETE_FILE_RSP,
    OPCODE_STOP_SYNC_RSP,
    file_entry_stride,
    parse_file_entry,
)

# HARNESS POLICY. Bounded by the u8 length field in the type-2 data frame.
DEFAULT_DATA_PAYLOAD_SIZE = 32
MAX_DATA_PAYLOAD_SIZE = 0xFF

# R7-S13 (RUNTIME_PROVEN against the unmodified SDK, r7/r7-s13-recording-pull.md):
# the client's DATA handler q$a.a reaches v3.finish(code) -- bleDataComplete /
# exportAudio completion -- ONLY from the EMPTY_PACKAGE branch (ALL.txt:69024-69108).
# A device therefore closes a transfer with HEAD . DATA... . EMPTY_PACKAGE(code) . TAIL;
# the sentinel must precede the TAIL (after-TAIL never completes, run 7). The CODE
# VALUE real firmware sends is UNKNOWN; 0 and 1 both complete on the normal path
# (runs 4b, 8). 0 is HARNESS_POLICY. None restores the pre-R7-S13 sequence.
DEFAULT_EMPTY_PACKAGE_CODE = 0


def pack_file_data_frame(
    offset: int,
    payload: bytes,
    session_id: int | None = None,
    port_version: int = 7,
) -> bytes:
    """Build a protocol-type-2 file-data frame.

    portVersion >= 7: [u8 2][u32le sessionId][u32le offset][u8 len][payload]
    portVersion  < 7: [u8 2][u32le offset][u8 len][payload]

    The length byte is the payload length; q$a.a clamps it to the frame end,
    so a frame whose declared length exceeds the real payload is accepted by
    the SDK with the payload truncated. This builder never lies: it refuses
    payloads that cannot be described by a u8.
    """
    if len(payload) > MAX_DATA_PAYLOAD_SIZE:
        raise ValueError(f"payload {len(payload)} exceeds the u8 length field")
    if not 0 <= offset <= 0xFFFFFFFF:
        raise ValueError(f"offset out of u32 range: {offset}")
    out = [bytes([FILE_DATA_TYPE])]
    if port_version >= 7:
        if session_id is None:
            raise ValueError("portVersion >= 7 frames carry a session id")
        out.append(pack("<I", session_id))
    out.append(pack("<I", offset))
    out.append(pack("<B", len(payload)))
    out.append(bytes(payload))
    return b"".join(out)


def pack_empty_package_frame(
    code: int, session_id: int | None = None, port_version: int = 7
) -> bytes:
    """Build the EMPTY_PACKAGE sentinel frame.

    q$a.a reads the code at `i + 5`, SKIPPING the byte at `i + 4` that a normal
    frame uses for its length. The filler byte is therefore never read; it is
    emitted as zero. code == 1 suppresses the restart path.
    """
    if not 0 <= code <= 0xFF:
        raise ValueError(f"code out of u8 range: {code}")
    out = [bytes([FILE_DATA_TYPE])]
    if port_version >= 7:
        if session_id is None:
            raise ValueError("portVersion >= 7 frames carry a session id")
        out.append(pack("<I", session_id))
    out.append(pack("<I", EMPTY_PACKAGE_OFFSET))
    out.append(b"\x00")          # never read by the SDK
    out.append(pack("<B", code))
    return b"".join(out)


def pack_sync_head(session_id: int, status: int = 0) -> bytes:
    """s6 SyncFileHeadRsp: [01][1C 00][u32le sessionId][u8 status]. 8 bytes."""
    return b"\x01\x1c\x00" + pack("<I", session_id) + pack("<B", status)


def pack_sync_tail(session_id: int, crc: int) -> bytes:
    """t6 SyncFileTailRsp: [01][1D 00][u32le sessionId][u16le crc]. 9 bytes.

    crc is an opaque pass-through: no SDK call site compares it.
    """
    return b"\x01\x1d\x00" + pack("<I", session_id) + pack("<H", crc & 0xFFFF)


def pack_file_list_frame(
    request_stamp: int,
    totals: int,
    entries: list[dict[str, int]],
    frame_start_index: int = 0,
    port_version: int = 7,
) -> bytes:
    """q2 file-list frame.

    [01][1A 00][u32le requestStamp][u16le totals][u16le frameStartIndex][entries]

    `request_stamp` must equal the value the host sent in p2, or s5.a(byte[])
    drops the frame. `frame_start_index` must equal the number of entries the
    host has already accumulated, or q2.a ignores the frame's entries.
    """
    stride = file_entry_stride(port_version)
    out = [b"\x01\x1a\x00", pack("<I", request_stamp), pack("<H", totals), pack("<H", frame_start_index)]
    for e in entries:
        body = pack("<II", e["session_id"], e["file_size"])
        if stride == 9:
            body += pack("<B", e.get("attribute", 0))
        elif stride == 10:
            body += pack("<BB", e.get("scene", 0), e.get("attribute", 0))
        out.append(body)
    return b"".join(out)


def pack_stop_sync_response() -> bytes:
    """a7 SyncRecFileStopRsp: [01][1E 00]. Header only -- a7 parses no fields."""
    return b"\x01\x1e\x00"


def pack_delete_file_response(session_id: int, status: int = 0, port_version: int = 7) -> bytes:
    """x6 SyncRecFileDelRsp: [01][1F 00][u32le sessionId][u8 status] on
    portVersion >= 7; [01][1F 00][u8 status] below it."""
    head = b"\x01\x1f\x00"
    if port_version >= 7:
        return head + pack("<I", session_id) + pack("<B", status)
    return head + pack("<B", status)


def parse_stop_sync_request(data: bytes) -> dict[str, int]:
    """Parse a 3-byte z6 stopSyncFile request (strict)."""
    from struct import unpack_from

    raw = bytes(data)
    if len(raw) != 3 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != 29:
        raise ValueError(f"not a z6 stopSyncFile request: {raw.hex()}")
    return {}


def parse_delete_file_request(data: bytes) -> dict[str, int]:
    """Parse a 7-byte w6 deleteFile request (strict)."""
    from struct import unpack_from

    raw = bytes(data)
    if len(raw) != 7 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_DELETE_FILE:
        raise ValueError(f"not a w6 deleteFile request: {raw.hex()}")
    return {"session_id": unpack_from("<I", raw, 3)[0]}


def pack_resume_record_response(
    session_id: int, start: int, status: int, scene: int, start_time: int
) -> bytes:
    """c5 RecordResumeRsp:
    [01][16 00][u32le sessionId][u32le start][u8 status][u8 scene][u32le startTime]. 17 bytes."""
    return (
        b"\x01\x16\x00"
        + pack("<I", session_id)
        + pack("<I", start)
        + pack("<B", status)
        + pack("<B", scene)
        + pack("<I", start_time)
    )


@dataclass
class TransferSession:
    """Device-side transfer session (one active session per peripheral).

    `start()` with start > 0 resumes from that byte offset, which is exactly
    what the SDK's own loss-recovery path does: on an offset gap the CLIENT
    writes stopSync (`q.k`) and, on the `a7` ack, re-issues
    syncFileStart(sessionId, lastPosition=cursor, end) (runnable
    `fileSyncLossPkgStop`, ALL.txt:69157-69182) -- BYTECODE_PROVEN and observed
    live ~35 ms after the gap (R7-S13 runs 6b/6c/6d, ledger 5.8/15). The 5 s
    runnable `q$a.f` is the separate *stall* path. The device side modelled
    here performs NO resend and NO ACK: it must deliver DATA in monotonic
    offset order from the requested start, close with EMPTY_PACKAGE before
    the TAIL, and (see profile.PlaudPeripheral.stream_in_task) abandon the
    previous stream when a new syncFileStart or stopSync arrives.
    """

    session_id: int = 0
    file_bytes: bytes = b""
    cursor: int = 0
    crc: int = 0x1234
    port_version: int = 7
    payload_size: int = DEFAULT_DATA_PAYLOAD_SIZE   # HARNESS POLICY
    head_status: int = 0                            # HARNESS POLICY
    empty_package_code: int | None = DEFAULT_EMPTY_PACKAGE_CODE  # R7-S13; None = legacy 3-part sequence
    done: bool = False

    def start(self, session_id: int, start: int, end: int) -> None:
        del end  # y6's `end` is 0 at every observed call site and is stored nowhere
        self.session_id = session_id
        self.cursor = max(0, start)
        self.done = False

    def frames(self, drop_offsets: tuple[int, ...] = ()) -> list[bytes]:
        """HEAD, DATA frames from the cursor, EMPTY_PACKAGE(code), then TAIL.

        The EMPTY_PACKAGE sentinel is what the genuine client completes on
        (R7-S13, RUNTIME_PROVEN); it is omitted when `empty_package_code` is
        None, which reproduces the pre-R7-S13 sequence that the real SDK
        never completes (runs 1, 2, 9).

        `drop_offsets` omits the DATA frame that would start at each listed
        offset WITHOUT advancing past it in the stream, so the next frame's
        offset no longer matches the host's cursor. That is the fault the
        SDK's gap detector is written to catch; it is a test hook, not device
        behaviour.
        """
        out = [pack_sync_head(self.session_id, self.head_status)]
        offset = self.cursor
        size = max(1, min(self.payload_size, MAX_DATA_PAYLOAD_SIZE))
        while offset < len(self.file_bytes):
            chunk = self.file_bytes[offset : offset + size]
            if offset not in drop_offsets:
                out.append(
                    pack_file_data_frame(
                        offset, chunk, self.session_id, self.port_version
                    )
                )
            offset += len(chunk)
        self.cursor = offset
        if self.empty_package_code is not None:
            out.append(
                pack_empty_package_frame(
                    self.empty_package_code, self.session_id, self.port_version
                )
            )
        out.append(pack_sync_tail(self.session_id, self.crc))
        self.done = True
        return out


@dataclass
class FileListAccumulator:
    """Host-side mirror of s5 + q2, for verification. Pure, no BLE.

    Reproduces the real gates, which the previous implementation did not model:

    * s5.a(byte[]): `if (TntBleCommUtils.d(bArr, 3) == e())` -- a frame whose
      requestStamp differs from the one sent in p2 is dropped ENTIRELY.
    * q2.<init> runs once, on the FIRST accepted frame; `totals` therefore
      comes from that frame and later frames' totals fields are never read.
    * q2.a: `if (TntBleCommUtils.b(bArr, 9) == this.c.size())` -- a frame whose
      frameStartIndex does not equal the current accumulated count contributes
      NOTHING. This is what makes duplicate and reordered frames harmless.
    * s5.f(): complete when `q2.c() == q2.b().size()`, i.e. accumulated ==
      totals. There is no terminal frame and no explicit end marker.
    * Nothing caps the accumulation at `totals`: a frame arriving with the
      right start index can push the count PAST totals, after which
      `size() == totals` is false forever and the transfer never completes.
      That overshoot is real SDK behaviour and is reproduced here.
    """

    request_stamp: int | None = None
    port_version: int = 7
    totals: int | None = None
    entries: list[dict[str, int]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.totals is not None and len(self.entries) == self.totals

    def ingest_frame(self, frame: bytes) -> str:
        """Ingest one raw q2 frame. Returns why it was accepted or dropped."""
        from plaudsim.filesync import parse_file_list_frame

        parsed = parse_file_list_frame(frame, self.port_version)
        if self.request_stamp is not None and parsed["request_stamp"] != self.request_stamp:
            return "dropped_stamp_mismatch"
        if self.request_stamp is None:
            self.request_stamp = parsed["request_stamp"]
        if self.totals is None:
            self.totals = parsed["totals"]
        if parsed["frame_start_index"] is None:
            return "header_only"
        if parsed["frame_start_index"] != len(self.entries):
            return "ignored_index_mismatch"
        self.entries.extend(parsed["entries"])
        return "accepted"


@dataclass
class FileTable:
    """Device-side synthetic file table backing file-list responses.

    `frames()` pages the table at `per_frame` entries, stamping each frame's
    frameStartIndex so a conforming host accumulates them in order.
    """

    entries: list[dict[str, int]] = field(default_factory=list)
    port_version: int = 7

    def frames(self, request_stamp: int, per_frame: int | None = None) -> list[bytes]:
        n = len(self.entries)
        step = n if per_frame is None else max(1, per_frame)
        if n == 0:
            return [pack_file_list_frame(request_stamp, 0, [], 0, self.port_version)]
        return [
            pack_file_list_frame(
                request_stamp, n, self.entries[i : i + step], i, self.port_version
            )
            for i in range(0, n, step)
        ]


def parse_sync_start_request(data: bytes) -> dict[str, int]:
    """Parse a 15-byte y6 syncFile request (strict)."""
    from struct import unpack_from

    raw = bytes(data)
    if len(raw) != 15 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_SYNC_START:
        raise ValueError(f"not a y6 syncFile request: {raw.hex()}")
    return {
        "session_id": unpack_from("<I", raw, 3)[0],
        "start": unpack_from("<I", raw, 7)[0],
        "end": unpack_from("<I", raw, 11)[0],
    }


def parse_resume_record_request(data: bytes) -> dict[str, int]:
    """Parse an 8-byte b5 resumeRecord request (strict)."""
    from struct import unpack_from

    raw = bytes(data)
    if len(raw) != 8 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_RESUME:
        raise ValueError(f"not a b5 resumeRecord request: {raw.hex()}")
    return {"session_id": unpack_from("<I", raw, 3)[0], "scene": raw[7]}


def parse_file_list_request(data: bytes) -> dict[str, int]:
    """Parse a 12-byte p2 getFileList request (strict)."""
    from struct import unpack_from

    raw = bytes(data)
    if len(raw) != 12 or raw[0] != 1 or unpack_from("<H", raw, 1)[0] != OPCODE_FILE_LIST:
        raise ValueError(f"not a p2 getFileList request: {raw.hex()}")
    return {
        "request_stamp": unpack_from("<I", raw, 3)[0],
        "start_session_id": unpack_from("<I", raw, 7)[0],
        "flag": raw[11],
    }


__all__ = [
    "DEFAULT_DATA_PAYLOAD_SIZE",
    "MAX_DATA_PAYLOAD_SIZE",
    "FileListAccumulator",
    "FileTable",
    "TransferSession",
    "pack_empty_package_frame",
    "pack_file_data_frame",
    "pack_file_list_frame",
    "pack_delete_file_response",
    "pack_resume_record_response",
    "pack_stop_sync_response",
    "parse_delete_file_request",
    "parse_stop_sync_request",
    "pack_sync_head",
    "pack_sync_tail",
    "parse_file_list_request",
    "parse_resume_record_request",
    "parse_sync_start_request",
    "FILE_LIST_ENTRY_OFFSET",
    "OPCODE_SYNC_HEAD",
    "OPCODE_SYNC_TAIL",
    "parse_file_entry",
]
