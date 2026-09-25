# Layer 2 — synthetic meeting generator

`generator/` produces meeting directories whose transcript, speaker turns,
word timings and sample-level activity are **facts of construction**: the
waveform is assembled from them, so nothing is detected, aligned or
estimated. It runs fully offline (no model weights, no corpora, no network)
and is byte-deterministic for a given scenario and seed.

Status: implemented and tested (`tests/test_generator_*.py`,
`tests/test_v4_der_roundtrip.py`). V4 — "the generator's ground truth
round-trips through `pyannote.metrics` at DER 0" — passes with DER and JER
exactly `0.0`, and a shifted copy scores above zero.

```
scenario (YAML/JSON/preset)
   │
   ├─ text.py      seeded lowercase tokens (or a caller's corpus file)
   ├─ tts/         synth(text, voice, seed) -> int16 16 kHz + exact word timings
   ├─ turns.py     turn table on a 1/128 s grid, overlap controller  ← GROUND TRUTH
   ├─ room.py      pyroomacoustics ISM: per-(mic, speaker) RIRs, wet stems, noise
   ├─ devices.py   mic presets (counts evidenced, geometry HARNESS_POLICY), device mix
   ├─ opus.py      Ogg/Opus (PyAV/libopus, CBR 80 B/frame/ch), raw packets, g4 framing
   ├─ e2ee.py      512-byte PLAUD.AI header + raw ChaCha20 payload
   └─ export.py    meeting.json, ref.rttm, ref.stm, WAVs, activity.npy, device/*
```

## 1. Ground truth by construction

| artefact | how it is produced | exactness |
|---|---|---|
| `segments[*].text` | the text source emits the tokens; the TTS renders exactly those tokens | exact |
| `segments[*].words[*].start/end` | the formant backend concatenates one rendered piece per word with **digital-silence gaps**; a word's span is the index range of its piece | sample-exact (`timing_exact: true`) |
| `segments[*].start/end` | turn start = planner cursor rounded **down** to the 1/128 s grid; end = start + utterance length rounded **up** | exact; a turn holds ≤ 124 samples of trailing silence |
| `activity.npy` | `mask[spk, start:end] = True` straight from the table | sample-exact |
| `stems/spkN.wav` | the utterance PCM placed at its start sample (same-speaker turns never overlap) | exact; zero outside the speaker's turns |
| `ref.rttm` / `ref.stm` | formatted from the table (`contract.format_rttm/format_stm`) | exact: k/128 values are dyadic, so `start + duration` is exact in IEEE doubles and `%.7f` text parses back to the identical double |
| `mix.wav`, `mics.wav`, `device/*` | room simulation + mixing + encoding of the stems | derived audio; lags the ground truth by each speaker's `direct_path_delay_s` (distance/343 m·s⁻¹, a few ms) |

The dyadic grid is what makes V4 a certainty: pyannote loads the RTTM to the
same doubles the mask yields, so DER/JER are exactly `0.0` with no tolerance
involved (`tests/test_v4_der_roundtrip.py`).

Overlap is controlled, not sampled: before each turn of a different speaker
the planner computes the overlap that brings the running ratio
(overlapped / union of speech) to the target, clipped to half of the shorter
turn, so three speakers never stack and the realised ratio lands within
±0.04 of the target on a 60 s meeting (`test_generator_turns.py`). Same-speaker
consecutive turns never overlap.

## 2. The meeting directory (Layer 2 → Layer 3 contract)

```
meeting.json         schema plaud-harness/meeting/1 (see generator/contract.py)
ref.rttm             SPEAKER <id> 1 <start> <dur> <NA> <NA> <spk> <NA> <NA>
ref.stm              <id> 1 <spk> <start> <end> <text>
mix.wav              device mix, scenario.device.channels (1|2), 16 kHz PCM16, 44-byte SDK header
mics.wav             raw simulated capsule channels (1, 2 or 4)
stems/spkN.wav       DRY per-speaker stems
activity.npy         np.packbits(mask, axis=1); audio.activity.n_samples gives the width
device/recording.ogg          plain_ogg           mono                       (a)
device/recording_stereo.ogg   plain_ogg           stereo                     (a)
device/recording_raw.opus     plain_raw_opus      bare 80-byte packets       (b)
device/recording_g4.bin       plain_raw_opus      g4 page framing            (b)
device/recording_e2ee.bin     encrypted_unresolved 512-byte PLAUD.AI header   (c)
```

`meeting.json` additionally carries `audio.device_details` (bytes, sha256,
classified shape, Ogg report, g4 page count, the synthetic E2EE key/nonce),
`device` (preset, capsule positions, evidence and policy strings), `room`
(absorption, image order, requested vs measured RT60), `noise`, `turn_taking`
(target vs realised overlap) and `generator.scenario` (the full input).
Times are seconds, speakers are `spk0..`, text is lowercase single-spaced
tokens. `generator.contract.validate_meeting` / `load_meeting` enforce it and
`python -m generator validate DIR` cross-checks RTTM/STM against it.

## 3. Evidence table — every device-shape claim

Provenance classes follow `emulator/plaudsim/audio.py` (DIRECT, INHERIT,
POLICY, UNKNOWN) and `docs/protocol-ledger.md` (SOURCE-DERIVED,
MECHANICALLY_PROVEN). Line numbers are into `build/evidence/javap/ALL.txt`
unless stated.

| claim encoded by the generator | class | where |
|---|---|---|
| 16 000 Hz, 320-sample (20 ms) frames | DIRECT | `audio.SAMPLE_RATE_HZ`, `audio.FRAME_SAMPLES` (OggUtils 16000/320) |
| exactly 80 bytes per frame per channel; no VBR path | DIRECT + SOURCE-DERIVED | `audio.OPUS_FRAME_BYTES`; `BleFile.calculateOpusOffset` ALL.txt:10429, `calculateOpusDuration` :10467, `calculateOpusFileSize` :10479; ledger §8 "Codec parameters" (libjni_ogg `putPkg` rejects off-size packets) |
| recordings are 1 or 2 channels | SOURCE-DERIVED | `BleFile(..., ch)` arithmetic, ledger §8; `audio.frame_bytes(channels)` |
| shape space = {plain, encrypted} × {ogg, raw}; tested in that order | MECHANICALLY_PROVEN | `audio.classify_recording` (AudioExporter: `fromFile`/`isEncrypted` ALL.txt:96528-96533, 512 skip :96623, `bipush 79` "OggS" compare :96951, format log :96992-96997) |
| "OggS" 4-byte sniff, shorter files are not Ogg | DIRECT | `audio.classify_container`, `OGG_MAGIC_BYTES` |
| Ogg page roles by count: 0 head, 1 tags (discarded unread), 2+ audio | DIRECT | `audio.classify_ogg_pages` (OggOpusParser) |
| OpusHead read at ch@9, preSkip u16le@10, rate u32le@12 | DIRECT | `audio.parse_opus_head` |
| a packet split across pages is mis-parsed, so the writer must never split one and must keep OpusTags to one page | DIRECT + ledger §8 "Quirks" | `audio.reassemble_page_packets`; enforced by `opus.check_sdk_ogg_constraints` |
| the SDK's parser skips serial/CRC/granule | DIRECT | `audio.parse_ogg_page` (20-byte skip, `crc_validated=False`) — why rewriting the serial is invisible to the SDK |
| g4 geometry: 512-byte lead, 32/43-byte page header, 5/8 packets per page, whole strides only | MECHANICALLY_PROVEN | `audio.g4_frame_params`, `g4_payload_spans`, `build_g4_stream` (g4.<init> bytecode) |
| 512-byte PLAUD.AI header layout and field sizes | SOURCE-DERIVED | ledger §8 lines 1100-1125; constructor `PlaudEncryptHeader(byte[])` ALL.txt:125016-125147 (8/u16/u16/u32/32/u16/u16/u16/u32/70/u32/12/u32/108/256); guard "Data must be at least 512 bytes" :125147; `HEADER_SIZE`/`MAGIC_STRING` :124983-124986; `isEncrypted` :125195 |
| payload = raw ChaCha20 (RFC 7539), no Poly1305, same key/nonce/counter 0 per segment; key = RSA-unwrapped 256-byte keyCipher | SOURCE-DERIVED | ledger §8 "File encryption" lines 1178-1207 |
| only magic, nonce, segment, keyCipher are consumed; `AudioDecryptor` ignores `segment` | SOURCE-DERIVED | ledger §8 (hence `segment = 0`) |
| 44-byte WAV header field order | DIRECT | `audio.wav_header_44` (AudioExporter) |
| BLE DATA payloads are the stored file byte for byte | MECHANICALLY_PROVEN | `audio.py` R7 note (t7.d() → o4 pass-through); `test_generator_transfer.py` serves the mono Ogg through `TransferSession` and the Bumble link |
| Note Pro 4 MEMS + 1 VPU; NotePin S 2 MEMS | OFFICIAL_DOC | `docs/architecture/hardware.md:26`, `docs/architecture/audio.md` §1 |
| which of the four shapes real firmware emits over BLE | **UNKNOWN (U20)** | `docs/architecture/audio.md` §1 — the generator emits all four and labels each |

The ChaCha20 primitive is pinned to the RFC 7539 §2.4.2 test vector in
`test_generator_e2ee.py`, which also fixes the (counter ‖ nonce) layout.

## 4. HARNESS_POLICY register (harness choices, not device claims)

Geometry and acoustics
- Capsule geometries: `mono` one point; `notepin_s_2mic` two capsules 30 mm apart on x; `note_pro_4mic` four capsules at the corners of a 50 × 30 mm rectangle. Real geometries are UNKNOWN.
- The VPU (vibration pickup) is not simulated; body conduction is outside an image-source model.
- Device mix: mono = mean of all capsules; stereo = mean of each preset's left/right group. The firmware's 4→1/2 reduction is UNKNOWN.
- Speakers auto-placed on a ring of radius 1.2 m around the device at 1.2 m height, clamped 0.3 m from walls.
- Shoebox room; RT60 → `pyroomacoustics.inverse_sabine`; image order capped at 24 (`DEFAULT_MAX_ORDER_CAP`) for runtime; the measured RT60 is reported next to the requested one.
- Wet audio keeps the physical propagation delay (`direct_path_delay_s` per speaker, distance/343 m·s⁻¹); pyroomacoustics' 40-sample fractional-delay lead is stripped from every RIR so that delay is exact in the audio.
- Levels: mic array and device mixes peak-normalised together to 0.9 FS (`gain` in meeting.json).
- Noise: "white" = independent Gaussian per channel (sensor noise) at the requested SNR over speech-active samples; "musan" = one WAV from a MUSAN-style directory (never downloaded; `data/corpora/musan/<category>` or `noise.musan_dir`), placed as a room source at a corner and convolved through the same room.

Speech and text
- The formant backend's vowel formant table, bandwidths, consonant classes, durations (vowel 110 ms, final 150 ms, fricative 70 ms, plosive 22 + 12 ms, sonorant 55 ms), 40–90 ms word gaps, f0 declination 1.08 → 0.92, 4 ms fades, −8 dBFS peak, and the eight `fv-*` voices (f0 80–270 Hz). None models a real speaker; the audio is speech-like, not intelligible.
- Vocabulary (≈190 letters-only words) with rank^−0.8 weighting; corpus windows when a corpus is given.
- Turn model defaults: alternating; pauses uniform 0.2–1.0 s (or exponential/fixed); 4–14 words per turn; 0.5 s lead-in; `p_self_continue` 0.15 in the random model; overlap ≤ 0.45 and ≤ half of the shorter turn.
- Voices assigned round-robin from the backend's list, rotated by the seed.
- Boundary grid 1/128 s (125 samples).

Encoding and files
- libopus via PyAV: hard CBR (`vbr=off`), 20 ms frames, application `voip`, complexity 5 (encode-time trade-off; the firmware's complexity is UNKNOWN), 32 kbps per channel (the only setting yielding 80 B/frame/ch).
- Ogg serial rewritten to `0x53594E31` ("SYN1") with page CRCs recomputed, because libavformat picks a random serial per encode and the harness requires byte-identical output.
- OpusHead/OpusTags are libavformat's (pre-skip 312, vendor "Lavf…", EOS set). The SDK's own writer's quirks (pre-skip 0, inflated granules, leaked OpusTags bytes, no EOS, vendor `TinnoTech123456789012`) are documented in ledger §8 and **not** reproduced.
- The bare packet stream is the mono Ogg's packets concatenated; the g4 stream drops the trailing packets that do not fill a whole page (reported as `packets_dropped_at_tail`) and uses a zero lead and zero page headers (g4 never reads them).
- E2EE values: synthetic 32-byte key `SYNTHETIC-E2EE-KEY-…`, 12-byte nonce `SYN-E2EE-NON`, userId `SYNTHETIC-USER-ID`, keyCipher = labelled filler (not RSA-2048), version 1, headerSize 512, crc 0, fileType 0, encryptType 0, counter 0, segment 0, zero reserved/algParams. The key/nonce are written into meeting.json so the file is openable; it is not decryptable by any real Plaud key.
- `TransferSession` payload size in the linked test: 240 bytes (u8 field, under the SDK's 255-byte MTU minus overheads).
- File layout, names, JSON key order, `activity.npy` packing, `meeting_id = synth-<name>-s<seed:04d>`.

## 5. Running it

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator presets
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator make --scenario default --out build/synthetic/demo --seed 7
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator make --scenario my.yaml --out DIR --set noise.kind=white --set noise.snr_db=10
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator batch --n 5 --scenario overlap_heavy --seed 100 --out build/synthetic/set1
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator validate build/synthetic/demo
./scripts/make-synthetic-set.sh                      # 3 × default under build/synthetic/default
N=5 SCENARIO=notepin_s_noisy ./scripts/make-synthetic-set.sh
```

Presets (all ≤ 60 s): `smoke` (2 spk, 12 s, mono), `default` (3 spk, 40 s,
Note Pro, 10 % overlap, 25 dB white), `overlap_heavy` (4 spk, 60 s, random
turns, 30 % overlap), `notepin_s_noisy` (2 spk, 45 s, NotePin S stereo, 10 dB
white), `single_speaker` (30 s). `python -m generator presets --yaml` prints
them as editable scenario files; every key accepts `--set dotted.key=value`.

Runtime: generation is sub-second; the encoder dominates (≈ 1 s per 20 s of
audio per Ogg at complexity 5). The default preset takes ≈ 6 s end to end.

## 6. Plugging in a real TTS

Implement `generator.tts.base.TTSBackend`: `voices()` and
`synth(text, voice, seed) -> SynthResult` with 16 kHz mono int16 PCM and one
`WordTiming` per input token; register it in `generator.tts.BACKENDS`.
`SynthResult.__post_init__` enforces monotonic, in-range timings. Set
`timing_exact=False` unless the boundaries are facts of construction; the
planner propagates the flag into `meeting.json.generator.timing_exact` so
downstream evaluation knows word timings are estimates. Two guarded examples
ship: `tts/piper.py` (needs `piper-tts` and `data/voices/*.onnx`; proportional
word timings) and `tts/kokoro.py` (needs `kokoro`, torch and local weights,
`HF_HUB_OFFLINE=1`; token timestamps when the model provides them). Both raise
`BackendUnavailable` here and never download anything; `python -m generator
backends` reports why.

## 7. Tests

`tests/test_generator_{text,tts,turns,room,e2ee,scenario,export,transfer,cli}.py`
and `tests/test_v4_der_roundtrip.py` (82 tests). Highlights that challenge
failure modes rather than echo fixtures: byte-level determinism across two
generations; speaking-rate/duration and f0/autocorrelation analytic checks
on the formant model; overlap targets realised within ±0.04 and no
three-way stacking; direct-path delay and 1/r² energy in an anechoic room;
SNR realised within 0.3 dB; RFC 7539 vector; header offsets read raw, not via
the packer; every device file classified by the emulator's own
`classify_recording`; Ogg CRC implementation checked against libavformat's
stored CRCs and a flipped bit; decode duration within one frame plus a
lag-searched correlation with a shifted-copy falsifier; the mono Ogg served
by `TransferSession` and over the Bumble GATT link, reassembled byte-exact;
DER/JER exactly 0.0 against self and mask, > 0 for shifted/dropped copies,
0.0 for a relabelling.

## 8. Not done, and why

- **Intelligible speech.** The required offline backend is speech-like pseudo-speech; ASR word-error evaluation on it is meaningless. Real TTS is pluggable (§6) but no weights are present and none may be downloaded.
- **Real device audio.** Nothing here is a Plaud capture; which of the four shapes real firmware emits (U20), the firmware's Opus settings, its 4→1/2 channel DSP, the VPU path and the real capsule geometry all need a device.
- **The SDK writer's Ogg quirks** (ledger §8) are not reproduced; the generator writes conformant Ogg that the SDK parser accepts. A "quirks" mode would need a bespoke muxer.
- **Real E2EE.** The keyCipher is a filler; no RSA key material exists or is fabricated. Only the shape (header + raw ChaCha20 body) is realised.
- **Corpora.** MUSAN/RIRS are not downloaded (`scripts/fetch-datasets.sh`); the MUSAN path is exercised in tests with synthetic WAV files only. Measured RIRs (RIRS_NOISES) are not wired in.
- **Beyond shoebox ISM:** no directivity, scattering, air absorption or moving speakers; no laughter/backchannels/disfluencies in the turn model.
- **Hypothesis files** (`hyp.json/.rttm/.stm`) are the pipeline's output; the generator only defines the schema constant (`contract.SCHEMA_HYPOTHESIS`).

## 9. Files

`generator/{__init__,__main__,_evidence,cli,contract,devices,e2ee,export,meeting,opus,room,scenario,testing,text,turns}.py`,
`generator/tts/{__init__,base,formant,piper,kokoro}.py`, `scripts/make-synthetic-set.sh`,
`requirements/generator.txt` (no packages added), tests listed in §7.
