# Audio layer — from the microphone to the transcript

Sources: the AAR bytecode and native libraries (R6/R7, ledger §8),
`emulator/plaudsim/audio.py`, the template apps and wrappers (export
pipeline), the official docs, and the community clients that hold real cloud
artefacts (riffado regression #160, applaud, plaud-toolkit). Classes as
elsewhere. **No authentic Plaud recording exists in this project** (R6-S3
sweep); every statement about bytes is about what the code enforces or what
community captures show, never about a file we hold.

## 1. Capture (device) — OFFICIAL_DOC + bytecode

- Note Pro: 4 MEMS + 1 VPU (vibration pickup); NotePin S: 2 MEMS. "Smart
  dual-mode (calls + in-person)" on Note Pro. Recording modes exposed as
  settings: `RecScene` {Normal, Interview, Classroom, Music, Meeting, Memo},
  `RecMode` {Normal, NC (noise-cancelling)}, `VadSensitivity` {Quality,
  lowBitrate, Normal, Aggressive}, `ENABLE_VAD`, `VPU_GAIN`, `MIC_GAIN`,
  `VPU_CLK`, `SAVE_RAW_FILE` ("RawWaveEnabled"). Product meanings of the values
  are UNKNOWN; the SDK reads `ENABLE_VAD` and `REC_MODE` right after connect
  (RUNTIME_PROVEN) and stores them on `BleDevice` (`setVadOpen(v==1)`,
  `setNcClose(v!=2)`).
- On-device format the SDK **enforces**: Opus, 16 kHz, 20 ms (320-sample)
  frames, **exactly 80 bytes per frame per channel** (= 32 kbps/channel CBR;
  `libjni_ogg` rejects off-size packets), 1 or 2 channels (`BleFile.channels`;
  `BleFile.calculateOpusDuration(fileSize, channels)` = 80 B / 20 ms / ch).
  Either a complete Ogg stream or a bare concatenation of fixed-size Opus
  packets, optionally behind a 512-byte `PLAUD.AI` E2EE header. The
  SDK-written Ogg carries OpusTags vendor `TinnoTech123456789012` and several
  non-conformant quirks (ledger §8). **Which of the four shapes real firmware
  emits over BLE is U20 (EXTERNAL_DATA_REQUIRED).**
- Files are keyed by `sessionId` (unix seconds); max single file 5 h
  (OFFICIAL_DOC; no constant found in any binary).

## 2. Transfer (BLE / Wi-Fi) — bytecode

DATA payloads are a pure pass-through (`o4`), so the concatenated payloads
equal the stored file byte-for-byte. Wi-Fi fast transfer moves the same bytes
over a WebSocket (device → phone) sealed with ChaCha20-Poly1305 or AES-GCM
(capability bit 3). `IWifiTransferAgent.downloadFile/downloadAllFiles` "write
undecrypted `.opus` bytes straight to disk" (OFFICIAL_DOC) — i.e. the raw
artefact keeps its E2EE header; `exportAudio*` runs the full pipeline.

## 3. Decode / export (SDK on the phone) — bytecode + OFFICIAL_SOURCE

```
raw file ─▶ AudioExporter
   ├─ isE2eeEncrypted?  (len ≥ 512 && magic "PLAUD.AI") ─▶ skip 512, ChaCha20 (raw ChaCha7539, no Poly1305) with the device key material
   ├─ sniff 4 bytes == "OggS" ?  ─▶ Ogg/Opus branch  (isOggAudio profile bit is WRITTEN but never READ — sniff-based; pinned by test)
   │                              ─▶ raw fixed-packet branch
   └─ decode (libopus) ─▶ PCM 16 kHz ─▶ PCM | WAV | OPUS | MP3 (liblame)   AudioExportFormat pcm=0 mp3=1 wav=2 opus=3
```

- `ExportStage DOWNLOADING → TRANSCODING`; progress 0–99 = bytes, 100 =
  local transcode (changelog v1.0.6 adds speed reporting).
- Output dirs: iOS `Documents/PlaudExports`, Android `filesDir/PlaudExports`
  (SDK) — the templates instead export MP3 mono into `Documents/` (iOS) or the
  **evictable** `cacheDir/export` (Android). MP3 is recommended because it
  "plays everywhere and is accepted by the transcription upload API";
  the templates' comments say WAV is rejected (`FILE_TYPE_INVALID`) and "the
  backend's opus pipeline currently returns empty results" (CLAIMS).
- Live PCM: iOS receives `blePcmData` (640-byte 16 kHz mono chunks) after
  `syncFile(sessionId, start, 0)` on record-start; Android has no such
  callback in the AAR (waveform never fed).
- The E2EE header (`PlaudEncryptHeader`: magic, version, headerSize, crc,
  userId, fileType, channel, encryptType, duration, counter, nonce, segment,
  algParams, keyCipher) binds a file to a `userId`; `decryptE2EEAudioFile`
  takes a private-key PEM (which key — `gen-key`'s — is UNKNOWN). The public
  `AudioDecryptor` helper ignores `segment`, unlike `AudioExporter` (ledger §8).

## 4. Upload (partner and consumer) — OFFICIAL_DOC / CLOUD_OBSERVED

Partner: `filetype` `mp3|opus`, 5 MiB parts, `file_md5` optional, S3 bucket
`plaud-bucket`; transcription accepts a public URL of "M4A, MP3, WAV" (docs
disagree with the upload API's own set). Consumer: `file_type MP3|OPUS`,
confirm with `scene: 101`. Retention of uploaded *audio* after the 24 h
`DownloadUrl` expires is UNKNOWN (only transcripts have a stated 7-day
retention on the partner surface).

## 5. Cloud artefacts — CLOUD_OBSERVED, CORROBORATED

- Consumer downloads (`/file/temp-url` with `is_opus=0|1`, `/file/download`)
  are **Ogg/Opus with OpusTags vendor `PALUD.AI`**, served as `.mp3` /
  `audio/mpeg` even when `is_opus=0` (riffado regression #160; applaud writes
  `audio.ogg`; plaud-toolkit sniffs ID3/MPEG sync before trusting the name).
  `PALUD.AI` appears nowhere in the SDK — and R7-S13 showed the SDK's OPUS
  export of a plain-Ogg recording is a byte-exact passthrough (vendor tag
  untouched, no re-encode) — so the cloud re-encodes or re-tags
  (U19). The MCP `get_file.presigned_url` is the same 24 h artefact.
- Therefore **BLE bytes = SDK-local file (proven) ≠ cloud artefact (evidenced)**:
  cloud downloads must not be used as fixtures for the local writer or the
  BLE path.
- plaud-toolkit's belief that the `.opus` variant is "encrypted" and that the
  MP3 appears only after the recording is opened in the Plaud app is UNVERIFIED
  and contradicted by riffado's plain-Ogg capture; a lazily-generated
  transcode variant is INFERRED, not shown.

## 6. Transcription input — OFFICIAL_DOC

The partner API runs "noise reduction, speaker detection, language
recognition" and ASR (`plaud-fast-whisper` | `plaud-omni-3` |
`azure-fast-transcribe`), optional diarization with 256-d speaker embeddings,
`hotwords`, `detection_level segment|chapter`, `vad.decode_silence`;
limits 60 req/min, 24 h max, 6 h with diarization, 5 h recommended chunking.
See `ai.md`.

## 7. Ownership

| step | owner |
|---|---|
| capture, on-device format, encryption at rest, sessions | device (TinnoTech platform firmware) |
| transfer (BLE/Wi-Fi), decrypt, decode, transcode | SDK on the phone |
| where the file lands, when the device copy is deleted, which format to keep | partner app (policy) |
| upload, presign, storage, re-encode/re-tag, delivery | cloud |
| ASR/diarization | cloud |

## 8. Open items

U19 (cloud transcode vs re-tag), U20 (real on-device shape), audio retention
in S3, the value semantics of the recording settings, the `is_opus` toggle,
and whether `SAVE_RAW_FILE` produces a second artefact. All need a device or a
recording; none can be settled from the corpus.
