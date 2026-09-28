# pipeline/ — the ASR/diarization stack under test

Track owner: `pipeline/**`, `tests/test_pipeline_*.py`, `requirements/pipeline.txt`, this file.
Status 2026-09-25: **implemented and tested offline; reviewed, 13 review findings fixed (§10).**
The model-free diarizer separates the generator's voices without a speaker-count hint on the
development seeds (§2.2). **`whisper-sherpa` runs real, openly licensed ASR (faster-whisper
small.en) and diarization (sherpa-onnx: pyannote segmentation-3.0 + 3D-Speaker CAM++) on this
M1's CPU** and has been run on AMI speech (§11). V5, the held-out AMI score, has **not** been
run here (§7). The first measured run on the four local AMI test-split meetings and the
Piper set (not V5) is [docs/v5-results.md](v5-results.md).

The package sits between Layer 2 and Layer 3 of PROJECT.md §3:

```
generator/ (Layer 2)  ──meeting dir──▶  pipeline/  ──hyp.json/.rttm/.stm──▶  evals/ (Layer 3)
```

A *meeting directory* (schema `plaud-harness/meeting/1`) goes in; a *hypothesis*
(schema `plaud-harness/hypothesis/1`, plus RTTM and STM renderings) comes out.
Nothing in this package talks to a device, a cloud or a network.

## 1. Interfaces (`pipeline/base.py`)

| Name | Signature | Notes |
|---|---|---|
| `Transcriber.transcribe(pcm, sample_rate)` | mono float32 PCM → `[{"start","end","text","words":[{"w","start","end"}]}]` | no speaker field; text is lowercase tokens |
| `Diarizer.diarize(pcm, sample_rate, num_speakers=None)` | → `[(start, end, speaker), ...]` | speakers are opaque strings |
| `Pipeline.run(audio_path, meeting_dir=None)` | → `Hypothesis` | `is_system_under_test` is `False` for oracles |
| `ComposedPipeline(transcriber, diarizer)` | glues the two with `assign_speakers` | either side may be `None` |
| `PipelineConfig(name, seed, sample_rate=16000, params={})` | per-run config | `params` is the CLI `--param k=v` bag |
| `Hypothesis` | `to_dict/from_dict/validate/rttm_lines/stm_lines` | `validate()` enforces the contract |
| `load_audio(path, target_sr=16000, *, on_gap="zero_fill")` | → `LoadedAudio(pcm, sample_rate, container, source_sample_rate, source_channels, gaps, container_duration_s, warnings)` | wav/flac via soundfile, everything else via PyAV |
| `register(name, ..., params=...)` / `get_pipeline(name, config)` / `describe_registry()` | registry | unavailable entries carry a reason; `params` is the set of accepted `--param` keys |
| `ParamError` | `PipelineError` + `ValueError` | unknown `--param` key or a bad value |

Supporting modules: `formats.py` (RTTM/STM/hyp.json writers and parsers),
`meeting.py` (meeting.json reader/validator, `meeting_from_stm`, `write_meeting_dir`),
`synthetic.py` (hand-built test signals and meeting dirs — **test support, not the generator**),
`energy_vad.py` (the model-free diarizer), `adapters.py` (optional model adapters),
`whisper_sherpa.py` (the sherpa-onnx diarizer and the `whisper-sherpa` composition, §11),
`model_store.py` (pinned model files: URLs, sha256, licences, fetcher, §11),
`cli.py` (`python -m pipeline`).

### Audio loading

`load_audio` routes by suffix: `.wav/.flac/.aiff` → `soundfile`, anything else →
PyAV. Every path ends in the same numpy mono average and the same
`scipy.signal.resample_poly` resampler, and **every path is clipped to [−1, 1]**
(before 2026-09-25 only the resampling branch clipped, so a 16 kHz float wav
reached the components at ±3.0 while the same data at 48 kHz was clipped). A
device-shaped Ogg/Opus file (libopus decodes at 48 kHz) and a 16 kHz wav reach the
components identically. The Ogg/Opus path is verified against the R6-S2 synthetic
fixtures (`tests/fixtures/r6s2_manifest.json`, sha-pinned, `not_plaud_capture: true`):
correlation 0.9999 at zero lag with the generator's own tone.

**Damaged Ogg/Opus.** libopus/PyAV decode past a damaged Ogg page without an
error and simply skip the lost packets; concatenating the frames then shifts every
later sample early (a 2 KB corruption in a 10 s file gave 9.0 s and a 1 s shift).
The PyAV path therefore tracks each frame's PTS: a forward jump of more than 1 ms is
zero-filled (`on_gap="zero_fill"`, recorded in `LoadedAudio.gaps`) or refused
(`on_gap="error"`, `AudioFormatError`). Errors raised while decoding (not only
while opening) become `AudioFormatError`. A decoded length that differs from the
container's declared duration by more than 20 ms is recorded in `warnings`.

A **truncated** file is self-consistent (the last page's granule position says 3 s),
so the container cannot expose it. `Pipeline.check_audio` compares the decoded
length with meeting.json `duration_s` whenever a meeting.json is found. The
`audio_check` param decides what happens (HARNESS_POLICY):

| `audio_check` | on a gap, a container mismatch, or \|decoded − duration_s\| > `duration_tolerance_s` (default 0.05 s) |
|---|---|
| `strict` (default) | `AudioFormatError` (CLI exit 1) |
| `warn` | scored; the problems are listed in `hyp.extra["audio"]["problems"]` |
| `off` | not checked |

On every generator meeting measured here (18 meetings, device Ogg, stereo Ogg and
mix.wav) the decoded length equals `duration_s` exactly.

### meeting.json is data, not trusted paths

* `meeting_id` must be one safe path component: `^\w[\w.-]{0,199}$`
  (word characters, `.` and `-`; no `/`, no `..`, no whitespace, not starting with `.` or `-`). The generator's ids
  (`synth-smoke-s0003`) and AMI ids (`ES2002a`) fit.
* Audio paths taken from meeting.json (`mix`, `device`, `device.<name>`, `stem:<spk>`)
  must be relative and must resolve (symlinks and `..` included) inside the meeting
  dir. A literal `--audio-key <relpath>` is the operator's own choice and is not
  constrained.
* A meeting.json that exists but does not validate is an error
  (`MeetingFormatError`), never a silent fallback to the audio file's stem.

## 2. What is a real system and what is a harness oracle

| Registry name | Kind | Runs here? | Output |
|---|---|---|---|
| `oracle` | **harness self-test** — returns `meeting.json` ground truth | yes | full |
| `perturbed-oracle` | **harness self-test** — ground truth with seeded, configurable damage | yes | full |
| `energy-vad-cluster` | **real, model-free system** — energy VAD + MFCC/f0 cells + spectral clustering | yes | diarization only (`text == ""`) |
| `whisper-sherpa` | **real system**: faster-whisper small.en (int8) + sherpa-onnx diarization (pyannote segmentation-3.0 + 3D-Speaker CAM++), words to max-overlap speaker (§11) | **yes**, after `python -m pipeline fetch-models` | full, per-word timings |
| `sherpa-onnx-diarization` | the same sherpa-onnx diarizer alone (§11) | **yes**, after `fetch-models` | diarization only |
| `faster-whisper` | adapter: faster-whisper ASR + `energy-vad-cluster` diarization (the model-free diarizer is tuned on synthetic voices; use `whisper-sherpa` for speech) | yes when faster-whisper is installed | full |
| `faster-whisper+pyannote` | adapter: faster-whisper ASR + pyannote.audio diarization | **no** | full |
| `pyannote-audio` | adapter: pyannote.audio diarization only | **no** | diarization only |
| `whisperx` | adapter: whisperx ASR + alignment + diarization | **no** | full |
| `embedding-cluster` | adapter: pretrained speaker embeddings (SpeechBrain ECAPA, ungated) + this package's VAD, cells and clustering | **no** | diarization only |

The two oracles have `is_system_under_test = False`; `python -m pipeline run`
prints `[HARNESS SELF-TEST, not a system under test]` and `batch.json` records
`"system_under_test": false`. **An oracle score is never a system's score.**
They exist so the evals layer can be shown to (a) return zero on a perfect
hypothesis and (b) move in the right direction under controlled damage.

Two properties of the perturbed oracle that the tests pin because they are easy
to get wrong:

* `speaker_swap_rate=1.0` on a **two**-speaker meeting is a permutation. DER and
  cpWER both optimise over speaker mappings, so that "damage" scores **zero**.
  Use a partial rate (or ≥3 speakers) to raise speaker confusion.
* `word_sub_rate=1.0` does **not** give cpWER 1.0: substitutes come from the
  meeting's own vocabulary, so the alignment can re-match a token elsewhere.
  `word_del_rate=1.0` is the analytic case (N deletions / N words = 1.0).

### 2.1 `energy-vad-cluster` in detail (`pipeline/energy_vad.py`)

Every number here is HARNESS_POLICY, chosen on synthetic sources (the pulse-train
voices of the unit tests and the generator's formant voices), not on speech.

1. **VAD** — 25 ms frames / 10 ms hop; frame energy in dB; floor = 10th percentile;
   frames below −60 dBFS are never speech; 150 ms hangover; silences < 150 ms
   bridged; speech runs < 200 ms dropped. The decision depends on the spread
   `P90 − P10` of the frame energies:
   * spread ≥ 12 dB (**contrast**): threshold = floor + margin, where the margin is
     12 dB, lowered to the two-class (Otsu) split of the frame-energy histogram
     when that split lies closer to the floor, but never below 6 dB. At 10 dB SNR,
     or in reverberant dense talk, speech sits only 10-15 dB above the floor and
     the fixed 12 dB margin missed most of it (§2.2).
   * spread < 12 dB and median below −40 dBFS: the contrast rule (finds nothing in
     quiet stationary audio).
   * spread < 12 dB, loud: the energies are split in two (Otsu) and periodicity
     decides (the f0 tracker's voicing, on ≤ 2000 sampled frames). If the two
     classes are ≥ 6 dB apart and the quieter one is mostly aperiodic, it is noise
     and the threshold is the split (**split**, e.g. voices 10 dB over white noise).
     Otherwise, if ≥ 30 % of frames are periodic, the file is **dense** speech:
     threshold = median − 12 dB (e.g. back-to-back talk with no pause, which used
     to yield *no speech at all*). Otherwise **flat**: stationary noise, nothing.
   The decision is recorded in `hyp.extra["diarization"]["vad"]`.
2. **Frame features** — MFCC: pre-emphasis 0.97, Hamming, |rFFT|², 26 HTK mel filters
   (50 Hz–Nyquist), log, DCT-II (ortho), 13 coefficients with c0 dropped; cepstral
   mean *and variance* normalisation over speech frames (units: per-dimension frame
   std). f0: 40 ms autocorrelation per 10 ms hop, normalised and unbiased, the first
   peak within 85 % of the highest in 60-400 Hz, parabolic refinement; a frame is
   voiced when the normalised peak exceeds 0.5.
3. **Cells and windows** — speech regions are cut into 0.5 s *cells* (the unit that
   gets a label; a tail < 0.2 s joins its predecessor). Each cell is described by a
   1.5 s *window* centred on it and clipped to its speech region: the window's mean
   CMVN-MFCC vector plus its median f0 in semitones (`f0_weight` = 1 embedding unit
   per semitone, centred on the file median). If ≥ 25 % of a window's voiced frames
   sit an octave (±3 semitones) below its median, the lower group's median is used:
   the tracker's typical gross error is an octave-up pick when a formant is near
   2·f0 (measured: 18 % of the alto voice's frames). Windows with < 5 voiced frames
   have no f0; they are left out of the clustering and then join the nearest cluster
   in MFCC space.
4. **Speaker count and clustering** — spectral clustering of the window vectors:
   self-tuning affinity `exp(−d²/(σᵢσⱼ))`, σᵢ = distance to the k-th neighbour
   (k = max(2, min(7, n/4))), normalised; labels from deterministic k-means on the
   row-normalised top eigenvectors.
   * With `num_speakers`: exactly that many clusters. Clusters smaller than
     max(3 cells, 2 % of cells) are not speakers; if the cut spends a slot on such an
     island the cut is retried with a larger k and the k largest clusters are kept.
   * Without: k = the largest eigengap of the affinity's spectrum (k ≤ 8), capped by
     the number of average-linkage clusters at `distance_threshold` = 2.0 that reach
     the minimum size. One such cluster means **one speaker**: this absolute test is
     the explicit k = 1 check (an eigengap cannot tell one tight speaker from two).
     Then small clusters are absorbed.
   * More than 2000 cells (about 17 minutes of speech): an evenly spaced subset of
     2000 is clustered and every cell takes its nearest centroid, so the O(n²)
     matrices stay bounded.
5. **Smoothing** — adjacent same-label cells merge (gap ≤ 0.3 s); islands < 0.3 s take
   a neighbour's label; residue < 0.3 s is dropped; labels are renamed
   `spk0, spk1, …` by first appearance.

**The f0 feature is synthetic-voice-friendly.** The generator's formant voices differ
mainly in f0 (`VOICES` in generator/tts/formant.py: 80-270 Hz, neighbours in that
table 2.0-4.5 semitones apart), and on those voices the chunk-mean MFCCs alone do not
separate speakers: within-speaker distances (median 1.4-1.7) overlap between-speaker
ones (median 1.5-2.5). The probability that a between-speaker distance exceeds a
within-speaker one (smoke and default, seeds 1-6, device Ogg) rises from 0.68 to 0.92
with the f0 term. Real speakers overlap far more in f0, so these weights are not a claim about
speech; a pretrained speaker embedding (`embedding-cluster`) is the path for real
recordings.

**Memory.** Energy, MFCC and f0 are computed in blocks (8 MiB of spectrum per block)
over a zero-copy strided view; nothing materialises a whole-file frame matrix.
Measured with tracemalloc on alternating pulse voices: 34 MiB peak for 1 minute,
37 MiB for 4, 44 MiB for 10, 128 MiB for 30 and 163 MiB for 60 minutes (8.8 s). The
review measured about 91 MiB per audio minute before (about 5 GiB for an hour). The
blockwise results equal the one-block results exactly (tested).

### 2.2 Measured DER, before and after (2026-09-25)

Scored with the evals layer's own defaults (`evals.score_meeting`,
`evals.metrics.DEFAULT_DER_COLLAR` = 0.25), which reproduce docs/integration.md's
seed-3 numbers. Generator meetings `generator.testing.fixture_meeting(preset, seed)`,
device Ogg (`device/recording.ogg`) unless noted; "n" is the hypothesis speaker count.
Seeds 1-3 were used while developing; seeds 4-6 are held out (different voice pairs).
"Before" is the pre-review code (HEAD `70249ba`).

| preset (ref speakers) | seed | before: unaided n / DER | before: hint DER | after: unaided n / DER | after: hint DER |
|---|---|---|---|---|---|
| smoke (2) | 1 | 2 / 0.507 | 0.507 | **2 / 0.059** | 0.059 |
| smoke (2) | 2 | 1 / 0.585 | 0.572 | **2 / 0.103** | 0.103 |
| smoke (2) | 3 | 1 / 0.373 | 0.294 | **2 / 0.067** | 0.067 |
| default (3) | 1 | 1 / 0.633 | 0.272 | **3 / 0.044** | 0.044 |
| default (3) | 2 | 1 / 0.641 | 0.432 | **3 / 0.050** | 0.050 |
| default (3) | 3 | 1 / 0.517 | 0.324 | **3 / 0.044** | 0.044 |
| single_speaker (1) | 1 | 2 / 0.085 | 0.084 | **1 / 0.084** | 0.084 |
| single_speaker (1) | 2 | 2 / 0.115 | 0.115 | **1 / 0.115** | 0.115 |
| single_speaker (1) | 3 | 1 / 0.086 | 0.086 | 1 / 0.086 | 0.086 |
| smoke (2), held out | 4 | 3 / 0.496 | 0.485 | 3 / 0.398 | 0.078 |
| smoke (2), held out | 5 | 2 / 0.598 | 0.598 | **1 / 0.596** | 0.143 |
| smoke (2), held out | 6 | 3 / 0.148 | 0.547 | 2 / 0.090 | 0.090 |
| default (3), held out | 4-6 | 2, 1, 1 / 0.490-0.612 | 0.291-0.504 | 3, 3, 3 / 0.063-0.074 | 0.063-0.074 |
| single_speaker (1), held out | 4-6 | 2, 2, 2 / 0.109-0.121 | 0.083-0.118 | 1, 1, 1 / 0.083-0.118 | same |

On `mix.wav` the pattern is the same: before, smoke unaided 0.511/0.585/0.378 and
single_speaker seeds 2 and 3 split into 3 speakers; after, smoke 0.067/0.122/0.101,
default 0.034/0.047/0.058, single_speaker 1 speaker on every seed, and every held-out
mix.wav count is exact. Across the 36 smoke/default/single_speaker files (seeds 1-6,
device Ogg and mix.wav) the unaided count is exact on 34; the two misses are held-out
device Oggs (smoke seed 4: 3 speakers; smoke seed 5: 1 speaker).

Stress presets and long meetings (means over seeds 1-3, device Ogg and mix.wav):

| set | before: unaided / hint DER | after: unaided / hint DER | after: unaided count exact |
|---|---|---|---|
| notepin_s_noisy (2 speakers, 10 dB SNR) | 0.742 / 0.742 | **0.018 / 0.018** | 6/6 |
| overlap_heavy (4 speakers, 30 % overlap) | 0.678 / 0.661 | 0.253 / 0.279 | 4/6 (two files: 5) |
| default preset at 300 s, seeds 1-2 | 0.609 / 0.544 | 0.105 / 0.052 | 2/4 (two files: 4) |

The notepin improvement is the VAD's Otsu margin (before, 70 % of the speech in
seed 1's mix.wav was missed); the rest is the f0 feature and the count estimate.

The integration gate "DER < 0.5 with the hint" (docs/integration.md) held on seed 3
only before; it now holds with margin on every seed above. The integration test's
tolerance of ±1 speaker without a hint is still met; on smoke seed 3 the count is now
exact.

## 3. How to add a system

1. Implement `Transcriber` and/or `Diarizer` (or a whole `Pipeline`) in a new
   module under `pipeline/`. Import heavy dependencies lazily inside
   `__init__`/methods and raise `PipelineUnavailable` if they are missing.
2. Register a factory, **declaring the `--param` keys it reads** (a typo'd key is
   then a usage error instead of being silently ignored):

   ```python
   from pipeline.base import COMMON_AUDIO_PARAMS, ComposedPipeline, PipelineConfig, register

   @register("my-system", description="...", availability=lambda: _missing("my_pkg"),
             params={"model", "device"} | COMMON_AUDIO_PARAMS)
   def _make(config: PipelineConfig | None = None):
       cfg = config or PipelineConfig(name="my-system")
       return ComposedPipeline(MyTranscriber(**cfg.params), MyDiarizer(), cfg, name="my-system")
   ```

   `availability` returns `None` when the system can run and a one-line reason
   otherwise; `python -m pipeline list` shows it. `COMMON_AUDIO_PARAMS` is
   `num_speakers`, `audio_check`, `duration_tolerance_s`. A pipeline that is not a
   `ComposedPipeline` should call `self.find_meeting`, `self.check_audio` and
   `audio_provenance` as `WhisperXPipeline.run` does.
3. Add the module to `_ensure_builtin_registrations()` in `base.py` (or import
   it from `pipeline/adapters.py`).
4. Emit text as lowercase tokens (`normalize_text` is the minimum); the evals
   layer owns real normalisation.
5. Add `tests/test_pipeline_<name>.py`. If the system needs models, test its own
   logic against stand-in modules (see `tests/test_pipeline_adapters.py`), and gate a
   real-model test on an explicit environment variable. Note that tests/conftest.py
   runs strict in CI: a skip that no documented gate explains fails the run, so a
   missing package is a checked branch, not an `importorskip`.

## 4. CLI

```
python -m pipeline list [--json]
python -m pipeline run   --pipeline NAME --audio FILE [--meeting-dir DIR] --out hyp.json
                         [--param k=v ...] [--seed N] [--sample-rate HZ]
python -m pipeline batch --pipeline NAME --root DIR --out DIR
                         [--audio-key mix|device|device.<name>|stem:<spk>|<relpath>]
                         [--param k=v ...] [--seed N] [--fail-fast]
python -m pipeline import-stm --stm ref.stm [--rttm ref.rttm] --audio mix.wav --out DIR [--meeting-id ID]
python -m pipeline models [--verify] [--json] [KEY ...] # pinned model files: present / sha256; --verify exits 1 if any is missing or bad (§11)
python -m pipeline fetch-models [KEY ...] [--force]   # download pinned models, no token (§11)
```

`run` writes `hyp.json`, `hyp.rttm`, `hyp.stm` side by side. `batch` writes
`<out>/<meeting_id>/hyp.*` and `<out>/batch.json` (schema
`plaud-harness/batch/1`: per-meeting status, audio used, elapsed time, error
text). One broken meeting is reported and does not sink the batch. A meeting
whose `meeting_id` was already written in the same batch is reported as an error
(the first hypothesis is kept, never overwritten), and no output path may leave
`--out`.

Exit codes (HARNESS_POLICY): **0** ok; **1** a run failed (including any unexpected
exception: `main` never lets one escape); **2** usage error: unknown or unavailable
pipeline, missing input, an unknown `--param` key, or a bad value.

`--param` values are JSON-coerced (`num_speakers=2` → int, `word_del_rate=0.3`
→ float, `vad_filter=true` → bool), then checked against the pipeline's declared keys
(`python -m pipeline list --json` shows them) and types: `num_speakers` must be a
positive integer; `energy-vad-cluster` fields keep their dataclass types (a float
field takes any number, an int field an integer, a bool field `true`/`false`).
`perturbed-oracle` reads `speaker_swap_rate, time_shift_s, boundary_jitter_s,
word_sub_rate, word_del_rate, word_ins_rate, seed`; `oracle` reads nothing;
`energy-vad-cluster` reads every field of `VadParams`, `MfccParams`, `PitchParams`,
`ClusterParams` plus `COMMON_AUDIO_PARAMS`.

`import-stm --meeting-id ID`: from a multi-meeting STM it selects that file id; for a
single-meeting STM whose file id differs it **renames** the import (the original id is
kept as `generator.scenario.stm_file_id`). An id that matches no row of a
multi-meeting STM, or an STM without speaker rows, is an error: an empty reference
would make every score meaningless. The NIST STM `<label>` field (e.g.
`<o,f0,female>`, recognised as a `<…>` token containing a comma right after the end
time) is kept out of the words; the NIST pseudo-speakers `inter_segment_gap`,
`excluded_region` and `ignore_time_segment_in_scoring` are dropped and counted, and
the excluded spans are kept in `generator.scenario.stm_excluded_regions`.

## 5. Evidence classes

Vocabulary from `docs/protocol-ledger.md` ("Confidence vocabulary" and the R7
mapping). This package encodes **three codec facts** and nothing else about the device:

| Fact | Class | Source | Used for |
|---|---|---|---|
| Recorder audio is 16 000 Hz | **BYTECODE_PROVEN** | `OggUtils.b = 16000`, `build/evidence/javap/ALL.txt:11496`; `sipush 16000` at `ALL.txt:11548`; mirrored in `emulator/plaudsim/audio.py:21` | the loader's default target rate |
| 20 ms / 320-sample frames | **BYTECODE_PROVEN** | `OggUtils.c = 320`, `ALL.txt:11498`; `sipush 320` at `ALL.txt:11550`; `emulator/plaudsim/audio.py:22` | exported as `DEVICE_FRAME_SAMPLES`; the synthetic Ogg's frame duration |
| Opus at 32 kbps CBR, exactly 80 B/frame/channel; the SDK's repack rejects any other packet size | **SOURCE-DERIVED** (SDK_PROVEN) | `docs/protocol-ledger.md:1171-1180` ("Codec parameters") | only to shape the *synthetic* `device/recording.ogg` that `pipeline.synthetic` writes |

The synthetic Ogg now realises that shape: `vbr=off`, 20 ms frames, 32 kbps, and a
test asserts every packet is exactly 80 bytes (before the review libopus defaulted to
VBR: variable packets, 34 bytes and up on the test signal; the review measured 43-152). `generator/opus.py` additionally sets
`application=voip`; `pipeline.synthetic` leaves libopus's default because the voip
mode's speech filtering lowers the pure-tone round-trip fidelity the loader tests rely
on, and nothing in the evidence fixes the application mode.

Everything else in the package is **HARNESS_POLICY** (§6).

**UNKNOWN, and not modelled:**

* The audio shape a **real** recorder emits over BLE. No authentic Plaud
  recording exists in this environment (R6-S3, R6-S4). The loader has been
  exercised on standard Ogg/Opus only. Ledger §8 documents quirks in files the
  SDK itself writes (preSkip 0, granule inflation, no EOS, non-conformant
  OpusTags, uninitialised bytes); whether PyAV/libopus or libsndfile tolerate
  such a file is **UNKNOWN** — the synthetic writer produces conformant Ogg
  and does not reproduce those quirks. Note that granule inflation would trip the
  container-duration warning (§1), which `audio_check=warn` records rather than
  refuses.
* Plaud's own transcription/diarization behaviour. The cloud endpoints are
  CLOUD_OBSERVED only (R7-S12c) and never exercised. This layer evaluates
  *our* stack on device-shaped audio; it says nothing about Plaud's ASR.

## 6. HARNESS_POLICY items

Contract and loading
* Schema strings `plaud-harness/meeting/1`, `plaud-harness/hypothesis/1`, `plaud-harness/batch/1`.
* Default target sample rate = the recorder's 16 kHz (the fact is BYTECODE_PROVEN; choosing it as the *loader default* is policy).
* Multichannel input is averaged to mono; resampling is gcd-reduced polyphase FIR (`resample_poly`); every path is clipped to [−1, 1].
* Routing by suffix (`.wav/.flac/.aiff` → soundfile, else PyAV); a wav that soundfile rejects is not retried with PyAV.
* PTS gaps > 1 ms are zero-filled (or refused with `on_gap="error"`); decoded length vs container duration tolerance 20 ms; decoded length vs meeting.json `duration_s` tolerance 0.05 s; `audio_check` default `strict`.
* `meeting.json` discovery: `--meeting-dir`, else the audio's directory, then one and two levels up. `meeting_id` falls back to the audio stem only when there is no meeting.json at all.
* `meeting_id` pattern `^\w[\w.-]{0,199}$`; meeting.json audio paths must stay inside the meeting dir.
* Times are written with 3 decimals in RTTM/STM; RTTM channel is always `1`; hyp.json keeps full floats.
* `FALLBACK_SPEAKER = "spk0"` when there is no diarizer. `assign_speakers` (via `SpeakerIndex`) gives each word the speaker with the maximal **total** overlap summed over that speaker's turns; a tie (within 1e-9 s) goes to the tied speaker whose overlapping turn started earliest (chosen **without evidence**; on the one 60 s AMI clip another tie-break scored better with a hint, §11.5; the rule text is recorded as `hyp.extra["assignment"]["rule"]`); a word that overlaps nothing (or has zero length) takes the turn at the smallest gap `max(0, t0 − end, start − t1)`, ties to the earliest turn. Before 2026-09-25 the maximum was per turn and "nearest" measured the word midpoint to the nearest turn *boundary*, which sent a zero-length word in the middle of a long turn to a neighbouring short turn. Consecutive same-speaker words of one ASR segment form one segment spanning exactly its words (sorted by start), never shorter than `MIN_SEGMENT_S` = 0.01 s (evals requires end > start and ASR emits zero-length words); a word whose text normalises to k tokens is split into k equal parts.
* `normalize_text`: lowercase, whitespace split, strip surrounding ASCII punctuation.
* Every registered pipeline declares its `--param` keys; unknown keys and bad values are `ParamError`.

Oracles
* Perturbation order is fixed: speaker swaps → word delete/substitute/insert → global time shift → boundary jitter → re-sort. RNG is `random.Random(seed)`.
* Substitution/insertion tokens come from the meeting vocabulary ∪ `FALLBACK_VOCAB`; a substitute always differs from the original token.
* Inserted words get 50 ms; a jittered/shifted segment is never shorter than 10 ms and never starts before 0; words are clipped into their segment.

`energy-vad-cluster`
* Every default in §2.1: VAD 25/10 ms, P10 floor, 12 dB margin (Otsu-lowered to ≥ 6 dB), −60 dBFS absolute floor, P90 spread test, −40 dBFS / 30 % voiced for the low-spread rules, 150 ms hangover, 200 ms min speech, 150 ms min silence; MFCC 26 mels, 13 coefficients, c0 dropped, CMVN over speech frames; f0 40 ms frames, 60-400 Hz, first peak within 85 %, voiced > 0.5; 0.5 s cells, 1.5 s windows, `f0_weight` 1 per semitone, ≥ 5 voiced frames, octave guard at 25 %; self-tuning affinity k = min(7, n/4), eigengap k ≤ 8, `distance_threshold` 2.0 (average linkage), minimum cluster max(3 cells, 2 %), 2000-cell clustering cap; 0.3 s island/merge rules; labels by first appearance; blocks of 8 MiB of spectrum.

Adapters
* Default models: faster-whisper `small.en` int8 on CPU, always the pinned, sha256-checked local copy (`python -m pipeline fetch-models`; unfetched is *unavailable*, never a library download). The `faster-whisper` and `faster-whisper+pyannote` entries also accept a local CTranslate2 directory or a faster-whisper size/repo name taken from the Hugging Face cache only; `allow_download=true` lets faster-whisper download such a name (unpinned, no token sent). `whisper-sherpa` accepts pinned names and local directories only (§11.1); pyannote `speaker-diarization-community-1` when pyannote.audio ≥ 4 is installed, else `speaker-diarization-3.1`; whisperx `base`; SpeechBrain `spkrec-ecapa-voxceleb`. Token from `$HF_TOKEN` (name configurable), never stored.
* `faster-whisper+pyannote` attributes words on pyannote 4.x's `exclusive_speaker_diarization`; `pyannote-audio` alone reports the overlap-aware `speaker_diarization`.
* whisperx words the aligner could not time are spread uniformly between their aligned neighbours (or the segment bounds) and take the previous aligned word's speaker; the count is `hyp.extra["whisperx"]["interpolated_words"]`.
* `embedding-cluster`: cosine distance, `distance_threshold` 0.5 — **uncalibrated**; tune on a dev split once a model is installed.

CLI and importer
* Exit codes 0/1/2; batch continues past a failing meeting unless `--fail-fast`; duplicate ids and out-of-tree paths are per-meeting errors.
* `import-stm` spreads a segment's tokens uniformly over its span when the source has no word times, and records `"word_times": "interpolated (HARNESS_POLICY)"` in `generator.scenario`.
* `pipeline.synthetic` voices (pulse trains + band-passes) and its Ogg/Opus writer (PyAV/libopus, 16 kHz mono, 32 kbps hard CBR, 20 ms) are test support.

## 7. V5 procedure — "full pipeline hits target cpWER / DER on held-out AMI"

**Not run here.** Results on the 4 local test-split meetings, which are not V5, are in
[docs/v5-results.md](v5-results.md), reproduced by `scripts/run-v5.sh`. Update 2026-09-25: the AMI manual annotations and four test-split
Mix-Headset recordings (ES2004a, IS1009a, TS3003a, EN2002a) are now under
`data/corpora/ami/raw/` (git-ignored), and `whisper-sherpa` (§11) runs real models, so
V5 is no longer blocked on models. The NXT → contract conversion now exists
(`python -m evals.ami`, [docs/evals.md "AMI"](evals.md#ami-the-v5-reference)), and the
four meetings are converted under `data/corpora/ami/meetings/`. V5 still needs a
full-meeting run, which this section's procedure describes. The 20 s and 60 s AMI
excerpts scored in §11.5 are sanity checks, **not** V5. What follows is the procedure
the code supports, written so an operator with the data can run it without touching
the package.

### Expected layout

```
data/corpora/ami/meetings/              (gitignored: data/corpora/)
  ES2002a/
    meeting.json        plaud-harness/meeting/1 — written by `python -m evals.ami convert`
    ref.stm             one line per reference speech turn (meeteval STM)
    ref.rttm            reference speaker turns
    mix.wav             16 kHz mono Mix-Headset, a read-only hard link to the raw WAV
    device/recording.ogg   OPTIONAL: the same audio in the device shape
                            (pipeline.synthetic.write_device_ogg_opus, or the
                            emulator's export path) to score the BLE-shaped input
  ES2002b/ ...
```

`batch` takes every direct subdirectory that has a `meeting.json`. Use the AMI
**test** split of the standard full-corpus partition for the held-out claim and
record which split file you used in `generator.scenario`. Meeting ids must match
`^\w[\w.-]{0,199}$` (AMI ids do).

### Commands

```bash
# 0. Obtain AMI (CC BY 4.0, docs/SOURCES.md): the manual-annotations zip and the
#    <ID>.Mix-Headset.wav files, under data/corpora/ami/raw/ (docs/evals.md "AMI").

# 1. Convert (evals layer): NXT words -> data/corpora/ami/meetings/<ID>/
.venv/bin/python -m evals.ami extract
.venv/bin/python -m evals.ami convert     # every <ID>.Mix-Headset.wav present
#    For another corpus that ships STM/RTTM, `pipeline import-stm` (§4) builds the
#    same layout (word times interpolated: HARNESS_POLICY).

# 2. Sanity: the oracle must score 0 on every meeting (evals layer)
.venv/bin/python -m pipeline batch --pipeline oracle --root data/corpora/ami/meetings --out build/hyp/oracle

# 3. Systems under test (energy-vad-cluster and whisper-sherpa run here)
.venv/bin/python -m pipeline batch --pipeline energy-vad-cluster --root data/corpora/ami/meetings --out build/hyp/evc
.venv/bin/python -m pipeline fetch-models    # once: pinned open weights, no token (§11)
.venv/bin/python -m pipeline batch --pipeline whisper-sherpa --root data/corpora/ami/meetings --out build/hyp/ws
.venv/bin/python -m pipeline batch --pipeline whisper-sherpa --root data/corpora/ami/meetings --out build/hyp/ws-hint --param num_speakers=4
.venv/bin/python -m pipeline batch --pipeline embedding-cluster --root data/corpora/ami/meetings --out build/hyp/emb
.venv/bin/python -m pipeline batch --pipeline whisperx        --root data/corpora/ami/meetings --out build/hyp/whisperx --param model=large-v3
.venv/bin/python -m pipeline batch --pipeline faster-whisper+pyannote --root data/corpora/ami/meetings --out build/hyp/fw-pa

# 4. Score (evals layer): cpWER via meeteval on hyp.stm vs ref.stm,
#    DER/JER via pyannote.metrics on hyp.rttm vs ref.rttm, then the gates.
```

The pass/fail thresholds ("target cpWER / DER") belong to the evals layer's
gate configuration, not to this package; this package only guarantees that
every hypothesis parses with `meeteval.io.STM/RTTM` and
`pyannote.database.util.load_rttm` (tested).

## 8. What is NOT done, and known limitations

* **V5 itself.** Not met (§7). Four full AMI test meetings and a synthetic Piper
  set were run on 25 Sep; the results are poor ([`docs/v5-results.md`](v5-results.md)).
* **`whisper-sherpa` limitations** are listed in §11.7 (uncalibrated cluster
  threshold that over-counts speakers unaided, word-run RTTM, timing under load).
* **Four adapters have never run against a model.** The faster-whisper adapter has
  (§11). `pyannote-audio`, `faster-whisper+pyannote`, `whisperx` and `embedding-cluster`
  are written against each package's documented public API, cited from the READMEs
  as remembered offline (they could not be fetched here). Their own logic is tested
  against stand-in modules shaped after that API (tests/test_pipeline_adapters.py):
  pyannote.audio 3.x and 4.x outputs, the `token=`/`use_auth_token=` spellings,
  `vad_filter` forwarding, whisperx's unaligned words, the embedding plumbing. Whether
  the real libraries match the stand-ins, and the 4.x default model name, are
  unverified until a stage installs them; pin the tested versions then
  (requirements/pipeline.txt lists the targeted API ranges).
* **`energy-vad-cluster` has not seen speech**, and its f0 feature is tuned for
  synthetic voices (§2.1). Remaining weaknesses measured on generator audio:
  * the closest voice pairs (mezzo/soprano/child, 2.0-2.7 semitones) are still
    miscounted without a hint on two held-out device Oggs (smoke seed 4 → 3 speakers,
    seed 5 → 1); on smoke seed 4 the eigengaps are nearly tied (0.321 vs 0.336), so the
    unaided count there is fragile;
  * no overlap handling: overlap_heavy DER stays 0.22-0.35 and two files in six
    gain a fifth speaker unaided; long (300 s) meetings gain a fourth speaker unaided
    in two of four files (DER still 0.14-0.16);
  * a speaker change inside one speech region resolves to a 0.5 s cell;
  * the 6 dB minimum VAD margin was checked on white noise only; fluctuating noise
    (babble, music) could be admitted as speech;
  * the low-spread rules rely on periodicity: loud aperiodic dense audio stays
    silence, and loud periodic non-speech (a hum) would be read as speech.
* **Truncated recordings** are caught only when a meeting.json gives the true
  duration; a bare file is self-consistent.
* **AMI NXT → contract conversion** is not in this package: `python -m evals.ami`
  (evals layer) does it. `import-stm` starts from STM/RTTM. STM alternations
  (`{a / b}`) and other NIST transcript markup are not interpreted.
* **No scoring or gates** live here (evals track). The tests call
  pyannote.metrics and meeteval directly only to prove the oracle/perturbation
  mechanics move the public metrics as claimed, and score generator meetings with
  `evals.score_meeting` for the diarizer's measurements.
* **SDK-writer Ogg quirks** (ledger §8) are not synthesised and the loader's
  tolerance to them is unknown.
* **Stems and multichannel** in `meeting.json` are read but unused; every
  pipeline works on one mono mix.

## 9. Tests

`tests/test_pipeline_oracle.py` (31), `test_pipeline_energy_vad.py` (52),
`test_pipeline_audio.py` (20), `test_pipeline_contract.py` (51),
`test_pipeline_cli.py` (25), `test_pipeline_adapters.py` (12) — 191 tests, about
26 s (the generator meetings take about 10 s to build) — plus `test_pipeline_models.py`
(33, 2026-09-25: 30 model-free, 3 real-model on AMI, about 29 s with the models; §11.6). Highlights: oracle == ground
truth; seeded perturbation counts; DER/cpWER move as claimed including the
permutation-invariance trap; VAD edges within one frame, hangover on the end only,
the contrast, split, dense and flat decisions; blockwise front end equal to the
one-block and whole-signal results; peak memory bounded; the f0 tracker on known
pulse trains and the octave guard; the count estimate on separated blobs, one blob,
an outlier island, two and three pulse voices without a hint; generator meetings
without a hint (smoke seeds 1-3 → 2 speakers, DER < 0.25; default seed 1 → 3;
single_speaker seed 2 → 1; notepin_s_noisy seed 1 → miss < 0.1); Ogg/Opus fixtures
(sha-pinned) decode to the generator tone; 80-byte CBR packets; clipping on every
path; zero-filled PTS gaps; truncation refused under `audio_check=strict`; CLI
files parse back with this package, meeteval and pyannote; batch isolates a broken
meeting, refuses duplicate ids and out-of-tree paths; `import-stm` renames, refuses an
empty import and drops NIST labels; typo'd params exit 2; the adapters' logic against
stand-ins.

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_pipeline_*.py -q -p no:cacheprovider --timeout=120`

The loader tests read `tests/fixtures/r6s2_16k_{mono,stereo}.ogg`. Those files match
`.gitignore`'s `*.ogg` and are not in the repository (only the manifest, with their
sha256, is), so on a clean checkout the four fixture tests fail until the files are
committed or regenerated with `scripts/generate_r6s2_fixture.py` (a regenerated file
must still match the pinned sha256).

## 10. Review fixes (2026-09-25)

Each fix has a test that fails on the pre-review code and passes now (run against a
scratch export of HEAD `70249ba` with the new tests).

| ID | What changed | Proof (test → before-fix result) |
|---|---|---|
| PIPE-01 | f0 feature, cells + windows, spectral clustering with an eigengap count, absolute k = 1 guard, small-cluster absorption (`energy_vad.py`) | `test_generator_smoke_meeting_two_voices_without_hint[1-3]` → seed 1 DER 0.507, seeds 2-3 found 1 speaker; `test_generator_default_meeting_three_voices_without_hint` → 1 speaker; `test_generator_single_speaker_is_not_split` → 2 speakers; table in §2.2 |
| PIPE-02 | low-spread VAD rules (dense / split / flat) and the Otsu margin (`energy_vad.energy_vad`) | `test_one_long_voice_is_one_speaker` → no speech at all; `test_back_to_back_voices_with_no_silence_are_speech_not_silence`, `test_low_snr_file_splits_noise_from_speech`, `test_generator_noisy_preset_speech_is_not_missed` (mix.wav miss 0.70 before) |
| PIPE-03 | PTS-gap zero-fill / refusal, decode errors wrapped, container and meeting.json duration checks, `audio_check` (`base.py`) | `test_mid_file_corruption_is_zero_filled_so_later_audio_keeps_its_time` → decoded 9.000 s; `test_truncated_ogg_is_refused_by_a_pipeline_that_knows_the_duration` → no error; `test_decode_time_errors_become_audio_format_errors` |
| PIPE-04 | `meeting_id` pattern, meeting.json audio paths contained, batch duplicate-id and out-of-tree refusal (`meeting.py`, `cli.py`) | `test_batch_refuses_a_meeting_id_that_escapes_out`, `test_batch_reports_a_duplicate_meeting_id_instead_of_overwriting` (before: "2 ok, 0 failed"), `test_batch_refuses_meeting_json_audio_paths_outside_the_meeting_dir`, `test_meeting_id_must_be_one_safe_path_component`, `test_resolve_audio_keeps_meeting_json_paths_inside_the_meeting_dir` |
| PIPE-05 | pyannote output normalised for 3.x and 4.x, `token=` first, version-aware default model, whisperx `token=` fallback, `vad_filter` forwarded, `embedding-cluster` added (`adapters.py`) | `test_pyannote_4x_output_object_is_read_not_crashed_on` → `AttributeError: 'DiarizeOutput' object has no attribute 'itertracks'`; `test_whisperx_diarization_pipeline_token_rename_is_guarded` → `TypeError`; `test_faster_whisper_pyannote_forwards_vad_filter_and_uses_the_exclusive_timeline`. PLAUSIBLE: verified against stand-ins only, except faster-whisper, which was re-checked against the installed 1.2.1 and run on AMI speech on 2026-09-25 (§11.3) |
| PIPE-06 | `meeting_from_stm` never builds an empty meeting; `--meeting-id` renames a single-meeting STM | `test_import_stm_meeting_id_renames_a_single_meeting_stm` → 0 segments; `test_meeting_from_stm_never_builds_an_empty_meeting` |
| PIPE-07 | whisperx unaligned words kept with interpolated times (`adapters.repair_word_times`) | `test_whisperx_keeps_words_it_could_not_align` → "we shipped units" |
| PIPE-08 | STM `<label>` field parsed out; NIST pseudo-speakers dropped (`formats.py`, `meeting.py`) | `test_import_stm_drops_the_nist_label_field` → speakers `['alice', 'inter_segment_gap']`; `test_parse_stm_separates_the_nist_label_field` |
| PIPE-09 | synthetic Ogg hard CBR, 80 B packets; docstring and citation fixes (`synthetic.py`, `base.py`, this file) | `test_synthetic_device_ogg_packets_are_80_byte_cbr` → sizes 34, 64, 66, … |
| PIPE-10 | adapter unavailability test parametrised per adapter; FasterWhisperTranscriber exercised | with a stand-in `faster_whisper` importable, the old test skipped entirely; the new one skips only `faster-whisper` and checks the other four; `test_faster_whisper_transcriber_emits_the_contract`; `test_real_faster_whisper_on_a_generator_clip` (runs once the package and `PLAUD_HARNESS_FW_MODEL` exist) |
| PIPE-11 | declared params, `ParamError` → exit 2, `main` catches everything → exit 1, malformed meeting.json raises (`base.py`, `cli.py`, `energy_vad.py`, `oracle.py`) | `test_typod_param_is_a_usage_error` → rc 0; `test_bad_param_value_is_a_usage_error_not_an_exception` → `TypeError` escaped `main`; `test_unexpected_exception_in_run_is_exit_1`; `test_malformed_meeting_json_is_an_error_not_a_silent_stem_fallback` → rc 0 |
| PIPE-12 | clip on every loader path (`base.load_audio`) | `test_float_wav_over_full_scale_is_clipped_on_every_path[16000]` → max 3.0 |
| PIPE-13 | blockwise energy/MFCC/f0, no full-length copies, in-place affinity, 2000-cell clustering cap | `test_diarizer_peak_memory_is_bounded_and_grows_slowly_with_length` → 360.6 MiB peak for 4 minutes (now 37); `test_front_end_results_do_not_depend_on_the_block_size` |

## 11. Real models on CPU: `whisper-sherpa` (2026-09-25)

`whisper-sherpa` is the first system here that runs trained models on speech. Both
halves use openly licensed, **ungated** weights (no account, no token) and run on the
M1's CPU. Code: `pipeline/whisper_sherpa.py` (diarizer, composition, registry entries),
`pipeline/adapters.py` (`FasterWhisperTranscriber`), `pipeline/model_store.py` (pinned
files), `pipeline/base.py` (`SpeakerIndex`, `assign_speakers`). Package versions:
`requirements/models.txt`. Every choice below is HARNESS_POLICY unless it says otherwise.

```
audio (16 kHz mono) ─┬─ faster-whisper small.en, int8, beam 5, word timestamps ─ words ─┐
                     └─ sherpa-onnx: pyannote seg-3.0 → CAM++ embeddings → clustering ─ turns ─┴─ SpeakerIndex ─ hyp.json/.rttm/.stm
```

### 11.1 Fetching the models

```bash
.venv/bin/python -m pipeline fetch-models          # all three assets; keeps files whose sha256 already matches
.venv/bin/python -m pipeline models --verify       # hash every file against its pin
.venv/bin/python -m pipeline list                  # whisper-sherpa: available / reason
```

Weights go to `data/models/` (git-ignored; `$PLAUD_HARNESS_MODELS_DIR` overrides). The
fetcher sends plain HTTPS GETs with **no Authorization header**. It writes `*.part`,
checks size and sha256 against the pin, then renames the file into place. A mismatch
deletes the partial file and fails. Archive members are extracted by exact name, never
by pattern. `models --verify [KEY ...]` exits 1 when any requested asset is missing or
has a file whose sha256 differs from its pin (before the 2026-09-25 review it exited 0
on missing files).

**Nothing downloads implicitly, and what is loaded is hashed** (review fixes,
2026-09-25):

* Availability (`pipeline list`) checks presence and size only, which is cheap. The
  constructors of `whisper-sherpa` and `sherpa-onnx-diarization` call
  `model_store.require`, which hashes every pinned file before the library reads it
  (small.en's 486 MB: 0.293 s here, measured once; memoised per process by device,
  inode, size and mtime). A file whose sha256 differs from the pin is refused with
  `model weights do not match their pinned sha256: …` and the fetch command. Before
  the fix, a same-size file with other contents ran and was reported with the
  *pinned* sha256. The window between the hash and the library's own read is not
  closed.
* `hyp.extra["models"]` now records the **computed** digests (`files[..].sha256`, next
  to `pinned_sha256`, `"pinned": true`, `"sha256_matches_pin": true`). An explicit
  model path (`segmentation_model=`, `embedding_model=`, a local `model=` directory) is
  hashed as well and recorded with `"pinned": false`.
* The legacy `faster-whisper` and `faster-whisper+pyannote` entries resolve every
  model name to a local directory before `WhisperModel` sees it. The pinned `small.en`
  is used only when fetched: an unfetched copy raises the `fetch-models` reason, where
  the library used to download about 486 MB from the unpinned `main` branch through
  huggingface_hub, which would send any stored token. Other size or repo names come from
  the Hugging Face cache only (`local_files_only=True`). `allow_download=true` opts in
  to faster-whisper's download (unpinned; `use_auth_token=False`, so no stored token is
  sent). Both entries now build a `ModelComposedPipeline`, so their hypotheses carry
  the same provenance, settings and timing as `whisper-sherpa`. Their registry
  availability still means "package importable": with an unfetched default model,
  `pipeline list` shows them *available*, and they fail at construction with the
  fetch reason.

### 11.2 Models: sources, hashes, licences

Fetched 2026-09-25. Each sha256 was computed locally. For the Hugging Face files it also
matches the repository's LFS pin, and the segmentation `model.onnx` matches the LFS pin
of `csukuangfj/sherpa-onnx-pyannote-segmentation-3-0`.

| asset key | file(s), bytes, sha256 | source URL | licence (where stated) |
|---|---|---|---|
| `faster-whisper-small.en` (ASR; 486.1 MB) | `model.bin` 483,545,366 `62b2a45b05ee59acb4a5341b33ee35e041395d378d418a18acfe4c9e768ee37a`; `config.json` 2,657 `666a9605530ac1f61fa8177f3702b4dacec9966749e42610839fcc32661d5fae`; `tokenizer.json` 2,128,466 `929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df`; `vocabulary.txt` 422,309 `ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf` | `https://huggingface.co/Systran/faster-whisper-small.en/resolve/d1d751a5f8271d482d14ca55d9e2deeebbae577f/<file>` (revision pinned) | **MIT**: model card metadata `license: mit`; repo not gated |
| `pyannote-segmentation-3.0-onnx` (segmentation; 6.0 MB) | `model.onnx` 5,992,913 `220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079`; `LICENSE` 1,061 `14d7016ad68e7394d6e6b78d96cc2ae431c905287b89674cfdf021e79e62b8ba`, from archive 6,958,444 B `24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488` | `https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2` | **MIT**, "Copyright (c) 2022 CNRS" (LICENSE in the archive; the ONNX metadata points at `huggingface.co/pyannote/segmentation-3.0/blob/main/LICENSE`; that card's metadata says `license: mit`) |
| `3dspeaker-campplus-en-voxceleb` (speaker embedding; 29.6 MB) | `3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` 29,596,978 `357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b` | `https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` (the tag really is spelled "recongition") | **Apache-2.0**: ModelScope `iic/speech_campplus_sv_en_voxceleb_16k`, field "License: Apache License 2.0" (the ONNX metadata names that model) |

Total download for this task: 486.1 + 7.0 + 29.6 MB for the pins, plus 36.6 MB fetched a
second time to test the fetcher into a scratch directory. About 559 MB in all, within the
1.5 GB budget. On disk: 502 MB under `data/models/`. The review fixes downloaded no
weights, only the licence pages and API metadata cited below (86 KB).

**Caveats, documented and not resolved:**

* **Gating.** The upstream `pyannote/segmentation-3.0` repository on Hugging Face is
  gated behind a click-through form asking for company and website ("Though this model
  uses MIT license…"). The harness did **not** accept that form. It uses k2-fsa's ONNX
  conversion from the sherpa-onnx GitHub release, which needs no account. The MIT
  licence permits that redistribution. The form is a download condition, not a licence
  term.
* **Training data.** Whisper: OpenAI has not published its training data (680k hours
  of web audio), so the data's licences are unknown. pyannote segmentation-3.0 was
  trained on the combined training sets of AISHELL, AliMeeting, **AMI**, AVA-AVD,
  DIHARD, Ego4D, MSDWild, REPERE and VoxConverse (model card). An AMI *test* meeting is
  held out only if pyannote used the standard AMI split, which cannot be verified here;
  this matters for V5. CAM++ was trained on VoxCeleb2 (5,994 speakers). The VoxCeleb2
  page (`https://www.robots.ox.ac.uk/~vgg/data/voxceleb/vox2.html`, read 2026-09-25)
  says "The provided VoxCeleb2 metadata is licensed under a Creative Commons
  Attribution-ShareAlike 4.0 International License", and that the URLs and timestamps,
  audio files, video files and identifying metadata "are no longer available from this
  website". `vox1.html` says the same for VoxCeleb1. Neither page states a licence for
  the audio itself. **Correction:** an earlier version of this section quoted the
  VoxCeleb page as offering the dataset "for research purposes under a Creative Commons
  Attribution 4.0 International License". That sentence is not on the current pages
  (the index, `vox1.html`, `vox2.html`, or the copy saved during the fetch). Its source
  cannot be traced, and it is withdrawn as unverified.
* **Whisper's upstream licence is declared twice** (checked 2026-09-25). OpenAI's
  GitHub repository (`https://github.com/openai/whisper`, `LICENSE`) is **MIT**,
  "Copyright (c) 2022 OpenAI". The Hugging Face card of `openai/whisper-small.en`
  (API `cardData.license`) says **apache-2.0**. The CTranslate2 conversion used here
  (Systran, revision `d1d751a5`) says `mit`. All three are permissive. The harness
  records the conversion's own card (MIT) and does not decide which upstream
  declaration governs.
* **Packages.** sherpa-onnx and sherpa-onnx-core are Apache-2.0, faster-whisper and
  CTranslate2 MIT, onnxruntime MIT. **piper-tts, installed in the same venv for the
  generator, is GPL-3.0-or-later**; nothing in `pipeline/` imports it.

### 11.3 Components and the API they were checked against

* **ASR**: `FasterWhisperTranscriber`, checked by `inspect.signature` against the
  installed faster-whisper 1.2.1. `WhisperModel(path, device="cpu", compute_type="int8",
  cpu_threads=4)`, then `transcribe(pcm, language="en", beam_size=5,
  word_timestamps=True, vad_filter=False)`, which returns a lazy segment generator and
  a `TranscriptionInfo`. Everything else is the library default (temperature fallback,
  `condition_on_previous_text=True`, …).
* **Review finding PIPE-05, faster-whisper part.** The call shape written from memory
  matched 1.2.1. The fixes are about real output rather than the call:
  * a word whose text normalises to several tokens is split (a `w` containing a space
    violates the evals contract);
  * word times are clipped to [0, duration] with `end ≥ start`;
  * a run made only of zero-length words gets a 10 ms segment. Real output has
    zero-length words (4 of 198 on the 60 s clip), and a lone one would have made an
    `end == start` segment, which evals rejects;
  * segments that normalise to nothing are dropped;
  * `TranscriptionInfo` goes to `hyp.extra["asr"]`;
  * `cpu_threads` is now a parameter;
  * the default model changed from `base` (multilingual) to the pinned `small.en`.
* **Diarization**: `SherpaOnnxDiarizer`, checked against the installed sherpa-onnx
  1.13.8. It builds an `OfflineSpeakerDiarizationConfig` from the pyannote segmentation
  model, the CAM++ embedding extractor and `FastClusteringConfig(num_clusters, threshold)`,
  with `min_duration_on` 0.3 s and `min_duration_off` 0.5 s (library defaults), then
  runs `process(float32).sort_by_start_time()`. `num_speakers` is passed as
  `num_clusters` (−1 without a hint). `set_config` is called only when the hint changes.
  Speaker ids become `spk0, spk1, …` by first appearance. sherpa's turns can overlap
  (the segmentation is overlap-aware) and are kept.
* **Turns are clipped to the audio** (review fix, 2026-09-25). sherpa pads its last
  10 s segmentation window, so on short input it returned a turn that ended after the
  file. The review measured 0.031-0.639 s on the first 0.5 s of this clip, and
  `sherpa-onnx-diarization` wrote that speech past end-of-file into hyp.rttm.
  `sherpa_turns` now clips every turn to [0, duration] and drops any turn left empty.
  With the final code the same 0.5 s gives one turn, 0.031-0.500 s (hyp.rttm
  `0.031 0.469`).
* **`num_speakers` is a request, not a guarantee.** sherpa lowers a count it cannot
  form, silently, and the result is not monotonic in the hint. On the 20 s test clip
  (ES2004a 780-800 s; final code, 2026-09-25 14:18, one run per hint, identical at
  14:04) hints 1, 2 and
  3 gave 1, 2 and 3 speakers, hints 4 and 6 gave 3, hint 12 gave 4, and no hint gave 3.
  `hyp.extra["diarization"]` records `n_speakers` and `hint_honoured`
  (`n_speakers == num_speakers`; `null` without a hint), and the diarizer issues a
  `RuntimeWarning` when they differ.
* **Assignment**: each word goes to the speaker with the maximal **total** overlap.
  Ties go to the speaker whose overlapping turn started earliest. That tie-break is
  unevidenced and moves cpWER on the 60 s clip (§11.5). Words outside every turn go to
  the smallest gap. The full rule is in §6, and its text is recorded in
  `hyp.extra["assignment"]["rule"]`. On the 60 s clip below, this rule and the
  pre-2026-09-25 per-turn rule agree on 196/198 words without a hint and on 198/198
  with `num_speakers=4` (re-checked with the final code). The 2 differences are
  zero-length words at 24.98 s inside a 20.973-27.976 s turn: the old rule sent them to
  a turn that ended 8 ms earlier, at 24.972 s.
* **Output**: hyp.json with per-word timings on every segment. hyp.rttm/hyp.stm are
  the **word-run** segments. The raw diarization turns are in
  `hyp.extra["diarization"]["turns"]`, for diarization-only scoring (or use
  `sherpa-onnx-diarization`). `hyp.extra` also records model provenance (paths, the
  sha256 **computed from the loaded files** next to the pins, licences; §11.1), component
  settings, `TranscriptionInfo`, `n_speakers` / `hint_honoured`, the assignment rule and
  counts, and wall-clock
  `timing` (`model_load_s`, `diarization_s`, `asr_s`, `assign_s`, `audio_s`, and
  `rtf` = (diarization + ASR + assignment) / audio duration, without model load or
  audio decode). Because of the timing, two runs' hyp.json differ; their segments do
  not.

`--param` keys: `model, device, compute_type, language, beam_size, vad_filter,
cpu_threads` (ASR); `segmentation_model, embedding_model, cluster_threshold,
min_duration_on, min_duration_off, diar_threads` (diarization); plus `num_speakers,
audio_check, duration_tolerance_s`. Defaults: `small.en`, `cpu`, `int8`, `en`, 5,
`false`, 4; the pinned ONNX files, 0.5, 0.3, 0.5, 4 threads. `allow_download` is not a
`whisper-sherpa` key. Only the legacy `faster-whisper` entries take it (§11.1).

### 11.4 Measured speed on this M1 (60 s clip)

Clip: AMI ES2004a Mix-Headset, 780.0-840.0 s (16 kHz, written to a PCM16 wav). This was
the densest 60 s window by reference word count (231 words, 4 talkers), searched in
15 s steps. Machine: Apple M1 (4 performance + 4 efficiency cores), 8 GB, macOS 26.5,
shared with an Android emulator, a Docker VM and other agents. **Every run below had
other load**, and the spread is large, so no single RTF is a clean figure.

| run | hint | model load s | diarization s | ASR s | assign s | RTF (compute / 60 s) | notes |
|---|---|---|---|---|---|---|---|
| raw libraries (smoke) | none | 0.29 + 0.75 | 6.35 | 11.30 | — | 0.294 | sherpa 2 threads; CT2 default threads |
| `python -m pipeline run` #1 | none | 1.022 | 3.740 | 13.729 | 0.001 | 0.291 | 19.14 s wall for the whole process, max RSS 965 MB |
| `python -m pipeline run` #2 | 4 | 0.938 | 4.005 | 13.789 | 0.003 | 0.297 | 19.47 s wall, max RSS 985 MB |
| in-process repeat 1 | none | 2.578 | 6.115 | 17.040 | 0.002 | 0.386 | emulator booting at ~340 % CPU, load avg ~11, swap 9.25/10 GB |
| in-process repeat 2 | none | (same) | 7.618 | 59.609 | 0.003 | 1.120 | same contention |
| in-process repeat 3 | none | (same) | 19.793 | 56.596 | 0.005 | 1.273 | same contention |
| in-process repeat 1 | 4 | 2.931 | 14.889 | 24.097 | 0.005 | 0.650 | same contention |
| in-process repeat 2 | 4 | (same) | 8.483 | 23.153 | 0.002 | 0.527 | same contention |
| in-process repeat 3 | 4 | (same) | 6.680 | 14.933 | 0.002 | 0.360 | same contention |
| final set, repeat 1 | none | 1.698 | 5.746 | 18.584 | 0.002 | 0.406 | no other inference process; load avg 4.4-7.0; emulator ~36 % CPU; swap 9.37/10 GB used |
| final set, repeat 2 | none | (same) | 6.379 | 15.695 | 0.002 | 0.368 | same |
| final set, repeat 3 | none | (same) | 5.921 | 15.474 | 0.002 | 0.357 | same |
| final set, repeat 1 | 4 | 0.745 | 7.130 | 15.726 | 0.002 | 0.381 | same |
| final set, repeat 2 | 4 | (same) | 7.503 | 16.655 | 0.002 | 0.403 | same |
| final set, repeat 3 | 4 | (same) | 7.322 | 15.995 | 0.002 | 0.389 | same |
| post-fix code A, 14:03, in-process | none | 1.246 | 5.514 | 15.474 | 0.002 | 0.350 | **another agent's full pytest run started at the same moment** (load avg 2.38 at start, 5.04 at end); load includes the first sha256 of the weights; max RSS 1,157 MB |
| post-fix code A, 14:03, in-process | 4 | 0.991 | 5.906 | 15.734 | 0.002 | 0.361 | same contention; weights' digests memoised |
| final code, 14:17, in-process | none | 1.176 | 4.050 | 13.967 | 0.003 | 0.300 | no pytest, generator or pipeline process running at the start; load avg 3.33 at start, 9.49 at end (other load arrived during the run); load includes the first sha256; max RSS 995 MB |
| final code, 14:17, in-process | 4 | 0.937 | 5.668 | 15.093 | 0.002 | 0.346 | same; digests memoised |

**Code state.** Every row above the last four was measured with the code of the first
report, before the review fixes of 2026-09-25. The last two rows used the final code,
whose hashes are listed under §11.5. "Code A" differs from it only in where
`pipeline/adapters.py` imports `time`. The fixes do not touch the timed compute
(sherpa `process`, the faster-whisper decode). They changed the assignment tie-break
(0.001-0.005 s here), the turn clipping, and model loading, which now hashes the
weights. Model load is outside the RTF.

Summary: across all 19 runs the RTF ranged from **0.291 to 1.273**. The six final-set runs
(no competing inference process) gave **0.357-0.406**, and the two single-shot CLI runs
gave 0.291 and 0.297. ASR takes 68.6-78.6 % of the compute (CLI and final-set runs). One process with both
models loaded peaks at about 1 GB RSS. The two post-fix processes, each of which built
both pipelines in turn and then a third diarizer, reached 1,157 MB and 995 MB max RSS.
Model load is 0.7-2.9 s, including the post-fix 1.246 s and 1.176 s with a cold sha256
of the weights. Hashing small.en
alone took 0.293 s, measured once. Word assignment is
negligible: 0.12-0.17 s for 10,000 words against 3,000 turns (the unit test). An hour of
meeting audio at RTF 0.357-0.406 would take roughly 21.4-24.4 minutes on this machine. That is
an extrapolation, not a measurement.

### 11.5 Accuracy sanity checks on AMI (NOT V5)

These are the only real-speech numbers here. They come from two excerpts of **one**
AMI test-split meeting (ES2004a; AMI is CC BY 4.0). They show that the stack works;
they are not a benchmark. Nothing was tuned on them: every parameter is a library
default or the §11.3 policy. Reference words come from the AMI manual annotations
(`words/ES2004a.?.words.xml`: `<w>` elements without `punc`, restricted to the window).

**60 s clip (780-840 s; 231 reference words, 4 talkers)**, scored with
`evals.score_meeting` at default settings (DER collar 0.25). The reference segments merge
one talker's consecutive words across gaps ≤ 0.5 s, a choice made for this check only.

**Code state of this table:** the final code, run 2026-09-25 14:17-14:18. That is git
HEAD `70249ba` plus the uncommitted working tree, with sha256 `pipeline/base.py`
`6813eeef1bf9c6a2ef4ab9cfdbf9e3b181447dad111fbeca22e48d5a6532b2bf`, `pipeline/adapters.py`
`cc07306d289a3a15f903ad11db5b093fd2bfa1e1dafa6c3302fa4b10fc139ea6`,
`pipeline/whisper_sherpa.py` `05b2d94254b361ea9e755c5e310ab14ca16a89396b613d77141cb4c4a81817a1`
and `pipeline/model_store.py`
`177040fa6a98c953798b5f4fb476718bc30ed1be3ad6a129cc7d7b9488427f2a`. The clip wav
(PCM16) has sha256 `3355cc92c9fecc7ba5336ca01580fc453c43abc5acafed43d665446c1f81cc1b`,
the same bytes as the first run's clip. An earlier run at 14:03 ("code A" in §11.4)
gave identical scores. Values are given to 4 decimals and seconds to 3.
The rule text in each hypothesis's `extra.assignment.rule` identifies the tie-break used.

| hypothesis | speakers found | DER (miss / FA / confusion, s of 58.70) | cpWER | WER, speaker-agnostic (concat) |
|---|---|---|---|---|
| `whisper-sherpa`, no hint (word-run RTTM) | 10 clusters (9 got words) | 0.5474 (17.610 / 0.265 / 14.260) | 0.7311 | 0.2773 |
| `whisper-sherpa`, `num_speakers=4` (word-run RTTM) | 4 (hint honoured) | 0.4998 (17.610 / 0.265 / 11.465) | 0.6807 | 0.2773 |
| raw sherpa turns, no hint | 10 | 0.4753 (5.346 / 1.338 / 21.219) | — | — |
| raw sherpa turns, `num_speakers=4` | 4 | 0.3876 (5.016 / 0.523 / 17.214) | — | — |

**Correction (review, 2026-09-25).** This table first gave word-run DER **0.545** and
**0.504** and cpWER **0.735** and **0.634**. Those figures came from hypotheses written at
12:53-12:54 by an intermediate `assign_speakers`, whose tie-break sent a tie to *the
speaker that appeared first in the meeting* (numpy `argmax` over per-speaker totals).
`pipeline/base.py` was then changed at 13:09 to the floor-holder tie-break without
re-running the table. Re-applying the intermediate rule to the stored words and turns
reproduces all 198 stored labels in both runs, so that rule is what produced them. The
re-run's ASR words and diarization turns are identical to the stored runs; only the
assignment differs. Raw-turn DERs, WER and the 20 s-clip numbers were unaffected: the
20 s-clip figures below were re-measured with the final code at 14:18 and are
identical.

**The tie-break was chosen without evidence, and it moves cpWER.** A tie here is a word
lying wholly inside two overlapping sherpa turns, so both overlaps equal the word's
length. The two rules on the *same* words and turns:

| tie-break | hint | words assigned differently | word-run DER | cpWER |
|---|---|---|---|---|
| earliest-starting overlapping turn (**shipped**) | none | — | 0.5474 | 0.7311 |
| speaker that appeared first (published first) | none | 5 of 198: "menus" 24.98 s, "i'm going to" 36.30-36.62 s, "right" 53.24 s | 0.5449 | 0.7353 |
| earliest-starting overlapping turn (**shipped**) | 4 | — | 0.4998 | 0.6807 |
| speaker that appeared first (published first) | 4 | 12 of 198, 23.96-53.24 s | 0.5039 | 0.6345 |

On this clip the first-appearance rule was better with the hint (cpWER 0.6345 against
0.6807) and slightly worse without it (0.7353 against 0.7311). One clip of one test-split
meeting, moving 5 and 12 words, is no basis for choosing either rule. The shipped rule
stays, labelled HARNESS_POLICY and unevidenced; choosing it properly needs a development
split (§11.7). Against the pre-2026-09-25 per-turn rule, the shipped rule agrees on
196/198 words without a hint and 198/198 with it (§11.3).

**20 s test clip (780-800 s; 85 reference words: talker D 57, A 27, C 1)**, the one
`tests/test_pipeline_models.py` uses:

* ASR: WER **0.128** against 86 reference tokens (after the test's normalisation: AMI
  acronyms `L_C_D_` joined, hyphens split, punctuation removed): 0 substitutions,
  8 deletions, 3 insertions. The deletions are AMI disfluencies and partial words
  ("b buttons", "mm-hmm") that Whisper leaves out.
* Diarization, as the share of each talker's reference words that land on that
  talker's majority label: with `num_speakers=2`, A 24/27 and D 54/57, on different
  labels. Without a hint, sherpa finds **3** labels, A splits 14/10/3 (0.52) and D is
  54/57.

### 11.6 Tests and skip gates

`tests/test_pipeline_models.py` has 33 tests. Thirty need no models and run everywhere,
CI included:

* the assignment rule (sum per speaker, tie to the floor-holder, gap-based nearest,
  zero-length words, token splitting, `MIN_SEGMENT_S`, 10,000 words × 3,000 turns);
* `whisper_words`, and `sherpa_turns` including clipping to the audio duration;
* both adapters against stand-in modules shaped after the installed releases, including
  the hint-not-honoured record and warning;
* `ModelComposedPipeline` end to end, re-parsed by `evals.io`, with the computed digests
  of explicit model files and the assignment rule text;
* the manifest (https sources, 64-hex pins, pinned HF revision) and the fetcher with an
  in-memory opener (download and verify, keep verified files, refuse a sha mismatch
  leaving nothing behind, extract only named archive members, ignore a `../evil`
  member);
* load-time hashing (added after the 2026-09-25 review). `model_store.require` records
  computed digests and refuses a same-size file with other bytes, including one reached
  through a symlink. The memo re-hashes a replaced file. The diarizer and transcriber
  constructors refuse tampered pinned weights before the library sees them.
* no implicit download. A library name is resolved with `local_files_only=True` and
  `use_auth_token=False` unless `allow_download=true`. The unfetched pinned `small.en` is
  unavailable, with or without `allow_download`. The legacy `faster-whisper` entry
  refuses its unfetched default and records provenance for a local model. A subprocess
  with an empty Hugging Face cache and `HF_HUB_OFFLINE=1` resolves `tiny` to
  "not in the local Hugging Face cache" and leaves the cache empty.
* `python -m pipeline models [--verify] [--json] [KEY ...]` (exit 1 when a requested
  asset is missing or mismatched, 2 for an unknown key) and the `fetch-models` usage
  error;
* the registry declarations.

Three `test_real_*` tests run the installed libraries on the 20 s clip:

* ASR WER < 0.35;
* the talkers separated with and without a hint. The same test checks that no turn ends
  after 0.5 s of audio, and that the hint-12 result's `hint_honoured` and warning agree
  with the speaker count sherpa returns;
* `whisper-sherpa` with and without a hint, writing a hypothesis that
  `evals.io.load_hypothesis` accepts, with the computed small.en and segmentation
  digests. When
**faster-whisper/sherpa-onnx are not importable (CI)**, these tests assert that the
registry reports "not importable" and pass without skipping. When the packages are
present but local inputs are missing, they skip with one of these reason prefixes
(both local-only inputs, never provided in CI):

| prefix | lifted by |
|---|---|
| `model weights not fetched: ` | `python -m pipeline fetch-models` (data/models/**) |
| `AMI data not present: ` | `data/corpora/ami/raw/ami_public_manual_1.6.2.zip` and `.../audio/ES2004a.Mix-Headset.wav` |

Checked here in three configurations after the 2026-09-25 review fixes:

* with everything present, 33 passed in 28.86 s;
* with faster_whisper, sherpa_onnx and ctranslate2 blocked from import (in subprocesses
  too), an empty models dir and `PLAUD_STRICT_SKIPS=1`, the four
  `test_pipeline_{models,adapters,contract,cli}.py` files gave 125 passed and 0 skipped;
* with the packages present and an empty models dir, 30 passed and 3 skipped, all with
  `model weights not fetched: `.

**These two prefixes are not yet registered in `tests/conftest.py` `ENV_GATES`** (that
file belongs to the Measure agent). Until they are, the third configuration fails
under `PLAUD_STRICT_SKIPS=1` with "3 skip(s) are not a documented CI gate". CI itself
is unaffected: without the packages, these tests never skip.

### 11.7 Limitations and what is not done

* **`cluster_threshold` is uncalibrated.** 0.5 is sherpa-onnx's default. With CAM++
  it over-counts unaided: 10 clusters for 4 talkers in 60 s, 3 for 2 talkers in 20 s.
  The hint fixes the count. Calibrate on a development split that is **not** an AMI
  test meeting: the four local AMI meetings are all test-split, so they are unusable
  for tuning.
* **hyp.rttm is word runs**, so silences between words and overlapped speech that
  Whisper did not transcribe count as missed speech (17.6 s of 58.7 s on the 60 s
  clip). Score diarization on `extra.diarization.turns` or `sherpa-onnx-diarization`
  to separate the two effects.
* **Timing was measured under shared load** (§11.4). A quiet-machine RTF was not
  obtained.
* Speed on full meetings: the 25 Sep V5 subset run measured real-time factors of
  0.2151–0.3405 on four full AMI meetings, under heavy load ([`docs/v5-results.md`](v5-results.md)).
  V5 itself is not met (§7).
* `vad_filter` is off. faster-whisper's Silero VAD may cut hallucinations on long
  silences but has not been evaluated here.
* Only `small.en` is pinned. Other sizes are available only through the legacy
  `faster-whisper` entries, from a local directory, the Hugging Face cache, or with
  `allow_download=true` (an unpinned download). Such a run records the hashes of the
  files it loaded, but no pin backs them.
* The assignment tie-break (earliest-starting overlapping turn) is unevidenced. On the
  60 s clip it changes cpWER in both directions (§11.5). Choosing a tie-break, like
  calibrating `cluster_threshold`, needs a development split that is not an AMI test
  meeting.
* Training-data caveats (§11.2) are documented, not resolved.
