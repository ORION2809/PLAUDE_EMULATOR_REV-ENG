# Layer 2 — synthetic meeting generator

`generator/` produces meeting directories whose transcript, speaker turns,
word timings and sample-level activity are **facts of construction**: the
waveform is assembled from them, so nothing is detected, aligned or
estimated. It runs fully offline (no model weights, no corpora, no network)
and is byte-deterministic for a given scenario and seed. The optional piper
backend (§6.2) adds real, intelligible speech from a local voice file. Its
word timings come from the voice's own duration alignment, and it is
byte-deterministic on one machine at a pinned thread count.

Status: implemented and tested (`tests/test_generator_*.py`,
`tests/test_v4_der_roundtrip.py`). V4 — "the generator's ground truth
round-trips through `pyannote.metrics` at DER 0" — passes with DER and JER
exactly `0.0`, and a shifted copy scores above zero.

Review fixes (2026-09-25, findings GEN-1…GEN-9): the overlap planner no longer
lets grid rounding exceed its caps (it used to stack three speakers and let a
speaker overwrite their own previous turn); `noise.snr_db` is now the SNR of
`mix.wav` rather than of one capsule; `audio.device_primary` is a validated
contract field; the g4 file is labelled `g4_framed` rather than as the SDK's
raw shape, and the fourth shape (`encrypted_raw_opus`) is now emitted; the
raw shapes record the codec lookahead they carry; the piper backend targets
the current piper API; ledger citations are by heading; rewriting a meeting
directory leaves nothing stale; and the set script resolves a relative output
path against the caller's directory. Each fix has a test that failed before it.
Auto-assigned voices are no longer silently shared when `n_speakers` exceeds
the backend's voice count. `generator.version` is now 0.2.0: for the same
scenario and seed, meetings with overlap or noise differ from 0.1.0 output.

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
| … with the piper backend | a word spans the samples the voice's own duration output gives its phoneme ids, mapped from 22.05 kHz to 16 kHz by round-half-up (§6.2) | exact with respect to the model's hard alignment (`timing_exact: true`): frame-quantised at the model rate (256 samples), within 0.5 sample at 16 kHz; not an acoustic onset; gaps are model audio, not silence |
| `segments[*].start/end` | turn start = planner cursor rounded **down** to the 1/128 s grid, or, for an overlapping turn, the previous turn's end minus an overlap that is itself a whole number of grid steps; end = start + utterance length rounded **up** | exact; a turn holds ≤ 124 samples of trailing silence |
| `activity.npy` | `mask[spk, start:end] = True` straight from the table | sample-exact. Its runs are the turns, except that two turns of one speaker that touch (end == next start: a zero pause, or a resumption clamped to the speaker's own previous end) form one run while meeting.json and the RTTM keep two segments. DER/JER are unaffected |
| `stems/spkN.wav` | the utterance PCM placed at its start sample; a speaker's turns never overlap, and `build_dry_stems` refuses (ValueError) a table where they would rather than overwrite one turn with another | exact; each turn's PCM intact; zero outside the speaker's turns |
| `ref.rttm` / `ref.stm` | formatted from the table (`contract.format_rttm/format_stm`) | exact: k/128 values are dyadic, so `start + duration` is exact in IEEE doubles and `%.7f` text parses back to the identical double |
| `mix.wav`, `mics.wav`, `device/*` | room simulation + mixing + encoding of the stems | derived audio; lags the ground truth by each speaker's `direct_path_delay_s` (distance to capsule 0 / 343 m·s⁻¹, a few ms; other capsules differ by ≤ 0.2 ms at these geometries). The Ogg files add nothing more (their pre-skip is trimmed by the demuxer). The bare packet shapes (`recording_raw.opus`, `recording_g4.bin`, `recording_raw_e2ee.bin`) have no pre-skip carrier: a decoder outputs `codec_lookahead_samples_16k` (104) extra leading samples and `flush_packets` (normally 1) extra 20 ms frames, both recorded in `device_details` |

The dyadic grid is what makes V4 a certainty: pyannote loads the RTTM to the
same doubles the mask yields, so DER/JER are exactly `0.0` with no tolerance
involved (`tests/test_v4_der_roundtrip.py`).

Overlap is controlled, not sampled: before each turn of a different speaker
the planner computes the overlap that brings the running ratio
(overlapped / union of speech) to the target. It rounds that overlap to the
nearest grid step and caps it, on the grid, at `max_overlap_fraction` (≤ 0.5)
of the shorter of the two grid-quantised turns and at the end of every earlier
turn. The overlap is quantised, not the start: the previous end is on the
grid, so the realised overlap is exactly the capped value. Quantising the
start (the old code) could add up to 124 samples beyond the cap. That stacked
three speakers (`overlap_heavy` seed 1: 250 samples) and let a speaker overlap
their own previous turn, whose last word the stem then lost (seed 27).
Guarantees, property-tested over 70 preset seeds plus 36 seeds of a stress
configuration where every cap binds (1–3-word turns, overlap 0.45):

- a turn overlaps only its immediate neighbours, so at most two speakers are
  ever active and the running totals are exact;
- no overlap exceeds `max_overlap_fraction` of the shorter turn;
- a speaker never overlaps themself; every turn's PCM is intact in its stem.

Realised overlap vs target, measured over seeds 1–30: alternating turns land
within 0.0064 of 0.30 (`default` with overlap 0.3) and within 0.0001 for the
`default` (0.10) and `notepin_s_noisy` (0.05) presets. The random model can
fall short. A same-speaker continuation (`p_self_continue`) adds speech that
may not be overlapped, and catch-up is capped at half the shorter turn.
`overlap_heavy` (target 0.30) has mean error −0.004 and worst −0.043 (seed 10).
The earlier "±0.04 on a 60 s meeting" claim was a single seed.

## 2. The meeting directory (Layer 2 → Layer 3 contract)

```
meeting.json         schema plaud-harness/meeting/1 (see generator/contract.py)
ref.rttm             SPEAKER <id> 1 <start> <dur> <NA> <NA> <spk> <NA> <NA>
ref.stm              <id> 1 <spk> <start> <end> <text>
mix.wav              device mix, scenario.device.channels (1|2), 16 kHz PCM16, 44-byte SDK header
mics.wav             raw simulated capsule channels (1, 2 or 4)
stems/spkN.wav       DRY per-speaker stems
activity.npy         np.packbits(mask, axis=1); audio.activity.n_samples gives the width
                              shape                sniffed_shape         content
device/recording.ogg          plain_ogg            plain_ogg             mono Ogg/Opus                 (a)
device/recording_stereo.ogg   plain_ogg            plain_ogg             stereo Ogg/Opus               (a)
device/recording_raw.opus     plain_raw_opus       plain_raw_opus        (a mono)'s packets, back to back (b)
device/recording_g4.bin       g4_framed            plain_raw_opus        (b) in g4 page framing
device/recording_e2ee.bin     encrypted_ogg        encrypted_unresolved  512-byte header + ChaCha20(a mono)
device/recording_raw_e2ee.bin encrypted_raw_opus   encrypted_unresolved  512-byte header + ChaCha20(b)
```

`audio.device` maps a key (`ogg_opus`, `ogg_opus_stereo`, `raw_opus`,
`g4_raw_opus`, `e2ee_ogg`, `e2ee_raw_opus`) to each file written.
`audio.device_primary` names **the** device recording: the plain Ogg at the
meeting's channel count (`ogg_opus` for 1 channel, `ogg_opus_stereo` for 2).
`validate_meeting` rejects a `device_primary` that is not a key of
`audio.device`. A 2-channel scenario with `export.include_stereo_ogg=false` is
rejected at load time, because it would drop the primary. JSON keys are
sorted on disk, so the first entry of `audio.device` is the encrypted file.
Use `generator.contract.device_primary_path(meeting)`, never dict order. It
falls back to `ogg_opus` for producers that do not set the field. Only the
Ogg files exist in stereo; the raw, g4 and E2EE shapes are always made from
the mono mix.

Per file, `audio.device_details` gives: bytes, sha256, channels; `shape`, the
layout the generator vouches for (one of the SDK's four `RECORDING_SHAPES`,
or `g4_framed`); `sniffed_shape`, what `classify_recording` (AudioExporter's
two tests) sees before any decryption; and `sdk_exporter_layout`, true when
the bytes are laid out for one of AudioExporter's four branches. The writer
raises if a sniff differs from the intended one. It also decrypts both E2EE
files with the synthetic key and checks the plaintext container. The g4 file
sniffs as raw Opus, but AudioExporter's raw branch reads back-to-back
80·ch-byte packets, and g4 starts with a 512-byte lead and puts a 32-byte
page header before every 400 bytes. So it is `g4_framed` with
`sdk_exporter_layout: false`; only a g4 de-framer
(`audio.g4_payload_spans`) recovers its packets. Details also include the Ogg
report, g4 page and packet counts, and the synthetic E2EE key and nonce. The
bare-packet shapes add `codec_lookahead_samples_16k`, `flush_packets` and
`decoded_samples_16k`.

`meeting.json` also carries `device` (preset, capsule positions, evidence
and policy strings), `room` (absorption, image order, requested vs measured
RT60) and `noise` (see §4 for the SNR reference point and
`snr_db_realised`). It also carries `turn_taking` (target vs realised
overlap) and `generator.scenario` (the full input). Times are in seconds,
speakers are `spk0..`, and text is lowercase single-spaced tokens.
`generator.contract.validate_meeting` and `load_meeting` enforce the
contract. `python -m generator validate DIR` also cross-checks the RTTM and
STM, checks that every listed file exists, and reports any file under
`stems/`, `device/` or `mics.wav` that meeting.json does not list.

Rewriting a directory: `write_meeting` builds and validates everything in
memory first. It then removes the generator-owned entries of an existing
meeting directory (`meeting.json`, `ref.*`, `mix.wav`, `mics.wav`,
`activity.npy`, and `stems/` and `device/` whole) before writing, so no
earlier meeting's stem, `mics.wav` or device file survives. It refuses, with
`FileExistsError` and without touching anything, a non-empty directory that
holds any other non-hidden entry (for example a `hyp.json` written into the
meeting directory). Remove such files or regenerate into a fresh directory.

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
| 512-byte PLAUD.AI header layout and field sizes | SOURCE-DERIVED | ledger §8 "Recorded audio" (header table at the top of the section); constructor `PlaudEncryptHeader(byte[])` ALL.txt:125016-125147 (8/u16/u16/u32/32/u16/u16/u16/u32/70/u32/12/u32/108/256); guard "Data must be at least 512 bytes" :125147; `HEADER_SIZE`/`MAGIC_STRING` :124983-124986; `isEncrypted` :125195 |
| payload = raw ChaCha20 (RFC 7539), no Poly1305, same key/nonce/counter 0 per segment; key = RSA-unwrapped 256-byte keyCipher | SOURCE-DERIVED | ledger §8 "File encryption" |
| with `segment == 0` AudioExporter runs ONE `ChaCha7539Engine` from counter 0 over the whole payload in 64 KiB reads (so one continuous `seal_recording` pass is what it decrypts); `segment > 0` restarts at counter 0 per segment | DIRECT | AudioExporter bytecode: `getSegment` ALL.txt:96618, `lcmp; ifle 402` :96662-96663, per-segment `decryptChaCha20(…, 0)` :96705, single engine :96756-96776, `sipush 65536` :96777, `processBytes` :96815 |
| only magic, nonce, segment, keyCipher are consumed; `AudioDecryptor` ignores `segment` | SOURCE-DERIVED | ledger §8 (hence `segment = 0`) |
| 44-byte WAV header field order | DIRECT | `audio.wav_header_44` (AudioExporter) |
| BLE DATA payloads are the stored file byte for byte | MECHANICALLY_PROVEN | `audio.py` R7 note (t7.d() → o4 pass-through); `test_generator_transfer.py` serves the mono Ogg through `TransferSession` and the Bumble link |
| Note Pro 4 MEMS + 1 VPU; NotePin S 2 MEMS | OFFICIAL_DOC | `docs/architecture/hardware.md:26`, `docs/architecture/audio.md` §1 |
| which of the four shapes real firmware emits over BLE | **UNKNOWN (U20)** | `docs/architecture/audio.md` §1. The generator emits all four (`plain_ogg`, `plain_raw_opus`, `encrypted_ogg`, `encrypted_raw_opus`) and labels each; the g4-framed file is a fifth layout for g4 consumers, not an AudioExporter shape |
| g4 has no call sites in the shipped app | SOURCE-DERIVED (as stated in the emulator's g4 note, not re-derived here) | `emulator/plaudsim/audio.py` g4 note (`t7.a()`/`t7.b()` have no call sites outside `t7`) |

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
- Noise: "white" = independent Gaussian per capsule (sensor noise); "musan" = one WAV from a MUSAN-style directory (never downloaded; `data/corpora/musan/<category>` or `noise.musan_dir`), placed as a room source at a corner and convolved through the same room.
- SNR reference point: `noise.snr_db` is the SNR of `mix.wav`, the device mix at `device.channels`. It is the mean speech power over the speech-active samples (any dry stem non-zero) divided by the mean noise power, both measured after the device mix; the noise is scaled so this holds exactly. Averaging k capsules lowers independent noise by about 10·log10(k) while the correlated speech stays. The old per-capsule definition therefore left `mix.wav` and every `device/*` file 3–6 dB cleaner than requested (the review measured 30.8 dB in the `default` preset's mono mix for 25 dB requested). `noise.snr_db_realised` reports `mix`, `mono_mix` (source of `recording.ogg` and the raw, g4 and E2EE files), `stereo_mix` (source of `recording_stereo.ogg`) and `capsules`; `snr_db_realised_ch0` is capsule 0 alone. When mono and stereo differ, the files differ. `notepin_s_noisy` (stereo, 10 dB) has `mix.wav` and `recording_stereo.ogg` at 10.0 dB, but `recording.ogg` and the raw, g4 and E2EE files at 12.8 dB. `default` has `mix.wav` at 25.0 dB, capsules at 19.2 dB and the stereo mix at 22.1 dB.

Speech and text
- The formant backend's vowel formant table, bandwidths, consonant classes, durations (vowel 110 ms, final 150 ms, fricative 70 ms, plosive 22 + 12 ms, sonorant 55 ms), 40–90 ms word gaps, f0 declination 1.08 → 0.92, 4 ms fades, −8 dBFS peak, and the eight `fv-*` voices (f0 80–270 Hz). None models a real speaker; the audio is speech-like, not intelligible.
- Vocabulary (≈190 letters-only words) with rank^−0.8 weighting; corpus windows when a corpus is given.
- Turn model defaults: alternating; pauses uniform 0.2–1.0 s (or exponential/fixed); 4–14 words per turn; 0.5 s lead-in; `p_self_continue` 0.15 in the random model; overlap ≤ 0.45 and ≤ half of the shorter turn; overlaps rounded to the nearest grid step before capping.
- Voices assigned round-robin from the backend's list, rotated by the seed; auto-assignment refuses `n_speakers` above the backend's voice count (8 for formant) rather than giving two speakers one voice. An explicit `speakers.voices` list may repeat a voice deliberately.
- Piper (§6.2): automatic voices come from one pinned model (`DEFAULT_VOICE_MODEL` = `en_US-libritts_r-medium` when present; otherwise the first model with a measured palette; otherwise even spacing with a `PaletteFallbackWarning`); the 16-voice libritts_r palette (interleaved low/high f0, spread over the 904 ids) and its seed-rotated odd-step selection; the aligned copy is used only when its sidecar's sha256 values match; noise scales pinned to 0; onnxruntime pinned to 1 intra-op thread; a sentence-final `.` for prosody; context phonemization with a per-token fallback; a phoneme owns its following PAD; lead-in and tail cut at the first/last word boundary; 4 ms edge fades; −8 dBFS peak per utterance.
- Boundary grid 1/128 s (125 samples).

Encoding and files
- libopus via PyAV: hard CBR (`vbr=off`), 20 ms frames, application `voip`, complexity 5 (encode-time trade-off; the firmware's complexity is UNKNOWN), 32 kbps per channel (the only setting yielding 80 B/frame/ch).
- Ogg serial rewritten to `0x53594E31` ("SYN1") with page CRCs recomputed, because libavformat picks a random serial per encode and the harness requires byte-identical output.
- OpusHead/OpusTags are libavformat's (pre-skip 312, vendor "Lavf…", EOS set). The SDK's own writer's quirks (pre-skip 0, inflated granules, leaked OpusTags bytes, no EOS, vendor `TinnoTech123456789012`) are documented in ledger §8 and **not** reproduced.
- The bare packet stream is the mono Ogg's packets concatenated, so it starts with libopus's 104-sample (16 kHz) lookahead, which the Ogg's pre-skip hides, and ends with a flush packet. The g4 stream drops the trailing packets that do not fill a whole page (reported as `packets_dropped_at_tail`) and uses a zero lead and zero page headers (g4 never reads them).
- `audio.device_primary` = the plain Ogg at the meeting's channel count.
- E2EE: both the mono Ogg and the bare packet stream are sealed (`include_e2ee` controls both). Values: synthetic 32-byte key `SYNTHETIC-E2EE-KEY-…`, 12-byte nonce `SYN-E2EE-NON`, userId `SYNTHETIC-USER-ID`, keyCipher = labelled filler (not RSA-2048), version 1, headerSize 512, crc 0, fileType 0, encryptType 0, counter 0, segment 0, zero reserved/algParams. The key/nonce are written into meeting.json so the file is openable; it is not decryptable by any real Plaud key.
- `TransferSession` payload size in the linked test: 240 bytes (u8 field, under the SDK's 255-byte MTU minus overheads).
- File layout, names, JSON key order, `activity.npy` packing, `meeting_id = synth-<name>-s<seed:04d>`; the rewrite rule above (clear generator-owned entries, refuse foreign ones).

## 5. Running it

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator presets
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator make --scenario default --out build/synthetic/demo --seed 7
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator make --scenario my.yaml --out DIR --set noise.kind=white --set noise.snr_db=10
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator batch --n 5 --scenario overlap_heavy --seed 100 --out build/synthetic/set1
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator validate build/synthetic/demo
./scripts/make-synthetic-set.sh                      # 3 × default under build/synthetic/default
N=5 SCENARIO=notepin_s_noisy ./scripts/make-synthetic-set.sh
./scripts/make-synthetic-set.sh my-set               # relative to YOUR cwd, not the repo root
PYTHON=/path/to/python ./scripts/make-synthetic-set.sh   # interpreter override (default .venv)
```

Presets (all ≤ 60 s): `smoke` (2 spk, 12 s, mono), `default` (3 spk, 40 s,
Note Pro, 10 % overlap, 25 dB white), `overlap_heavy` (4 spk, 60 s, random
turns, 30 % overlap), `notepin_s_noisy` (2 spk, 45 s, NotePin S stereo, 10 dB
white), `single_speaker` (30 s), and two that need the local Piper voice
(§6.2): `piper_smoke` (2 spk, 25 s, mono, 10 % overlap) and `piper_meeting`
(2 spk, 60 s, Note Pro). Longer Piper meetings override `duration_s` and the
rest with `--set` (§6.3). `python -m generator presets --yaml` prints
them as editable scenario files; every key accepts `--set dotted.key=value`.

Runtime: generation is sub-second; the encoder dominates (≈ 1 s per 20 s of
audio per Ogg at complexity 5). The default preset takes ≈ 6 s end to end.

## 6. TTS backends

### 6.1 Plugging in a real TTS

Implement `generator.tts.base.TTSBackend`: `voices()` and
`synth(text, voice, seed) -> SynthResult` with 16 kHz mono int16 PCM and one
`WordTiming` per input token; register it in `generator.tts.BACKENDS`.
`SynthResult.__post_init__` enforces monotonic, in-range timings. Set
`timing_exact=False` unless the boundaries are facts of construction; the
planner propagates the flag into `meeting.json.generator.timing_exact` so
downstream evaluation knows word timings are estimates. A backend may also
offer `assign_voices(n_speakers, seed)`; `generator.meeting.pick_voices` then
uses it instead of the round-robin over `voices()` (piper does, §6.2). Neither
optional backend ever downloads anything; `python -m generator backends`
reports why one is unavailable.

- `tts/piper.py` gives real, intelligible speech with exact word timings
  (§6.2). It needs the `piper-tts` package and a local voice; without them it
  raises `BackendUnavailable`.
- `tts/kokoro.py` needs `kokoro`, torch and local weights, with
  `HF_HUB_OFFLINE=1`. It uses token timestamps when the model provides them
  (estimates, `timing_exact=False`). It is untested here.

### 6.2 Piper: real speech, word boundaries from the voice's own alignment

Inputs, all local and git-ignored (none is fetched by the harness):

| input | version / sha256 | licence |
|---|---|---|
| `piper-tts` (installed in `.venv` by the lead) | 1.8.0, pinned in `requirements/models.txt`. That file is optional and is not included by `all.txt` or `ci.txt`, so CI does not have piper-tts. The installed `voice.py` differs from the vendored `reference/upstream/piper` only by a Lithuanian phonemizer branch and the espeak lock; `patch_voice_with_alignment.py` is identical | GPL-3.0-or-later (package metadata). The harness imports it only when the piper backend is selected |
| `onnxruntime` | 1.30.0 (`requirements/models.txt`; a dependency of piper-tts) | MIT |
| `onnx` | 1.23.0 (`requirements/models.txt`). **Not** a dependency of piper-tts: it is the `[alignment]` extra (and in `train`/`dev`), so plain `pip install piper-tts` does not bring it. Needed only to make the aligned copy, or to patch the voice in memory when no verified copy exists | Apache-2.0 |
| `data/voices/piper/en_US-libritts_r-medium.onnx` | `10bb85e071d616fcf4071f369f1799d0491492ab3c5d552ec19fb548fac13195` (78 580 914 B) | see source and caveat below |
| `…/en_US-libritts_r-medium.onnx.json` | `b471dc60d2d8335e819c393d196d6fbf792817f40051257b269878505bc9afb3` (20 123 B); 904 speakers, 22 050 Hz, hop 256, espeak `en-us`, inference noise 0.333 / length 1 / noise_w 0.333 | |
| `…/MODEL_CARD` | `0ccde6927e5bb4d743f4ea39618a9387ba18cca3351220a8a9cfdbc68b30fcb9` (279 B) | |
| `…/en_US-libritts_r-medium.aligned.onnx` | `10356488f1ae6019943f3d92034338bdfb43066c400d4186e101741293e742a0` (78 580 932 B) | derived from the voice |
| `…/en_US-libritts_r-medium.aligned.source.json` | the sidecar that ties the aligned copy to its source (below) | harness file |

Source: `https://huggingface.co/rhasspy/piper-voices/tree/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/libritts_r/medium`.
Checked on 2026-09-25 through the Hugging Face API. The repo head was revision
`c10ece1aade47bb51c153c893d14e5bf8e5b7117` (last modified 2026-09-17), not
gated. At that revision the listing gives `en_US-libritts_r-medium.onnx` as
78 580 914 B with LFS sha256 `10bb85e0…c13195`, `.onnx.json` as 20 123 B and
`MODEL_CARD` as 279 B. All three match the local files (the card also by
sha256, fetched raw). The download date of the local copy was not recorded,
so the revision is the one whose contents match, not a proven download
revision. The repo's own metadata says `license: mit`
(`cardData.license` and the `license:mit` tag). That differs from the
CC BY 4.0 that the voice's `MODEL_CARD` gives for the dataset. The harness
does not resolve which applies to what.

Licence caveat (documented, not resolved): the voice's `MODEL_CARD` gives the
dataset as LibriTTS-R (openslr 141, CC BY 4.0) and says the model was
"fine-tuned from English lessac medium". The lessac base voice's own card, in
the same repo (`en/en_US/lessac/medium/MODEL_CARD`, fetched raw 2026-09-25),
names its dataset licence as
`https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html`.
That page (fetched 2026-09-25, 31 133 B, sha256 `6fef9536…29896d02a`) is the
"Research Licence Agreement" for the Blizzard 2013 Materials. Its terms:
- the licence is for "Research Purposes only";
- "Research Purposes" excludes "using the Materials for any commercial
  purpose, including the development, marketing, commercialisation, sale or
  licencing of voice synthesis or speech recognition products or services";
- clause 7: the user agrees "not to lend, hire, sell, distribute or otherwise
  part with the Materials".

Whether those terms reach weights fine-tuned from a model trained on the
Materials, and audio made with such weights, is **not established here**.
Anyone redistributing or commercially using audio made with this voice
should settle it first. The CC BY 4.0 dataset licence of LibriTTS-R asks for
attribution. Since 28 Sep 2026 a Piper meeting.json carries it in
`generator.tts.attribution` (§6.3 gives the same line).

The aligned copy was first made with piper's own tool, never touching `reference/`:

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m piper.patch_voice_with_alignment \
  data/voices/piper/en_US-libritts_r-medium.onnx \
  --output data/voices/piper/en_US-libritts_r-medium.aligned.onnx
# INFO: Marked tensor as output: /Ceil_output_0
```

It is the original graph plus one output, `/Ceil_output_0` (the ceil'd VITS
durations, one per phoneme id, in 256-sample frames). Re-running the command
into a scratch file gave the identical sha256. The test re-applies
`add_alignment_output` to the original in memory and compares the
serialisations.

The harness command that replaces it also writes a provenance sidecar:

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator.tts.piper align data/voices/piper/en_US-libritts_r-medium.onnx
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator.tts.piper check   # each voice's load plan
```

`align` does what piper's tool does (`onnx.load`, `add_alignment_output`,
`onnx.save`). Run into a scratch directory and then in place, it gave the same
`10356488…742a0` bytes. It then writes `<stem>.aligned.source.json` with the
sha256 and size of the source `.onnx` and of the aligned copy, the output
name and the piper-tts/onnx versions. Both files are replaced atomically. The
name is not `<stem>.aligned.onnx.json`, which piper would read as a voice
config.

Loading is checked at two points (HARNESS_POLICY):
- **When the aligned copy is used.** The backend uses `<stem>.aligned.onnx`
  only while the sidecar exists and its two sha256 values match the current
  `.onnx` and the aligned file (`check_aligned`; hashing both takes about
  0.04 s each here and is cached per file identity). A stale copy is not
  used silently. That covers a changed `.onnx` beside an old aligned file, an
  edited aligned file, or an aligned file made by piper's tool with no
  sidecar. Such a copy triggers an `AlignedCopyIgnoredWarning`, and the model
  is patched in memory instead, which gives the same pcm and words (20 of
  20 utterances identical: text seeds 1–10 × palette voices 0–1, 8 words). The config (`.onnx.json`) is not part of the check, because
  the aligned graph does not depend on it and it is always read from the
  `.onnx.json`.
- **When the backend is constructed.** The constructor decides, per voice
  file and without loading it, whether the default loader can open it
  (`default_load_plan`): `aligned-file`, `in-memory-patch` (needs `onnx`), or
  unavailable (no verified copy and no `onnx`, or no `onnxruntime`). An
  unloadable file is not offered. If none is left, the constructor raises
  `BackendUnavailable` with the reason. `available_backends()` and
  `python -m generator backends` then print that reason, and the tests skip
  with it instead of failing at the first render. Example, for a voice exactly
  as downloaded with no `onnx`: `piper backend: no voice under … can be loaded
  with alignments: en_US-libritts_r-medium: no
  en_US-libritts_r-medium.aligned.onnx; patching en_US-libritts_r-medium.onnx
  in memory needs the onnx package (piper-tts[alignment]; not a dependency of
  piper-tts itself), which is not installed`.

A voice with no alignment output is refused at load. Each render's turn
notes record `voice_model`, `voice_sha256`, `config_sha256` and
`model_source` (`aligned-file`, `in-memory-patch` or `custom-loader`).

**How a word gets its samples** (`generator/tts/piper.py`):

1. Phonemize with piper's own espeak-ng phonemizer (`PiperVoice.phonemize`).
   "Context" mode phonemizes the whole utterance plus a sentence-final `.`
   (HARNESS_POLICY, for declarative prosody) and splits at the `" "`
   word-separator phonemes. espeak fuses some word pairs: "for the" →
   `fɚðə`, "with the" → `wɪððə`, "that the" → `ðætðɪʲ`. When the groups do not
   match the tokens one to one, the utterance falls back to "per-token" mode,
   where each token is phonemized alone and the separators are inserted by the
   backend. Measured: all 179 distinct vocabulary words phonemize alone to
   exactly one group. Of 3540 utterance phonemizations (text seeds 1–59 × 30
   utterances of 1–14 words, with and without the final `.`), 172 did not
   give one group per token (the 8 printed were all fusions). In backend runs, 12 of 200 utterances (seeds 1–40 × 5, 4–14 words,
   palette voices) used per-token mode. Context mode is used only when every
   token alone gives one group, the utterance gives exactly one group per
   token, and each group starts with the same phoneme as its token alone
   (ignoring stress and length marks). The last condition catches a context
   split of one token that coincides with a fusion elsewhere: that would keep
   the count but shift the groups. A stub test builds exactly that case. Over
   the 12 520 words of the context-mode utterances in the 3540-utterance
   sample, no first phoneme differed. A shift that preserves every first
   phoneme would still pass unnoticed.
2. Build the phoneme-id sequence exactly as `piper.phoneme_ids.phonemes_to_ids`
   lays it out (BOS PAD (phoneme PAD)* EOS; the test compares the two). Each id
   is tagged with its owning token. A phoneme owns the PAD after it (piper's
   own `PhonemeAlignment` convention). BOS, the separators, the final `.` and
   EOS own no token.
3. `PiperVoice.phoneme_ids_to_audio(ids, SynthesisConfig(speaker_id, noise_scale=0,
   noise_w_scale=0), include_alignments=True)` returns the audio and the samples
   per id. The backend checks, on every call, that the counts tile the audio
   (sum == length, one count per id) and refuses otherwise. Every count is a
   whole number of 256-sample frames and at least one frame (asserted in the
   tests). A word spans the samples of its ids. The test also checks that the
   audio, ids and per-phoneme alignment equal piper's public
   `synthesize(text + ".", include_alignments=True)` for the same voice.
4. Resample the whole utterance to 16 kHz (`scipy.signal.resample_poly`,
   320/441, zero-phase). Map every boundary x with round-half-up(x·320/441) in
   integer arithmetic. Cut to [first word start, last word end), so
   `words[0].start == 0` and `words[-1].end == len(pcm)`, as in the formant
   backend. Then apply 4 ms half-cosine edge fades and set the peak to −8 dBFS
   (HARNESS_POLICY, as the formant backend).

What "exact" means here (`timing_exact: true`): a word's boundaries are those
of the model's hard monotonic alignment. They are frame-quantised at the model
rate (256 samples = 11.6 ms at 22.05 kHz) because that is how VITS allocates
audio, and they are reproduced at 16 kHz within 0.5 sample (31.25 µs) by the
rounding. They are not acoustic onsets. The HiFi-GAN decoder's receptive field
lets a phoneme's energy spill a few ms across a frame boundary, and the
resampling filter smooths across it. Inter-word gaps are the audio the model
gave the separator token, not digital silence. They measured 23.2–267.0 ms
(median 34.8 ms, p90 58.0 ms) over 120 renders (seeds 1–40 × 3 utterances,
4–14 words, palette voices). Over 200 renders the median gap level is only
6.3 dB below the median word level (60.4 vs 66.6 dB re 1 LSB²), and 34.4 % of
gaps are more than 10 dB below it. The gaps are mostly coarticulated
transitions, not pauses.

The cut regions are quiet but not empty, and the tail can hold the end of the
last word. The lead-in is the audio of BOS and its PAD. The tail is the audio
of the final `.`, its PAD and EOS. Neither belongs to a word in the alignment.
Acoustically, the release of a word-final stop can fall in the tail frames
after the last word's labelled end. That audio is removed with the tail (and
the 4 ms fade then applies to the new end).

Measured with `the team agreed on the <w>` for w in status, test, risk,
budget, chart, noise, class and issue, × palette voices 0–5 (48 renders). The
first 512 model samples (23.2 ms) after the labelled end were louder than the
word's last 512 samples in 4 of 48 renders: budget/7314 +0.2 dB,
budget/1165 +5.5 dB, budget/5622 +0.6 dB and chart/5622 +1.3 dB. Above 3 kHz
they were louder in 2 of 48: budget/1165 +3.3 dB and issue/3945 +1.0 dB. For
budget/1165, 128-sample windows gave these levels re the utterance peak:
- −53.3 dB at −11.6 ms, inside the final /t/ closure;
- −38.5 dB at the labelled end;
- −35.9 dB at +11.6 ms, the release, which is cut.

Levels, as sample statistics rather than bounds:
- **Lead-in** (120 renders, text seeds 1–40 × 3 utterances of 4–14 words,
  palette voices): 23.2–104.5 ms long (median 46.4), 17.5 dB (min) and
  26.1 dB (median) below the kept utterance.
- **Tail, same 120 renders:** 34.8–127.7 ms long (median 69.7), 18.7 dB (min)
  and 25.5 dB (median) below.
- **Tail, the 48 renders above:** 34.8–139.3 ms long, 18.9 dB (min, issue/3945)
  and 25.4 dB (median) below.
- **Review, other texts:** the independent review measured a minimum of
  16.5 dB (issue, reader 3945). Other texts can go lower still.

The words[-1].end == len(pcm) invariant is kept, as in the formant backend. A
labelled end that includes the release would need a boundary this backend
does not have (HARNESS_POLICY, not changed). The turn table,
RTTM and STM are built from these utterances exactly as for the formant
backend (§1), so V4 still holds. `test_generator_piper.py` scores a 25 s
two-speaker Piper meeting at DER = JER = 0.0 against itself and against the
activity mask, and checks every stem sample and word time against a fresh
render.

**Determinism** (measured on this M1, onnxruntime 1.30.0 CPU):
- With both noise scales pinned to 0 (the default), the same ids, speaker and
  thread count give byte-identical audio and durations. That holds for
  repeated calls in one session, for a new session, and in a second process.
  `synth-piper-2spk-overlap-s0102` regenerated in a second process gave all
  14 files byte-identical. Later, the whole §6.3 set was regenerated with the
  final code in fresh processes, and all 59 files were byte-identical.
- The bytes depend on onnxruntime's intra-op thread count. 1, 2 and 4 threads
  gave three different audio hashes for the same utterances, and the default
  (0) equalled 4 here. Over 40 utterances (seeds 1–40, 10 words, speaker
  (seed·37) mod 904) the durations were identical between 1 and 4 threads and
  the audio differed by at most 2.88e-5 full scale. The backend therefore
  pins `intra_op_num_threads = 1` (`DEFAULT_THREADS`, HARNESS_POLICY). Other
  CPU architectures and onnxruntime versions are not verified.
- With the voice's own noise scales (`PiperBackend(deterministic=False)`, 0.333
  / 0.333) the output changes on every call, even its length (37 376 vs
  38 656 samples for the same ids). Each call's word timings are still exact
  for that call (tested).
- The seed argument cannot reach piper's sampling, which happens inside the
  ONNX graph. It is ignored and recorded as such in the turn notes.

**Voices** (HARNESS_POLICY): a multi-speaker voice offers one voice per
speaker, named `<stem>:<reader>` (reader = key of `speaker_id_map`, the
LibriTTS-R reader id), for example `en_US-libritts_r-medium:7314`. An
explicit `speakers.voices` list may name any of the 904. Auto-assignment
draws from a fixed 16-voice palette. To build it, 64 candidates
sid = round(k·903/63) rendered `PALETTE_F0_TEXT` with noise 0, and the median
autocorrelation f0 was taken (`median_f0`). The low group is f0 < 135 Hz (27
candidates) and the high group f0 > 200 Hz (14). From each group, sorted by
id, the entries at round(j·(len−1)/7) were kept, then interleaved
low/high:

| slot | sid | reader | f0 (Hz) | group | slot | sid | reader | f0 (Hz) | group |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 43 | 7314 | 96.3 | low | 1 | 14 | 5712 | 218.3 | high |
| 2 | 201 | 1165 | 128.6 | low | 3 | 57 | 2592 | 237.1 | high |
| 4 | 330 | 3945 | 117.3 | low | 5 | 115 | 5622 | 222.7 | high |
| 6 | 459 | 8222 | 114.8 | low | 7 | 229 | 7754 | 204.2 | high |
| 8 | 602 | 7069 | 128.9 | low | 9 | 301 | 1678 | 237.1 | high |
| 10 | 659 | 8879 | 118.5 | low | 11 | 444 | 3119 | 216.2 | high |
| 12 | 788 | 957 | 116.1 | low | 13 | 588 | 6924 | 218.3 | high |
| 14 | 889 | 2674 | 128.2 | low | 15 | 731 | 1425 | 239.7 | high |

For n speakers, `palette_indices` starts at slot seed mod 16 and steps by the
largest odd number ≤ 16 // n (7 for two speakers, 5 for three, 3 for four or
five). The picks are distinct, spread over the palette (and so over the id
range), and consecutive speakers alternate between the low and high groups.
Beyond 16 speakers, further ids are spread evenly over the rest of the set.
The test re-measures every palette voice's f0 (identical to the recorded
value) and its group. f0 is a proxy for variety, not a gender or identity
label.

Which model auto-assignment uses (HARNESS_POLICY, `default_voice_model`):
1. `PiperBackend(voice_model=…)` if pinned;
2. else `DEFAULT_VOICE_MODEL` = `en_US-libritts_r-medium` whenever it is
   offered;
3. else the first model, by file name, with a measured `PALETTES` entry whose
   readers still map to the recorded speaker ids;
4. else the first multi-speaker model, with 16 evenly spaced ids and a
   `PaletteFallbackWarning`. The low/high alternation is then not guaranteed.

Other voice files in `data/voices/piper/` therefore do not change the §6.3
voices while the libritts_r voice is present. A test puts a 109-speaker
`en_GB-vctk-medium` placeholder (which sorts first) beside it and gets the
§6.3 readers for all four seeds, with no warning. Beyond 16 speakers, the
extra ids come from the same model.

**Speed**: 200 utterances (427.3 s of audio) rendered in 42.0 s and 43.4 s
in two runs on one thread, including phonemization and resampling
(real-time factor 0.098 and 0.102).
The model loads in about 0.6 s. A 120 s two-speaker meeting takes 24 s end to
end. The 135 s three-speaker meeting with image order 40 takes 45 s.

**Intelligibility sanity check** (not the Measure stage's evaluation): the
local `data/models/faster-whisper/small.en` (int8, beam 5, no VAD,
`condition_on_previous_text=False`) was run on `mix.wav` and scored with jiwer
against the segments' text in start order:

| meeting | WER | ref words | S / D / I |
|---|---|---|---|
| formant `build/synthetic/smoke/synth-smoke-s0009` (12 s) | 0.9615 | 26 | 3 / 22 / 0 |
| piper `synth-piper-2spk-s0101` (120 s, RT60 0.34 s measured, no noise) | 0.1463 | 410 | 48 / 11 / 1 |
| piper `synth-piper-3spk-reverb-noise-s0103` (135 s, RT60 0.79 s measured, 15 dB white) | 0.7256 | 441 | 169 / 32 / 119 |

The text is seeded word salad from the harness vocabulary, so the ASR's
language model gets no help. The reverberant, noisy meeting is hard for
small.en: the ASR inserts 119 words there.

### 6.3 The Piper set for the Measure stage

`build/synthetic/piper/` (git-ignored, 143 MB) holds four meetings from the
`piper_meeting` preset. It was made with this exact command from the
repository root:

```
OUT=build/synthetic/piper
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator make --scenario piper_meeting --seed 101 --set name=piper-2spk --set duration_s=120 --out $OUT/synth-piper-2spk-s0101
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator make --scenario piper_meeting --seed 102 --set name=piper-2spk-overlap --set duration_s=120 --set turn_taking.overlap_ratio=0.15 --out $OUT/synth-piper-2spk-overlap-s0102
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator make --scenario piper_meeting --seed 103 --set name=piper-3spk-reverb-noise --set n_speakers=3 --set duration_s=135 --set "room.dims_m=[7,6,3]" --set room.rt60_s=0.7 --set room.max_order=40 --set noise.kind=white --set noise.snr_db=15 --out $OUT/synth-piper-3spk-reverb-noise-s0103
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m generator make --scenario piper_meeting --seed 104 --set name=piper-4spk --set n_speakers=4 --set duration_s=150 --set turn_taking.model=random --out $OUT/synth-piper-4spk-s0104
```

| meeting | s | spk | turns | words | overlap realised | room (RT60 req → measured) | noise | voices (readers) | speech per speaker (s) |
|---|---|---|---|---|---|---|---|---|---|
| `synth-piper-2spk-s0101` | 120 | 2 | 46 | 410 | 0.0 | 6×5×3 m, 0.3 → 0.3384 | none | 5622, 957 | 51.5, 39.8 |
| `synth-piper-2spk-overlap-s0102` | 120 | 2 | 67 | 573 | 0.150013 (target 0.15) | 6×5×3 m, 0.3 → 0.3384 | none | 8222, 6924 | 69.8, 67.5 |
| `synth-piper-3spk-reverb-noise-s0103` | 135 | 3 | 50 | 441 | 0.0 | 7×6×3 m, 0.7 → 0.7912 (order 40) | white, 15.0 dB in `mix.wav` | 7754, 957, 5712 | 38.2, 37.4, 29.3 |
| `synth-piper-4spk-s0104` | 150 | 4 | 53 | 451 | 0.0 | 6×5×3 m, 0.3 → 0.3384 | none | 7069, 3119, 2674, 5712 | 14.5, 35.1, 36.8, 29.8 |

The commands depend only on the inputs in §6.2's table: with the libritts_r
voice present, other voice files do not change the voices (§6.2, Voices).
After this revision's backend changes (load gate, sidecar, voice-model
policy), `synth-piper-2spk-s0101` was regenerated in a fresh process into a
scratch directory. It was byte-identical to the stored meeting (`diff -r`),
took 26.2 s wall, and passed `validate`.

Attribution to use with this set (since 28 Sep 2026 also in each new Piper
meeting.json, `generator.tts.attribution`; the four meetings below were made
before that, on 25 Sep):
"Speech synthesised with the Piper voice en_US-libritts_r-medium
(rhasspy/piper-voices), trained on LibriTTS-R (Koizumi et al., 2023,
https://www.openslr.org/141/, CC BY 4.0) and fine-tuned from the lessac
medium voice (Blizzard 2013 research licence; see §6.2)."

Pipeline results on this set (whisper-sherpa, energy-vad-cluster, oracle) are in
[docs/v5-results.md](v5-results.md).

All four use the Note Pro 4-mic placeholder with a mono device mix, pass
`python -m generator validate`, and have `generator.timing_exact: true`. The
reverberant meeting sets `room.max_order=40` because the default cap of 24
realised only 0.5085 s for the requested 0.7 s. The 3-speaker meeting's
capsules are at 9.16 dB SNR and its stereo mix at 12.097 dB; the device mix
and every mono `device/*` file are at 15.0 dB. In the 4-speaker meeting the
random turn model gave spk0 only 14.5 s of speech. Two voices recur across
meetings (reader 957 in s0101 and s0103, 5712 in s0103 and s0104).

## 7. Tests

`tests/test_generator_{text,tts,turns,room,e2ee,scenario,export,transfer,cli,piper}.py`
and `tests/test_v4_der_roundtrip.py` (136 tests). 13 of the 29 piper tests
need the local voice, loaded, and skip without it with the reason prefix
`piper voice unavailable:`. CI has neither piper-tts nor the voice. The same
prefix covers a voice that is present but cannot be loaded here, for example
with no verified aligned copy and no `onnx`. Highlights that challenge
failure modes rather than echo fixtures:

- byte-level determinism across two generations;
- speaking-rate/duration and f0/autocorrelation analytic checks on the
  formant model;
- planner invariants (no stacking, no self-overlap, caps, only adjacent
  turns overlap, stems intact) over many seeds of every overlapping preset
  and a cap-binding stress configuration, plus overlap-target error bounds
  measured over seeds;
- direct-path delay and 1/r² energy in an anechoic room;
- the SNR measured in `mix.wav` itself for 1- and 2-channel Note Pro and
  NotePin S;
- the RFC 7539 vector; header offsets read raw, not via the packer;
- every device file sniffed by the emulator's own `classify_recording`, and
  both E2EE files decrypted to their plain twins;
- the raw packet stream decoded and matched bit for bit to the Ogg decode at
  exactly the recorded pre-skip;
- the Ogg CRC implementation checked against libavformat's stored CRCs and a
  flipped bit; decode duration within one frame, plus a lag-searched
  correlation with a shifted-copy falsifier;
- `device_primary` resolved and a dangling one rejected; no stale files after
  a rewrite; a foreign directory refused;
- `validate` flags unlisted files; the set script run from another directory;
- piper, with no model (these run in CI): the alignment arithmetic, the
  round-half-up boundary map checked exhaustively against exact fractions, the
  per-token fallback on a fused word pair, refusal of a voice without
  alignments or with counts that do not tile the audio, the palette selection,
  and `generate_meeting` driven by a fake voice. Also:
  - the load gate: a voice as downloaded without `onnx` is unavailable at
    construction, in `available_backends()` and in `generator backends`, and
    an unloadable file beside a loadable one is not offered;
  - the sidecar: `align` writes it, a changed source, an edited copy, a
    foreign or missing sidecar each un-verify the copy, and a stale copy
    without `onnx` makes the backend unavailable;
  - the model choice: a measured model wins over file-name order, and an
    unmeasured palette warns;
- piper, with the real voice: the voice files are the measured ones and the
  render's notes say so; the sidecar verifies the aligned copy; the aligned
  file is the original plus exactly one output; a vctk placeholder in the
  directory leaves the §6.3 voices unchanged; a stale aligned copy is not
  used (the render fails on the bad `.onnx` instead); the ids equal `phonemes_to_ids`; audio, ids and per-phoneme
  alignment equal piper's own `synthesize()`; words and gaps tile every
  utterance exactly at both rates; the cut lead/tail stay more than 12 dB
  below speech; bytes repeat across calls and sessions, and the noisy mode
  demonstrably does not; each palette voice's f0 is re-measured; a 25 s
  two-speaker Piper meeting has DER = JER = 0.0 and its stems and word times
  equal fresh renders;
- auto-assigned voices never shared;
- every ledger citation resolved to a real heading;
- the mono Ogg served by `TransferSession` and over the Bumble GATT link,
  reassembled byte-exact;
- DER/JER exactly 0.0 against self and mask, > 0 for shifted/dropped copies,
  and 0.0 for a relabelling.

## 8. Not done, and why

- **Intelligible speech without local inputs.** The default offline backend is speech-like pseudo-speech; ASR word-error evaluation on it is meaningless (small.en WER 0.9615 on a formant smoke meeting, §6.2). The piper backend gives intelligible speech but needs piper-tts (GPL-3.0-or-later) and the local voice, which CI does not have. The lessac base-voice data licence is unresolved (§6.2).
- **Natural text.** Piper meetings speak the seeded vocabulary as word salad. `text_corpus` accepts a real text file, but none is bundled.
- **Piper metadata in meeting.json: done 28 Sep 2026.** A meeting whose turns carry TTS notes (the Piper backend) now exports `generator.tts` (`generator/export.py` `build_tts_metadata`): the distinct `timing_method` (`piper-duration-alignment`), `phonemization`, noise scales, onnxruntime threads, `deterministic_by_config` and `boundary_rounding` values; `voice_files` (`voice_model`, `voice_sha256`, `config_sha256`, `model_source`); and `attribution` (the LibriTTS-R CC BY 4.0 line, `generator/tts/piper.py` `VOICE_ATTRIBUTIONS`, for the listed voice only). Its `provenance.ground_truth` says that word boundaries are the voice's own alignment, not acoustic onsets. `generator.timing_exact` stays `true`: it means exact with respect to that alignment (§6.2). A formant meeting.json is unchanged byte for byte (no turn carries notes). Tests: `tests/test_generator_piper.py` (a stand-in voice, and the real voice when installed).
- **Piper portability.** Byte-determinism was measured on one M1 with onnxruntime 1.30.0 at one pinned thread count. Other CPUs, onnxruntime versions and espeak-ng data may change bytes (durations did not change with thread count, but other factors were not tested).
- **Consumers still on dict order.** `pipeline/meeting.py` `resolve_audio(…, "device")` takes the first `audio.device` entry, which is the encrypted file, and `emulator/serve.py` serves `device/recording.ogg` (mono) in `PLAUD_MEETING_DIR` mode even for 2-channel meetings. Both belong to other components; they should use `generator.contract.device_primary_path` (requested).
- **Stereo raw/g4/E2EE.** Only the Ogg files exist in stereo; the raw, g4 and E2EE shapes are mono even for a 2-channel scenario (their `channels` field says so).
- **Overlap target in the random model** can fall short of the target (−0.043 worst over `overlap_heavy` seeds 1–30; see §1).
- **Per-capsule delays.** `direct_path_delay_s` is measured to capsule 0; the device mix averages capsules whose arrival times differ by up to about 0.17 ms (≈ 3 samples; the 58 mm diagonal of the Note Pro placeholder) at the placeholder geometries.
- **Real device audio.** Nothing here is a Plaud capture; which of the four shapes real firmware emits (U20), the firmware's Opus settings, its 4→1/2 channel DSP, the VPU path and the real capsule geometry all need a device.
- **The SDK writer's Ogg quirks** (ledger §8) are not reproduced; the generator writes conformant Ogg that the SDK parser accepts. A "quirks" mode would need a bespoke muxer.
- **Real E2EE.** The keyCipher is a filler; no RSA key material exists or is fabricated. Only the shape (header + raw ChaCha20 body) is realised.
- **Corpora.** MUSAN/RIRS are not downloaded (`scripts/fetch-datasets.sh`); the MUSAN path is exercised in tests with synthetic WAV files only. Measured RIRs (RIRS_NOISES) are not wired in.
- **Beyond shoebox ISM:** no directivity, scattering, air absorption or moving speakers; no laughter/backchannels/disfluencies in the turn model.
- **Hypothesis files** (`hyp.json/.rttm/.stm`) are the pipeline's output; the generator only defines the schema constant (`contract.SCHEMA_HYPOTHESIS`).

## 9. Files

`generator/{__init__,__main__,_evidence,cli,contract,devices,e2ee,export,meeting,opus,room,scenario,testing,text,turns}.py`,
`generator/tts/{__init__,base,formant,piper,kokoro}.py`, `scripts/make-synthetic-set.sh`,
`tests/test_generator_piper.py`, local-only `data/voices/piper/*` (§6.2),
`requirements/generator.txt` (no packages added), tests listed in §7.
