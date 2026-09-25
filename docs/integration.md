# Cross-layer integration tests

Status 2026-09-24: **implemented; 21 tests, all passing, ~4 s added to the
suite.** These tests wire the six packages together across the shared data
contract (`plaud-harness/meeting/1` in, `plaud-harness/hypothesis/1` out) and
across the two transports the emulator speaks. Nothing here touches a real
Plaud service or device: the "cloud" is `mockcloud/` in-process over
`httpx.ASGITransport`, the "device" is `emulator/plaudsim` over Bumble's
virtual link or a loopback WebSocket, the "phone" is `tests/wifi_support.py`.

| file | tests | what it proves |
|---|---|---|
| `tests/test_v4_e2e.py` | 9 | generator -> pipeline `oracle` -> evals scores exactly 0; the two CLIs agree; `perturbed-oracle` moves DER/cpWER; a speaker swap makes literal WER exceed cpWER |
| `tests/test_integration_emulator_serves_generator.py` | 2 | the generator's Ogg pulled over Bumble (HEAD, DATA..., EMPTY_PACKAGE, TAIL) is byte-exact; the pulled bytes decode identically and `energy-vad-cluster` diarizes them within a documented tolerance |
| `tests/test_integration_wifi_serves_generator.py` | 3 | the same Ogg over the Wi-Fi device emulator to the phone double: byte-exact, plain and sealed, plus a ranged resume |
| `tests/test_integration_mockcloud_roundtrip.py` | 3 | identity chain -> bind -> multipart upload (>= 2 parts) -> complete -> transcription task PENDING -> STARTED -> SUCCESS -> ground truth -> hyp.json -> DER == cpWER == 0; the file:// knob; a falsifier |
| `tests/test_integration_identity.py` | 4 | a mock-minted user JWT -> `normalize_historical_id` / `build_k3_from_historical_id` -> the BLE peripheral answers l3 status 0, the session reaches BOUND and pulls the file; pv 7 truncation; two refusal falsifiers |

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_v4_e2e.py tests/test_integration_*.py -q -p no:cacheprovider --timeout=120`

## Evidence class

Every passing test here is **EMULATOR_INTEGRATION_PROVEN for the harness's own
components**: it shows that *our* generator, *our* emulators, *our* mock cloud,
*our* pipeline seam and *our* evals agree with one another end to end. It is
not evidence about real Plaud hardware, the real SDK, or the real cloud. Where
a step rests on device/SDK evidence, the underlying test is cited instead:

* the HEAD / DATA / EMPTY_PACKAGE / TAIL close sequence is RUNTIME_PROVEN
  against the genuine SDK in R7-S13 (`tests/test_r7_s13_transfer_close.py`);
  here it is merely reproduced by the emulator and reassembled by a test central;
* the k3 derivation (`strip "client_user_"`, drop `-`, pad/truncate to
  `token_width(pv)`) is BYTECODE_PROVEN + RUNTIME_PROVEN in R7-S12
  (`tests/test_r7_s12_k3_runtime.py`); here it is applied to a token the mock
  minted, and the *emulator's* acceptance of it is HARNESS_POLICY
  (`accept_any_token`);
* the HTTP contract shapes are the mock's own DOC-EXACT / BYTECODE_PROVEN
  claims (docs/mockcloud.md); this file exercises them, it does not add to them;
* the metric numbers are library-defined (pyannote.metrics, jiwer, meeteval;
  docs/evals.md) and the oracle pipelines are harness self-tests
  (`is_system_under_test is False`). **No number below is a system's score**
  except the `energy-vad-cluster` figures, which are that model-free system's
  score on one 12 s synthetic meeting and nothing more.

## Shared fixture

All five files use the same meeting so the Ogg served over BLE, over Wi-Fi and
through the mock cloud is one file: `generator.testing.fixture_meeting(base,
"smoke", seed 3)` -- 12.0 s, 2 speakers (`spk0`, `spk1`), 5 turns, mono, no
overlap, `device/recording.ogg` = 49 161 bytes (`plain_ogg`). It is generated
once per pytest process (~1.9 s, the only noticeable cost) and cached by the
generator's own helper.

## What each test proves

### `test_v4_e2e.py` -- the V4 rung, end to end

* `load_audio` takes both shapes the generator writes: the device Ogg
  (PyAV, 48 kHz -> 16 kHz) and `mix.wav` (soundfile, 16 kHz); durations agree
  within one 20 ms frame.
* `oracle` run on `device/recording.ogg` (meeting.json discovered two levels
  up) -> `evals.score_meeting`: **DER 0.0, JER 0.0, cpWER 0.0, tcpWER 0.0
  (word-level timings), literal and concatenated WER 0.0, speaker-count error
  0**, all exactly, and the `oracle` gate suite passes. The zero is over a
  non-empty UEM (`der.total > 0`).
* The CLI seam: `pipeline.cli.main(["run", "--pipeline", "oracle", ...])`
  writes `hyp.json/.rttm/.stm` that `evals.io.load_hypothesis` parses, and
  `evals.cli.main(["score", ..., "--suite", "oracle"])` exits 0 with a
  `plaud-harness/eval-report/1` document whose gate block says `passed: true`.
  HARNESS_POLICY: the CLIs are called in-process (`main(argv)`) to keep the
  suite fast; the subprocess entry points are covered by
  `tests/test_pipeline_cli.py` and `tests/test_evals_cli.py`.
* `perturbed-oracle` with zero rates is the identity (DER = cpWER = 0).
  With `word_sub_rate 0.3, boundary_jitter_s 0.2` (seed 1): **DER > 0
  (measured 0.037), JER > 0, cpWER > 0 (0.32)** and the oracle gate now
  fails naming `der.der` and `cpwer.error_rate`. Word damage alone
  (`word_sub_rate` 0.2 -> 0.6) raises cpWER monotonically while DER stays 0.
* Speaker swap: `speaker_swap_rate 1.0` on the two-speaker meeting is a
  permutation, so **cpWER = DER = JER = 0 while the label-literal WER is 1.2
  (> 1.0: every reference word is paired with the other speaker's words)** --
  literal WER > cpWER as the decision log predicts, and the speaker-agnostic
  concatenated WER is 0 because it cannot see attribution at all. A partial
  swap (`0.5`, seed 2) raises DER (confusion > 0) and cpWER, and
  `wer_literal >= cpwer` holds -- a theorem, since cpWER is the minimum over
  label assignments and literal WER is one assignment.

### `test_integration_emulator_serves_generator.py` -- BLE

* The generator's Ogg is loaded into `PlaudPeripheral`'s file table; a
  Bumble `TwoDevices` central connects the way the SDK does (MTU 255 first,
  then real ATT discovery), reads the file list (one entry, the right size),
  issues `syncFileStart` and receives `3 + ceil(49161/240) = 208` frames:
  HEAD (status 0), 205 DATA frames with contiguous offsets, the EMPTY_PACKAGE
  sentinel (type 2, offset `0xFFFFFFFF`, skipped when reassembling) and TAIL
  with the configured CRC. The reassembled body **equals the file byte for
  byte, matches the sha256 recorded in `meeting.json`, and classifies as
  `plain_ogg`** with the SDK's own two-test rule. No request was rejected.
* The pulled bytes, written to a `.ogg` and read through `pipeline.load_audio`,
  produce **PCM `np.array_equal` to the on-disk file's** (PyAV, 48 kHz
  source, 16 kHz output, duration within 20 ms of 12.0 s).
* `energy-vad-cluster` on the pulled bytes (see the tolerance below): with
  `num_speakers=2` it finds exactly 2 speakers (speaker-count error 0), loses
  no reference speech (miss rate 0.0), scores DER 0.294 (< 0.5) and, being a
  clusterer and not an oracle, more than 0; without the hint it yields 1
  speaker (|error| = 1 <= tolerance) with the same zero miss; the hypothesis
  from the pulled bytes equals the one from the on-disk file segment for segment.

### `test_integration_wifi_serves_generator.py` -- Wi-Fi

* `WifiDevice` (store = the Ogg under session 1700000000) dials
  `PhoneWifiServer` on `127.0.0.1:0`; SayHello -> Handshake -> READY;
  `getFileList` returns the one record with the right size; `download` yields
  **`dl.data == ogg`**, 13 `FileSyncContent` chunks (12 x 4096 + 1 x 9), offsets
  `0, 4096, ..., 49152`, `last` only on the final chunk, no drops, no errors;
  sha256 matches `meeting.json`; the received bytes classify as `plain_ogg`
  and decode through `load_audio` to PCM identical to the on-disk file.
* A ranged resume (`start 4096, end 12305`) returns exactly `ogg[4096:12305]`
  in three chunks.
* The same file over a ChaCha20-Poly1305-sealed session with the synthetic
  keys: every wire frame passes the phone's `> 200` ciphertext heuristic and
  the plaintext is byte-exact.
* Every await is bounded (10 s) and the file carries `pytest.mark.timeout(120)`
  in addition to the command-line `--timeout`.

### `test_integration_mockcloud_roundtrip.py` -- cloud

* Oracle-path knob: `POST /_mock/meetings {"dir": <meeting dir>}` registers the
  directory and reports `device/recording.ogg` among the fingerprinted files.
* Identity chain: Basic -> partner token (`bearer`) -> user token; the JWT
  decodes under the mock's public HS256 secret, `sub` starts with
  `client_user_` and normalises to 32 hex (so it would survive the SDK's k3
  derivation).
* Bind: `POST sdk/bind` -> `{type, sn, is_bind: true}`; `GET sdk/binding` ->
  `is_bind: true`, history = the UUID part of `sub`.
* Upload: `generate-presigned-urls` for 49 161 bytes at the shrunk
  `ChunkSize` 20 000 -> **3 parts (>= 2)**; each `PUT` returns the quoted MD5
  ETag of its part; `complete-upload` with `file_md5` returns the file's MD5;
  `GET DownloadUrl` is byte-exact with `audio/ogg`.
* Transcription: `POST ai/transcriptions/` -> `PENDING`, `data {}`; polling
  the GET sees exactly **`["PENDING", "STARTED", "SUCCESS"]`**; the task's
  `source` is `objectstore+meeting+meeting.json`; the `results[]` texts and
  `(start, end)` pairs equal `meeting.json` segment for segment; one
  `Speaker N` label per reference speaker; `duration` is the rounded integer
  and `language` is `en`.
* `result_to_hypothesis` -> `evals.io.write_hypothesis_files` (`hyp.json`,
  `hyp.rttm`, `hyp.stm`) -> `load_hypothesis` -> `score_meeting`: **DER 0.0,
  JER 0.0, cpWER 0.0, tcpWER 0.0, speaker-count error 0; the `oracle` gate
  suite passes.** The label-literal WER is 2.0 because the cloud says
  `Speaker 1` where the reference says `spk0` -- the cpWER rationale observed
  across a layer boundary. `unbind` then returns `is_bind: false`.
* The `file://` knob (`local_file_roots=(meeting dir,)`) reaches the same
  ground truth with `source == "file+meeting+meeting.json"` and 256-dim
  embeddings for both speakers.
* Falsifier: the same bytes uploaded to a mock **without** the registration
  are unknown audio: the placeholder transcript comes back
  (`language_probability 0.0`, duration read from the Ogg granules) and evals
  scores cpWER > 0, DER > 0, oracle gate failed. The zero above is not free.

### `test_integration_identity.py` -- JWT to BLE

* Two independent derivations agree: a byte-for-byte re-implementation of
  `NiceBuildSdk.resolveHandshakeToken` on the mock's JWT and
  `plaudsim.handshake.normalize_historical_id(sub)`; the result is 32 hex, so
  `build_k3_from_historical_id(sub, 9)` is a 38-byte frame with the token
  neither padded nor truncated, while pv 7 carries the first 16 characters;
  the same partner user always yields the same token, a different user a
  different one; the empty sub yields `None` (the SDK writes nothing).
* Over Bumble at pv 9: the k3 frame is answered with **l3 status 0,
  `port_version 9`**, the peripheral logs exactly the derived token, and the
  session then follows the SDK's own connect order -- getSsn (equals the
  advertised serial), battery, syncTime -> `BOUND` (`lifecycle_log` ends
  `handshaked, bound`) -- and pulls the generator's Ogg byte-exact post-bind.
* pv 7: the peripheral sees the truncated 16-char token and still answers 0.
* Falsifiers: `accept_any_token=False` answers **nothing** (the request is
  logged as rejected, lifecycle never reaches HANDSHAKED);
  `handshake_status=1` answers l3 status 1 and likewise never reaches
  HANDSHAKED. Acceptance is the device's decision, and here the device is ours.

## HARNESS_POLICY register (choices made by these tests)

| policy | value | where |
|---|---|---|
| shared fixture | preset `smoke`, seed 3 (12 s, 2 speakers) | every file |
| BLE DATA payload | 240 bytes (u8 length field; under the SDK's 255-byte MTU minus overheads) | emulator, identity |
| BLE session id / TAIL CRC | `0x0655A1B0` / `0x0BEE` | emulator |
| speaker-count tolerance without a hint | 1 | emulator |
| maximum miss rate for the VAD | 0.02 (measured 0.0) | emulator |
| maximum DER with the hint | 0.5 (a two-speaker coin flip; measured 0.294) | emulator |
| Wi-Fi chunk size / heartbeat / timers | 4096 B (emulator default) / 50 ms / idle and exit timers off | wifi |
| Wi-Fi await bound | 10 s per await, `pytest.mark.timeout(120)` per file | wifi (and all async files) |
| mock chunk size | 20 000 bytes (documented default 5 242 880) so the file is a real multipart upload | mockcloud |
| mock task step | 0.02 s (0.0 where the walk is not asserted) | mockcloud |
| perturbation rates | `word_sub_rate 0.3 + boundary_jitter_s 0.2` (seed 1); `speaker_swap_rate 1.0` (seed 1) and `0.5` (seed 2); `word_sub_rate 0.2/0.6` (seed 3) | v4 |
| CLI invocation | in-process `main(argv)` rather than a subprocess | v4 |

## Model-free diarizer: the documented tolerance

`energy-vad-cluster`'s `distance_threshold = 2.5` was tuned on the pipeline's
pulse-train test voices (docs/pipeline.md §2). On the generator's formant
voices the whole 12 s meeting is one VAD region (pauses 0.2-1.0 s are bridged
by hangover/reverberation), and at the default threshold the 11 one-second
chunks fall into **one** cluster (DER 0.373, all confusion; miss 0.0; false
alarm 0.067). With the `num_speakers=2` hint the same chunks split into two
clusters with three turns (DER 0.294: confusion 0.227, false alarm 0.067,
miss 0.0). The test therefore asserts only what is provable today -- exact
count with the hint, |error| <= 1 without, no missed reference speech, DER
below 0.5 with the hint -- and does **not** claim the diarizer separates the
synthetic voices unaided. Re-tuning on a dev split precedes any stronger claim.

## xfails

None. Every planned integration ran; no test is marked xfail.

## Findings (not blockers)

* `mockcloud.oracle.try_pipeline_oracle` probes `pipeline.oracle` for
  `oracle_hypothesis`, `hypothesis_from_meeting_dir`, `oracle` or `run_oracle`;
  the pipeline exposes `OraclePipeline` and a registry factory instead, so the
  hook never fires and the mock reads `meeting.json` directly. The result is
  the same ground truth (asserted: DER = cpWER = 0), and the task `source`
  records `+meeting.json`. Wiring the duck-typed name is a one-line change in
  either package; neither is owned by this track, so it is recorded here.
* The label-literal WER of a correct cloud result is 2.0 (labels `Speaker N`
  vs `spkN`). Anyone reading `wer_literal` from a cloud round-trip is reading
  the lie docs/evals.md warns about; cpWER is the headline for a reason.

## Not done

* No real device, real SDK or real cloud is exercised; the composed stack
  (docker/, emulator/serve.py, scripts/*-up.sh) belongs to another track and
  is not driven from these tests.
* The BLE and Wi-Fi tests serve the mono `plain_ogg` only; the stereo, raw
  packet, g4 and E2EE shapes the generator also writes are byte streams to the
  transports and would transfer identically, but only the shape the pipeline
  can decode was pushed through the whole chain.
* `energy-vad-cluster` is exercised on one meeting; no threshold re-tuning was
  attempted (not this track's package).
