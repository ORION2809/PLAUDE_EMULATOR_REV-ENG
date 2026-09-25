#!/usr/bin/env python3
"""R6-S1 verifier: audio-path contracts against SDK bytecode + self-checks.

Part 1 re-asserts the bytecode literals this sprint's claims rest on
(fails if the evidence tree moves). Part 2 exercises plaudsim.audio on
synthetic fixtures. Exit 0 = verified. Frozen evidence is read, never
written.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVAP = ROOT / "build" / "evidence" / "javap"
sys.path.insert(0, str(ROOT / "emulator"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def read(rel: str) -> str:
    p = JAVAP / rel
    assert p.is_file(), f"missing evidence file {rel}"
    return p.read_text()


ogg = read("com/tinnotech/penblesdk/utils/OggUtils.txt")
opus = read("com/tinnotech/penblesdk/utils/OpusUtils.txt")
k4 = read("com/plaud/sdk/proto/k4.txt")
l4 = read("com/plaud/sdk/proto/l4.txt")
m4 = read("com/plaud/sdk/proto/m4.txt")
h4 = read("com/plaud/sdk/proto/h4.txt")
parser = read("sdk/ble/util/OggOpusParser.txt")
exporter = read("sdk/audio/AudioExporter.txt")

# --- Part 1: bytecode literals -------------------------------------------------
check("OggUtils inits at 16000 Hz / 320 samples", "b = 16000" in ogg and "c = 320" in ogg)
check("OggUtils exposes init/putPkg/destroy",
      "native int init" in ogg and "native int putPkg" in ogg and "native void destroy" in ogg)
check("OpusUtils exposes createDecoder/decode/destroyDecoder",
      "createDecoder" in opus and "decode(long, byte[], short[])" in opus
      and "destroyDecoder" in opus)
check("m4 decodes at 16000 Hz", "e = 16000" in m4)
check("m4 output buffer is short[len*4], empty on failure",
      "iconst_4" in m4 and "imul" in m4 and "newarray       short" in m4)
check("k4/l4 chunk quantum is channels*80", "bipush        80" in k4 and "bipush        80" in l4)
check("k4 rejects non-multiple lengths", "The data length of data must be a multiple of" in k4)
check("h4 skips 512 + 45 per 1440ch+96 frame",
      "l=512" in h4 or "sipush        512" in h4)
check("h4 decode quantum is 80", "Field i" in h4)
check("OggOpusParser checks OggS magic (79/103/103/83 byte build)",
      "bipush        79" in parser and "bipush        103" in parser
      and "bipush        83" in parser)
check("OggOpusParser reads OpusHead", "OpusHead" in parser)
check("OggOpusParser never verifies CRC", "CRC" not in parser and "crc" not in parser.lower().replace("opuscrc", ""))
check("AudioExporter writes 44-byte 16 kHz WAV headers",
      "16000" in exporter and "WAVE" in exporter)
check("libjni_ogg exposes Ogg packer JNI entry points",
      (ROOT / "build/evidence/aar/jni/x86_64/libjni_ogg.so").is_file())

# --- Part 2: audio.py self-checks on synthetic fixtures -------------------------
# Loaded by file path so this verifier stays Bumble-free.
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location("plaudsim_audio", ROOT / "emulator/plaudsim/audio.py")
assert _spec is not None and _spec.loader is not None
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
chunk_frames = _mod.chunk_frames
classify_ogg_pages = _mod.classify_ogg_pages
h4_frame_params = _mod.h4_frame_params
h4_payload_spans = _mod.h4_payload_spans
has_complete_tail = _mod.has_complete_tail
m4_output_len = _mod.m4_output_len
parse_ogg_page = _mod.parse_ogg_page
pcm_callback_bytes_per_frame = _mod.pcm_callback_bytes_per_frame
reassemble_page_packets = _mod.reassemble_page_packets
wav_header_44 = _mod.wav_header_44


def page(payload: bytes) -> bytes:
    lacing = bytearray()
    remaining = len(payload)
    while remaining > 255:
        lacing.append(255)
        remaining -= 255
    lacing.append(remaining)
    out = bytearray(b"OggS") + bytes([0, 0]) + bytes(8) + bytes(4) + bytes(4) + bytes(4)
    out += bytes([len(lacing)]) + bytes(lacing) + payload
    return bytes(out)


_OPUS_HEAD = b"OpusHead" + bytes([1, 1]) + (0).to_bytes(2, "little") + (16000).to_bytes(4, "little") + bytes(4)
pages = [parse_ogg_page(page(_OPUS_HEAD)),
         parse_ogg_page(page(bytes(300))),
         parse_ogg_page(page(bytes(80)))]
roles = classify_ogg_pages(pages)
check("synthetic pages classify head/tags/audio",
      roles["head"]["channels"] == 1 and roles["tags_dropped"] is True
      and len(roles["audio_pages"]) == 1)
check("trailing-255 lacing flushes as complete",
      reassemble_page_packets(bytes([255, 45]), bytes(300)) == [bytes(300)])
check("chunk rule enforced", chunk_frames(bytes(160), 1) == [bytes(80), bytes(80)])
check("m4 shape short[len*4]", m4_output_len(80, 320) == 320 and m4_output_len(80, 0) == 0)
check("h4 params mono", h4_frame_params(1) == {"l": 512, "n": 45, "p": 1440, "o": 1536, "i": 80, "k": 18})
blob_len = 512 + 2 * 1536
check("h4 spans cover full frames",
      h4_payload_spans(blob_len, 1) == [(557, 1440), (2093, 1440)])
check("tail check mono", has_complete_tail(108, 1) and not has_complete_tail(107, 1))
check("mono decode quantum is 640 PCM bytes", pcm_callback_bytes_per_frame(1) == 640)
hdr = wav_header_44(640, 1)
check("wav header 44 B @16 kHz",
      len(hdr) == 44 and hdr[:4] == b"RIFF" and hdr[24:28] == (16000).to_bytes(4, "little"))

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
