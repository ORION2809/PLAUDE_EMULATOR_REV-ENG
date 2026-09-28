"""Plaud audio-path contracts, mechanically recovered (R6-S1).

Provenance key used below:
  DIRECT   literal constant or explicit code path in the shipped AAR bytecode
  INHERIT  frozen R3 / earlier-ledger fact, re-cited not re-derived
  POLICY   test-harness choice, not a device claim
  UNKNOWN  not recoverable from available evidence

Scope: Java/Kotlin-level contracts only (Ogg page walk, Opus packet
framing/chunking, PCM/WAV shapes). The native encoders (`libjni_ogg`
Ogg packer, `libopusJni` Opus codec) are symbol-confirmed but their
internals were not traced here; Opus *decoding* is therefore specified
structurally (input/output shapes) but not executed in this module.
"""

from __future__ import annotations

from struct import pack, unpack_from

# --- codec geometry (DIRECT: OggUtils.b=16000/c=320, k4/l4/h4 ctor math) ----
SAMPLE_RATE_HZ = 16000  # OggUtils.a() passes sipush 16000 to init(); m4.e
FRAME_SAMPLES = 320  # OggUtils.c; 320 samples @16kHz == 20 ms
OPUS_FRAME_BYTES = 80  # k4/l4 h = channels*80; AudioExporter frameSize
PCM_BYTES_PER_SAMPLE = 2  # 16-bit little-endian shorts throughout


def frame_bytes(channels: int) -> int:
    """One decode quantum: channels * 80 bytes (DIRECT: bipush 80, imul)."""
    if channels <= 0:
        raise ValueError(f"channels must be positive: {channels}")
    return 80 * channels


# --- Ogg page walk (DIRECT: sdk/ble/util/OggOpusParser.parseStream) ---------
OGG_PAGE_MAGIC = b"OggS"  # get_display_metrics byte[4]
OGG_PAGE_HEADER_LEN = 27  # 4 magic + 1 version + 1 type + 8 granule + 4 + 4 + 4 + 1
OPUS_HEAD_MAGIC = b"OpusHead"  # calculate_result_value byte[8]


def parse_ogg_page(data: bytes, offset: int = 0) -> dict[str, object]:
    """Parse one Ogg page exactly as OggOpusParser walks it.

    DIRECT behaviors reproduced: strict 4-byte magic (no resync search);
    version+type bytes read then discarded; 20 bytes (granule 8 + serial 4
    + sequence 4 + CRC 4) SKIPPED with nothing stored, compared, or
    verified (crc_validated=False is a finding, not a gap); lacing values
    summed unsigned; payload read in full. Raises ValueError where the SDK
    aborts the stream (bad magic, EOF).
    """
    raw = bytes(data)
    if len(raw) - offset < 4 or raw[offset : offset + 4] != OGG_PAGE_MAGIC:
        raise ValueError("not an OggS page at offset %d" % offset)
    if len(raw) - offset < OGG_PAGE_HEADER_LEN:   # 27 bytes, the segment count included
        raise ValueError("truncated Ogg page header")
    seg_count = raw[offset + 26]
    lacing = raw[offset + 27 : offset + 27 + seg_count]
    if len(lacing) < seg_count:
        raise ValueError("truncated lacing table")
    total = sum(lacing)
    body_at = offset + 27 + seg_count
    payload = raw[body_at : body_at + total]
    if len(payload) < total:
        raise ValueError("truncated page payload")
    return {
        "version": raw[offset + 4],
        "header_type": raw[offset + 5],
        "granule": None,  # DIRECT: skipped, never stored
        "serial": None,  # DIRECT: skipped, never checked
        "sequence": None,  # DIRECT: skipped, never checked
        "crc_stored": None,  # DIRECT: skipped with the 20-byte gap
        "crc_validated": False,  # DIRECT: no CRC primitive in the parser
        "lacing": bytes(lacing),
        "payload": payload,
        "total_len": 27 + seg_count + total,
    }


def iter_ogg_pages(data: bytes):
    """Yield consecutive pages; stops at the first truncated page (UNKNOWN:
    the SDK logs and returns partial; this generator simply stops)."""
    raw = bytes(data)
    offset = 0
    while offset < len(raw):
        try:
            page = parse_ogg_page(raw, offset)
        except ValueError:
            return
        yield page
        offset += int(page["total_len"])


def parse_opus_head(packet: bytes) -> dict[str, int]:
    """OpusHead fields (DIRECT: len>=19 guard, 8-byte magic, ch@9,
    preSkip u16le@10, sampleRate u32le@12; defaults 48000/1/0 from ctor)."""
    raw = bytes(packet)
    if len(raw) < 19:
        raise ValueError(f"OpusHead too short: {len(raw)}")
    if raw[:8] != OPUS_HEAD_MAGIC:
        raise ValueError("not a valid OpusHead")
    return {
        "channels": raw[9],
        "pre_skip": raw[10] | (raw[11] << 8),
        "sample_rate": raw[12] | (raw[13] << 8) | (raw[14] << 16) | (raw[15] << 24),
    }


#: OggOpusParser's constructor defaults, kept when page 0 is not a valid OpusHead.
OPUS_HEAD_DEFAULTS = {"channels": 1, "pre_skip": 0, "sample_rate": 48000}


def classify_ogg_pages(pages: list[dict[str, object]]) -> dict[str, object]:
    """Page-role-by-count (DIRECT): page 0 is OpusHead, page 1 is discarded
    unread (OpusTags), pages 2+ are audio. No content sniffing anywhere.

    Like the SDK (OggOpusParser.process_item_data 0-37 "OpusHead too short",
    52-76 "Not a valid OpusHead": log and return), an invalid page 0 does NOT
    stop the stream: ``head`` then holds the constructor defaults,
    ``head_valid`` is False and ``head_error`` says why; pages 2+ are still
    audio.  ``parse_opus_head`` itself stays strict."""
    pages = list(pages)
    head, valid, error = None, None, None
    if pages:
        try:
            head, valid = parse_opus_head(bytes(pages[0]["payload"])), True
        except ValueError as exc:
            head, valid, error = dict(OPUS_HEAD_DEFAULTS), False, str(exc)
    return {
        "head": head,
        "head_valid": valid,
        "head_error": error,
        "tags_dropped": len(pages) > 1,
        "audio_pages": pages[2:],
    }


def reassemble_page_packets(lacing: bytes, payload: bytes) -> list[bytes]:
    """Page-local packet reassembly (DIRECT): accumulate lacing values; a
    value < 255 terminates a packet (emitted if in-bounds and non-empty);
    a 255 continues; a trailing 255 at the last lacing entry is FLUSHED AS
    A COMPLETE packet (verified against bytecode offsets 432-442 -- the
    earlier "discarded" reading was wrong). Continuations across pages are
    NOT carried (state resets per page), so cross-page packets are lost."""
    out: list[bytes] = []
    cur = 0
    off = 0
    for idx, entry in enumerate(lacing):
        cur += entry
        if entry < 255 or idx == len(lacing) - 1:
            if cur > 0 and off + cur <= len(payload):
                out.append(payload[off : off + cur])
            off += cur
            cur = 0
    return out


# --- Opus packet chunking (DIRECT: k4/l4 a(byte[],long)) --------------------
def chunk_frames(data: bytes, channels: int) -> list[bytes]:
    """Split bytes into channels*80 decode quanta.

    DIRECT: the SDK buffers sub-h inputs and decodes whole h multiples;
    on the direct path a length that is not a multiple of h raises
    RuntimeException("The data length of data must be a multiple of "+h).
    """
    h = frame_bytes(channels)
    raw = bytes(data)
    if len(raw) % h != 0:
        raise ValueError(f"The data length of data must be a multiple of {h}")
    return [raw[i : i + h] for i in range(0, len(raw), h)]


def m4_output_len(packet_len: int, decoded_samples: int) -> int:
    """m4.a structural contract (DIRECT): out = new short[len*4]; the FULL
    array is returned when decode() reports > 0 (trailing slots stay zero
    when fewer samples were produced), else an empty array."""
    return packet_len * 4 if decoded_samples > 0 else 0


# --- h4 "ogg2pcm" wire framing ------------------------------------------------
# h4_frame_params is DIRECT (h4.<init>).  h4_payload_spans is an INFERENCE from
# those constants (a 512-byte lead, then 1536-byte frames of 45 + 1440 + 51
# bytes); it is NOT what h4.a executes.  h4_bytecode_payload_spans models the
# bytecode (review 2026-09-28): on the first call ``r`` is set to ``l`` (70-91),
# so ``position(l - r)`` at 340-381 is position(0) -- no lead is skipped
# (g4.a sets o = offset instead, 63-76) -- and the drain loop (437-499) steps
# ``position += n`` then ``get(p)``: 1485 bytes per frame, never skipping the
# trailing 51, while ``remaining() >= o``.
def h4_frame_params(channels: int) -> dict[str, int]:
    """h4 ctor math (DIRECT): l=512 stream skip, n=45 per-frame skip,
    p=1440*ch payload, o=p+96 wire frame, i=80*ch decode quantum, k=18."""
    if channels <= 0:
        raise ValueError(f"channels must be positive: {channels}")
    p = 1440 * channels
    return {"l": 512, "n": 45, "p": p, "o": p + 96, "i": 80 * channels, "k": 18}


def h4_payload_spans(total_len: int, channels: int) -> list[tuple[int, int]]:
    """(payload_offset, payload_len) spans of full h4 wire frames AS INFERRED
    from the constructor constants: the first 512 bytes skipped, then per
    (1440*ch+96)-byte frame 45 bytes skipped and 1440*ch bytes of opus
    payload.  The bytecode does something else (h4_bytecode_payload_spans);
    the remainder is handled by flush()/hasCompleteTail, not here."""
    params = h4_frame_params(channels)
    spans: list[tuple[int, int]] = []
    off = params["l"]
    while off + params["o"] <= total_len:
        spans.append((off + params["n"], params["p"]))
        off += params["o"]
    return spans


def h4_bytecode_payload_spans(total_len: int, channels: int) -> list[tuple[int, int]]:
    """(payload_offset, payload_len) spans h4.a(byte[], long) reads when the
    whole stream arrives in its FIRST call (see the section note): offsets
    45 + k*(45+1440*ch) while ``total_len - k*(45+1440*ch) >= 1440*ch+96``."""
    params = h4_frame_params(channels)
    step = params["n"] + params["p"]
    spans: list[tuple[int, int]] = []
    pos = 0
    while total_len - pos >= params["o"]:
        spans.append((pos + params["n"], params["p"]))
        pos += step
    return spans


def has_complete_tail(buffered: int, channels: int) -> bool:
    """h4.hasCompleteTail (DIRECT): buffered bytes equal i*idx+27+2*idx for
    idx 1..7 when channels == 4, else i*idx+27+idx for idx 1..4, where
    i = 80*channels -- i.e. whole frames plus one 27-byte page header."""
    i = frame_bytes(channels)
    if channels == 4:
        return any(buffered == i * idx + 27 + 2 * idx for idx in range(1, 8))
    return any(buffered == i * idx + 27 + idx for idx in range(1, 5))


# --- PCM / WAV shapes (DIRECT field order: AudioExporter) -------------------
def shorts_to_le16_bytes(samples: list[int]) -> bytes:
    """short[] -> byte[] little-endian (DIRECT: len*2, &255, >>8&255)."""
    out = bytearray()
    for s in samples:
        out += pack("<h", s)
    return bytes(out)


def wav_header_44(data_len: int, channels: int, sample_rate: int = 16000) -> bytes:
    """44-byte WAV header (DIRECT field order: RIFF chunkSize WAVE fmt␣ 16
    PCM(1) channels 16000 byteRate blockAlign 16 data dataLen; size math is
    the standard PCM formula)."""
    if channels <= 0 or data_len < 0:
        raise ValueError("bad wav header args")
    byte_rate = sample_rate * channels * PCM_BYTES_PER_SAMPLE
    block_align = channels * PCM_BYTES_PER_SAMPLE
    return (
        b"RIFF"
        + pack("<I", 36 + data_len)
        + b"WAVEfmt "
        + pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16)
        + b"data"
        + pack("<I", data_len)
    )


def pcm_callback_bytes_per_frame(channels: int) -> int:
    """PCM bytes per decode quantum: h bytes in, short[h*4] out, 2 bytes per
    short (DIRECT m4 shape). Mono: 80*4*2 = 640 bytes == 320 samples @16 kHz
    == 20 ms, corroborated by the iOS template's 640-byte blePcmData."""
    return frame_bytes(channels) * 4 * PCM_BYTES_PER_SAMPLE


# ===========================================================================
# R7: the recording-format SHAPE SPACE, recovered from the SDK's own ingestion
# ===========================================================================
#
# Earlier sprints treated "what are the real Plaud audio bytes?" as one
# question that only an authentic capture could answer. It is really three
# questions about three different artifacts, and two of them are answerable
# from the shipped SDK alone.
#
# The BLE receive path does NOT transform anything. PlaudDeviceAgent.syncFile
# builds its sink with t7.d() -> o4, a pure pass-through, and hands it to
# q.a(sessionId, start, end, ..., v3). So:
#
#     concatenated BLE DATA payloads  ==  the stored file, byte for byte
#
# which means the SDK's own *ingestion* of that file is a specification of
# what the device is allowed to send. AudioExporter performs exactly two
# tests, in this order:
#
#   1. `file.length() >= 512` and `PlaudEncryptHeader.fromFile(file).isEncrypted()`
#      (magic == "PLAUD.AI") -> skip 512 bytes, ChaCha20-decrypt the remainder
#      (raw ChaCha20, no Poly1305 tag).
#   2. read 4 bytes and compare to 'O','g','g','S' (79,103,103,83). The SDK's
#      own log names the two outcomes:
#          "标准 Ogg Opus"            = standard Ogg/Opus
#          "非 OggS (原始 Opus/AVC)"  = non-OggS, raw Opus ("AVC")
#
# So the shape space is CLOSED and small: {plain, encrypted} x {ogg, raw}.
# An emulator does not need an authentic capture to serve a conforming
# recording -- it needs to serve one of these four shapes. An authentic
# capture would tell us WHICH shape a given firmware emits; it is no longer
# needed to know the format space itself.

RECORDING_HEADER_SIZE = 512          # AudioExporter `file.length() < 512` guard
RECORDING_HEADER_MAGIC = b"PLAUD.AI"  # PlaudEncryptHeader.MAGIC_STRING
OGG_MAGIC_BYTES = (79, 103, 103, 83)  # AudioExporter's literal byte compare

#: The four shapes AudioExporter can ingest. Ordered as the SDK tests them.
RECORDING_SHAPES = ("encrypted_ogg", "encrypted_raw_opus", "plain_ogg", "plain_raw_opus")


def classify_recording(data: bytes) -> dict[str, object]:
    """Classify a stored recording exactly as AudioExporter does.

    Returns {shape, encrypted, container, payload_offset}. `payload_offset` is
    where the audio bytes begin -- 512 when an encryption header is present,
    else 0. For an encrypted file the container cannot be determined without
    the key, so `container` is None and `shape` is reported as unresolved
    until `classify_recording_plaintext` is applied to the decrypted bytes.

    MECHANICALLY_PROVEN: AudioExporter lines 410-415 (length/isEncrypted),
    546-559 (skip 512), 616-640 (the 4-byte OggS compare + the format log).
    """
    raw = bytes(data)
    encrypted = (
        len(raw) >= RECORDING_HEADER_SIZE
        and raw[: len(RECORDING_HEADER_MAGIC)].rstrip(b"\x00 ") == RECORDING_HEADER_MAGIC
    )
    if encrypted:
        return {
            "shape": "encrypted_unresolved",
            "encrypted": True,
            "container": None,
            "payload_offset": RECORDING_HEADER_SIZE,
        }
    container = classify_container(raw)
    return {
        "shape": f"plain_{container}",
        "encrypted": False,
        "container": container,
        "payload_offset": 0,
    }


def classify_container(plaintext: bytes) -> str:
    """The SDK's 4-byte sniff: "OggS" -> 'ogg', anything else -> 'raw_opus'.

    Note the guard order in AudioExporter: a file shorter than 4 bytes never
    reaches the compare, and is treated as NOT Ogg.
    """
    raw = bytes(plaintext)
    if len(raw) >= 4 and tuple(raw[:4]) == OGG_MAGIC_BYTES:
        return "ogg"
    return "raw_opus"


# --- g4 "ogg2opus" wire framing (MECHANICALLY_PROVEN: g4.<init> bytecode) ---
#
# g4 and h4 are two DIFFERENT Ogg page geometries that the SDK carries for
# de-framing a device stream. Neither is reachable from the shipped app --
# `t7.a()`/`t7.b()` have no call sites outside t7 itself, and the shipped
# export path uses the generic OggOpusParser instead. They are public API
# surface for SDK consumers, so they document Tinno's INTENDED device framing
# rather than observed runtime behaviour. Labelled accordingly.
#
# g4.<init> (sipush/bipush operands, verified in javap):
#     g = channels * 80              bytes per 20 ms frame, all channels
#     channels <= 2:  i = 512   k = 32   m = channels * 400
#     channels >  2:  i = 847   k = 43   m = channels * 640
#     l = k + m                      page stride
#
# The page-header sizes are exactly Ogg's: 27 fixed bytes plus one lacing
# byte per packet. m/g packets per page gives
#     channels <= 2: 400/80 = 5 packets  -> 27 + 5      = 32   MATCHES k
#     channels == 4: 640/80 = 8 packets, but a 4-channel packet is 320 bytes
#                    (> 255) so each needs 2 lacing bytes
#                                        -> 27 + 8*2    = 43   MATCHES k
# and g4.hasCompleteTail independently confirms the lacing rule: it accepts
# `g*idx + 27 + idx*2` for 4 channels and `g*idx + 27 + idx` otherwise.


def g4_frame_params(channels: int) -> dict[str, int]:
    """g4 ctor math (MECHANICALLY_PROVEN). See the note above."""
    if channels <= 0:
        raise ValueError(f"channels must be positive: {channels}")
    if channels > 2:
        lead, page_header, payload = 847, 43, channels * 640
    else:
        lead, page_header, payload = 512, 32, channels * 400
    return {
        "lead": lead,
        "page_header": page_header,
        "payload": payload,
        "stride": page_header + payload,
        "frame": channels * 80,
        "packets_per_page": payload // (channels * 80),
    }


def g4_payload_spans(total_len: int, channels: int) -> list[tuple[int, int]]:
    """(offset, length) of each page payload in a g4-framed stream.

    Mirrors g4.receiveVoiceData's drain loop: skip the leading header once,
    then repeatedly `position += page_header; get(payload)` while at least a
    full stride remains.
    """
    p = g4_frame_params(channels)
    spans: list[tuple[int, int]] = []
    off = p["lead"]
    while off + p["stride"] <= total_len:
        spans.append((off + p["page_header"], p["payload"]))
        off += p["stride"]
    return spans


def g4_has_complete_tail(buffered: int, channels: int) -> bool:
    """g4.hasCompleteTail (MECHANICALLY_PROVEN): whole frames plus a 27-byte
    page header and one lacing byte per packet -- two per packet at 4 channels.
    """
    frame = frame_bytes(channels)
    if channels == 4:
        return any(buffered == frame * idx + 27 + 2 * idx for idx in range(1, 8))
    return any(buffered == frame * idx + 27 + idx for idx in range(1, 5))


def build_g4_stream(payloads: list[bytes], channels: int, lead: bytes | None = None) -> bytes:
    """Build a g4-framed stream from per-page payloads.

    HARNESS_POLICY: the page-header bytes are zero filler. g4 never reads
    them -- it only advances the buffer position by `page_header` -- so their
    content is unconstrained by the SDK. This builder exists so the emulator
    can serve a stream a g4 consumer would de-frame correctly; it does NOT
    claim real devices emit zero page headers.
    """
    p = g4_frame_params(channels)
    head = bytes(p["lead"]) if lead is None else bytes(lead)
    if len(head) != p["lead"]:
        raise ValueError(f"lead must be exactly {p['lead']} bytes, got {len(head)}")
    out = bytearray(head)
    for payload in payloads:
        if len(payload) != p["payload"]:
            raise ValueError(
                f"each page payload must be exactly {p['payload']} bytes, got {len(payload)}"
            )
        out += bytes(p["page_header"])
        out += payload
    return bytes(out)
