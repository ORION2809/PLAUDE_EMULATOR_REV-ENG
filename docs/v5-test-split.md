# V5 on the full AMI test split (2026-09-28)

**Read this first.** The V5 target is missed by a wide margin. On all 16 meetings
of the standard AMI full-corpus test split (9.06 h, 90 149 reference words, headset
mix), the realistic configuration of `whisper-sherpa` (no speaker count given,
clustering threshold and word-assignment tie-break chosen on the AMI dev split)
scores **macro DER 0.6472 and cpWER 0.8428** against the target's 0.20 and 0.30
(`evals/gates.yaml` `ami-headset`: FAIL). Given the reference speaker count it
scores DER 0.6396 and cpWER 0.8567. The speech recogniser alone, blind to
speakers, gets a WER of 0.3221. The dominant error is diarization: sherpa confuses
speakers for 0.34 of the reference time, and scoring word runs instead of turns
raises missed speech from 0.07 to 0.26. The numbers come from `scripts/run-v5.sh`
(`build/v5-test/`), except the `embedding-cluster` calibration and hypotheses (§7,
`scripts/calibrate-embedding-cluster.py`, `build/v5-calib/`). Every threshold is
HARNESS_POLICY, and no threshold or setting was tuned on the test split; the
`embedding-cluster` lead below was singled out after its test DER was seen.

**Update, 29 Sep: a better system, still short of the target.** A diarizer built on
pretrained ECAPA speaker embeddings (`embedding-cluster`, §7, no ASR) scores DER 0.3708
when told the speaker count, against 0.5192 for sherpa's own turns, but without a hint
its speaker count collapses to one on half the meetings. `whisper-sherpa-ecapa` (§7.1)
takes the count from sherpa-onnx and the turns from ECAPA; chosen on the dev split, it
scores **macro DER 0.4104 and cpWER 0.5132** on the test split without a hint (0.3965
and 0.4821 with it). `ami-headset` still fails.

**Update, 29–30 Sep: pyannote.** With the gated pyannote models accessible (§7.2),
`faster-whisper+pyannote` scores **cpWER 0.3477** (DER 0.3140 on its word runs), and
pyannote's own turns score **DER 0.1295**, under the target's 0.20. cpWER still misses
0.30; what remains is mostly the transcript (WER 0.3221). pyannote's segmentation model
was trained on data that includes AMI (§7.2), so these numbers may be optimistic.

This extends [`docs/v5-results.md`](v5-results.md) (25 Sep), which measured the same
stack on 4 of these 16 meetings. What is new since then:

* the other 12 test meetings (CC BY 4.0, fetched from the AMI mirror, sha256 pinned
  in `scripts/run-v5.sh`);
* the clustering threshold and the tie-break were calibrated on the 18 AMI **dev**
  meetings ([`docs/pipeline.md`](pipeline.md) §11.8, `build/v5-calib/`);
* speech recognition is repeatable (seeded) and shared between systems through a
  transcript cache, so all whisper-sherpa systems score the same 68 632 words;
* `evals` keeps DER/JER/WER for meetings where meeteval refuses cpWER (more than 20
  speakers), so the default configuration is scored at all.

Headline, macro mean over the 16 meetings (evals defaults: DER collar 0.25 total
width, overlap scored; cpWER/tcpWER meeteval 0.4.3, tcpWER collar 5 s):

| system | DER | miss | false alarm | confusion | JER | cpWER | tcpWER | WER, speaker-agnostic | mean speaker-count error |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| whisper-sherpa, calibrated, no hint | 0.6472 | 0.2638 | 0.0414 | 0.3421 | 0.7508 | 0.8428 | 0.9628 | 0.3221 | -0.31 |
| whisper-sherpa, hint | 0.6396 | 0.2622 | 0.0415 | 0.3359 | 0.7473 | 0.8567 | 0.9808 | 0.3221 | +0.00 |
| whisper-sherpa, defaults, no hint | 0.7632 | 0.2650 | 0.0397 | 0.4585 | 0.7600 | refused (16 of 16) | refused | 0.3221 | +51.25 |
| whisper-sherpa, hint, tie-break latest_start (re-assigned) | 0.6412 | 0.2639 | 0.0414 | 0.3359 | 0.7394 | 0.8279 | 0.9576 | 0.3221 | +0.00 |
| whisper-sherpa, calibrated threshold, tie-break floor (re-assigned) | 0.6442 | 0.2620 | 0.0415 | 0.3407 | 0.7573 | 0.8697 | 0.9831 | 0.3221 | -0.31 |
| sherpa's own turns, calibrated (DER/JER only) | 0.5263 | 0.0683 | 0.0337 | 0.4244 | 0.6949 | n/a | n/a | n/a | -0.31 |
| sherpa's own turns, hint (DER/JER only) | 0.5192 | 0.0684 | 0.0338 | 0.4170 | 0.6779 | n/a | n/a | n/a | +0.00 |
| sherpa's own turns, defaults (DER/JER only) | 0.6957 | 0.0695 | 0.0372 | 0.5890 | 0.7137 | n/a | n/a | n/a | +59.50 |
| energy-vad-cluster, no hint (no ASR) | 0.5871 | 0.2030 | 0.0598 | 0.3243 | 0.6842 | n/a | n/a | n/a | +0.94 |
| energy-vad-cluster, hint (no ASR) | 0.5788 | 0.2025 | 0.0600 | 0.3162 | 0.6891 | n/a | n/a | n/a | +0.00 |
| embedding-cluster, calibrated, no hint (no ASR; §7) | 0.4967 | 0.1968 | 0.0609 | 0.2390 | 0.6506 | n/a | n/a | n/a | -1.50 |
| embedding-cluster, hint (no ASR; §7) | 0.3708 | 0.1988 | 0.0600 | 0.1120 | 0.4548 | n/a | n/a | n/a | +0.00 |
| embedding-cluster, defaults, no hint (no ASR; §7) | 0.5021 | 0.1966 | 0.0609 | 0.2447 | 0.6591 | n/a | n/a | n/a | -1.56 |
| **whisper-sherpa-ecapa, no hint (§7.1, 29 Sep)** | **0.4104** | 0.2624 | 0.0410 | 0.1071 | 0.5391 | **0.5132** | 0.5297 | 0.3221 | -0.31 |
| whisper-sherpa-ecapa, hint (§7.1) | 0.3965 | 0.2631 | 0.0409 | 0.0925 | 0.4968 | 0.4821 | 0.4994 | 0.3221 | +0.00 |
| **faster-whisper+pyannote, no hint (§7.2)** | 0.3140 | 0.2610 | 0.0409 | 0.0121 | 0.3358 | **0.3477** | 0.3562 | 0.3221 | +0.25 |
| **pyannote-audio, no hint (no ASR; §7.2)** | **0.1295** | 0.0755 | 0.0245 | 0.0295 | 0.1773 | n/a | n/a | n/a | +0.25 |
| oracle (self-test) | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | +0.00 |

"Hint" means `num_speakers` = the meeting's number of active reference speakers
(4 on 15 meetings, 3 on EN2002c). It is an oracle value a recorder does not have.
Micro averages (pooled over meetings) are within 0.013 of the macro ones for every
whisper-sherpa system; all are in `build/v5-test/summary.md`.

## 1. What was run

| label | how | what it is |
|---|---|---|
| whisper-sherpa, calibrated | `whisper-sherpa --param cluster_threshold=1.15 --param assignment_tie_break=latest_start` | the system under test in its realistic form: no speaker count, both settings chosen on the dev split |
| whisper-sherpa, hint | `whisper-sherpa --param num_speakers=N` | the 25 Sep configuration: default threshold (0.5) and tie-break (`floor`), oracle speaker count |
| whisper-sherpa, defaults | `whisper-sherpa` | every default, no hint: sherpa-onnx's own threshold 0.5 |
| re-assigned variants | `V5_REASSIGN` (no model run) | the hint run with `latest_start`, and the calibrated run with `floor`: the same words and turns, another tie-break, so the tie-break's effect is isolated. The derive stage first re-assigns each source with its own rule and checks that this reproduces it exactly |
| sherpa's own turns | derived | `hyp.extra.diarization.turns`, before word assignment; diarization only |
| word runs, no text | derived | each whisper-sherpa hypothesis's segments without text; their DER/JER equal the full hypothesis's on all 16 meetings for all three systems (checked by the summary) |
| energy-vad-cluster | model-free baseline | energy VAD + MFCC/f0 cells + spectral clustering; no ASR |
| faster-whisper+pyannote, pyannote-audio (no hint) | `faster-whisper+pyannote --param diarization_device=mps`, `pyannote-audio --param device=mps` (§7.2), `.venv-pyannote`, `$HF_TOKEN` | pyannote.audio 4.0.7 `speaker-diarization-community-1`; the first with the whisper-sherpa transcript on pyannote's exclusive turns, the second pyannote's overlap-aware turns (no ASR) |
| whisper-sherpa-ecapa (no hint, hint) | `whisper-sherpa-ecapa` (§7.1), `.venv-pyannote` | the whisper-sherpa transcript with turns from ECAPA embeddings cut into the number of speakers sherpa-onnx finds at `cluster_threshold` 1.15 |
| embedding-cluster (defaults, hint, calibrated) | `scripts/calibrate-embedding-cluster.py --test-root` (§7) | the same VAD and cells with SpeechBrain ECAPA embeddings and cosine clustering; no ASR. Replayed from one embedding pass per meeting; the replay equals real `embedding-cluster` runs on all three paths (§7) |
| oracle | self-test | the reference itself; scores exactly 0 on all 16 meetings |

Models: faster-whisper 1.2.1 `small.en` (int8, beam 5, English, seed 0), sherpa-onnx
1.13.8 with pyannote segmentation-3.0 and 3D-Speaker CAM++ (English, VoxCeleb), all
pinned by sha256 in `pipeline/model_store.py` (licences and training-data caveats in
[`docs/v5-results.md`](v5-results.md) §3).

## 2. Data

The BUT AMI setup's full-corpus test list (`data/corpora/ami/but_setup/lists/test.meetings.txt`):
16 meetings, 9.06 h. References by `python -m evals.ami convert` (converter
version 2, `--truncated keep`) into `data/corpora/ami/test/`; audio is each
meeting's `Mix-Headset.wav` (16 kHz mono), a sum of close-talking headset
microphones, not a far-field recording. Sizes equal the server's Content-Length;
sha256 were computed on download and are pinned in `scripts/run-v5.sh`.
AMI is CC BY 4.0 (AMI Consortium; J. Carletta et al., MLMI 2006).

| meeting | duration s | active speakers | reference words | bytes | sha256 |
|---|---:|---:|---:|---:|---|
| EN2002a | 2142.709 | 4 | 7632 | 68566744 | `a15017d0d3c871866971ee178052a0ce3a7e44d6fbf89fc23dcab326511b65f5` |
| EN2002b | 1786.848 | 4 | 6074 | 57179180 | `50e2c8e4737850c4bf3cc24e7087c755e1c733cd91822106afd0df485ba64893` |
| EN2002c | 2972.256 | 3 | 10752 | 95112236 | `a057708b3adb553f709fcde97d2af8e0455076bc3f88c250975227433d6e0270` |
| EN2002d | 2209.899 | 4 | 7843 | 70716802 | `6aa99d9d71cb695ea699d6be277f78eff68e847c93560031486eba9f18164ebe` |
| ES2004a | 1049.355 | 4 | 2653 | 33579394 | `3e2560b19bee6952c7c7ce041b0f1ea8a7ea9468044c4eea79d2a2c67e24ab0f` |
| ES2004b | 2345.493 | 4 | 6874 | 75055832 | `ad0cf07c42b1694ccf7bea8f1a37348d9cfab194787abc7d050b29ba56e365c8` |
| ES2004c | 2334.368 | 4 | 7084 | 74699820 | `47dc9bd2e3b1d174842091ae9e1fe765218f0c87658b17d6469c404e03ec2ac5` |
| ES2004d | 2222.291 | 4 | 6204 | 71113346 | `3cfce3297ebb7faeb53c02727eea5e9f2c76e9ed4b62db3b03d73e66836e91f3` |
| IS1009a | 838.833 | 4 | 2018 | 26844093 | `6eb5a0ede0d9e72794f976ce7bea5b78133eae969f99b4c5418b43c2468d25b1` |
| IS1009b | 2052.333 | 4 | 6199 | 65676093 | `07c2891ae6ad7c507b2a4f15b2dcd0a2343491df9e4e6a58f9db0d0b2c5e1382` |
| IS1009c | 1820.833 | 4 | 4574 | 58268093 | `f317910070db0f2649315decbb34686d648bbe9dc3f7576710184f4d572bdbcc` |
| IS1009d | 1944.500 | 4 | 4813 | 62225427 | `470440114ee2ed076cfc77c53136ad5d3af500d8a5a6ca36755e83534f5ca4ea` |
| TS3003a | 1505.643 | 4 | 2534 | 48180608 | `b6ce59ff735c234afdd94b13744aa26f6785af746170543fd03b33160e920a6d` |
| TS3003b | 2210.304 | 4 | 4888 | 70729772 | `0c94f3a09ab747caa7714efe8852f5ff37d36cf272b75709344991df1aa266ca` |
| TS3003c | 2570.000 | 4 | 4648 | 82240044 | `b9f455870e8cb0ba765523a1767cc366775ab7e5e3eabf37d9c91289e5a1a70a` |
| TS3003d | 2618.200 | 4 | 5359 | 83782444 | `ece4a41c37c1092c737d1dfe313afb18248364625991f745fed4b372c3c67eb3` |

## 3. Calibration on the dev split

Two settings were chosen on the 18 AMI dev meetings (9.67 h), never on test:

* **`cluster_threshold` = 1.15**, the lowest macro DER without a hint over a
  25-point grid (0.800–1.400). sherpa-onnx's default, 0.5, is off the chart: at 0.8
  the dev meetings already get 12–36 speakers. The sweep replays sherpa-onnx from
  cached segmentation and embeddings (`pipeline/sherpa_sweep.py`), bit-identical to
  real sherpa-onnx at every threshold checked: 0.7, 0.9, 0.95, 1.1 and 1.3 on IS1008a,
  and the selected 1.15 on IS1008a and TS3004a.
* **`assignment_tie_break` = `latest_start`**: the dev reference words assigned to
  the 1.15 turns, macro cpWER 0.9344 against 1.0075 for `floor`; 16.1 % of words were
  ties.

Details and every grid point: [`docs/pipeline.md`](pipeline.md) §11.8 and
`build/v5-calib/` (`summary.md`, `tiebreak.md`, `selected.json`, `validation/`).

## 4. How it was run, and what the timing means

One `python -m pipeline run` process per (system, meeting), from
`scripts/run-v5.sh` with `V5_AMI_ROOT=data/corpora/ami/test`, the 16 meeting ids
and `V5_ASR_CACHE=build/v5-test/asr-cache`. The default-configuration run decoded
each meeting and filled the cache; the hint and calibrated runs took the transcript
from it and ran only diarization. The three systems' words are identical on all 16
meetings (68 632 words; `summary.json` "asr_words_identical_across_hint_runs"). 79 of 9 221 decoded
segments (0.86 %, in 7 meetings) needed faster-whisper's sampled temperature
fallback; decoding is seeded, and a fresh process reproduced EN2002a's 5 120 words
exactly ([`docs/pipeline.md`](pipeline.md) §11.8).

**Timing is not a speed measurement.** Inference ran on 28 Sep from about 07:25 to
16:35 UTC on the 8 GB M1, with up to four heavy jobs at once (a second V5 instance,
the dev calibration, and for about 15 minutes the Android emulator) and heavy
swapping. The runs that decoded speech had real-time factors from 0.319 to 1.145;
diarization-only runs from 0.160 to 0.682. Peak memory of one process was 1 842 MB
(EN2002c, the 49.5-minute meeting). Per-run wall time, memory and load averages are
in `build/v5-test/logs/` and `summary.md`.

## 5. Results per meeting

| meeting | min | spk | calibrated: DER | cpWER | speakers | hint: DER | cpWER | defaults: DER | speakers | WER | sherpa turns, calibrated: DER | energy-vad-cluster: DER |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| EN2002a | 35.7 | 4 | 0.7533 | 0.9256 | 3 | 0.7286 | 0.9674 | 0.8323 | 79 | 0.4384 | 0.5822 | 0.5537 |
| EN2002b | 29.8 | 4 | 0.7685 | 0.8765 | 3 | 0.7650 | 0.9195 | 0.8675 | 57 | 0.4555 | 0.6007 | 0.5693 |
| EN2002c | 49.5 | 3 | 0.6864 | 0.9377 | 4 | 0.6794 | 0.9179 | 0.8268 | 84 | 0.3887 | 0.5250 | 0.5786 |
| EN2002d | 36.8 | 4 | 0.7426 | 0.8795 | 4 | 0.7301 | 0.9643 | 0.8345 | 95 | 0.4224 | 0.5367 | 0.6331 |
| ES2004a | 17.5 | 4 | 0.6834 | 0.8922 | 3 | 0.6649 | 0.9239 | 0.7231 | 31 | 0.2759 | 0.5724 | 0.7395 |
| ES2004b | 39.1 | 4 | 0.5197 | 0.7329 | 4 | 0.5173 | 0.7476 | 0.6383 | 46 | 0.2639 | 0.4131 | 0.5117 |
| ES2004c | 38.9 | 4 | 0.5038 | 0.7470 | 4 | 0.5017 | 0.7955 | 0.6203 | 48 | 0.2795 | 0.3861 | 0.5464 |
| ES2004d | 37.0 | 4 | 0.6691 | 0.8807 | 4 | 0.6633 | 0.9160 | 0.7993 | 61 | 0.3287 | 0.5702 | 0.5297 |
| IS1009a | 14.0 | 4 | 0.5581 | 0.6715 | 4 | 0.5444 | 0.6640 | 0.7550 | 34 | 0.3266 | 0.4519 | 0.6604 |
| IS1009b | 34.2 | 4 | 0.5128 | 0.8130 | 4 | 0.5214 | 0.8387 | 0.6800 | 50 | 0.2763 | 0.4441 | 0.5188 |
| IS1009c | 30.3 | 4 | 0.6622 | 0.9910 | 3 | 0.5731 | 0.9080 | 0.7644 | 39 | 0.2444 | 0.5695 | 0.4577 |
| IS1009d | 32.4 | 4 | 0.6466 | 0.8165 | 4 | 0.6454 | 0.8581 | 0.7807 | 51 | 0.2695 | 0.5535 | 0.5897 |
| TS3003a | 25.1 | 4 | 0.6374 | 0.9073 | 2 | 0.6846 | 0.8646 | 0.8498 | 41 | 0.2605 | 0.5107 | 0.6246 |
| TS3003b | 36.8 | 4 | 0.6436 | 0.8014 | 4 | 0.6465 | 0.8034 | 0.7084 | 50 | 0.3038 | 0.5193 | 0.5325 |
| TS3003c | 42.8 | 4 | 0.6391 | 0.8008 | 4 | 0.6401 | 0.8094 | 0.6923 | 42 | 0.3049 | 0.5477 | 0.6750 |
| TS3003d | 43.6 | 4 | 0.7290 | 0.8110 | 4 | 0.7271 | 0.8085 | 0.8381 | 75 | 0.3154 | 0.6382 | 0.6732 |

"min" is the meeting length in minutes; "spk" its active reference speakers;
"speakers" the hypothesis's. WER is speaker-agnostic and identical for all three
whisper-sherpa systems.

* **Calibration fixes the speaker count, not the attribution.** Without it the
  diarizer finds 35–108 clusters per meeting (mean error +59.50), 31–95 of which
  receive words (mean error +51.25), and meeteval refuses every cpWER. With it: 2–4 speakers, mean error −0.31, and DER within
  0.008 of the hinted run. Ten meetings get the right count, five too few (3 for
  4 on EN2002a, EN2002b, ES2004a and IS1009c; 2 for 4 on TS3003a) and one too many
  (4 for 3 on EN2002c).
* **The tie-break is worth about 0.03 cpWER** on test, in the direction the dev proxy
  predicted but less than half its size (0.07 there): `latest_start` lowers cpWER
  from 0.8697 to 0.8428 on the calibrated turns and from 0.8567 to 0.8279 on the
  hinted ones, and moves DER by at most 0.003.
* **Diarization dominates.** Confusion is 0.34 of the reference time for the word
  runs and 0.42 for sherpa's own turns. Word runs also miss 0.26 of the speech
  (pauses inside turns, overlapped speech Whisper did not transcribe) against 0.07
  for the turns, which is why sherpa's own turns score a lower DER (0.5263 calibrated)
  than the words attributed to them.
* **The model-free baseline beats the words' DER.** energy-vad-cluster scores
  0.5871 without a hint, lower than every whisper-sherpa word-run DER, because it
  scores whole speech regions; it has no words at all.
* **The meeting series differ.** EN2002 (three or four people, with the most overlap) is the
  hardest: WER 0.39–0.46 and cpWER 0.88–0.94 calibrated. ES2004b/c and IS1009b are
  the easiest, near DER 0.51.

**Variants** (`build/v5-test/reports/ami/*.collar0.json`, `*.norm.json`): at DER
collar 0 every standard report's DER rises by 0.021–0.031 for the 28 Sep systems and
the ECAPA turn views (calibrated 0.6752; `embedding-cluster` with the hint 0.031; the
supplementary lifted-guard defaults run by 0.032), and by 0.044–0.045 for
`whisper-sherpa-ecapa` and its word-run views (0.4542; hint 0.4411). Normalising numbers and
contractions lowers WER to 0.3075 and the calibrated cpWER to 0.8342 (hint 0.8487).
The defaults run scored with meeteval's speaker guard lifted (supplementary): meeteval
then fails on 6 meetings for another reason ("too few fallback keys", more than about
56 hypothesis speakers), and the other 10 score cpWER 1.0907 macro.

## 6. Against the 25 Sep subset

The 25 Sep run measured the hinted system on EN2002a, ES2004a, IS1009a and TS3003a:
macro DER 0.6557, cpWER 0.8533. On all 16 meetings the same configuration gives
0.6396 and 0.8567, so the subset was representative within about 0.02. Three of
the four meetings reproduce 25 Sep's DER exactly; EN2002a changed from 0.7289 to
0.7286 because its transcript now comes from seeded decoding (its 25 Sep words were
from one of three different unseeded runs).

## 7. embedding-cluster: pretrained speaker embeddings

`embedding-cluster` is the harness's own VAD and 0.5 s cells, each embedded from a
1.5 s window, with SpeechBrain's ECAPA speaker embeddings (`spkrec-ecapa-voxceleb`, trained on VoxCeleb, ungated) and the
model-free spectral clustering, on cosine distance. It has no ASR, so it gives DER
and JER only. It ran in a separate environment (torch 2.14.0, SpeechBrain 1.1.1,
`.venv-torch`), because the main one has no torch.

**Speed, and one fix.** With torch 2.14 on this M1, PyTorch's NNPACK convolution
took 114.94 s per ECAPA batch against 3.39 s without it, so the adapter now turns
NNPACK off on CPU (`nnpack: false` in the run's settings). Real runs then took RTF
0.057 (EN2002a), 0.063 (EN2002b) and 0.155 (EN2002c) with other jobs running, and
0.031 (IS1009a) and 0.032 (EN2002b) on 29 Sep with the machine otherwise idle; peak
memory 1.0–1.5 GB.

**Replay.** As for sherpa (§3), the embeddings do not depend on the clustering
threshold, so `scripts/calibrate-embedding-cluster.py` embeds each meeting once
and replays the clustering, smoothing and relabelling per threshold and per hint.
The replay's turns are identical to real `embedding-cluster` runs on every path
used here:

* the default threshold, no hint: IS1008a (dev, 4 speakers) and EN2002a, EN2002b,
  EN2002c (test, 1 speaker each) (`validation/`, `validation-test/real/`,
  `test-replay.json`);
* the hint: IS1009a, `num_speakers=4` (243 turns, 4 speakers);
* the calibrated threshold: EN2002b, `distance_threshold=0.55` (493 turns,
  2 speakers, where the default gives 1)

(`build/v5-calib/embedding-cluster/validation-test/hint-and-calibrated-check.json`).

**Calibration on the dev split.** The threshold caps the eigengap's speaker count
by the number of average-linkage clusters that reach the minimum size. It was swept
from 0.100 to 1.200 in steps of 0.025 over the 18 dev meetings, no hint. (A first
grid, 0.100–0.600, was stopped before it finished and replaced, after an unarchived
check on three dev meetings suggested the cap acts up to about 1.0. Restricted to
0.100–0.600, the full results would also select 0.55.) Macro DER, collar 0.25:

| threshold | macro DER | macro JER | confusion | mean speaker-count error | hypothesis speakers per meeting |
|---:|---:|---:|---:|---:|---|
| 0.100–0.375 | 0.7037 | 0.9031 | 0.4696 | −3.00 | 1 |
| 0.500 (default) | 0.5409 | 0.6866 | 0.3069 | −1.67 | 1–5 |
| **0.550 (selected)** | **0.5295** | 0.6554 | 0.2954 | −1.44 | 1–5 |
| 0.550–0.825 | 0.5295 | 0.6554 | 0.2954 | −1.44 | 1–5 |
| 0.900 | 0.5705 | 0.7101 | 0.3364 | −1.67 | 1–5 |
| 1.000–1.200 | 0.7037 | 0.9031 | 0.4696 | −3.00 | 1 |

Every grid point is in `build/v5-calib/embedding-cluster/selected.json`. The
selection rule (lowest DER, then the smaller speaker-count error, then the lower
threshold) picks 0.55 from the plateau.

**Test split** (`build/v5-test/reports/ami/embedding-cluster*.json`):

| meeting | reference speakers | calibrated: DER | speakers | hint: DER | sherpa's turns, calibrated: DER | sherpa's turns, hint: DER |
|---|---:|---:|---:|---:|---:|---:|
| EN2002a | 4 | 0.6715 | 1 | 0.3267 | 0.5822 | 0.5650 |
| EN2002b | 4 | 0.5557 | 2 | 0.4104 | 0.6007 | 0.5870 |
| EN2002c | 3 | 0.6739 | 1 | 0.3115 | 0.5250 | 0.5091 |
| EN2002d | 4 | 0.7083 | 1 | 0.4392 | 0.5367 | 0.5367 |
| ES2004a | 4 | 0.7451 | 1 | 0.4336 | 0.5724 | 0.5403 |
| ES2004b | 4 | 0.2261 | 5 | 0.4649 | 0.4131 | 0.4131 |
| ES2004c | 4 | 0.2050 | 5 | 0.3711 | 0.3861 | 0.3861 |
| ES2004d | 4 | 0.5350 | 3 | 0.3351 | 0.5702 | 0.5702 |
| IS1009a | 4 | 0.5263 | 1 | 0.3376 | 0.4519 | 0.4519 |
| IS1009b | 4 | 0.1913 | 5 | 0.4125 | 0.4441 | 0.4441 |
| IS1009c | 4 | 0.1568 | 5 | 0.1561 | 0.5695 | 0.4731 |
| IS1009d | 4 | 0.6610 | 1 | 0.2885 | 0.5535 | 0.5535 |
| TS3003a | 4 | 0.3689 | 1 | 0.5146 | 0.5107 | 0.5723 |
| TS3003b | 4 | 0.6836 | 1 | 0.3141 | 0.5193 | 0.5193 |
| TS3003c | 4 | 0.3974 | 4 | 0.3974 | 0.5477 | 0.5477 |
| TS3003d | 4 | 0.6407 | 2 | 0.4200 | 0.6382 | 0.6382 |

* **Given the speaker count, it attributes speech far better than sherpa.** Hinted
  macro DER 0.3708 against 0.5192 for sherpa's own hinted turns, lower on 15 of 16
  meetings; confusion 0.112 of the reference time against 0.417. The hint was
  honoured on all 16.
* **Without a hint, its speaker count is the weak part.** At the calibrated
  threshold it finds a single speaker on 8 of 16 meetings (speaker-count error
  −3 on 7 of them), 2–3 on 3 and 4–5 on 5. Where it finds 4–5 speakers its DER
  is 0.16–0.40; where it collapses to one, 0.37–0.75. The macro DER, 0.4967,
  is still below sherpa's calibrated turns (0.5263) and the model-free baseline
  (0.5871), but that average mixes the two regimes.
* **The calibration hardly moved the test result.** 0.55 and the default 0.5 give
  the same hypotheses on 15 of 16 meetings; only EN2002b changes (1 → 2 speakers,
  DER 0.6434 → 0.5557). Macro DER 0.5021 → 0.4967.
* **Missed speech is the VAD's.** Miss is 0.197 of the reference time, as for
  energy-vad-cluster (0.203): both use the same energy VAD.
* **It has no words.** Its turns are not attached to a transcript, so there is no
  cpWER for it. Assigning the whisper-sherpa transcript to these turns would give
  one without re-running a model (as `V5_REASSIGN` does for tie-breaks). Because this
  system was chosen after seeing its test DER, that was first justified on the dev
  split and then run on test as `whisper-sherpa-ecapa` (§7.1).

### 7.1 Sherpa's count with ECAPA's turns: `whisper-sherpa-ecapa`

The two diarizers fail in opposite ways: calibrated sherpa-onnx counts well (10 of 16
meetings right) but attributes poorly, and the ECAPA clustering attributes well when
told the count but counts poorly. `whisper-sherpa-ecapa` combines them: sherpa-onnx at
`cluster_threshold` 1.15 gives the number of speakers, the ECAPA clustering cuts its
turns into that many, and the whisper-sherpa words are assigned to those turns
([`docs/pipeline.md`](pipeline.md) §11.10).

**Chosen on the dev split.** The idea came from §7's test results, so it was checked on
the 18 dev meetings first, without a hint (`scripts/check-sherpa-ecapa-dev.py`,
`build/v5-calib/whisper-sherpa-ecapa/`): DER 0.4314 against 0.5295 (ECAPA alone) and
0.5431 (sherpa alone), and a cpWER of 0.6145 for the dev reference words attributed to
its turns, against 0.9344 for sherpa's. Its settings (threshold 1.15, tie-break
`floor`) come from the dev split; nothing was tuned on test.

**Test split (real runs; `build/v5-test/reports/ami/whisper-sherpa-ecapa*.json`):**

| meeting | speakers (reference / found) | DER | cpWER | hint: DER | hint: cpWER | whisper-sherpa calibrated: DER | cpWER |
|---|---|---:|---:|---:|---:|---:|---:|
| EN2002a | 4 / 3 | 0.5563 | 0.6351 | 0.4637 | 0.4475 | 0.7533 | 0.9256 |
| EN2002b | 4 / 3 | 0.5378 | 0.5473 | 0.5131 | 0.5063 | 0.7685 | 0.8765 |
| EN2002c | 3 / 4 | 0.4232 | 0.4157 | 0.4019 | 0.3720 | 0.6864 | 0.9377 |
| EN2002d | 4 / 4 | 0.5306 | 0.5243 | 0.5306 | 0.5243 | 0.7426 | 0.8795 |
| ES2004a | 4 / 3 | 0.5119 | 0.6242 | 0.4125 | 0.4323 | 0.6834 | 0.8922 |
| ES2004b | 4 / 4 | 0.4552 | 0.7070 | 0.4552 | 0.7070 | 0.5197 | 0.7329 |
| ES2004c | 4 / 4 | 0.3912 | 0.6230 | 0.3912 | 0.6230 | 0.5038 | 0.7470 |
| ES2004d | 4 / 4 | 0.3406 | 0.4350 | 0.3406 | 0.4350 | 0.6691 | 0.8807 |
| IS1009a | 4 / 4 | 0.3186 | 0.3860 | 0.3186 | 0.3860 | 0.5581 | 0.6715 |
| IS1009b | 4 / 4 | 0.4070 | 0.6416 | 0.4070 | 0.6416 | 0.5128 | 0.8130 |
| IS1009c | 4 / 3 | 0.2938 | 0.5068 | 0.1633 | 0.2584 | 0.6622 | 0.9910 |
| IS1009d | 4 / 4 | 0.3284 | 0.3740 | 0.3284 | 0.3740 | 0.6466 | 0.8165 |
| TS3003a | 4 / 2 | 0.3797 | 0.3410 | 0.5247 | 0.5568 | 0.6374 | 0.9073 |
| TS3003b | 4 / 4 | 0.3221 | 0.4515 | 0.3221 | 0.4515 | 0.6436 | 0.8014 |
| TS3003c | 4 / 4 | 0.3537 | 0.5017 | 0.3537 | 0.5017 | 0.6391 | 0.8008 |
| TS3003d | 4 / 4 | 0.4168 | 0.4964 | 0.4168 | 0.4964 | 0.7290 | 0.8110 |

* **cpWER is lower on all 16 meetings** than calibrated whisper-sherpa's; macro 0.5132
  against 0.8428. The words are identical (WER 0.3221); only their speakers changed.
  Speaker confusion falls from 0.342 to 0.107 of the reference time.
* **Knowing the count adds little.** With the hint, DER 0.3965 and cpWER 0.4821; where
  sherpa's count is right (10 meetings) the two runs are identical. On TS3003a the
  wrong count (2 for 4) happens to score better than the hint.
* **What is left is mostly ASR and missed speech.** The transcript's WER is 0.3221,
  and cpWER stays well above it (macro 0.4821 with the hint). Speaker-agnostic WER is
  not a strict floor: concatenation also charges for word order in overlapped speech,
  so on EN2002c with the hint cpWER is 0.3720 against WER 0.3887. Missed speech is 0.26
  of the reference time, from word runs as in §5. Scoring the ECAPA turns themselves
  gives DER 0.3896.
* **Real runs equal the replay** on all 32 runs (with and without the hint). The 16
  no-hint runs decoded the speech afresh and reproduced the cached transcripts word for
  word, at RTF 0.26–0.38 on the 8 GB M1 (sherpa's counting pass and the ASR). The EN2002a
  and EN2002b hint runs of a first, stopped attempt (29 Sep) also decoded afresh and
  matched, but they were replaced and are not kept. The 16 hinted runs were repeated on
  30 Sep from the cached transcript, so that they record the hint (`num_speakers_hint`,
  `hint_honoured`), identical, at RTF 0.023–0.035.

### 7.2 pyannote: `pyannote-audio` and `faster-whisper+pyannote`

With the owner's Hugging Face account, pyannote's gated pipelines ran on 29 Sep:
pyannote.audio 4.0.7 with `pyannote/speaker-diarization-community-1` (CC BY 4.0,
revision `3533c8cf`), on the M1's GPU (MPS). The token was read from the
environment, telemetry was off, and the adapters recorded the model snapshot's file
hashes ([`docs/pipeline.md`](pipeline.md) §11.11). `faster-whisper+pyannote` took
its transcript from the shared cache (same 68 632 words) and assigned the words to
pyannote's *exclusive* turns; `pyannote-audio` scores pyannote's overlap-aware turns.
Nothing was tuned: both use the library's defaults.

| meeting | reference / found speakers | pyannote-audio: DER | DER, collar 0 | faster-whisper+pyannote: DER | cpWER | WER | whisper-sherpa-ecapa: cpWER |
|---|---|---:|---:|---:|---:|---:|---:|
| EN2002a | 4 / 4 | 0.1780 | 0.2190 | 0.4612 | 0.4644 | 0.4384 | 0.6351 |
| EN2002b | 4 / 4 | 0.1723 | 0.2074 | 0.4712 | 0.4806 | 0.4555 | 0.5473 |
| EN2002c | 3 / 3 | 0.1542 | 0.1754 | 0.4026 | 0.3989 | 0.3887 | 0.4157 |
| EN2002d | 4 / 4 | 0.2008 | 0.2322 | 0.4801 | 0.4651 | 0.4224 | 0.5243 |
| ES2004a | 4 / 5 | 0.1438 | 0.1876 | 0.3512 | 0.2963 | 0.2759 | 0.6242 |
| ES2004b | 4 / 5 | 0.0867 | 0.1262 | 0.2464 | 0.2773 | 0.2639 | 0.7070 |
| ES2004c | 4 / 5 | 0.0846 | 0.1240 | 0.2336 | 0.2931 | 0.2795 | 0.6230 |
| ES2004d | 4 / 4 | 0.1400 | 0.1878 | 0.3020 | 0.3770 | 0.3287 | 0.4350 |
| IS1009a | 4 / 4 | 0.1458 | 0.1969 | 0.2845 | 0.3717 | 0.3266 | 0.3860 |
| IS1009b | 4 / 5 | 0.1049 | 0.1519 | 0.2025 | 0.3141 | 0.2763 | 0.6416 |
| IS1009c | 4 / 4 | 0.0631 | 0.1002 | 0.1573 | 0.2567 | 0.2444 | 0.5068 |
| IS1009d | 4 / 4 | 0.1105 | 0.1642 | 0.2630 | 0.3036 | 0.2695 | 0.3740 |
| TS3003a | 4 / 4 | 0.1396 | 0.1714 | 0.3438 | 0.2822 | 0.2605 | 0.3410 |
| TS3003b | 4 / 4 | 0.0841 | 0.1198 | 0.2305 | 0.3169 | 0.3038 | 0.4515 |
| TS3003c | 4 / 4 | 0.1025 | 0.1340 | 0.2581 | 0.3160 | 0.3049 | 0.5017 |
| TS3003d | 4 / 4 | 0.1616 | 0.2108 | 0.3359 | 0.3497 | 0.3154 | 0.4964 |

* **pyannote's turns meet the DER half of the target.** Macro DER 0.1295 (≤ 0.20), and
  the speaker count is right on 12 of 16 meetings (one too many on the other 4). Its
  exclusive turns (one speaker at a time, the ones the words are assigned to) score
  0.1704.
* **Attribution is nearly solved; the transcript is not.** With the words attached,
  speaker confusion is 0.012 and cpWER 0.3477, just above the WER of the transcript
  itself (0.3221). cpWER is at or under 0.30 on 5 of 16 meetings. The word runs'
  missed speech (0.26) raises their DER to 0.3140, as for every word-level system here.
  `ami-headset` fails for `faster-whisper+pyannote` on both metrics.
* **An outside check of the scoring.** pyannote's model card reports 17.0 % DER for
  community-1 on AMI (IHM) with no forgiveness collar and overlap scored. Scored the
  same way here (collar 0), the pooled DER of `pyannote-audio` over the 16 test meetings
  is 17.07 % (macro 16.93 %).
* **Possibly optimistic.** The community-1 card reports AMI benchmarks but does not list
  its training data; its segmentation model is pyannote segmentation-3.0 (the card
  cites Plaquet & Bredin 2023 for it), which was trained on data that includes AMI.
  Whether these test meetings were held out cannot be checked here.
* **Speed.** On the GPU, RTF 0.18–0.26 for `pyannote-audio`; with the cached transcript,
  0.13–0.19 for `faster-whisper+pyannote`. On the CPU, one 14-minute meeting took RTF
  0.95.

## 8. Checks

* **Oracle** scores 0 on every metric on all 16 meetings.
* **NIST md-eval agrees with the harness's DER** (`scripts/crosscheck-md-eval.sh`,
  `build/v5-test/md-eval-crosscheck.json`): at collar 0, all 224 per-meeting DERs
  (14 systems and views × 16 meetings) are identical to md-eval's printed precision
  (1e-4); at collar 0.25, 213 of 224 are, and 11 differ by at most 0.0054 — 7 of
  them the over-clustered default turns. Because they agree everywhere at collar 0,
  the differences come from how each tool applies the collar, most likely where it
  meets the speaker mapping; this was not traced further.
* **Re-assignment is exact:** each re-assigned source reproduced itself with its own
  tie-break before the variant was written.
* **Word runs:** their DER/JER equal the full hypotheses' on all 16 meetings for all
  three whisper-sherpa systems.

## 9. Gates

* `ami-headset` (the V5 target: macro DER ≤ 0.20, cpWER ≤ 0.30): **FAIL** for the
  calibrated system (0.6472, 0.8428) and for the hinted one (0.6396, 0.8567)
  (`build/v5-test/gates/`).
* Five **regression** gates record these runs: `ami-test-whisper-sherpa-cal`,
  `ami-test-whisper-sherpa-hint` (28 Sep), `ami-test-whisper-sherpa-ecapa`,
  `ami-test-whisper-pyannote` and `ami-test-pyannote-audio` (29 Sep), each the
  measured macro value plus 0.03 (0.25 on the mean absolute speaker-count error),
  rounded up to 3 decimals. Passing them means "no worse than when measured", never
  "good". `ami-headset` also fails for `whisper-sherpa-ecapa` (0.4104, 0.5132) and
  `faster-whisper+pyannote` (0.3140, 0.3477). `tests/test_v5_gates.py`
  checks every threshold against this block:

```json v5-test-gate-calibration
{
  "schema": "plaud-harness/v5-gate-calibration/1",
  "margin_policy": "HARNESS_POLICY: measured macro value + 0.03 on every rate, + 0.25 on the mean absolute speaker-count error, rounded up to 3 decimals",
  "suites": {
    "ami-test-whisper-sherpa-cal": {
      "system": "whisper-sherpa-cal",
      "gate_on": "macro",
      "measured": {
        "der.der": 0.6472308523024526,
        "jer.jer": 0.7508014755964543,
        "cpwer.error_rate": 0.8427863693662574,
        "tcpwer.error_rate": 0.9627520722531847,
        "wer_concat.wer": 0.3221439520471201,
        "speaker_count.abs_error": 0.4375
      },
      "margin": {
        "der.der": 0.03,
        "jer.jer": 0.03,
        "cpwer.error_rate": 0.03,
        "tcpwer.error_rate": 0.03,
        "wer_concat.wer": 0.03,
        "speaker_count.abs_error": 0.25
      }
    },
    "ami-test-whisper-sherpa-ecapa": {
      "system": "whisper-sherpa-ecapa",
      "gate_on": "macro",
      "measured": {
        "der.der": 0.4104268812822428,
        "jer.jer": 0.5391012354890686,
        "cpwer.error_rate": 0.5131547568557172,
        "tcpwer.error_rate": 0.5297159970863285,
        "wer_concat.wer": 0.3221439520471201,
        "speaker_count.abs_error": 0.4375
      },
      "margin": {
        "der.der": 0.03,
        "jer.jer": 0.03,
        "cpwer.error_rate": 0.03,
        "tcpwer.error_rate": 0.03,
        "wer_concat.wer": 0.03,
        "speaker_count.abs_error": 0.25
      }
    },
    "ami-test-whisper-pyannote": {
      "system": "whisper-pyannote",
      "gate_on": "macro",
      "measured": {
        "der.der": 0.3139903292556576,
        "jer.jer": 0.33576801262124834,
        "cpwer.error_rate": 0.34770881374613366,
        "tcpwer.error_rate": 0.3562472071395135,
        "wer_concat.wer": 0.3221439520471201,
        "speaker_count.abs_error": 0.25
      },
      "margin": {
        "der.der": 0.03,
        "jer.jer": 0.03,
        "cpwer.error_rate": 0.03,
        "tcpwer.error_rate": 0.03,
        "wer_concat.wer": 0.03,
        "speaker_count.abs_error": 0.25
      }
    },
    "ami-test-pyannote-audio": {
      "system": "pyannote-audio",
      "gate_on": "macro",
      "measured": {
        "der.der": 0.1295394885928911,
        "jer.jer": 0.17728312211376873,
        "speaker_count.abs_error": 0.25
      },
      "margin": {
        "der.der": 0.03,
        "jer.jer": 0.03,
        "speaker_count.abs_error": 0.25
      }
    },
    "ami-test-whisper-sherpa-hint": {
      "system": "whisper-sherpa-hint",
      "gate_on": "macro",
      "measured": {
        "der.der": 0.6395526662080258,
        "jer.jer": 0.7473111445637105,
        "cpwer.error_rate": 0.8566687494957568,
        "tcpwer.error_rate": 0.9807515176743756,
        "wer_concat.wer": 0.3221439520471201,
        "speaker_count.abs_error": 0.0
      },
      "margin": {
        "der.der": 0.03,
        "jer.jer": 0.03,
        "cpwer.error_rate": 0.03,
        "tcpwer.error_rate": 0.03,
        "wer_concat.wer": 0.03,
        "speaker_count.abs_error": 0.25
      }
    }
  }
}
```

## 10. What these numbers mean, and what they do not

* **This is the whole standard test split,** measured with one ASR model
  (faster-whisper `small.en`) and several diarizers (sherpa-onnx with and without
  calibration and hint, energy-vad-cluster, embedding-cluster, whisper-sherpa-ecapa,
  pyannote community-1), on one machine. No confidence interval is given; per-meeting
  spread is large (calibrated `whisper-sherpa` DER 0.5038–0.7685).
* **One published number is compared at matching settings:** the community-1 model
  card's 17.0 % DER on AMI (IHM), collar 0 with overlap scored, against 17.07 % pooled
  here (§7.2). Other published AMI results are not comparable without reproducing their
  setup (microphone condition, collar, hypothesis form, model sizes, speaker-count
  knowledge).
* **Possible contamination.** pyannote segmentation-3.0, the segmentation model of both
  sherpa-onnx's diarizer here and community-1, was trained on data that includes AMI;
  Whisper's training data is unpublished. A better score would not prove
  generalisation.
* **The calibration used AMI dev meetings,** which are the same corpus and
  recording setup as test. A deployed recorder in another room with other people
  would need its own calibration data.
* **What carries over:** the stack runs end to end on 16 real meetings of 14–50
  minutes on an 8 GB M1, repeatably. For `faster-whisper+pyannote` the remaining error
  is mainly the transcript (speaker confusion 0.012, cpWER 0.3477 against WER 0.3221);
  for the sherpa-based systems it is speaker attribution.

## 11. Reproduce

```bash
IDS="EN2002a EN2002b EN2002c EN2002d ES2004a ES2004b ES2004c ES2004d IS1009a IS1009b IS1009c IS1009d TS3003a TS3003b TS3003c TS3003d"
./scripts/calibrate-sherpa.sh                     # dev split: threshold and tie-break (build/v5-calib)
V5_OUT=build/v5-test V5_AMI_ROOT=data/corpora/ami/test V5_AMI_MEETINGS="$IDS" V5_DATASETS=ami \
V5_ASR_CACHE=$PWD/build/v5-test/asr-cache \
V5_EXTRA_SYSTEMS="whisper-sherpa-cal:whisper-sherpa:0:cluster_threshold=1.15,assignment_tie_break=latest_start" \
V5_REASSIGN="whisper-sherpa-hint-lt:whisper-sherpa-hint:latest_start whisper-sherpa-cal-floor:whisper-sherpa-cal:floor" \
V5_AMI_GATES="ami-headset:whisper-sherpa-hint:macro ami-headset:whisper-sherpa-cal:macro ami-test-whisper-sherpa-cal:whisper-sherpa-cal:macro ami-test-whisper-sherpa-hint:whisper-sherpa-hint:macro" \
  ./scripts/run-v5.sh
./scripts/crosscheck-md-eval.sh build/v5-test data/corpora/ami/test

# embedding-cluster (§7): an interpreter with torch and SpeechBrain (requirements/embedding.txt).
# The test pass checks its replay against real runs in validation-test/real/, made first:
V=build/v5-calib/embedding-cluster/validation-test/real
for m in EN2002a EN2002b EN2002c; do PYTHONDONTWRITEBYTECODE=1 .venv-torch/bin/python -m pipeline run \
  --pipeline embedding-cluster --audio data/corpora/ami/test/$m/mix.wav --meeting-dir data/corpora/ami/test/$m \
  --out $V/$m/hyp.json; done
PYTHONDONTWRITEBYTECODE=1 .venv-torch/bin/python scripts/calibrate-embedding-cluster.py      # dev split
PYTHONDONTWRITEBYTECODE=1 .venv-torch/bin/python scripts/calibrate-embedding-cluster.py --test-root data/corpora/ami/test
# then the same run-v5 line with these additions, scoring only the new systems:
#   V5_EXTRA_SYSTEMS="<as above> embedding-cluster:embedding-cluster:0 embedding-cluster-hint:embedding-cluster:1 embedding-cluster-cal:embedding-cluster:0:distance_threshold=0.55"
#   V5_STAGES=score,gates,summary V5_SCORE_ONLY="embedding-cluster embedding-cluster-hint embedding-cluster-cal"

# whisper-sherpa-ecapa (§7.1): the dev check, then an interpreter with faster-whisper, sherpa-onnx,
# torch and SpeechBrain (requirements/pyannote.txt) for the runs
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check-sherpa-ecapa-dev.py
PYTHON=$PWD/.venv-pyannote/bin/python V5_ONLY_EXTRA=1 V5_STAGES=infer <the run-v5 line above, with> \
  V5_EXTRA_SYSTEMS="whisper-sherpa-ecapa:whisper-sherpa-ecapa:0 whisper-sherpa-ecapa-hint:whisper-sherpa-ecapa:1" ./scripts/run-v5.sh
# then derive,score,gates,summary with those systems added to V5_EXTRA_SYSTEMS and
#   ami-test-whisper-sherpa-ecapa:whisper-sherpa-ecapa:macro added to V5_AMI_GATES
# pyannote (§7.2): a Hugging Face token with access to the gated models, in the environment only
export HF_TOKEN=<token> PYANNOTE_METRICS_ENABLED=false
#   the same infer line with V5_EXTRA_SYSTEMS="whisper-pyannote:faster-whisper+pyannote:0:language=en,cpu_threads=4,diarization_device=mps pyannote-audio:pyannote-audio:0:device=mps"
#   (language and cpu_threads as in whisper-sherpa, so the transcript comes from the cache)
# then derive,score,gates,summary with those systems added to V5_EXTRA_SYSTEMS and
#   ami-headset:whisper-pyannote:macro ami-test-whisper-pyannote:whisper-pyannote:macro
#   ami-test-pyannote-audio:pyannote-audio:macro added to V5_AMI_GATES
```

The inputs stage downloads and sha256-checks the 16 WAVs and converts them. With
`V5_STAGES=score,gates,summary` the saved hypotheses are rescored; on 28 Sep that took
about 65 minutes, with other jobs running. The md-eval cross-check (§8) was run on 28 Sep,
before the `embedding-cluster`, `whisper-sherpa-ecapa`, `pyannote-audio` and
`faster-whisper+pyannote` systems were added, and covers 14 systems and views (none of
those).

## 12. Not done

* A quiet-machine speed measurement (section 4).
* Any other ASR model on the full split; `whisperx` (not installed). A larger Whisper
  model, chosen on the dev split, is the obvious next step for cpWER.
* A transcript attached to `embedding-cluster`'s own no-hint turns (its eigengap count at
  the calibrated 0.55). With the hint this is `whisper-sherpa-ecapa-hint`, whose turns
  equal `embedding-cluster`'s hinted turns (§7.1).
* Calibration of `min_duration_on/off` or any sherpa setting other than the
  threshold.
