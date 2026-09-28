# V5-style results: the first real numbers (2026-09-25)

**Read this first.** This is one measured run of the only real composed pipeline
here, `whisper-sherpa` (Whisper small.en through faster-whisper, plus sherpa-onnx
diarization). It is **not** the V5 verdict of PROJECT.md. The AMI part covers
**4 of the 16 meetings** in the standard AMI test split, at full length. That is
not "the AMI test set", and no result here is compared with a published number.
The synthetic part is clean, word-salad Piper speech with one TTS voice per
speaker. Every harness choice below is HARNESS_POLICY, and every number is quoted
as measured, including the bad ones. Inference, scoring, gates and
the summary tables are reproduced by `scripts/run-v5.sh` (section 10); its outputs
are under the git-ignored `build/v5/`. The diagnostics in sections 8.1, 8.3 and 8.4,
including the EN2002a table in section 8.3, used scratch scripts that are not in the
repository.

**Corrections, 25 Sep 2026.** A verification pass the same day rescored the saved
hypotheses and checked every table cell against the saved outputs. It found no
mismatch, and no table value, gate threshold or conclusion changed. It did find 11
wording and precision errors: 10 in the prose and 1 in a licence cell (CAM++,
section 3). They are corrected in place. Section 12 lists each one with its old
wording. One supplementary number was added: a cpWER for the no-hint EN2002a run,
computed without meeteval (section 8.1).

Headline (macro mean over the 4 meetings of each set; evals defaults, section 5):

| data | configuration | DER | JER | cpWER | tcpWER | WER, speaker-agnostic |
|---|---|---|---|---|---|---|
| AMI, 4 test meetings | whisper-sherpa, hint (`num_speakers=4`) | 0.6557 | 0.8150 | 0.8533 | 0.9709 | 0.3250 |
| AMI, 4 test meetings | whisper-sherpa, no hint: `python -m evals batch` | not scorable: 4 of 4 meetings refused by meeteval (section 8.1) | – | – | – | – |
| AMI, 3 of the 4 meetings | whisper-sherpa, no hint, guard lifted (supplementary) | 0.7760 | 0.8020 | 1.1096 | 1.2290 | 0.2876 |
| AMI, 4 test meetings | whisper-sherpa, no hint, word runs without text | 0.7871 | 0.8123 | n/a | n/a | n/a |
| AMI, 4 test meetings | sherpa raw turns, hint | 0.5324 | 0.7424 | n/a | n/a | n/a |
| AMI, 4 test meetings | energy-vad-cluster (no ASR), no hint | 0.6445 | 0.7566 | n/a | n/a | n/a |
| Piper, `mix.wav` | whisper-sherpa, hint | 0.6149 | 0.7699 | 1.0993 | 1.1233 | 0.2501 |
| Piper, `mix.wav` | whisper-sherpa, no hint | 0.6760 | 0.7191 | 0.9962 | 1.0732 | 0.2501 |
| Piper, `mix.wav` | energy-vad-cluster (no ASR), hint | 0.1920 | 0.2562 | n/a | n/a | n/a |
| Piper, device Ogg/Opus | whisper-sherpa, hint | 0.6052 | 0.7434 | 1.0438 | 1.0599 | 0.2764 |
| Piper, device Ogg/Opus | whisper-sherpa, no hint | 0.6819 | 0.7335 | 1.0242 | 1.0722 | 0.2764 |
| AMI and Piper | oracle (self-test) | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

What stands out:

* **The pipeline as registered (no hint) over-clusters badly.** sherpa-onnx's default
  `cluster_threshold` 0.5 found 95, 35, 36 and 47 clusters on the four 4-speaker AMI
  meetings, and 7 to 11 on the 2- to 4-speaker Piper meetings.
* **The evals layer cannot score the no-hint AMI runs at all.** meeteval 0.4.3
  refuses cpWER when a side has more than 20 speakers, and `python -m evals batch`
  then drops the whole meeting (section 8.1). The no-hint AMI numbers above come
  from a labelled supplementary path (the guard lifted in-process), for 3 of the 4
  meetings. EN2002a (83 speakers with words) fails even then. A cpWER computed
  without meeteval, 1.1229, is given for it in section 8.1.
* **With the reference speaker count as the hint** (`num_speakers`, honoured on all
  4 AMI meetings), word-run DER is still 0.5444-0.7289 per AMI meeting. Missed
  speech (0.3124 of reference time, macro) and speaker confusion (0.3096) dominate
  it. Scoring the raw sherpa turns instead gives 0.4519-0.5723.
* **On the Piper set the diarizer is close to chance** even with the hint: confusion
  is 0.50 of reference time. The model-free `energy-vad-cluster`, tuned on synthetic
  voices, does far better there (DER 0.1920 with the hint). The ASR half does better:
  WER 0.1220-0.2304 on the three no-noise Piper meetings (simulated room, RT60
  0.3384 s measured) and 0.4603 on the reverberant (RT60 0.7912 s), noisy one.
* **ASR is not bit-reproducible on long audio.** Two runs with the same ASR settings
  (hint and no-hint) on EN2002a produced 5504 and 5118 words (WER 0.4072 against
  0.4371). A third diagnostic run produced 5223 words, and it diverged exactly where faster-whisper's
  temperature fallback started sampling (section 8.3). On every other meeting the ASR
  words matched between the two runs.

## 1. What was run

| label | registry name | `--param` | what it is |
|---|---|---|---|
| oracle | `oracle` | – | **harness self-test**: meeting.json's ground truth. It must score exactly 0, and it did on all 8 meetings |
| energy-vad-cluster | `energy-vad-cluster` | none | model-free diarizer (docs/pipeline.md §2.1): energy VAD + MFCC/f0 + spectral clustering. No ASR, so its WER columns are n/a |
| energy-vad-cluster, hint | `energy-vad-cluster` | `num_speakers=N` | the same with the speaker-count hint |
| whisper-sherpa | `whisper-sherpa` | none | **the system under test** (docs/pipeline.md §11), with every default |
| whisper-sherpa, hint | `whisper-sherpa` | `num_speakers=N` | the same with the speaker-count hint |
| word runs, no text | derived | – | the whisper-sherpa hypothesis's own segments with the text removed. DER/JER are identical to the full hypothesis's (checked, section 8.1). They exist so that DER/JER are available where meeteval refuses cpWER |
| sherpa raw turns | derived | – | `hyp.extra.diarization.turns`: sherpa's turns before word assignment. Diarization only |

**Hint (HARNESS_POLICY):** N = the number of reference speakers with at least one
segment. That is 4 on every AMI meeting, and 2, 2, 3 and 4 on the Piper meetings. It
is an **oracle count**, which a deployed recorder does not have. No-hint is the
realistic configuration; the hint isolates the clustering-count error.

Every run was one `python -m pipeline run` process per (system, meeting), run in
sequence. Only one inference process ran at a time: the script waits while another
model run, generation or pytest session is active. The pipeline seed is 0 (the CLI
default); none of these systems takes its randomness from it (section 8.3). Inference
ran from 2026-09-25 09:02 to 10:08 UTC.

## 2. Data

### 2.1 AMI: 4 test-split meetings, full length, headset mix

EN2002a, ES2004a, IS1009a and TS3003a. All four are in the Full-corpus-ASR **test**
split (the BUT setup's `lists/test.meetings.txt`, 16 meetings). They are the only
test meetings whose audio is on this machine. **No cropping:** every meeting was run
and scored over its whole length, 5536.54 s (92.2 min) in all. The brief allowed a
first-N-minutes crop; it was not needed.

* Audio: `<ID>.Mix-Headset.wav`, the AMI headset mix (16 kHz, mono, PCM16). It is a
  sum of close-talking headset microphones, not a far-field recording. Hashes were
  computed when the summary was written, and they match the pins in
  `scripts/run-v5.sh` and `data/corpora/ami/raw/SHA256SUMS.local`.
* Reference: `python -m evals.ami convert` (converter version 2, `--truncated keep`;
  docs/evals.md "AMI"). Speech time is the per-speaker union of the forced-aligned
  word times, and it equals the BUT `only_words` RTTMs line for line on these four
  meetings. Words come from the NXT manual transcripts. The per-meeting statistics
  (overlap fraction 0.2741991, 0.1578987, 0.1357204 and 0.0457581) are in
  docs/evals.md "Measured on the four local meetings".
* Licence: AMI annotations and audio are CC BY 4.0. Attribution: AMI Consortium;
  J. Carletta et al., "The AMI meeting corpus: a pre-announcement", MLMI 2006.

| meeting | duration s | active speakers | reference words | audio file | bytes | sha256 |
|---|---|---|---|---|---|---|
| EN2002a | 2142.7094 | 4 | 7632 | `data/corpora/ami/meetings/EN2002a/mix.wav` | 68566744 | `a15017d0d3c871866971ee178052a0ce3a7e44d6fbf89fc23dcab326511b65f5` |
| ES2004a | 1049.3547 | 4 | 2653 | `data/corpora/ami/meetings/ES2004a/mix.wav` | 33579394 | `3e2560b19bee6952c7c7ce041b0f1ea8a7ea9468044c4eea79d2a2c67e24ab0f` |
| IS1009a | 838.8333 | 4 | 2018 | `data/corpora/ami/meetings/IS1009a/mix.wav` | 26844093 | `6eb5a0ede0d9e72794f976ce7bea5b78133eae969f99b4c5418b43c2468d25b1` |
| TS3003a | 1505.6426 | 4 | 2534 | `data/corpora/ami/meetings/TS3003a/mix.wav` | 48180608 | `b6ce59ff735c234afdd94b13744aa26f6785af746170543fd03b33160e920a6d` |

### 2.2 Synthetic Piper set

`build/synthetic/piper/`, four meetings generated by the exact commands of
docs/generator.md §6.3 (generator seeds 101-104; voice `en_US-libritts_r-medium`).
The set is 2-speaker (s0101), 2-speaker with 15 % overlap (s0102), 3-speaker in a
reverberant room with RT60 0.79 s measured and white noise at 15 dB (s0103), and
4-speaker with random turn-taking (s0104). s0101, s0102 and s0104 are simulated
rooms with RT60 0.3384 s measured and no added noise. The text is seeded word salad
from the harness vocabulary. `mix.wav` is the simulated Note Pro 4-mic device mix (mono).
`piper-device` is the same meetings' `device/recording.ogg`: the device-shaped
Ogg/Opus, 32 kbps CBR, 20 ms frames (docs/generator.md §2). Licence: the Piper voice
is trained on LibriTTS-R (CC BY 4.0) and fine-tuned from the lessac voice, whose
Blizzard 2013 data licence is research-only. That question is unresolved
(docs/generator.md §6.2). piper-tts is GPL-3.0-or-later.

| meeting | duration s | active speakers | reference words | audio file | bytes | sha256 |
|---|---|---|---|---|---|---|
| synth-piper-2spk-overlap-s0102 | 120.0000 | 2 | 573 | `build/synthetic/piper/synth-piper-2spk-overlap-s0102/mix.wav` | 3840044 | `4412f2d036963471ade57905529948e22fd3e87168e7439d79a22283a975bbc5` |
| synth-piper-2spk-s0101 | 120.0000 | 2 | 410 | `build/synthetic/piper/synth-piper-2spk-s0101/mix.wav` | 3840044 | `cc018c2b52520b5e74d3e06a63a94f4c254c4c72fb0e1e98eadc551a9f00b4f0` |
| synth-piper-3spk-reverb-noise-s0103 | 135.0000 | 3 | 441 | `build/synthetic/piper/synth-piper-3spk-reverb-noise-s0103/mix.wav` | 4320044 | `2425f750cf7a37ba21285d5ef19817188f670186c85718966fc86e7fc7c4b720` |
| synth-piper-4spk-s0104 | 150.0000 | 4 | 451 | `build/synthetic/piper/synth-piper-4spk-s0104/mix.wav` | 4800044 | `84ec131b0fa0bd79db13e3da50d2f4fefa6a3dd36d63fda37b4d66a9a6867558` |

| meeting | duration s | active speakers | reference words | audio file | bytes | sha256 |
|---|---|---|---|---|---|---|
| synth-piper-2spk-overlap-s0102 | 120.0000 | 2 | 573 | `build/synthetic/piper/synth-piper-2spk-overlap-s0102/device/recording.ogg` | 489477 | `2d8a6adb3e528ad693433e3f700f7b1c653174e8012543e007b674dc2d077c99` |
| synth-piper-2spk-s0101 | 120.0000 | 2 | 410 | `build/synthetic/piper/synth-piper-2spk-s0101/device/recording.ogg` | 489477 | `3f7cd4f6f57abd4e376f00e70527a1e224fe3bfd0dffaff96e1268652866883c` |
| synth-piper-3spk-reverb-noise-s0103 | 135.0000 | 3 | 441 | `build/synthetic/piper/synth-piper-3spk-reverb-noise-s0103/device/recording.ogg` | 550632 | `9c2e48280cd3d0db50eee2b632161fc24792e99b4129c589bafaff3f1ec0edf7` |
| synth-piper-4spk-s0104 | 150.0000 | 4 | 451 | `build/synthetic/piper/synth-piper-4spk-s0104/device/recording.ogg` | 611787 | `7e9abc2ddbc548cd53d77297f72f144596ff94812276c0d2452cd05eeea188da` |

## 3. Models

The three pinned, ungated files of docs/pipeline.md §11.2 were used: no account, no
token and no download during this run (`python -m pipeline models --verify`: all
verified). Every hypothesis records the sha256 **computed at load** in
`hyp.extra.models`, and all of them equal the pins (`sha256_matches_pin: true`).

| role | asset | bytes | source | licence |
|---|---|---|---|---|
| ASR | faster-whisper `small.en` (CTranslate2, loaded as int8), revision `d1d751a5f8271d482d14ca55d9e2deeebbae577f`; `model.bin` sha256 `62b2a45b05ee59acb4a5341b33ee35e041395d378d418a18acfe4c9e768ee37a` | 486,098,798 (4 files) | `https://huggingface.co/Systran/faster-whisper-small.en` | MIT (conversion's card). Upstream Whisper is declared MIT (OpenAI GitHub) and Apache-2.0 (HF card); training data unpublished |
| segmentation | pyannote segmentation-3.0, ONNX by k2-fsa; `model.onnx` sha256 `220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079` | 5,993,974 (with LICENSE) | `https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2` | MIT (CNRS). **Trained on data that includes AMI** (model card); whether pyannote held out the standard AMI test split cannot be verified here |
| speaker embedding | 3D-Speaker CAM++ (English, VoxCeleb2); sha256 `357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b` | 29,596,978 | `https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` | Apache-2.0 (ModelScope card). Training data VoxCeleb2: its site licenses only the metadata (CC BY-SA 4.0) and states no licence for the audio (`pipeline/model_store.py` caveat) |

Packages: faster-whisper 1.2.1, ctranslate2 4.8.2, sherpa-onnx 1.13.8, onnxruntime
1.30.0, numpy 2.4.6. Scoring: pyannote.metrics 4.1, pyannote.core 6.0.1, meeteval
0.4.3, jiwer 4.0.0. Python 3.11.16. Settings are the §11.3 defaults: ASR beam 5, word
timestamps, no VAD filter, 4 CPU threads, `condition_on_previous_text` on (library
default). Diarization: `cluster_threshold` 0.5, `min_duration_on` 0.3 s,
`min_duration_off` 0.5 s, 4 threads.

Code state: git HEAD `70249ba` plus the uncommitted working tree.
`build/v5/summary.json` `environment.code_sha256` records the sha256 of every
`pipeline/*.py`, `evals/*.py`, `evals/gates.yaml` and `scripts/run-v5.sh`. No
`pipeline/*.py` or `evals/*.py` file was modified after 08:46:17 UTC, before the
first run. `pipeline/base.py` `6813eeef…`, `adapters.py` `cc07306d…`,
`whisper_sherpa.py` `05b2d942…` and `model_store.py` `177040fa…` are the code state
recorded in docs/pipeline.md §11.5.

## 4. Machine, speed and memory

Apple M1 (4 performance + 4 efficiency cores), 8 GB RAM, macOS 26.5.2, CPU only.
The machine was shared with an Android emulator, a Docker VM, IDEs and other
agents. Swap in use at each run's start was 6781-8414 MiB (swap size 8192-9216 MiB).
The logs do not record swap during or after a run. (Correction, 25 Sep 2026: an
earlier version said "6.8-8.7 GB (of an 8-10 GB swap file) throughout", which nothing
on disk supports.) The 1-minute load average at run boundaries ranged from 1.94 to
19.26 (per run below).
**These are timings under contention, not a quiet-machine benchmark.** RTF =
(diarization + ASR + assignment) / audio duration. It excludes model load (0.953-1.343 s
over the 24 whisper-sherpa runs, including the weights' sha256) and audio decode
(docs/pipeline.md §11.3).

| system | meeting | audio s | diarization s | ASR s | RTF | wall s (process) | max RSS MB | load avg 1 min start → end | started (UTC) |
|---|---|---|---|---|---|---|---|---|---|
| whisper-sherpa | EN2002a | 2142.709 | 206.5 | 448.1 | 0.306 | 656.6 | 1523 | 2.77 → 6.38 | 2026-09-25T09:12:01Z |
| whisper-sherpa | ES2004a | 1049.355 | 103.5 | 164.1 | 0.255 | 269.9 | 1315 | 6.38 → 8.33 | 2026-09-25T09:22:58Z |
| whisper-sherpa | IS1009a | 838.833 | 85.7 | 116.3 | 0.241 | 204.2 | 1279 | 8.33 → 6.79 | 2026-09-25T09:27:28Z |
| whisper-sherpa | TS3003a | 1505.643 | 136.8 | 187.1 | 0.215 | 326.1 | 1258 | 6.79 → 19.26 | 2026-09-25T09:30:52Z |
| whisper-sherpa, hint | EN2002a | 2142.709 | 257.7 | 472.0 | 0.341 | 732.1 | 1621 | 19.26 → 7.03 | 2026-09-25T09:36:18Z |
| whisper-sherpa, hint | ES2004a | 1049.355 | 106.6 | 186.8 | 0.280 | 295.3 | 1273 | 7.03 → 10.53 | 2026-09-25T09:48:30Z |
| whisper-sherpa, hint | IS1009a | 838.833 | 90.5 | 126.8 | 0.259 | 219.5 | 1242 | 10.53 → 6.20 | 2026-09-25T09:53:26Z |
| whisper-sherpa, hint | TS3003a | 1505.643 | 147.1 | 178.9 | 0.217 | 328.2 | 1289 | 6.20 → 5.66 | 2026-09-25T09:57:05Z |
| energy-vad-cluster | EN2002a | – | – | – | – | 5.5 | 458 | 2.93 → 2.86 | 2026-09-25T09:11:32Z |
| energy-vad-cluster | ES2004a | – | – | – | – | 2.9 | 319 | 2.86 → 2.86 | 2026-09-25T09:11:37Z |
| energy-vad-cluster | IS1009a | – | – | – | – | 2.2 | 236 | 2.86 → 2.79 | 2026-09-25T09:11:40Z |
| energy-vad-cluster | TS3003a | – | – | – | – | 3.8 | 318 | 2.79 → 2.73 | 2026-09-25T09:11:43Z |
| energy-vad-cluster, hint | EN2002a | – | – | – | – | 5.5 | 372 | 2.73 → 2.75 | 2026-09-25T09:11:46Z |
| energy-vad-cluster, hint | ES2004a | – | – | – | – | 2.9 | 307 | 2.75 → 2.75 | 2026-09-25T09:11:52Z |
| energy-vad-cluster, hint | IS1009a | – | – | – | – | 2.2 | 220 | 2.75 → 2.77 | 2026-09-25T09:11:55Z |
| energy-vad-cluster, hint | TS3003a | – | – | – | – | 3.8 | 326 | 2.77 → 2.77 | 2026-09-25T09:11:57Z |
| oracle (self-test) | EN2002a | – | – | – | – | 0.3 | 84 | 2.93 → 2.93 | 2026-09-25T09:11:30Z |
| oracle (self-test) | ES2004a | – | – | – | – | 0.3 | 75 | 2.93 → 2.93 | 2026-09-25T09:11:31Z |
| oracle (self-test) | IS1009a | – | – | – | – | 0.3 | 74 | 2.93 → 2.93 | 2026-09-25T09:11:31Z |
| oracle (self-test) | TS3003a | – | – | – | – | 0.3 | 75 | 2.93 → 2.93 | 2026-09-25T09:11:32Z |

| system | meeting | audio s | diarization s | ASR s | RTF | wall s (process) | max RSS MB | load avg 1 min start → end | started (UTC) |
|---|---|---|---|---|---|---|---|---|---|
| whisper-sherpa | synth-piper-2spk-overlap-s0102 | 120.000 | 8.2 | 30.0 | 0.319 | 40.0 | 1060 | 1.94 → 4.36 | 2026-09-25T09:02:19Z |
| whisper-sherpa | synth-piper-2spk-s0101 | 120.000 | 9.6 | 19.2 | 0.239 | 30.5 | 1031 | 4.36 → 4.81 | 2026-09-25T09:02:59Z |
| whisper-sherpa | synth-piper-3spk-reverb-noise-s0103 | 135.000 | 12.3 | 28.0 | 0.299 | 42.0 | 1049 | 4.81 → 5.54 | 2026-09-25T09:03:30Z |
| whisper-sherpa | synth-piper-4spk-s0104 | 150.000 | 14.4 | 24.0 | 0.256 | 40.1 | 989 | 5.54 → 5.40 | 2026-09-25T09:04:12Z |
| whisper-sherpa, hint | synth-piper-2spk-overlap-s0102 | 120.000 | 11.9 | 31.7 | 0.363 | 45.4 | 1096 | 5.40 → 5.43 | 2026-09-25T09:04:52Z |
| whisper-sherpa, hint | synth-piper-2spk-s0101 | 120.000 | 12.0 | 20.2 | 0.268 | 33.9 | 1006 | 5.43 → 5.08 | 2026-09-25T09:05:37Z |
| whisper-sherpa, hint | synth-piper-3spk-reverb-noise-s0103 | 135.000 | 15.1 | 31.0 | 0.342 | 48.0 | 980 | 5.08 → 5.53 | 2026-09-25T09:06:11Z |
| whisper-sherpa, hint | synth-piper-4spk-s0104 | 150.000 | 17.1 | 29.4 | 0.310 | 48.6 | 899 | 5.53 → 7.52 | 2026-09-25T09:06:59Z |

| system | meeting | audio s | diarization s | ASR s | RTF | wall s (process) | max RSS MB | load avg 1 min start → end | started (UTC) |
|---|---|---|---|---|---|---|---|---|---|
| whisper-sherpa | synth-piper-2spk-overlap-s0102 | 120.000 | 14.8 | 26.6 | 0.345 | 43.9 | 1279 | 5.66 → 5.44 | 2026-09-25T10:02:35Z |
| whisper-sherpa | synth-piper-2spk-s0101 | 120.000 | 14.1 | 21.5 | 0.297 | 38.0 | 1292 | 5.44 → 5.33 | 2026-09-25T10:03:19Z |
| whisper-sherpa | synth-piper-3spk-reverb-noise-s0103 | 135.000 | 16.5 | 26.7 | 0.320 | 45.7 | 1307 | 5.33 → 5.85 | 2026-09-25T10:03:57Z |
| whisper-sherpa | synth-piper-4spk-s0104 | 150.000 | 20.5 | 36.6 | 0.380 | 59.7 | 1157 | 5.85 → 10.36 | 2026-09-25T10:04:43Z |
| whisper-sherpa, hint | synth-piper-2spk-overlap-s0102 | 120.000 | 15.0 | 29.4 | 0.371 | 47.2 | 1254 | 10.36 → 7.94 | 2026-09-25T10:05:42Z |
| whisper-sherpa, hint | synth-piper-2spk-s0101 | 120.000 | 15.8 | 23.0 | 0.323 | 41.4 | 1170 | 7.94 → 7.28 | 2026-09-25T10:06:30Z |
| whisper-sherpa, hint | synth-piper-3spk-reverb-noise-s0103 | 135.000 | 18.3 | 29.1 | 0.352 | 50.1 | 1135 | 7.28 → 9.69 | 2026-09-25T10:07:11Z |
| whisper-sherpa, hint | synth-piper-4spk-s0104 | 150.000 | 19.1 | 29.6 | 0.325 | 51.5 | 1312 | 9.69 → 7.93 | 2026-09-25T10:08:01Z |

* **AMI** (92.2 min of audio): RTF 0.2151-0.3055 without the hint and 0.2165-0.3405
  with it. That is 1456.76 s and 1575.10 s of process wall time for the two passes.
  ASR is 54.8-68.4 % of the compute per run, and diarization takes the rest. That is a larger
  diarization share than on the 60 s clip of docs/pipeline.md §11.4, where ASR took
  68.6-78.6 %.
* **Piper** (8.75 min): RTF 0.239-0.380.
* **Memory:** max RSS 898-1621 MB per whisper-sherpa process (MB = 10^6 bytes). The
  peak was on EN2002a, the longest meeting (35.7 min): 1522 MB without the hint and
  1621 MB with it. `energy-vad-cluster` peaked at 458 MB, and the oracle at 83 MB.
* **Contention I added.** Two low-priority scoring runs of mine (`nice 19`,
  09:36:32-09:37:17 UTC and about 09:42 UTC for 36 s) overlapped the EN2002a hint
  run (09:36:18-09:48:30 UTC). The CAM++ probes of section 8.4 ran between the Piper
  runs and the AMI runs. The ASR diagnostic (10:14-10:21 UTC) and the Piper repeat
  (10:22-10:48 UTC) ran after all the inference reported here. The TS3003a no-hint
  run ended at load average 19.26 from other load.

## 5. Scoring conventions

Everything is `python -m evals batch` (docs/evals.md), per meeting, plus its
**macro** (mean of per-meeting rates) and **micro** (pooled over the 4 meetings)
aggregates.

* DER and JER: pyannote.metrics. Collar 0.25 **total width** (±0.125 s) centred on
  reference boundaries, overlap scored, UEM `[0, duration_s]`. The table in section
  6.4 repeats them at collar 0.
* **The whisper-sherpa hypothesis is word runs.** Its segments span the ASR words
  assigned to each speaker, so silence inside a turn and speech that Whisper did
  not transcribe count as missed speech. The "sherpa raw turns" rows show the
  diarizer's own turns.
* cpWER (headline) and tcpWER (collar 5 s): meeteval 0.4.3. WER, speaker-agnostic
  (`wer_concat`): jiwer on all words in start order. It measures the ASR alone,
  blind to attribution. cpWER and tcpWER exceed 1 when the errors, insertions
  included, outnumber the reference words.
* Normaliser: the evals default (lowercase, punctuation, hyphen splitting). The
  variant `--numbers-to-words --expand-contractions` is in section 6.4.
* "spk count err": hypothesis speakers minus active reference speakers, a signed
  mean.
* Rounding (stated 25 Sep 2026): table cells are rounded to the digits shown. Figures
  in the prose are exact or truncated, never rounded up. A prose figure can therefore
  be one unit lower in its last digit than the table cell it summarises (section 4:
  1522 MB in the prose, 1523 in the table).

## 6. Results

### 6.1 AMI subset (EN2002a, ES2004a, IS1009a, TS3003a)

| system | report | agg | DER | miss | FA | confusion | JER | cpWER | tcpWER | WER (concat) | spk count err (mean) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| oracle (self-test) | evals batch | macro | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00 |
| oracle (self-test) | evals batch | micro | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00 |
| energy-vad-cluster | evals batch | macro | 0.6445 | 0.2253 | 0.0718 | 0.3474 | 0.7566 | n/a | n/a | n/a | 0.50 |
| energy-vad-cluster | evals batch | micro | 0.6164 | 0.2459 | 0.0511 | 0.3194 | 0.7566 | n/a | n/a | n/a | 0.50 |
| energy-vad-cluster, hint | evals batch | macro | 0.6709 | 0.2263 | 0.0723 | 0.3723 | 0.7792 | n/a | n/a | n/a | 0.00 |
| energy-vad-cluster, hint | evals batch | micro | 0.6495 | 0.2474 | 0.0514 | 0.3507 | 0.7792 | n/a | n/a | n/a | 0.00 |
| whisper-sherpa | evals batch | macro | no meeting scored | – | – | – | – | – | – | – | – |
| whisper-sherpa | evals batch | micro | no meeting scored | – | – | – | – | – | – | – | – |
| whisper-sherpa | guard lifted | macro | 0.7760 | 0.2748 | 0.0394 | 0.4618 | 0.8020 | 1.1096 | 1.2290 | 0.2876 | 31.33 |
| whisper-sherpa | guard lifted | micro | 0.7825 | 0.2800 | 0.0401 | 0.4624 | 0.8020 | 1.1135 | 1.2282 | 0.2847 | 31.33 |
| whisper-sherpa, hint | evals batch | macro | 0.6557 | 0.3124 | 0.0336 | 0.3096 | 0.8150 | 0.8533 | 0.9709 | 0.3250 | 0.00 |
| whisper-sherpa, hint | evals batch | micro | 0.6830 | 0.3509 | 0.0279 | 0.3043 | 0.8150 | 0.8974 | 1.0000 | 0.3631 | 0.00 |
| word runs, no text | evals batch | macro | 0.7871 | 0.3083 | 0.0330 | 0.4459 | 0.8123 | n/a | n/a | n/a | 43.25 |
| word runs, no text | evals batch | micro | 0.8005 | 0.3410 | 0.0276 | 0.4319 | 0.8123 | n/a | n/a | n/a | 43.25 |
| word runs, no text, hint | evals batch | macro | 0.6557 | 0.3124 | 0.0336 | 0.3096 | 0.8150 | n/a | n/a | n/a | 0.00 |
| word runs, no text, hint | evals batch | micro | 0.6830 | 0.3509 | 0.0279 | 0.3043 | 0.8150 | n/a | n/a | n/a | 0.00 |
| sherpa raw turns | evals batch | macro | 0.7179 | 0.0886 | 0.0359 | 0.5933 | 0.7609 | n/a | n/a | n/a | 49.25 |
| sherpa raw turns | evals batch | micro | 0.7299 | 0.0943 | 0.0347 | 0.6009 | 0.7609 | n/a | n/a | n/a | 49.25 |
| sherpa raw turns, hint | evals batch | macro | 0.5324 | 0.0873 | 0.0348 | 0.4103 | 0.7424 | n/a | n/a | n/a | 0.00 |
| sherpa raw turns, hint | evals batch | micro | 0.5468 | 0.0938 | 0.0325 | 0.4205 | 0.7424 | n/a | n/a | n/a | 0.00 |

The "guard lifted" rows average **3 meetings** (ES2004a, IS1009a, TS3003a), because
EN2002a could not be scored even with the guard lifted. `python -m evals batch`
itself scored **no** meeting of the no-hint run (section 8.1). The "word runs, no
text" rows give the no-hint run's DER/JER on all 4 meetings.

| system | meeting | scored by | DER | JER | cpWER | tcpWER | WER (concat) | hyp speakers | hint → honoured |
|---|---|---|---|---|---|---|---|---|---|
| whisper-sherpa, hint | EN2002a | evals batch | 0.7289 | 0.8384 | 0.9608 | 1.0366 | 0.4371 | 4 | 4 → true |
| whisper-sherpa, hint | ES2004a | evals batch | 0.6649 | 0.7793 | 0.9239 | 1.0241 | 0.2759 | 4 | 4 → true |
| whisper-sherpa, hint | IS1009a | evals batch | 0.5444 | 0.7680 | 0.6640 | 0.7953 | 0.3266 | 4 | 4 → true |
| whisper-sherpa, hint | TS3003a | evals batch | 0.6846 | 0.8741 | 0.8646 | 1.0276 | 0.2605 | 4 | 4 → true |
| whisper-sherpa | EN2002a | not scorable (meeteval) | – | – | – | – | – | 83 | – |
| whisper-sherpa | ES2004a | guard lifted | 0.7231 | 0.7278 | 1.0633 | 1.1338 | 0.2759 | 31 | – |
| whisper-sherpa | IS1009a | guard lifted | 0.7550 | 0.8107 | 1.0446 | 1.2190 | 0.3266 | 34 | – |
| whisper-sherpa | TS3003a | guard lifted | 0.8498 | 0.8675 | 1.2210 | 1.3343 | 0.2605 | 41 | – |
| word runs, no text | EN2002a | evals batch | 0.8206 | 0.8433 | n/a | n/a | n/a | 83 | – |
| word runs, no text | ES2004a | evals batch | 0.7231 | 0.7278 | n/a | n/a | n/a | 31 | – |
| word runs, no text | IS1009a | evals batch | 0.7550 | 0.8107 | n/a | n/a | n/a | 34 | – |
| word runs, no text | TS3003a | evals batch | 0.8498 | 0.8675 | n/a | n/a | n/a | 41 | – |
| sherpa raw turns, hint | EN2002a | evals batch | 0.5650 | 0.7296 | n/a | n/a | n/a | 4 | – |
| sherpa raw turns, hint | ES2004a | evals batch | 0.5403 | 0.6959 | n/a | n/a | n/a | 4 | – |
| sherpa raw turns, hint | IS1009a | evals batch | 0.4519 | 0.7200 | n/a | n/a | n/a | 4 | – |
| sherpa raw turns, hint | TS3003a | evals batch | 0.5723 | 0.8243 | n/a | n/a | n/a | 4 | – |
| sherpa raw turns | EN2002a | evals batch | 0.7517 | 0.7860 | n/a | n/a | n/a | 95 | – |
| sherpa raw turns | ES2004a | evals batch | 0.6269 | 0.6471 | n/a | n/a | n/a | 35 | – |
| sherpa raw turns | IS1009a | evals batch | 0.7173 | 0.7897 | n/a | n/a | n/a | 36 | – |
| sherpa raw turns | TS3003a | evals batch | 0.7756 | 0.8209 | n/a | n/a | n/a | 47 | – |
| energy-vad-cluster | EN2002a | evals batch | 0.5537 | 0.7416 | n/a | n/a | n/a | 2 | – |
| energy-vad-cluster | ES2004a | evals batch | 0.7395 | 0.6596 | n/a | n/a | n/a | 6 | – |
| energy-vad-cluster | IS1009a | evals batch | 0.6604 | 0.7843 | n/a | n/a | n/a | 6 | – |
| energy-vad-cluster | TS3003a | evals batch | 0.6246 | 0.8409 | n/a | n/a | n/a | 4 | – |
| energy-vad-cluster, hint | EN2002a | evals batch | 0.5834 | 0.6845 | n/a | n/a | n/a | 4 | – |
| energy-vad-cluster, hint | ES2004a | evals batch | 0.6950 | 0.7206 | n/a | n/a | n/a | 4 | – |
| energy-vad-cluster, hint | IS1009a | evals batch | 0.6428 | 0.8160 | n/a | n/a | n/a | 4 | – |
| energy-vad-cluster, hint | TS3003a | evals batch | 0.7624 | 0.8960 | n/a | n/a | n/a | 4 | – |

### 6.2 Piper set, `mix.wav`

| system | report | agg | DER | miss | FA | confusion | JER | cpWER | tcpWER | WER (concat) | spk count err (mean) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| oracle (self-test) | evals batch | macro | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00 |
| oracle (self-test) | evals batch | micro | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00 |
| energy-vad-cluster | evals batch | macro | 0.2304 | 0.0103 | 0.0367 | 0.1833 | 0.3060 | n/a | n/a | n/a | 0.25 |
| energy-vad-cluster | evals batch | micro | 0.2422 | 0.0103 | 0.0364 | 0.1956 | 0.3644 | n/a | n/a | n/a | 0.25 |
| energy-vad-cluster, hint | evals batch | macro | 0.1920 | 0.0098 | 0.0367 | 0.1455 | 0.2562 | n/a | n/a | n/a | 0.00 |
| energy-vad-cluster, hint | evals batch | micro | 0.2045 | 0.0098 | 0.0364 | 0.1583 | 0.3197 | n/a | n/a | n/a | 0.00 |
| whisper-sherpa | evals batch | macro | 0.6760 | 0.0164 | 0.0846 | 0.5750 | 0.7191 | 0.9962 | 1.0732 | 0.2501 | 6.25 |
| whisper-sherpa | evals batch | micro | 0.6759 | 0.0166 | 0.0807 | 0.5786 | 0.7319 | 0.9984 | 1.0725 | 0.2491 | 6.25 |
| whisper-sherpa, hint | evals batch | macro | 0.6149 | 0.0152 | 0.0985 | 0.5012 | 0.7699 | 1.0993 | 1.1233 | 0.2501 | -0.25 |
| whisper-sherpa, hint | evals batch | micro | 0.6185 | 0.0154 | 0.0964 | 0.5068 | 0.7887 | 1.0912 | 1.1141 | 0.2491 | -0.25 |
| word runs, no text | evals batch | macro | 0.6760 | 0.0164 | 0.0846 | 0.5750 | 0.7191 | n/a | n/a | n/a | 6.25 |
| word runs, no text | evals batch | micro | 0.6759 | 0.0166 | 0.0807 | 0.5786 | 0.7319 | n/a | n/a | n/a | 6.25 |
| word runs, no text, hint | evals batch | macro | 0.6149 | 0.0152 | 0.0985 | 0.5012 | 0.7699 | n/a | n/a | n/a | -0.25 |
| word runs, no text, hint | evals batch | micro | 0.6185 | 0.0154 | 0.0964 | 0.5068 | 0.7887 | n/a | n/a | n/a | -0.25 |
| sherpa raw turns | evals batch | macro | 0.6293 | 0.0028 | 0.0441 | 0.5824 | 0.7089 | n/a | n/a | n/a | 6.50 |
| sherpa raw turns | evals batch | micro | 0.6329 | 0.0028 | 0.0442 | 0.5858 | 0.7237 | n/a | n/a | n/a | 6.50 |
| sherpa raw turns, hint | evals batch | macro | 0.5541 | 0.0007 | 0.0508 | 0.5026 | 0.7505 | n/a | n/a | n/a | -0.25 |
| sherpa raw turns, hint | evals batch | micro | 0.5596 | 0.0007 | 0.0506 | 0.5083 | 0.7732 | n/a | n/a | n/a | -0.25 |

| system | meeting | scored by | DER | JER | cpWER | tcpWER | WER (concat) | hyp speakers | hint → honoured |
|---|---|---|---|---|---|---|---|---|---|
| whisper-sherpa, hint | synth-piper-2spk-overlap-s0102 | evals batch | 0.4477 | 0.6582 | 0.9389 | 0.9389 | 0.2304 | 2 | 2 → true |
| whisper-sherpa, hint | synth-piper-2spk-s0101 | evals batch | 0.6398 | 0.7503 | 0.9317 | 0.9390 | 0.1878 | 2 | 2 → true |
| whisper-sherpa, hint | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.6120 | 0.8269 | 1.3401 | 1.3401 | 0.4603 | 2 | 3 → false |
| whisper-sherpa, hint | synth-piper-4spk-s0104 | evals batch | 0.7603 | 0.8443 | 1.1863 | 1.2749 | 0.1220 | 4 | 4 → true |
| whisper-sherpa | synth-piper-2spk-overlap-s0102 | evals batch | 0.5874 | 0.6778 | 1.0052 | 1.0209 | 0.2304 | 9 | – |
| whisper-sherpa | synth-piper-2spk-s0101 | evals batch | 0.7666 | 0.7151 | 0.9122 | 0.9366 | 0.1878 | 7 | – |
| whisper-sherpa | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.5727 | 0.6684 | 1.0408 | 1.1156 | 0.4603 | 10 | – |
| whisper-sherpa | synth-piper-4spk-s0104 | evals batch | 0.7772 | 0.8150 | 1.0266 | 1.2195 | 0.1220 | 10 | – |
| sherpa raw turns, hint | synth-piper-2spk-overlap-s0102 | evals batch | 0.4160 | 0.6237 | n/a | n/a | n/a | 2 | – |
| sherpa raw turns, hint | synth-piper-2spk-s0101 | evals batch | 0.4642 | 0.7095 | n/a | n/a | n/a | 2 | – |
| sherpa raw turns, hint | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.7095 | 0.8366 | n/a | n/a | n/a | 2 | – |
| sherpa raw turns, hint | synth-piper-4spk-s0104 | evals batch | 0.6269 | 0.8322 | n/a | n/a | n/a | 4 | – |
| sherpa raw turns | synth-piper-2spk-overlap-s0102 | evals batch | 0.5854 | 0.6615 | n/a | n/a | n/a | 9 | – |
| sherpa raw turns | synth-piper-2spk-s0101 | evals batch | 0.5749 | 0.6885 | n/a | n/a | n/a | 7 | – |
| sherpa raw turns | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.6711 | 0.6818 | n/a | n/a | n/a | 11 | – |
| sherpa raw turns | synth-piper-4spk-s0104 | evals batch | 0.6858 | 0.8038 | n/a | n/a | n/a | 10 | – |
| energy-vad-cluster | synth-piper-2spk-overlap-s0102 | evals batch | 0.2357 | 0.2430 | n/a | n/a | n/a | 4 | – |
| energy-vad-cluster | synth-piper-2spk-s0101 | evals batch | 0.0814 | 0.0861 | n/a | n/a | n/a | 2 | – |
| energy-vad-cluster | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.1375 | 0.2289 | n/a | n/a | n/a | 3 | – |
| energy-vad-cluster | synth-piper-4spk-s0104 | evals batch | 0.4668 | 0.6659 | n/a | n/a | n/a | 3 | – |
| energy-vad-cluster, hint | synth-piper-2spk-overlap-s0102 | evals batch | 0.0650 | 0.0906 | n/a | n/a | n/a | 2 | – |
| energy-vad-cluster, hint | synth-piper-2spk-s0101 | evals batch | 0.0814 | 0.0861 | n/a | n/a | n/a | 2 | – |
| energy-vad-cluster, hint | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.1375 | 0.2289 | n/a | n/a | n/a | 3 | – |
| energy-vad-cluster, hint | synth-piper-4spk-s0104 | evals batch | 0.4843 | 0.6192 | n/a | n/a | n/a | 4 | – |

### 6.3 Piper set, device Ogg/Opus (`device/recording.ogg`)

| system | report | agg | DER | miss | FA | confusion | JER | cpWER | tcpWER | WER (concat) | spk count err (mean) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| whisper-sherpa | evals batch | macro | 0.6819 | 0.0160 | 0.1090 | 0.5570 | 0.7335 | 1.0242 | 1.0722 | 0.2764 | 5.75 |
| whisper-sherpa | evals batch | micro | 0.6860 | 0.0161 | 0.1078 | 0.5622 | 0.7477 | 1.0224 | 1.0688 | 0.2795 | 5.75 |
| whisper-sherpa, hint | evals batch | macro | 0.6052 | 0.0150 | 0.1090 | 0.4812 | 0.7434 | 1.0438 | 1.0599 | 0.2764 | -0.25 |
| whisper-sherpa, hint | evals batch | micro | 0.6105 | 0.0151 | 0.1078 | 0.4877 | 0.7672 | 1.0277 | 1.0437 | 0.2795 | -0.25 |
| word runs, no text | evals batch | macro | 0.6819 | 0.0160 | 0.1090 | 0.5570 | 0.7335 | n/a | n/a | n/a | 5.75 |
| word runs, no text | evals batch | micro | 0.6860 | 0.0161 | 0.1078 | 0.5622 | 0.7477 | n/a | n/a | n/a | 5.75 |
| word runs, no text, hint | evals batch | macro | 0.6052 | 0.0150 | 0.1090 | 0.4812 | 0.7434 | n/a | n/a | n/a | -0.25 |
| word runs, no text, hint | evals batch | micro | 0.6105 | 0.0151 | 0.1078 | 0.4877 | 0.7672 | n/a | n/a | n/a | -0.25 |
| sherpa raw turns | evals batch | macro | 0.6112 | 0.0028 | 0.0483 | 0.5601 | 0.7192 | n/a | n/a | n/a | 6.00 |
| sherpa raw turns | evals batch | micro | 0.6164 | 0.0027 | 0.0483 | 0.5654 | 0.7354 | n/a | n/a | n/a | 6.00 |
| sherpa raw turns, hint | evals batch | macro | 0.5306 | 0.0008 | 0.0482 | 0.4816 | 0.7222 | n/a | n/a | n/a | -0.25 |
| sherpa raw turns, hint | evals batch | micro | 0.5371 | 0.0009 | 0.0482 | 0.4881 | 0.7500 | n/a | n/a | n/a | -0.25 |

| system | meeting | scored by | DER | JER | cpWER | tcpWER | WER (concat) | hyp speakers | hint → honoured |
|---|---|---|---|---|---|---|---|---|---|
| whisper-sherpa, hint | synth-piper-2spk-overlap-s0102 | evals batch | 0.4071 | 0.5884 | 0.7818 | 0.7906 | 0.3072 | 2 | 2 → true |
| whisper-sherpa, hint | synth-piper-2spk-s0101 | evals batch | 0.6298 | 0.7490 | 0.9415 | 0.9439 | 0.1756 | 2 | 2 → true |
| whisper-sherpa, hint | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.5743 | 0.7799 | 1.2744 | 1.2744 | 0.4921 | 2 | 3 → false |
| whisper-sherpa, hint | synth-piper-4spk-s0104 | evals batch | 0.8095 | 0.8563 | 1.1774 | 1.2306 | 0.1308 | 4 | 4 → true |
| whisper-sherpa | synth-piper-2spk-overlap-s0102 | evals batch | 0.5609 | 0.6730 | 0.9564 | 0.9703 | 0.3072 | 8 | – |
| whisper-sherpa | synth-piper-2spk-s0101 | evals batch | 0.7285 | 0.7511 | 0.8976 | 0.9122 | 0.1756 | 6 | – |
| whisper-sherpa | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.5656 | 0.6639 | 1.0590 | 1.1179 | 0.4921 | 10 | – |
| whisper-sherpa | synth-piper-4spk-s0104 | evals batch | 0.8728 | 0.8461 | 1.1840 | 1.2882 | 0.1308 | 10 | – |
| sherpa raw turns, hint | synth-piper-2spk-overlap-s0102 | evals batch | 0.3825 | 0.5539 | n/a | n/a | n/a | 2 | – |
| sherpa raw turns, hint | synth-piper-2spk-s0101 | evals batch | 0.4418 | 0.7035 | n/a | n/a | n/a | 2 | – |
| sherpa raw turns, hint | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.6564 | 0.7894 | n/a | n/a | n/a | 2 | – |
| sherpa raw turns, hint | synth-piper-4spk-s0104 | evals batch | 0.6418 | 0.8418 | n/a | n/a | n/a | 4 | – |
| sherpa raw turns | synth-piper-2spk-overlap-s0102 | evals batch | 0.5636 | 0.6588 | n/a | n/a | n/a | 8 | – |
| sherpa raw turns | synth-piper-2spk-s0101 | evals batch | 0.5373 | 0.7150 | n/a | n/a | n/a | 6 | – |
| sherpa raw turns | synth-piper-3spk-reverb-noise-s0103 | evals batch | 0.6408 | 0.6706 | n/a | n/a | n/a | 11 | – |
| sherpa raw turns | synth-piper-4spk-s0104 | evals batch | 0.7029 | 0.8326 | n/a | n/a | n/a | 10 | – |

Speaker-agnostic WER is 0.2764 from the device Ogg against 0.2501 from `mix.wav`
(macro, +0.0263). Per meeting, 3 of 4 got worse: s0102 0.2304 → 0.3072, s0103
0.4603 → 0.4921 and s0104 0.1220 → 0.1308. s0101 got better: 0.1878 → 0.1756. With
n = 4 no confidence interval is given. (Correction, 25 Sep 2026: an earlier version
said "The Opus round trip costs the ASR about 2.6 points". That is only the macro
change, and one of the four meetings improved.) DER is not systematically worse; the
diarizer is near chance on both.

### 6.4 Variants: DER collar 0, and normalisation

| data | system | DER (collar 0.25) | DER (collar 0) | cpWER (default normaliser) | cpWER (+ numbers, contractions) | WER (default) | WER (+ numbers, contractions) |
|---|---|---|---|---|---|---|---|
| ami | whisper-sherpa, hint | 0.6557 | 0.6839 | 0.8533 | 0.8457 | 0.3250 | 0.3100 |
| ami | whisper-sherpa, no hint (guard lifted, 3 meetings) | 0.7760 | 0.8001 | 1.1096 | refused by meeteval | 0.2876 | refused by meeteval |
| piper | whisper-sherpa, hint | 0.6149 | 0.6784 | 1.0993 | 1.1021 | 0.2501 | 0.2519 |
| piper | whisper-sherpa | 0.6760 | 0.7329 | 0.9962 | 0.9968 | 0.2501 | 0.2519 |
| piper-device | whisper-sherpa, hint | 0.6052 | 0.6752 | 1.0438 | 1.0465 | 0.2764 | 0.2786 |
| piper-device | whisper-sherpa | 0.6819 | 0.7466 | 1.0242 | 1.0264 | 0.2764 | 0.2786 |

At collar 0 each whisper-sherpa DER in the table rises by 0.024-0.070. The number
and contraction normalisation lowers the AMI hint run's cpWER from 0.8533 to 0.8457
and its WER from 0.3250 to 0.3100 (macro). On the Piper set it raises them slightly:
WER goes from 0.2501 to 0.2519. `evals.io` applies the same
normaliser to both sides, so this is how the ASR output and the word-salad reference
differ, not an error.

## 7. What these numbers mean, and what they do not

* **Not the AMI test set.** These are 4 of 16 test-split meetings, chosen because
  their audio is here, not at random. Meeting-level spread is large (hint run DER
  0.5444-0.7289, cpWER 0.6640-0.9608). With n = 4 no confidence interval is
  meaningful, and none is given. The V5 target suite `ami-headset` (full test split)
  remains unmeasured.
* **Not comparable to published AMI numbers**, and none is quoted. Published AMI
  diarization and ASR results differ from this setup in the meeting set (all 16 or
  more), the microphone condition (IHM, SDM or MDM against the headset mix here), the
  hypothesis form (diarization turns against word runs), the collar, the model sizes,
  and whether the speaker count is given. Comparing one of them with a number here
  would first need that setup reproduced.
* **Possible train/test contamination.** pyannote segmentation-3.0 was trained on
  data that includes AMI (section 3). Whisper's training data is unpublished. A
  better score would not prove generalisation.
* **The hint is an oracle.** The configuration closest to a product is the no-hint
  one, and it is unusable as it stands (31-95 clusters on AMI).
* **Synthetic Piper speech is not meeting speech.** It is clean read speech, one TTS
  voice per speaker, and word-salad text with no language-model help. The room is
  simulated and there are no disfluencies or backchannels. Its ASR WER says little
  about real meetings. Its diarization results say something about **this
  diarizer and these TTS voices** (section 8.4), not about real speakers. All four
  meetings' voices come from one multi-speaker TTS model.
* **The gates are regression gates.** They are the measured values plus a margin
  (section 9). Passing them means "no worse than on 2026-09-25", never "good".
* **What does carry over:** the pipeline runs end to end on 35-minute real meetings
  on an 8 GB M1, at RTF 0.215-0.380 under contention and 1.52-1.62 GB peak memory on
  the longest meeting. Its diarization,
  not its ASR, is the dominant error on both data sets. The default clustering
  threshold is unusable on long meetings.

## 8. Failures and oddities observed

### 8.1 The evals layer cannot score an over-clustered hypothesis

`meeteval.wer.wer.cp._minimum_permutation_word_error_rate` (0.4.3,
`meeteval/wer/wer/cp.py:299-301`: the `if` at line 299, the `raise` at 301) raises
`RuntimeError: Are you sure? Found a total of 83 speakers in the input (Reference: 4, Hypothesis: 83)` whenever either side has
more than 20 speakers. `evals.score_meeting` computes cpWER and tcpWER with it, so
the whole meeting, DER included, lands under `errors`, and `python -m evals batch`
exits 2. All 4 no-hint AMI meetings failed this way (83, 31, 34 and 41 hypothesis
speakers). Nothing in the Piper runs reached 20 speakers.

Supplementary path (HARNESS_POLICY, `score_lifted` in `scripts/run-v5.sh`): the same
`evals.cli.main batch` is run in-process with only that `if … > 20: raise` block cut
out of meeteval's source and re-compiled in meeteval's module namespace. The
docstring of `cp_word_error_rate` (cp.py:128-129), the public function that calls it,
says: "This implementation uses the Hungarian algorithm, so it works for large
numbers of speakers." (Correction, 25 Sep 2026: an earlier version put this quote in
`_minimum_permutation_word_error_rate`'s docstring and gave the guard as cp.py:300.)

* Checks:
  * A control run of the lifted path on every hypothesis the guard does not stop
    (the AMI hint run and both Piper runs, 7 to 11 speakers without the hint)
    reproduced the standard reports' per-meeting values exactly (`summary.json`:
    `lifted_control_equals_standard: true`).
  * The text-free word runs gave exactly the full hypotheses' DER/JER on every
    meeting both could score (`wordruns_der_jer_equal_full_hypothesis`).
* EN2002a (83 speakers with words) still fails with the guard lifted:
  `RuntimeError: Too few fallback keys provided! There are more over-/under-estimated
  speakers than fallback_keys in abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ`.
  meeteval's assignment has 52 spare labels. Its no-hint cpWER and tcpWER are
  therefore **not measured by meeteval**; its word-run DER is 0.8206 and its JER
  0.8433.
  * **Supplementary, computed without meeteval (added 25 Sep 2026).** cpWER as the
    optimal one-to-one matching of speakers over each speaker's concatenated words
    (evals default normaliser, Levenshtein distance per speaker pair; every word of
    an unmatched speaker counts as an error). EN2002a no-hint: 8570 errors / 7632
    reference words = **1.1229**. The same calculation gives exactly the three
    guard-lifted meeteval values (ES2004a 2821/2653, IS1009a 2108/2018, TS3003a
    3094/2534). Two separate scripts gave the same count: the verification pass's
    and one written for this correction. Neither is in the repository. This number
    is not meeteval's, it is in no table or macro, and no gate uses it. tcpWER was
    not computed this way.
* Request for the evals owner (not done here): a hypothesis with more than 20
  speakers should not take the meeting's DER/JER down with it. For example, cpWER
  and tcpWER could become `None` (the gate fails closed) with the reason recorded.

### 8.2 Over-clustering without the hint

sherpa's clusters (and those that received words): EN2002a 95 (83), ES2004a 35 (31),
IS1009a 36 (34), TS3003a 47 (41). The Piper meetings (s0102, s0101, s0103, s0104)
gave 9, 7, 11 (10) and 10 clusters for 2, 2, 3 and 4 speakers, and the device Ogg 8,
6, 11 (10) and 10. On AMI the count roughly grows with length: 35-36 for the 13.9-17.4
min meetings, 47 for 25 min and 95 for 35.7 min. `cluster_threshold` stays uncalibrated. Calibrating it needs a
development split that is **not** an AMI test meeting (docs/pipeline.md §11.7); it
was not tuned here.

### 8.3 ASR run-to-run divergence on EN2002a

The hint only changes diarization, so the ASR words of the two runs should match.
They matched exactly on ES2004a, IS1009a, TS3003a and all 8 Piper runs (4 `mix.wav`
and 4 device Ogg). On EN2002a they did not: 5504 against 5118 words. The first
difference is at 737.76 s, and from there the transcripts differ to the end of the
meeting. `condition_on_previous_text` likely carries the difference forward; that was
not tested. (Correction, 25 Sep 2026: an earlier version stated this mechanism as
fact.)

* A third run of the same ASR call (diagnostic, same file, same settings, recording
  faster-whisper's per-segment `temperature`) produced 5223 words. It decoded 15
  segments between 546.1 and 571.02 s at temperature 0.2 (compression ratio 2.0674,
  avg logprob −0.2848), and it diverged from both earlier runs at exactly 546.1 s.
* **Inferred cause, not proven:** faster-whisper's default temperature fallback
  (0.0, 0.2, …) samples when a window trips its thresholds, and nothing in the
  harness seeds CTranslate2's sampler. The pipeline does not record per-segment
  temperatures, so the two stored runs cannot be checked for it directly. The two
  stored runs also agreed on every word through 737.76 s, including 546.1-571.02 s,
  where the third run sampled at temperature 0.2. So either both stored runs drew the
  same temperature-0.2 result there, or the third run had already diverged at
  temperature 0. (Correction, 25 Sep 2026: this caveat was missing.)

Effect: the three ASR outputs were re-assigned (the shipped rule) to the **same**
hinted diarization turns and scored with evals defaults. The hint run's own
hypothesis reproduces exactly, which is the control.

| EN2002a ASR run | words | DER | JER | cpWER | tcpWER | WER, speaker-agnostic |
|---|---|---|---|---|---|---|
| no-hint run's ASR | 5504 | 0.7174 | 0.8318 | 0.9862 | 1.0579 | 0.4072 |
| hint run's ASR (= the stored hint hypothesis) | 5118 | 0.7289 | 0.8384 | 0.9608 | 1.0366 | 0.4371 |
| diagnostic third run (raw faster-whisper words, punctuation stripped by the diagnostic, not by `whisper_words`) | 5223 | 0.7220 | 0.8348 | 0.9679 | 1.0418 | 0.4245 |

On this one meeting the spread, from the unrounded values, is 0.0298 in WER, 0.0254
in cpWER, 0.0213 in tcpWER and 0.0114 in DER. On the 4-meeting macro that is at most
0.0074.

A full repeat of every Piper inference (all 20 hypotheses, into scratch) was
identical to the first run, sherpa turns included. On this machine, then, sherpa
diarization repeated exactly on the Piper meetings (120-150 s each). AMI diarization
was never repeated, so whether it repeats is not known. Of the ASR outputs compared,
only the long AMI meeting's did not repeat. (Correction, 25 Sep 2026: an earlier
version said "sherpa diarization repeated exactly" without limiting it to the Piper
meetings.)

### 8.4 sherpa diarization is near chance on the Piper voices

With the hint honoured, confusion is still 0.50 of reference time on `mix.wav`
(0.48 on the device Ogg). The raw turns are no better (hint DER 0.5541). Yet
`energy-vad-cluster` separates the same voices (hint DER 0.0650-0.1375 on the 2- and
3-speaker meetings). A diagnostic with the same CAM++ file, through sherpa-onnx's
`SpeakerEmbeddingExtractor`, gave these results. The probe scripts (`embed_*.py`)
are in the session scratch directory `scratchpad/stage2/measure/`, not in the
repository.

* **Fixed-length chunks separate the voices where probed.** 6 s chunks of one
  speaker's non-overlapped speech (from `mix.wav`) give P(same-speaker cosine > cross-speaker
  cosine) = 1.000 on s0101, 1.000 on IS1009a and 0.995 on ES2004a. On s0101, chunks of
  1.0, 1.5, 2, 3, 4 and 6 s give 0.808, 0.997, 1.000, 0.976, 1.000 and 1.000. s0101's
  two voices are distinguishable by this embedding model, and so are the speakers of
  IS1009a and ES2004a. Among the Piper meetings, the chunk probe covered only s0101.
  (Correction, 25 Sep 2026: an earlier version said "The voices are distinguishable by
  this embedding model" without that limit.)
* **Whole reference turns do not.** Whole turns of ≥ 1.5 s gave 0.557 (s0101), 0.589
  (s0102), 0.549 (s0103), 0.592 (s0104) and 0.569 (IS1009a). That is near chance, and
  it holds on the dry stems as well (s0101 0.589, s0104 0.599). On s0101's 7 turns of ≥ 3 s, the whole turn
  gave 0.473, while its first 1.5 s gave 1.000 and its last 1.5 s 0.918.
* This discrepancy was **not traced**. It suggests that something about turn-length
  inputs, not the voices, degrades the embeddings. The diarizer embeds such
  segments, and that is consistent with its failure. It is recorded as open, not
  explained.

### 8.5 Smaller observations

* `num_speakers=3` was **not honoured** on the 3-speaker Piper meeting s0103 (sherpa
  returned 2, on both `mix.wav` and the device Ogg). The pipeline warned and recorded
  `hint_honoured: false`. `speaker_count.abs_error` of the Piper hint run is
  therefore 0.25 (macro).
* cpWER and tcpWER above 1: a wrong speaker count turns most reference words into
  deletions and most hypothesis words into insertions. The highest values are on
  Piper s0103 with the hint: cpWER and tcpWER 1.3401, with the hint not honoured
  (2 clusters for 3 speakers). Next is AMI TS3003a without the hint (guard lifted):
  tcpWER 1.3343.
* The 3-speaker reverberant, noisy Piper meeting (RT60 0.79 s, 15 dB) has WER 0.4603
  against 0.1220-0.2304 for the three no-noise meetings (RT60 0.3384 s).
* `energy-vad-cluster` on real AMI speech: DER 0.6445 without the hint (2 to 6
  speakers found) and 0.6709 with it. That is comparable to whisper-sherpa's word runs
  and worse than sherpa's raw turns with the hint (0.5324). It was never tuned on
  speech (docs/pipeline.md §2.1).
* While preparing this run I edited `scripts/run-v5.sh` in place while a background
  run of it was executing. bash re-reads a running script, so the stale process was
  neutralised before its inference block finished: its inode was blanked and the
  script restored as a new file. No inference output was affected. Every hypothesis
  was written by the script's functions as parsed at launch, and every log ends in
  `# exit 0`.

## 9. Gates

`evals/gates.yaml` now has two **calibrated regression suites**. They gate the
hinted runs, because `python -m evals batch` cannot score the no-hint AMI run
(section 8.1).

* **Policy (HARNESS_POLICY).** Threshold = the macro value measured here + margin,
  rounded **up** to 3 decimals. The margin is 0.03 absolute on each rate and 0.25 on
  the mean absolute speaker-count error (one meeting off by one more speaker). The
  gates are `max` only.
* **Why 0.03.** The only observed run-to-run variation is the EN2002a ASR divergence:
  at most 0.0298 on one meeting and at most 0.0074 on the macro (section 8.3). 0.03
  on the macro absorbs that happening on all four meetings at once, in the same
  direction. The cost: a regression smaller than 0.03 on the 4-meeting mean passes.
  For example, the assignment tie-break change of docs/pipeline.md §11.5 moved one
  60 s clip's cpWER by 0.046; diluted over four meetings, a change of that size could
  pass. This margin is a choice, not a statistical bound. Other CPUs, onnxruntime or
  CTranslate2 versions, and other thread counts were not tried.
* **`ami-headset`** is kept as the **full-test-split target** (DER ≤ 0.20, cpWER ≤
  0.30). Its description now says it has not been measured. On the subset, the hint
  run is far above it (DER 0.6557, cpWER 0.8533).
* The placeholder suites `synthetic-clean` and `synthetic-noisy` keep their values,
  which `tests/test_evals_gates.py` and `tests/test_evals_cli.py` pin. Only
  `synthetic-clean`'s description changed; it now points at the Piper suite. Both are
  still uncalibrated.

```json v5-gate-calibration
{
  "schema": "plaud-harness/v5-gate-calibration/1",
  "margin_policy": "HARNESS_POLICY: threshold = round_up(measured macro + margin, 3 decimals); margin 0.03 absolute per rate, 0.25 on speaker_count.abs_error; max-only regression gates; measured 2026-09-25 by scripts/run-v5.sh",
  "suites": {
    "ami-subset-whisper-sherpa": {
      "gate_on": "macro",
      "hypotheses": "build/v5/hyp/ami/whisper-sherpa-hint/",
      "report": "build/v5/reports/ami/whisper-sherpa-hint.json",
      "measured": {
        "der.der": 0.655715979791863,
        "jer.jer": 0.8149518617667365,
        "cpwer.error_rate": 0.8533368256092926,
        "tcpwer.error_rate": 0.9709116173709755,
        "wer_concat.wer": 0.3250099258753024,
        "speaker_count.abs_error": 0.0
      },
      "margin": {
        "der.der": 0.03,
        "jer.jer": 0.03,
        "cpwer.error_rate": 0.03,
        "tcpwer.error_rate": 0.03,
        "wer_concat.wer": 0.03,
        "speaker_count.abs_error": 0.25
      },
      "threshold": {
        "der.der": 0.686,
        "jer.jer": 0.845,
        "cpwer.error_rate": 0.884,
        "tcpwer.error_rate": 1.001,
        "wer_concat.wer": 0.356,
        "speaker_count.abs_error": 0.25
      }
    },
    "synthetic-piper-whisper-sherpa": {
      "gate_on": "macro",
      "hypotheses": "build/v5/hyp/piper/whisper-sherpa-hint/",
      "report": "build/v5/reports/piper/whisper-sherpa-hint.json",
      "measured": {
        "der.der": 0.6149163124869673,
        "jer.jer": 0.7699476211616436,
        "cpwer.error_rate": 1.0992535296701889,
        "tcpwer.error_rate": 1.1232557469650888,
        "wer_concat.wer": 0.2501100125062582,
        "speaker_count.abs_error": 0.25
      },
      "margin": {
        "der.der": 0.03,
        "jer.jer": 0.03,
        "cpwer.error_rate": 0.03,
        "tcpwer.error_rate": 0.03,
        "wer_concat.wer": 0.03,
        "speaker_count.abs_error": 0.25
      },
      "threshold": {
        "der.der": 0.645,
        "jer.jer": 0.8,
        "cpwer.error_rate": 1.13,
        "tcpwer.error_rate": 1.154,
        "wer_concat.wer": 0.281,
        "speaker_count.abs_error": 0.5
      }
    }
  }
}
```

`tests/test_v5_gates.py` checks, with no data, that every threshold equals
`round_up(measured + margin, 3)` from this block, that the suites pass on the
measured values and fail at twice the margin, and that `ami-headset` is labelled
unmeasured. The gate commands, as run by `scripts/run-v5.sh` on 2026-09-25 (literal
output):

```
$ python -m evals batch --refs data/corpora/ami/meetings --hyps build/v5/hyp/ami/oracle --gates evals/gates.yaml --suite oracle --gate-on each --report build/v5/gates/oracle.ami.json
EN2002a [oracle]  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
ES2004a [oracle]  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
IS1009a [oracle]  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
TS3003a [oracle]  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
macro  n=4  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
micro  n=4  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
[PASS] suite 'oracle' on EN2002a (6 checks)
[PASS] suite 'oracle' on ES2004a (6 checks)
[PASS] suite 'oracle' on IS1009a (6 checks)
[PASS] suite 'oracle' on TS3003a (6 checks)
exit 0

$ python -m evals batch --refs build/synthetic/piper --hyps build/v5/hyp/piper/oracle --gates evals/gates.yaml --suite oracle --gate-on each --report build/v5/gates/oracle.piper.json
synth-piper-2spk-overlap-s0102 [oracle]  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
synth-piper-2spk-s0101 [oracle]  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
synth-piper-3spk-reverb-noise-s0103 [oracle]  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
synth-piper-4spk-s0104 [oracle]  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
macro  n=4  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
micro  n=4  cpWER 0.0000  tcpWER 0.0000  DER 0.0000  JER 0.0000
[PASS] suite 'oracle' on synth-piper-2spk-overlap-s0102 (6 checks)
[PASS] suite 'oracle' on synth-piper-2spk-s0101 (6 checks)
[PASS] suite 'oracle' on synth-piper-3spk-reverb-noise-s0103 (6 checks)
[PASS] suite 'oracle' on synth-piper-4spk-s0104 (6 checks)
exit 0

$ python -m evals batch --refs data/corpora/ami/meetings --hyps build/v5/hyp/ami/whisper-sherpa-hint --gates evals/gates.yaml --suite ami-subset-whisper-sherpa --gate-on macro --report build/v5/gates/ami-subset-whisper-sherpa.ami.json
EN2002a [whisper-sherpa]  cpWER 0.9608  tcpWER 1.0366  DER 0.7289  JER 0.8384
ES2004a [whisper-sherpa]  cpWER 0.9239  tcpWER 1.0241  DER 0.6649  JER 0.7793
IS1009a [whisper-sherpa]  cpWER 0.6640  tcpWER 0.7953  DER 0.5444  JER 0.7680
TS3003a [whisper-sherpa]  cpWER 0.8646  tcpWER 1.0276  DER 0.6846  JER 0.8741
macro  n=4  cpWER 0.8533  tcpWER 0.9709  DER 0.6557  JER 0.8150
micro  n=4  cpWER 0.8974  tcpWER 1.0000  DER 0.6830  JER 0.8150
[PASS] suite 'ami-subset-whisper-sherpa' on macro (6 checks)
exit 0

$ python -m evals batch --refs build/synthetic/piper --hyps build/v5/hyp/piper/whisper-sherpa-hint --gates evals/gates.yaml --suite synthetic-piper-whisper-sherpa --gate-on macro --report build/v5/gates/synthetic-piper-whisper-sherpa.piper.json
synth-piper-2spk-overlap-s0102 [whisper-sherpa]  cpWER 0.9389  tcpWER 0.9389  DER 0.4477  JER 0.6582
synth-piper-2spk-s0101 [whisper-sherpa]  cpWER 0.9317  tcpWER 0.9390  DER 0.6398  JER 0.7503
synth-piper-3spk-reverb-noise-s0103 [whisper-sherpa]  cpWER 1.3401  tcpWER 1.3401  DER 0.6120  JER 0.8269
synth-piper-4spk-s0104 [whisper-sherpa]  cpWER 1.1863  tcpWER 1.2749  DER 0.7603  JER 0.8443
macro  n=4  cpWER 1.0993  tcpWER 1.1233  DER 0.6149  JER 0.7699
micro  n=4  cpWER 1.0912  tcpWER 1.1141  DER 0.6185  JER 0.7887
[PASS] suite 'synthetic-piper-whisper-sherpa' on macro (6 checks)
exit 0
```

## 10. Reproduce

```bash
./scripts/run-v5.sh            # all stages (run time: see "Run time" below)
V5_STAGES=score,gates,summary ./scripts/run-v5.sh   # rescore only (about 2 min)
```

* **Stages:** `inputs` (verify, and download only what is missing: the AMI zip and
  WAVs from `groups.inf.ed.ac.uk/ami`, the Piper voice at piper-voices revision
  `c10ece1a`, the model weights through `python -m pipeline fetch-models`; each
  checked against a pinned sha256), `infer` (skips every hypothesis that exists;
  `FORCE=1` recomputes), `derive`, `score`, `gates` and `summary`.
* **Outputs:** `build/v5/hyp/<dataset>/<system>/<meeting>/hyp.json`, per-run logs
  with `/usr/bin/time -l` and load averages, the evals reports (`.json`, `.md` and
  `.txt` per variant), `build/v5/gates/<suite>.<dataset>.txt` and
  `build/v5/summary.{md,json}`. The results tables of sections 4 and 6 are
  rendered from `summary.json`. The EN2002a table in section 8.3 is not; it comes
  from the diagnostic run described there.
* **Exit codes:** 1 if a gate fails, 2 if an inference run fails, 3 if an input is
  missing, 4 after giving up waiting for other heavy processes.
* **Run time.** The 48 inference processes of 2026-09-25 took 3773.76 s (62.8 min)
  of process wall time in total, under contention (section 4). A run from scratch
  also downloads the inputs and generates the Piper set. That time was not measured
  and is not included. (Correction, 25 Sep 2026: an earlier version said "about 63
  min of inference on this M1 when nothing exists yet".)
* **Idempotence check:** a full run on the finished tree skipped all 48
  hypotheses, rescored, passed all four gate commands, and exited 0 in 136.07 s of
  wall time.

## 11. Not done

* The other 12 AMI test meetings (no audio here), so V5 itself is not done, and
  neither are dev or train meetings.
* No calibration of `cluster_threshold` or of the assignment tie-break. Both need a
  development split that is not an AMI test meeting.
* No-hint cpWER and tcpWER from meeteval for EN2002a (meeteval limits, section 8.1).
  A cpWER computed without meeteval was added on 25 Sep 2026. No tcpWER exists for
  that run.
* A quiet-machine RTF. No run on another CPU or with other library versions.
* The CAM++ whole-turn anomaly (section 8.4) and the ASR fallback seeding (section
  8.3) are diagnosed only as far as stated.
* Other AMI microphone conditions (SDM, MDM, individual headsets) and a device-shaped
  AMI input.

## 12. Corrections (25 Sep 2026)

A verification pass on 25 Sep 2026 found the 11 errors below. None changes a table
value, a gate threshold or a conclusion. Each is corrected in place; the substantive
ones also carry a dated note where they occur.

| # | section | was | now |
|---|---|---|---|
| 1 | 4 | "Swap in use was 6.8-8.7 GB (of an 8-10 GB swap file) throughout". Nothing on disk supports it | the logs record swap only at each run's start: 6781-8414 MiB in use, swap size 8192-9216 MiB |
| 2 | 8.1 | the "works for large numbers of speakers" quote attributed to `_minimum_permutation_word_error_rate`'s docstring; guard at `cp.py:300` | the quote is in `cp_word_error_rate`'s docstring (`cp.py:128-129`); the guard is `cp.py:299-301` (`if` at 299, `raise` at 301) |
| 3 | 8.3 | "(`condition_on_previous_text` carries the difference forward)", stated as fact; no caveat on the inferred cause | "likely", and not tested. Added: the two stored runs agreed on every word through 737.76 s, including where the third run sampled, so either both drew the same temperature-0.2 result or the third run had already diverged at temperature 0 |
| 4 | 8.3 | "sherpa diarization repeated exactly" | repeated exactly on the Piper meetings (120-150 s); AMI diarization was never repeated |
| 5 | 8.4 | "The voices are distinguishable by this embedding model" | limited to s0101's two voices and the IS1009a and ES2004a speakers; the chunk probe covered only s0101 among the Piper meetings |
| 6 | headline | "Two runs of the same configuration" | "Two runs with the same ASR settings (hint and no-hint)" |
| 7 | headline, 8.5 | "clean-room" Piper meetings | "no-noise" meetings in a simulated room with RT60 0.3384 s measured. The verification pass suggested "RT60 0.34 s"; the measured value is given instead, because 0.34 rounds it up |
| 8 | 6.3 | "The Opus round trip costs the ASR about 2.6 points" | that is only the macro change (+0.0263); 3 of 4 meetings got worse and s0101 got better (0.1878 → 0.1756); n = 4 |
| 9 | several | prose figures rounded up | exact or truncated; list below |
| 10 | 3 | CAM++ licence "Apache-2.0 (ModelScope card)" only | the VoxCeleb2 training-data caveat of `pipeline/model_store.py` added: only the metadata is CC BY-SA 4.0, and no licence is stated for the audio |
| 11 | 10 | "about 63 min of inference on this M1 when nothing exists yet" | 3773.76 s (62.8 min) is the sum of the 48 process times under contention; a run from scratch also downloads inputs and generates the Piper set, which is not counted |

Item 9 in detail. The verification pass named the first four. The same check, applied
to the rest of the prose for this correction, found the others. Figures that were
truncated, not rounded up, are marked "(consistency)": they now carry the same
number of digits as the figure beside them.

| section | was | now |
|---|---|---|
| headline | speaker confusion "0.31" | 0.3096 (missed speech 0.31 → 0.3124, consistency) |
| headline | word-run DER "0.54-0.73" | 0.5444-0.7289 (raw turns 0.45-0.57 → 0.4519-0.5723, consistency) |
| 2.1 | overlap fractions "0.27, 0.16, 0.14, 0.05" | 0.2741991, 0.1578987, 0.1357204 and 0.0457581 (docs/evals.md); 0.16, 0.14 and 0.05 were rounded up |
| 4 | "max RSS 0.90-1.62 GB" | 898-1621 MB (lowest 898,580,480 bytes) |
| 2.1, 4 | "92.3 min" | 92.2 min (5536.54 s) |
| 4 | load average "1.9 to 19.3" | 1.94 to 19.26 |
| 4 | model load "0.953-1.344 s" | 0.953-1.343 s (highest 1.34397 s) |
| 4 | AMI RTF "0.215-0.306" and "0.217-0.341" | 0.2151-0.3055 and 0.2165-0.3405 |
| 4 | ASR share "54.9-68.5 %" | 54.8-68.4 % |
| 4 | EN2002a no-hint peak "1523 MB"; oracle "84 MB" | 1522 MB (1,522,548,736 bytes); 83 MB (83,623,936 bytes) |
| 8.2 | "14-17.5 min" meetings | 13.9-17.4 min (838.83 s and 1049.35 s) |
| 8.3 | compression ratio "2.07" | 2.0674 (avg logprob −0.28 → −0.2848, consistency) |
| 8.3, 9 | WER spread "0.0299", DER spread "0.0115", macro "at most 0.0075" | 0.0298 (0.029874), 0.0114 (0.011486), 0.0074 (0.0074685) |

Three more, found by a fact-check on 28 Sep 2026:

| section | was | now |
|---|---|---|
| intro | "Everything is reproduced by `scripts/run-v5.sh`" | inference, scoring, gates and the summary tables; the diagnostics in sections 8.1, 8.3 and 8.4, including the EN2002a table in 8.3, used scratch scripts that are not in the repository |
| headline | "Every other meeting repeated exactly." | only the ASR words were compared on the other meetings; AMI diarization was never repeated (section 8.3) |
| 10 | "Every table in this document is rendered from `summary.json`" | the results tables of sections 4 and 6; the EN2002a table in section 8.3 is not |

Added on 25 Sep, not a correction: the meeteval-free cpWER for the no-hint EN2002a
run (8570/7632 = 1.1229, section 8.1), and the rounding convention in section 5.
