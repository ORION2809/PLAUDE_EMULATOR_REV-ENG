# R6-S1: Plaud audio-path reconstruction (archaeology + conformance)

Verdict: **AUDIO CONTRACT PARTIALLY RECONSTRUCTED** (Java/Kotlin layer
complete; native decode internals and live-device confirmation remain).

`verify_audio_contracts.py` (ALL CHECKS PASSED) + `tests/test_r6_s1_audio.py`
(16 passed). Full suite 188 → 204. No frozen claim contradicted; one
ledger sub-claim corrected (see §7). No Plaud audio bytes exist anywhere
in this repo; all fixtures are synthetic.

## 1. Exact SDK call chain (DIRECT from javap unless noted)

```text
BLE DATA (type-2 frames)
  → q transfer assembly → v3/o4 passthrough (o4.a forwards bytes to callback)
  → app/SDK FileOutputStream (NiceBuildSdk/PhDeviceAgent lambdas, invokedynamic)
  → file on disk
  → AudioExporter (decrypt → sniff OggS → dispatch Ogg vs raw → PCM/WAV/MP3/Ogg)
```

Live-streaming pipeline (g4/h4/k4/l4 via t7 factory, channels default
p7.m): present in the AAR but **no factory call site** (`t7.c/b/a`) was
found outside t7 itself. Treat the streaming stages as
mechanically recovered but with UNKNOWN liveness in this build; the
file-level AudioExporter path is the evidenced live one. Note: the
streaming stages remain mechanically recovered regardless of liveness.

Stage map (t7 factory; channels default p7.m):

| Stage | Tag | In | Out | Ctor |
|---|---|---|---|---|
| g4 | ogg2opus | file bytes + ts | opus packets (m-byte windows) | ch; i=512/847, k=32/43, m=400/640·ch |
| k4 | opus2pcm* | opus bytes + ts | **Ogg FILE** via native putPkg | (path, ch); h=80·ch |
| l4 | opus2pcm | opus bytes + ts | PCM short[] via m4 | ch; h=80·ch |
| h4 | ogg2pcm | ogg file bytes + ts | opus middles + PCM short[] via m4 | ch; l=512, n=45, p=1440·ch, o=p+96 |
| m4 | OpusDecode | opus packet byte[] | short[len·4] iff n>0 else empty | createDecoder(16000, ch) |
| n4 | OpusEncode | PCM shorts | opus bytes | createEncoder(16000, ch, bitrate) |
| r4 | pcm2opus | PCM shorts | opus bytes | ch |

\* k4's log tag is "opus2pcm" but it writes an Ogg container, not PCM.

## 2. Contracts (the R6 ledger)

| Layer | Input | Output | Provenance | Confidence |
|---|---|---|---|---|
| BLE transfer | type-2 DATA frames | raw file bytes (512-B header + framed opus) | R3 frozen + h4 framing math | DIRECT framing, INHERIT transfer |
| Transfer assembly | DATA payloads | file on disk via FileOutputStream | NiceBuildSdk/PhDeviceAgent lambdas | DIRECT wiring, lambda bodies UNKNOWN (invokedynamic) |
| Decrypt | file + RSA privkey | raw bytes (ChaCha7539 raw stream, NO tag) | AudioDecryptor/AudioExporter | DIRECT; segment>0 → per-block keystream reset |
| Ogg walk | Ogg bytes | packets + OpusHead fields | OggOpusParser | DIRECT incl. quirks below |
| Chunking | opus bytes | channels·80 quanta (else RuntimeException) | k4/l4 | DIRECT |
| Opus decode | 80-B packet | short[len·4] PCM 16-bit | m4 + OpusUtils JNI | DIRECT shape; native internals UNKNOWN |
| PCM/WAV | short[] | LE16 bytes / 44-B 16 kHz WAV | AudioExporter | DIRECT order; size math standard |

Key numbers (all DIRECT): 16000 Hz, 320 samples (20 ms), 80 B/frame/ch,
512-B stream skip (== encryption header size), 45-B per-frame skip,
1440·ch payload per 1440·ch+96 wire frame, 51-B Ogg overhead per frame
(45+1440+51 == 1536 mono), 640 PCM bytes per mono frame == iOS 640-B
blePcmData, WAV 44 B @16 kHz, MP3 via Lame @16 kHz.

Quirks preserved, not repaired: OggOpusParser verifies no CRC/serial/seq,
drops page 1 unread, flushes trailing-255 lacing as complete (bytecode
offsets 432-442), loses cross-page packets; m4 returns the full
short[len·4] with trailing zeros; AudioDecryptor always uses keystream
index 0 while AudioExporter resets per segment block; getCounter() is
never consumed; PKCS#1 RSA keys fail (PKCS#8 only); putPkg return value
is discarded (`pop`) at every Java call site.

## 3. Transfer→audio boundary (§4 answer)

The SDK's transfer layer ends at raw file bytes: o4/v3 forward bytes
untouched to a FileOutputStream callback, and audio begins at the
decrypt/sniff/dispatch in AudioExporter. The 512-B header is consumed by
decryption (skip(512)), and h4's unconditional 512-B stream skip is the
second witness that transferred files carry the 512-B header region.
Whether the region is zeros when unencrypted is UNKNOWN. No wrapper
between TransferSession-equivalent and OggUtils beyond the file itself
was found. Lambda bodies behind invokedynamic are UNKNOWN by tooling,
not by absence.

## 4. Fixtures / third-party (§5–§6)

No `.ogg/.opus/.wav/.pcm` binaries anywhere in the repo (incl.
reference/). `docs/fixtures/*.json` hold records only. Third-party
(riffado: mono 16 kHz Opus/MP3, OggS sniffing; plaud-api/toolkit:
.opus/.mp3 cloud extensions; OpusRepair.kt: malformed OpusTags +
Ogg CRC poly 0x04C11DB7) corroborates container/rate facts only --
labeled third-party, never merged into bytecode claims. iOS
`.swiftinterface` + template (official-SDK): 640-B blePcmData @16 kHz
mono, `syncFile(decode)`, .ogg (iOS) vs .opus (Android) storage,
JXWaveHelper 16000, upload rejects WAV.

## 5. Decoder validation (§7)

Three claims kept separate: (1) generic decode mechanics -- NOT executed
(no libopus in this environment; no arbitrary audio downloaded);
(2) SDK expects Ogg/Opus -- DIRECT (sniff + dispatch + OpusHead parse);
(3) transferred files are Ogg/Opus -- SUPPORTED but not closed (framing
math + header-size witnesses; needs a real captured file to close).
Structural tests cover everything executable.

## 6. Correction to prior notes (§12)

Ledger §8's "trailing lacing 255 flushed as complete" was re-derived
from bytecode and is CORRECT (an in-sprint misreading said "discarded";
adjudicated at OggOpusParser offsets 426-442). h4.hasCompleteTail is
`position == i·idx+27(+idx)` (i = 80·ch), not a squared form. Nothing
else in frozen R3/audio notes changed.

## 7. Implementation

New `emulator/plaudsim/audio.py` (proven contracts only: Ogg walk,
OpusHead, page roles, lacing, chunking, m4 shape, h4 spans/tail, LE16,
WAV header). No transfer changes, no ASR/diarization deps.

## 8. Unknowns / next slice

Native putPkg/-2 semantics and libopus internals (symbols confirmed,
behavior not traced); invokedynamic lambda bodies; streaming-pipeline
liveness; unencrypted-header zeros; device ACK/SEQ equivalents n/a.
Next slice: synthesize a spec-conformant Ogg/Opus fixture with an
external encoder and run it through `audio.py` + a real Opus decoder
once a decoder library is available -- closes claims (1) and (3) above.
