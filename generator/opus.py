"""Device-shaped Opus outputs: Ogg/Opus, bare packet stream, g4 framing.

EVIDENCE (device/SDK facts, re-cited from emulator/plaudsim/audio.py and
docs/protocol-ledger.md §8 "Recorded audio" (BleFile geometry), §8 "Codec
parameters" and §8 "Quirks"):
* 16 kHz, 20 ms (320-sample) frames                 audio.SAMPLE_RATE_HZ / FRAME_SAMPLES (DIRECT)
* exactly 80 bytes per frame per channel            audio.OPUS_FRAME_BYTES; BleFile.calculateOpusDuration
                                                    (ALL.txt:10467) / calculateOpusOffset (ALL.txt:10429);
                                                    libjni_ogg putPkg rejects off-size packets (ledger
                                                    "Codec parameters": no variable-bitrate path)
* 1 or 2 channels                                    BleFile(..., ch) arithmetic, ledger section 8
* container sniff: 4 bytes "OggS" else raw          audio.classify_container (ALL.txt:96951)
* Ogg page roles by COUNT (0 head, 1 tags, 2+ audio) audio.classify_ogg_pages (OggOpusParser)
* a packet split across pages is mis-parsed          audio.reassemble_page_packets; ledger "Quirks"
* g4 page geometry (512 lead, 32/43 header, 5/8 pkt) audio.g4_frame_params / build_g4_stream

HARNESS_POLICY (harness choices, not device claims):
* encoder: PyAV/libopus, `vbr=off` (hard CBR), application "voip", 20 ms
  frames, bitrate 32 kbps per channel -- chosen because it is the only
  libopus setting that yields the SDK-enforced 80 B/frame/channel;
  complexity 5 by default (encode-time trade-off; firmware value UNKNOWN);
* the Ogg stream serial number is rewritten to SYNTHETIC_OGG_SERIAL and the
  page CRCs recomputed, because libavformat picks a random serial per encode
  and the harness requires byte-deterministic output. The SDK's parser skips
  serial and CRC entirely (audio.parse_ogg_page), so this changes nothing it
  reads; PyAV/libogg DO verify CRC, hence the recompute;
* OpusTags/OpusHead contents are whatever libavformat writes (vendor
  "Lavf..."); the SDK's own writer uses vendor "TinnoTech123456789012" with
  the ledger's non-conformant quirks, which are NOT reproduced;
* the bare packet stream is the Ogg's audio packets concatenated, so the two
  plain shapes carry identical Opus payloads. It has no pre-skip carrier: the
  Ogg's OpusHead pre-skip (312 at 48 kHz = 104 samples at 16 kHz, libopus's
  lookahead) is trimmed by any Ogg demuxer, whereas a decoder of the bare
  stream (or of g4) outputs those samples first, plus the encoder's flush
  packet at the end. export.py records both per file
  (`codec_lookahead_samples_16k`, `flush_packets`);
* trailing packets that do not fill a whole g4 page are dropped (g4's drain
  loop only consumes whole strides; the remainder path is not modelled).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np

from generator._evidence import plaud_audio as A
from generator.contract import SAMPLE_RATE

#: HARNESS_POLICY: fixed synthetic Ogg stream serial ("SYN1" as ASCII bytes).
SYNTHETIC_OGG_SERIAL = 0x53594E31

FRAME_SAMPLES = A.FRAME_SAMPLES  # 320 (DIRECT)
OPUS_FRAME_BYTES = A.OPUS_FRAME_BYTES  # 80 (DIRECT)


def _ogg_crc_table() -> np.ndarray:
    table = np.zeros(256, dtype=np.uint32)
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if (r & 0x80000000) else (r << 1)
            r &= 0xFFFFFFFF
        table[i] = r
    return table


_CRC_TABLE = _ogg_crc_table()
_CRC_TABLE_LIST = [int(x) for x in _CRC_TABLE]


def ogg_crc32(data: bytes) -> int:
    """Ogg page CRC: poly 0x04C11DB7, init 0, no reflection, no final xor."""
    crc = 0
    table = _CRC_TABLE_LIST
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ b]
    return crc


@dataclass
class OggPage:
    offset: int
    total_len: int
    header_type: int
    serial: int
    sequence: int
    crc_stored: int
    lacing: bytes
    payload: bytes

    @property
    def continued(self) -> bool:
        return bool(self.header_type & 0x01)

    @property
    def eos(self) -> bool:
        return bool(self.header_type & 0x04)


def walk_pages(data: bytes) -> list[OggPage]:
    """Full page walk (reads the fields the SDK skips, for the harness's own
    checks; the SDK-faithful walk is plaud_audio.iter_ogg_pages)."""
    raw = bytes(data)
    out: list[OggPage] = []
    off = 0
    while off < len(raw):
        page = A.parse_ogg_page(raw, off)  # raises on bad magic / truncation
        out.append(
            OggPage(
                offset=off,
                total_len=int(page["total_len"]),
                header_type=int(page["header_type"]),
                serial=int.from_bytes(raw[off + 14 : off + 18], "little"),
                sequence=int.from_bytes(raw[off + 18 : off + 22], "little"),
                crc_stored=int.from_bytes(raw[off + 22 : off + 26], "little"),
                lacing=bytes(page["lacing"]),
                payload=bytes(page["payload"]),
            )
        )
        off += int(page["total_len"])
    return out


def rewrite_ogg_serial(data: bytes, serial: int = SYNTHETIC_OGG_SERIAL) -> bytes:
    """Set every page's serial to `serial` and recompute its CRC."""
    raw = bytearray(data)
    for page in walk_pages(bytes(raw)):
        off = page.offset
        raw[off + 14 : off + 18] = int(serial).to_bytes(4, "little")
        raw[off + 22 : off + 26] = b"\x00\x00\x00\x00"
        crc = ogg_crc32(bytes(raw[off : off + page.total_len]))
        raw[off + 22 : off + 26] = crc.to_bytes(4, "little")
    return bytes(raw)


def verify_ogg_crcs(data: bytes) -> bool:
    raw = bytes(data)
    for page in walk_pages(raw):
        body = bytearray(raw[page.offset : page.offset + page.total_len])
        body[22:26] = b"\x00\x00\x00\x00"
        if ogg_crc32(bytes(body)) != page.crc_stored:
            return False
    return True


def encode_ogg_opus(
    pcm: np.ndarray,
    channels: int,
    bitrate_per_channel: int = 32000,
    complexity: int = 5,
    deterministic: bool = True,
) -> bytes:
    """pcm: int16 (channels, n) or (n,). Returns a standard Ogg/Opus stream.

    `complexity` is libopus's 0-10 setting (HARNESS_POLICY; the firmware's
    value is UNKNOWN). It changes encode time and the bits spent inside each
    fixed 80-byte packet, never the packet size."""
    import av

    if channels not in (1, 2):
        raise ValueError("device recordings are 1 or 2 channels (ledger section 8)")
    x = np.asarray(pcm)
    if x.dtype != np.int16:
        raise ValueError("pcm must be int16")
    if x.ndim == 1:
        x = x.reshape(1, -1)
    if x.shape[0] != channels:
        raise ValueError(f"pcm has {x.shape[0]} channels, expected {channels}")
    n = x.shape[1]
    interleaved = np.empty(n * channels, dtype=np.int16)
    for c in range(channels):
        interleaved[c::channels] = x[c]
    layout = "mono" if channels == 1 else "stereo"
    buf = io.BytesIO()
    container = av.open(buf, "w", format="ogg")
    stream = container.add_stream("libopus", rate=SAMPLE_RATE)
    stream.layout = layout
    stream.bit_rate = int(bitrate_per_channel) * channels
    stream.options = {"vbr": "off", "frame_duration": "20", "application": "voip", "compression_level": str(int(complexity))}
    frame = av.AudioFrame.from_ndarray(interleaved.reshape(1, -1), format="s16", layout=layout)
    frame.sample_rate = SAMPLE_RATE
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    data = buf.getvalue()
    return rewrite_ogg_serial(data) if deterministic else data


def ogg_audio_packets(data: bytes) -> list[bytes]:
    """Audio packets exactly as the SDK's OggOpusParser would recover them
    (page roles by count; page-local lacing reassembly)."""
    pages = list(A.iter_ogg_pages(data))
    roles = A.classify_ogg_pages(pages)
    packets: list[bytes] = []
    for page in roles["audio_pages"]:
        packets.extend(A.reassemble_page_packets(page["lacing"], page["payload"]))
    return packets


def check_sdk_ogg_constraints(data: bytes, channels: int) -> dict[str, object]:
    """Structural checks against what the SDK parser tolerates. Raises on a
    violation; returns a report otherwise."""
    pages = walk_pages(data)
    if len(pages) < 3:
        raise ValueError("Ogg/Opus stream needs head, tags and at least one audio page")
    head = A.parse_opus_head(pages[0].payload)
    if head["channels"] != channels or head["sample_rate"] != SAMPLE_RATE:
        raise ValueError(f"OpusHead mismatch: {head}")
    if not pages[1].payload.startswith(b"OpusTags"):
        raise ValueError("page 1 is not OpusTags (the SDK discards page 1 unread)")
    if pages[2].continued:
        raise ValueError("OpusTags spans more than one page; the SDK would treat the spill as audio")
    for p in pages[2:]:
        if p.continued or (p.lacing and p.lacing[-1] == 255):
            raise ValueError("an audio packet is split across pages; the SDK mis-parses that")
    packets = ogg_audio_packets(data)
    expected = A.frame_bytes(channels)
    sizes = {len(p) for p in packets}
    if sizes != {expected}:
        raise ValueError(f"packet sizes {sorted(sizes)} != {{{expected}}}: not the SDK's fixed-size framing")
    return {
        "pages": len(pages),
        "audio_pages": len(pages) - 2,
        "packets": len(packets),
        "packet_bytes": expected,
        "pre_skip": head["pre_skip"],
        "eos_flag_set": pages[-1].eos,
        "serial": pages[0].serial,
        "crc_valid": verify_ogg_crcs(data),
    }


def raw_packet_stream(packets: list[bytes], channels: int) -> bytes:
    """The 'non-OggS raw Opus' shape: fixed-size packets back to back."""
    expected = A.frame_bytes(channels)
    for i, p in enumerate(packets):
        if len(p) != expected:
            raise ValueError(f"packet {i} is {len(p)} bytes, need exactly {expected}")
    out = b"".join(packets)
    A.chunk_frames(out, channels)  # the SDK's own multiple-of-h check
    return out


def g4_stream(packets: list[bytes], channels: int) -> tuple[bytes, int]:
    """g4-framed stream from fixed-size packets. Returns (bytes, dropped)."""
    p = A.g4_frame_params(channels)
    per_page = p["packets_per_page"]
    pages = [b"".join(packets[i : i + per_page]) for i in range(0, len(packets) - per_page + 1, per_page)]
    dropped = len(packets) - len(pages) * per_page
    return A.build_g4_stream(pages, channels), dropped


def decode_ogg_opus(data: bytes) -> tuple[np.ndarray, int]:
    """Independent decode via PyAV/libopus -> (float32 (channels, n), rate).
    libopus decodes at 48 kHz; callers divide by the returned rate."""
    import av

    container = av.open(io.BytesIO(bytes(data)))
    stream = container.streams.audio[0]
    frames = [f.to_ndarray() for f in container.decode(stream)]
    rate = int(stream.rate)
    container.close()
    if not frames:
        return np.zeros((1, 0), dtype=np.float32), rate
    return np.concatenate(frames, axis=1), rate


__all__ = [
    "FRAME_SAMPLES",
    "OPUS_FRAME_BYTES",
    "OggPage",
    "SYNTHETIC_OGG_SERIAL",
    "check_sdk_ogg_constraints",
    "decode_ogg_opus",
    "encode_ogg_opus",
    "g4_stream",
    "ogg_audio_packets",
    "ogg_crc32",
    "raw_packet_stream",
    "rewrite_ogg_serial",
    "verify_ogg_crcs",
    "walk_pages",
]
