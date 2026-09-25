# pipeline/ — the ASR/diarization stack under test

Track owner: `pipeline/**`, `tests/test_pipeline_*.py`, `requirements/pipeline.txt`, this file.
Status: **implemented and tested offline; V5 (held-out AMI) cannot be run here** — see §7.

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
| `load_audio(path, target_sr=16000)` | → `LoadedAudio(pcm, sample_rate, container, source_sample_rate, source_channels)` | wav/flac via soundfile, everything else via PyAV |
| `register(name, ...)` / `get_pipeline(name, config)` / `describe_registry()` | registry | unavailable entries carry a reason |

Supporting modules: `formats.py` (RTTM/STM/hyp.json writers and parsers),
`meeting.py` (meeting.json reader/validator, `meeting_from_stm`, `write_meeting_dir`),
`synthetic.py` (hand-built test signals and meeting dirs — **test support, not the generator**),
`cli.py` (`python -m pipeline`).

### Audio loading

`load_audio` routes by suffix: `.wav/.flac/.aiff` → `soundfile`, anything else →
PyAV. Every path ends in the same numpy mono average and the same
`scipy.signal.resample_poly` resampler, so a device-shaped Ogg/Opus file
(libopus decodes at 48 kHz) and a 16 kHz wav reach the components identically.
The Ogg/Opus path is verified against the R6-S2 synthetic fixtures
(`tests/fixtures/r6s2_manifest.json`, sha-pinned, `not_plaud_capture: true`):
correlation 0.9999 at zero lag with the generator's own tone.

## 2. What is a real system and what is a harness oracle

| Registry name | Kind | Runs here? | Output |
|---|---|---|---|
| `oracle` | **harness self-test** — returns `meeting.json` ground truth | yes | full |
| `perturbed-oracle` | **harness self-test** — ground truth with seeded, configurable damage | yes | full |
| `energy-vad-cluster` | **real, model-free system** — energy VAD + MFCC + agglomerative clustering | yes | diarization only (`text == ""`) |
| `faster-whisper` | adapter: faster-whisper ASR + `energy-vad-cluster` diarization | **no** (package absent) | full |
| `faster-whisper+pyannote` | adapter: faster-whisper ASR + pyannote.audio diarization | **no** | full |
| `pyannote-audio` | adapter: pyannote.audio diarization only | **no** | diarization only |
| `whisperx` | adapter: whisperx ASR + alignment + diarization | **no** | full |

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

### `energy-vad-cluster` in detail (`pipeline/energy_vad.py`)

1. **VAD** — 25 ms frames / 10 ms hop; frame energy in dB; noise floor = 10th
   percentile; speech = floor + 12 dB (never below −60 dBFS); 150 ms hangover;
   silences < 150 ms bridged; speech runs < 200 ms dropped.
2. **MFCC** — pre-emphasis 0.97, Hamming, |rFFT|², 26 HTK mel filters
   (50 Hz–Nyquist), log, DCT-II (ortho), 13 coefficients with c0 dropped.
   Cepstral mean *and variance* normalisation over speech frames only, so the
   clustering threshold is dimensionless (units of per-dimension frame std).
3. **Chunks** — each speech region is cut into 1.0 s chunks (a tail < 0.4 s
   joins its predecessor); one mean vector per chunk.
4. **Clustering** — `scipy.cluster.hierarchy.linkage(method="average")`,
   euclidean; cut at `num_speakers` (`--param num_speakers=K`) or at
   `distance_threshold=2.5`.
5. **Smoothing** — adjacent same-label chunks merge (gap ≤ 0.3 s); islands
   < 0.3 s take a neighbour's label; residue < 0.3 s is dropped; labels are
   renamed `spk0, spk1, …` by first appearance.

On the hand-built two-voice signal in the tests (pulse trains at 100 Hz /
230 Hz through 200–1200 Hz / 1200–3800 Hz band-passes, silence gaps) the
within-speaker chunk distance is ≤ 1.22 and the between-speaker distance is
≥ 4.68; the threshold sits at the geometric middle. **That number was tuned on
those synthetic sources, not on speech.** A test makes the failure mode
explicit (`distance_threshold=50` merges everyone). Before any real-speech
claim the threshold must be re-tuned on a dev split.

## 3. How to add a system

1. Implement `Transcriber` and/or `Diarizer` (or a whole `Pipeline`) in a new
   module under `pipeline/`. Import heavy dependencies lazily inside
   `__init__`/methods and raise `PipelineUnavailable` if they are missing.
2. Register a factory:

   ```python
   from pipeline.base import ComposedPipeline, PipelineConfig, register

   @register("my-system", description="...", availability=lambda: _missing("my_pkg"))
   def _make(config: PipelineConfig | None = None):
       cfg = config or PipelineConfig(name="my-system")
       return ComposedPipeline(MyTranscriber(**cfg.params), MyDiarizer(), cfg, name="my-system")
   ```

   `availability` returns `None` when the system can run and a one-line reason
   otherwise; `python -m pipeline list` shows it.
3. Add the module to `_ensure_builtin_registrations()` in `base.py` (or import
   it from `pipeline/adapters.py`).
4. Emit text as lowercase tokens (`normalize_text` is the minimum); the evals
   layer owns real normalisation.
5. Add `tests/test_pipeline_<name>.py`. If the system needs models, the test
   must `pytest.skip` when they are absent and the docs must say UNTESTED here.

## 4. CLI

```
python -m pipeline list [--json]
python -m pipeline run   --pipeline NAME --audio FILE [--meeting-dir DIR] --out hyp.json
                         [--param k=v ...] [--seed N] [--sample-rate HZ]
python -m pipeline batch --pipeline NAME --root DIR --out DIR
                         [--audio-key mix|device|device.<name>|stem:<spk>|<relpath>]
                         [--param k=v ...] [--seed N] [--fail-fast]
python -m pipeline import-stm --stm ref.stm [--rttm ref.rttm] --audio mix.wav --out DIR [--meeting-id ID]
```

`run` writes `hyp.json`, `hyp.rttm`, `hyp.stm` side by side. `batch` writes
`<out>/<meeting_id>/hyp.*` and `<out>/batch.json` (schema
`plaud-harness/batch/1`: per-meeting status, audio used, elapsed time, error
text). One broken meeting is reported and does not sink the batch; the exit
code is 1 if anything failed, 2 for usage errors, 0 otherwise.

`--param` values are JSON-coerced (`num_speakers=2` → int, `word_del_rate=0.3`
→ float, `vad_filter=true` → bool). `perturbed-oracle` reads
`speaker_swap_rate, time_shift_s, boundary_jitter_s, word_sub_rate,
word_del_rate, word_ins_rate`; `energy-vad-cluster` reads every field of
`VadParams`, `MfccParams`, `ClusterParams` plus `num_speakers`.

## 5. Evidence classes

Vocabulary from `docs/protocol-ledger.md` ("Confidence vocabulary" and the R7
mapping). This package encodes **one** device fact:

| Fact | Class | Source |
|---|---|---|
| Recorder audio is 16 000 Hz | **BYTECODE_PROVEN** | `OggUtils.b = 16000`, `build/evidence/javap/ALL.txt:11496`; `sipush 16000` at `ALL.txt:11548`; mirrored in `emulator/plaudsim/audio.py:22` |
| 20 ms / 320-sample frames | **BYTECODE_PROVEN** | `OggUtils.c = 320`, `ALL.txt:11498`; `sipush 320` at `ALL.txt:11550` |
| Opus at 32 kbps CBR, 80 B/frame/channel | **SOURCE-DERIVED** (SDK_PROVEN) | `docs/protocol-ledger.md` §8 "Codec parameters" — used here only to shape the *synthetic* `device/recording.ogg` that `pipeline.synthetic` writes |

Everything else in the package is **HARNESS_POLICY** (§6).

**UNKNOWN, and not modelled:**

* The audio shape a **real** recorder emits over BLE. No authentic Plaud
  recording exists in this environment (R6-S3, R6-S4). The loader has been
  exercised on standard Ogg/Opus only. Ledger §8 documents quirks in files the
  SDK itself writes (preSkip 0, granule inflation, no EOS, non-conformant
  OpusTags, uninitialised bytes); whether PyAV/libopus or libsndfile tolerate
  such a file is **UNKNOWN** — the synthetic writer produces conformant Ogg
  and does not reproduce those quirks.
* Plaud's own transcription/diarization behaviour. The cloud endpoints are
  CLOUD_OBSERVED only (R7-S12c) and never exercised. This layer evaluates
  *our* stack on device-shaped audio; it says nothing about Plaud's ASR.

## 6. HARNESS_POLICY items

Contract and loading
* Schema strings `plaud-harness/meeting/1`, `plaud-harness/hypothesis/1`, `plaud-harness/batch/1`.
* Default target sample rate = the recorder's 16 kHz (the fact is BYTECODE_PROVEN; choosing it as the *loader default* is policy).
* Multichannel input is averaged to mono; resampling is gcd-reduced polyphase FIR (`resample_poly`); output clipped to [−1, 1].
* Routing by suffix (`.wav/.flac/.aiff` → soundfile, else PyAV); a wav that soundfile rejects is not retried with PyAV.
* `meeting.json` discovery: `--meeting-dir`, else the audio's directory, then one and two levels up. `meeting_id` falls back to the audio stem.
* Times are written with 3 decimals in RTTM/STM; RTTM channel is always `1`; hyp.json keeps full floats.
* `FALLBACK_SPEAKER = "spk0"` when there is no diarizer; `assign_speakers` gives each word the max-overlap turn, else the nearest turn; consecutive same-speaker words form one segment.
* `normalize_text`: lowercase, whitespace split, strip surrounding ASCII punctuation.

Oracles
* Perturbation order is fixed: speaker swaps → word delete/substitute/insert → global time shift → boundary jitter → re-sort. RNG is `random.Random(seed)`.
* Substitution/insertion tokens come from the meeting vocabulary ∪ `FALLBACK_VOCAB`; a substitute always differs from the original token.
* Inserted words get 50 ms; a jittered/shifted segment is never shorter than 10 ms and never starts before 0; words are clipped into their segment.

`energy-vad-cluster`
* All VAD/MFCC/cluster defaults in §2 (frame 25/10 ms, +12 dB over the 10th-percentile floor, −60 dBFS absolute floor, 150 ms hangover, 200 ms min speech, 150 ms min silence; 26 mels, 13 MFCC, c0 dropped, CMVN over speech frames; 1.0 s chunks, average linkage, `distance_threshold = 2.5`, 0.3 s island/merge rules; labels by first appearance). The threshold was tuned on synthetic sources.

CLI and importer
* Exit codes 0/1/2; batch continues past a failing meeting unless `--fail-fast`.
* `import-stm` spreads a segment's tokens uniformly over its span when the source has no word times, and records `"word_times": "interpolated (HARNESS_POLICY)"` in `generator.scenario`.
* `pipeline.synthetic` voices (pulse trains + band-passes) and its Ogg/Opus writer (PyAV/libopus, 16 kHz mono, 32 kbps) are test support.

## 7. V5 procedure — "full pipeline hits target cpWER / DER on held-out AMI"

**This cannot be run in this environment.** `data/corpora/` is empty, no ASR or
diarization model is present, and none may be downloaded here. What follows is
the procedure the code supports, written so an operator with the data can run
it without touching the package.

### Expected layout

```
data/corpora/ami/                       (gitignored: data/corpora/)
  ES2002a/
    meeting.json        plaud-harness/meeting/1 — written by `import-stm`
    ref.stm             one line per reference segment (meeteval STM)
    ref.rttm            reference speaker turns (copied verbatim if --rttm given)
    mix.wav             16 kHz mono (e.g. the Mix-Headset channel; record the choice)
    device/recording.ogg   OPTIONAL: the same audio in the device shape
                            (pipeline.synthetic.write_device_ogg_opus, or the
                            emulator's export path) to score the BLE-shaped input
  ES2002b/ ...
```

`batch` takes every direct subdirectory that has a `meeting.json`. Use the AMI
**test** split of the standard full-corpus partition for the held-out claim and
record which split file you used in `generator.scenario`.

### Commands

```bash
# 0. Obtain AMI (CC BY 4.0, docs/SOURCES.md) audio and STM/RTTM references.
#    Converting the NXT XML annotations to STM/RTTM is NOT done by this track
#    (see §8); use a documented converter and keep it under scripts/.

# 1. Import each meeting (word times are interpolated: HARNESS_POLICY)
for id in $(cat splits/test.txt); do
  .venv/bin/python -m pipeline import-stm \
      --stm refs/$id.stm --rttm refs/$id.rttm \
      --audio audio/$id.Mix-Headset.wav \
      --out data/corpora/ami/$id
done

# 2. Sanity: the oracle must score 0 on every meeting (evals layer)
.venv/bin/python -m pipeline batch --pipeline oracle --root data/corpora/ami --out build/hyp/oracle

# 3. Systems under test (only the first runs here)
.venv/bin/python -m pipeline batch --pipeline energy-vad-cluster --root data/corpora/ami --out build/hyp/evc
.venv/bin/python -m pipeline batch --pipeline whisperx        --root data/corpora/ami --out build/hyp/whisperx --param model=large-v3
.venv/bin/python -m pipeline batch --pipeline faster-whisper+pyannote --root data/corpora/ami --out build/hyp/fw-pa

# 4. Score (evals layer): cpWER via meeteval on hyp.stm vs ref.stm,
#    DER/JER via pyannote.metrics on hyp.rttm vs ref.rttm, then the gates.
```

The pass/fail thresholds ("target cpWER / DER") belong to the evals layer's
gate configuration, not to this package; this package only guarantees that
every hypothesis parses with `meeteval.io.STM/RTTM` and
`pyannote.database.util.load_rttm` (tested).

## 8. What is NOT done, and why

* **V5 itself.** No AMI data, no models, no download allowed. Nothing about
  real-speech accuracy is claimed.
* **The model adapters are UNTESTED.** `faster-whisper`, `pyannote-audio`,
  `faster-whisper+pyannote` and `whisperx` are written against each package's
  documented public API and construct lazily; the registry reports them
  unavailable with the import error. They have never executed against a model
  here. API drift (e.g. pyannote's `use_auth_token` → `token`, whisperx moving
  `DiarizationPipeline` into `whisperx.diarize`) is guarded but unverified.
* **AMI NXT → STM/RTTM conversion** is out of scope; `import-stm` starts from
  STM/RTTM.
* **`energy-vad-cluster` has not seen speech.** Its thresholds are tuned on
  synthetic pulse-train voices; it has no overlap handling and no ASR.
* **No scoring or gates** live here (evals track). The tests call
  pyannote.metrics and meeteval directly only to prove the oracle/perturbation
  mechanics move the public metrics as claimed.
* **SDK-writer Ogg quirks** (ledger §8) are not synthesised and the loader's
  tolerance to them is unknown.
* **Stems and multichannel** in `meeting.json` are read but unused; every
  pipeline works on one mono mix.

## 9. Tests

`tests/test_pipeline_oracle.py`, `test_pipeline_energy_vad.py`,
`test_pipeline_audio.py`, `test_pipeline_contract.py`, `test_pipeline_cli.py`
— 104 tests. Highlights: oracle == ground truth (deep equality); zero rates ==
identity; seed determinism; analytic counts at rate 1.0 (all deleted, all
substituted differ, all inserted doubles, all swapped flips a 2-speaker
meeting, single-speaker swaps are impossible and counted); binomial-bounded
monotone counts; DER/cpWER move as claimed including the permutation-invariance
trap; VAD edges within one frame, hangover on the end only, click/gap rules;
mel filterbank unit peaks; two synthetic voices → exactly two clusters with
boundaries inside tolerance under a known count, a threshold and a −35 dB
floor; one voice stays one cluster; silence yields nothing; a loose threshold
collapses speakers; Ogg/Opus mono/stereo fixtures (sha-pinned) decode to the
generator tone; 48 kHz wav resamples; garbage inputs raise `AudioFormatError`;
CLI `run` files parse back with this package, meeteval and pyannote and score
DER 0 for the oracle; batch isolates a broken meeting; `import-stm` round-trips
through the oracle.

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_pipeline_*.py -q -p no:cacheprovider`
