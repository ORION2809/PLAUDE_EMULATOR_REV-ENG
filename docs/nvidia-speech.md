# NVIDIA Nemotron vs the current speech stack: findings

**Interim, as of 30 September 2026.** Branch `research/nvidia-nemotron`. The runs
are still in progress (section 9). Numbers marked *partial* will change.

## 1. Summary

**Accuracy: NVIDIA is ahead so far, on AMI.** On the 6 of 16 AMI test meetings
finished so far:

| System | Macro cpWER |
|---|---|
| Nemotron 3.5 ASR + Nemotron 3 Diarization, NVIDIA runtime defaults | **0.353** |
| Current best: faster-whisper `small.en` + pyannote community-1 | 0.397 |

Nemotron wins on all six meetings. AMI favours NVIDIA, because its diarizer trained on
AMI train and dev (section 3). The held-out NOTSOFAR runs will decide whether the lead
holds.

**Speed: too slow on a budget Android phone at NVIDIA's defaults.** Measured on a Mivi One
(Snapdragon 4 Gen 2, CPU) with a 60 s clip:

| Stack | RTF | Peak memory |
|---|---|---|
| NVIDIA ASR + speaker tags | 3.57 | 1.7 GB |
| Current stack on device (whisper.cpp + sherpa-onnx) | 1.45 | 0.66 GB |

NVIDIA's English model at the 1.12 s right context brings its ASR down to RTF 0.72.
Nemotron's diarizer alone still runs at RTF 1.84 on this CPU.

**Size.** NVIDIA's pair is 849 MB (742 MB ASR + 107 MB diarizer). The current on-device
stack is 300 MB (264 MB + 36 MB).

**Verdict so far.** Nemotron gives better speaker-attributed transcripts. On a low-end
Android CPU it cannot keep up with real time, and its diarizer is the bottleneck. Nothing
here measures a flagship phone, a phone GPU or NPU, or an iPhone (section 8).

## 2. What was compared

| System | ASR | Diarization | Runtime on a phone |
|---|---|---|---|
| `nemotron` | Nemotron 3.5 ASR Streaming 0.6B | Nemotron 3 Diarization (100M) | NVIDIA NeMo-Speech.cpp (ggml) |
| `nemotron-tagged` | same | same, via the runtime's own word speaker tags | NeMo-Speech.cpp |
| `nemotron-en-rc13` | Nemotron Speech Streaming EN 0.6B, 1.12 s right context | (joined with any diarizer) | NeMo-Speech.cpp |
| `faster-whisper+pyannote` (current best) | Whisper `small.en` (CTranslate2) | pyannote community-1 | none (PyTorch, desktop) |
| `whisper-cpp+sherpa` (current, on-device form) | Whisper `small.en` q8_0 (whisper.cpp) | sherpa-onnx: segmentation-3.0 + CAM++ | whisper.cpp + sherpa-onnx |

Also measured:
- Parakeet TDT 0.6B v3, Streaming Sortformer v2, and Nemotron 3.5 at the 1.12 s right
  context (queued).
- An ASR × diarizer matrix: every transcript on every diarizer's turns, built from saved
  runs with the harness's word-to-speaker rule
  ([`research/nvidia/recombine.py`](../research/nvidia/recombine.py)).

The adapters are [`pipeline/nemo_speech.py`](../pipeline/nemo_speech.py) and
[`pipeline/whisper_cpp.py`](../pipeline/whisper_cpp.py). The on-device layer is
[`research/nvidia/android/`](../research/nvidia/android/): a C++ engine per vendor, a
benchmark CLI, and the SpeechBench app. On the same audio, each engine returns exactly the
same words, timestamps, speaker tags and diarization turns as the vendor's own CLI
(checked for all four engines).

## 3. How it was tested

**Data**
- **AMI test split:** 16 meetings, 9.06 h, headset mix. This is the harness's V5 set.
- **NOTSOFAR-1 "eval-full-only":** 49 meetings, 5.0 h, 3–7 speakers, English workplace
  meetings, CC BY 4.0.
  - Two versions share one reference: a single far-field device (Plaud-like) and a
    close-talk mix.
  - These 49 are in neither Nemotron's training data nor NVIDIA's published evaluation
    ([`research/nvidia/prepare_notsofar.py`](../research/nvidia/prepare_notsofar.py)).

**Scoring**
- **Harness protocol:** cpWER, tcpWER and WER with meeteval; DER and JER with
  pyannote.metrics at collar 0.25 (total width).
- **NVIDIA's protocol:** collar 0, overlap scored, forced-alignment references
  ([`research/nvidia/score_nvidia_protocol.py`](../research/nvidia/score_nvidia_protocol.py)).
  This is how the model card's 9.25% AMI DER was measured.

**No tuning.** Each NVIDIA model runs at documented settings: the CLI defaults, and the
model card's 1.12 s context. Nemotron's diarizer trained on AMI dev, so the dev split
cannot serve as held-out data for it.

**Training-data overlap**

| Model | AMI | NOTSOFAR |
|---|---|---|
| Nemotron 3 Diarization | train + dev (the harness's dev split); test not listed | train + dev; the 49 used here not seen |
| Nemotron 3.5 ASR | not listed | not listed |
| Nemotron-EN, Parakeet v3 | listed, split not stated | not listed |
| pyannote segmentation-3.0 (sherpa-onnx, community-1) | listed | not listed |
| Whisper | unpublished | `small.en` predates NOTSOFAR |

## 4. Accuracy so far

**Speaker-attributed transcripts.** *Partial:* EN2002a–d and ES2004a–b, harness protocol,
no speaker count given.

| System | Macro cpWER | Macro WER | cpWER per meeting |
|---|---|---|---|
| `nemotron` | **0.353** | **0.349** | 0.430 · 0.409 · 0.360 · 0.422 · 0.267 · 0.232 |
| `faster-whisper+pyannote` | 0.397 | 0.374 | 0.464 · 0.481 · 0.399 · 0.465 · 0.296 · 0.277 |
| `whisper-sherpa-ecapa` | 0.576 | 0.374 | 0.635 · 0.547 · 0.416 · 0.524 · 0.624 · 0.707 |
| `whisper-sherpa` (calibrated) | 0.874 | 0.374 | 0.926 · 0.877 · 0.938 · 0.880 · 0.892 · 0.733 |

**Diarizers on their own turns.** Same 6 meetings, harness protocol.

| Diarizer | DER | Missed speech | False alarm | Speaker confusion |
|---|---|---|---|---|
| pyannote community-1 (overlap-aware) | **0.156** | 0.101 | 0.019 | 0.036 |
| Nemotron 3 Diarization (runtime defaults) | 0.186 | 0.155 | 0.024 | **0.007** |
| pyannote community-1 (exclusive turns) | 0.231 | 0.201 | 0.010 | 0.020 |

- Nemotron's lead in cpWER has two sources:
  - Better words: WER 0.349 against 0.374.
  - Almost no speaker confusion. Its cpWER is only 0.005 above its WER, against 0.023 for
    Whisper + pyannote.
- pyannote misses less speech, so its overall DER is lower.
- **Scoring protocol changes DER a lot.** Under NVIDIA's protocol, pyannote's DER over
  all 16 meetings is 34.7% (false alarm 24.9%). Under the harness protocol it is 13%.
  The forced-alignment references are cut tightly around words, and Nemotron was trained
  on labels of that style. A third party (Argmax OpenBench) reports 36% for community-1
  under the same references. Nemotron under NVIDIA's protocol is pending; the model card
  claims 9.25%.

## 5. Speed and memory on a real Android phone

**Device:** Mivi One (MVNOE), Snapdragon 4 Gen 2 (SM4450): 2 Cortex-A78 cores at
2.2 GHz and 6 Cortex-A55 cores at 1.96 GHz; 7.3 GB RAM; Android 16.

**Build:** CPU only, arm64 with dotprod and fp16, 4 threads (NeMo-Speech.cpp hard-codes 4).

**Method:** each run is one process on a 60 s AMI clip, measured with `toybox time -v`,
with a 30 s cooldown between runs
([`research/nvidia/android/bench.sh`](../research/nvidia/android/bench.sh)).

| Run | Wall time (s) | RTF | Peak RSS (MB) |
|---|---|---|---|
| Nemotron 3.5 ASR, runtime defaults | 104.3 | 1.74 | 1 538 |
| Nemotron-EN ASR, 1.12 s right context | 43.2 | **0.72** | 1 484 |
| Nemotron 3 Diarization | 110.6 | 1.84 | 228 |
| Nemotron 3.5 ASR + speaker tags (one pass) | 214.3 | 3.57 | 1 714 |
| whisper.cpp `small.en` q8_0 | 63.2 | 1.05 | 663 |
| sherpa-onnx diarization | 23.7 | 0.39 | 207 |
| NVIDIA's own CLI, ASR (`nemo-speech transcribe --device cpu`) | 110.3 | 1.84 | 2 375 |
| whisper.cpp's own CLI (`whisper-cli`) | 64.1 | 1.07 | 618 |

- **Full pipelines on this phone:**
  - NVIDIA: RTF 3.57, so an hour of audio takes about 3.6 h.
  - Current stack: RTF 1.45 (1.05 + 0.39 run in sequence).
- **Why Nemotron is heavy on a CPU:** Nemotron ASR has about 2.6 times Whisper
  `small.en`'s parameters, and the phone RTFs scale with that.
  - The CPU path also runs even a 60 s clip through the streaming runner, in 160 ms
    chunks at the default.
  - Nemotron-EN at the 1.12 s context ran 2.4× faster than Nemotron 3.5 at the default.
    Whether the model or the setting causes that is being separated (section 9).
- **Mac (M1, Metal)**, from the accuracy runs, which ran under contention:
  - Nemotron ASR: RTF about 0.25, peak 1.9 GB.
  - Nemotron diarization: RTF about 0.08, peak 0.9 GB on a 36-minute meeting.
  - Quiet Mac runs are pending ([`research/nvidia/speed.sh`](../research/nvidia/speed.sh)).

## 6. Size and licences

| Component | Size | Licence |
|---|---|---|
| Nemotron 3.5 ASR, q8_0 GGUF | 741.5 MB | OpenMDW-1.1 |
| Nemotron-EN ASR, q8_0 GGUF | 699.9 MB | NVIDIA Open Model License |
| Parakeet TDT 0.6B v3, q8_0 GGUF | 714.0 MB | CC BY 4.0 |
| Nemotron 3 Diarization, q8_0 GGUF | 107.0 MB | OpenMDW-1.1 |
| Streaming Sortformer v2, q8_0 GGUF | 147.1 MB | CC BY 4.0 |
| Whisper `small.en`, whisper.cpp q8_0 | 264.5 MB | MIT |
| Whisper `small.en`, faster-whisper (desktop; int8 at load) | 483.5 MB | MIT |
| sherpa-onnx diarizer (segmentation-3.0 6.0 MB + CAM++ 29.6 MB) | 35.6 MB | MIT, Apache-2.0 |
| pyannote community-1 (gated) | 32.8 MB | CC BY 4.0 |

Native runtime libraries, arm64, stripped:

| Runtime | Size | Licence |
|---|---|---|
| NeMo-Speech.cpp (ggml and its libraries) | 4.3 MB | Apache-2.0 |
| whisper.cpp | 1.7 MB | MIT |
| sherpa-onnx (mostly onnxruntime) | 25.5 MB | Apache-2.0 |

The SpeechBench debug APK with all three is 30.2 MB, without models.

**Licence caution:** NVIDIA's Hugging Face repos ship no licence file. An app bundling the
OpenMDW models must include the OpenMDW text itself.

## 7. What we learned about NVIDIA's runtime

**Release and platform support**
- **Build from `main`.** The only release (v0.1.0, 19 Aug) cannot run Nemotron 3
  Diarization. We built commit `0f706e4`.
- **No Android or iOS support.** We built it for Android ourselves with NDK 26.1. Two
  fixes, both build flags only:
  - link `liblog`, because static SentencePiece calls Android logging;
  - keep whisper.cpp's ggml static and hidden, because both runtimes ship `libggml*.so`
    under the same names.

**Defaults that differ from the model cards**
- **ASR model and context.** The default ASR is the multilingual Nemotron 3.5; NVIDIA
  recommends the English model for English. Audio over about 400 s, which is every
  meeting here, runs through the streaming runner at a 160 ms right context. Nemotron
  3.5 was not trained at that setting.
- **Diarization presets and thresholds** differ from the card's configurations.
- **Compaction on long recordings.** After 20 minutes the diarizer finalises older audio
  with library-default thresholds, so the CLI's threshold flags only affect the tail. The
  header `diar.h` documents this.

**CPU and output**
- **CPU threads are fixed at 4** (`src/runtime/ggml/session.cpp`).
- **The CPU path is compute-heavy.** It is built GPU-first.
- **Word timestamps are 80 ms token spans.** Word-run DER therefore looks worse than
  Whisper's even when attribution is better, so compare cpWER and turn DER.

## 8. iPhone

**Not measurable here.** This Mac has only the command-line tools, not Xcode, so nothing
can be built for iPhone. Third-party figures, on different runtimes (Core ML / Core AI,
not NeMo-Speech.cpp):

**Nemotron 3.5 ASR, iPhone 17 Pro GPU**
- 53 ms per 320 ms chunk, RTF 0.167.
- First load after install takes about 52 s; a cached load about 4 s.
- The model ships as two fp16 halves of 605 MB and 615 MB.
- ([source](https://huggingface.co/mlboydaisuke/Nemotron-3.5-ASR-Streaming-CoreAI))

**Nemotron 3 Diarization, iPhone 17 Pro GPU**
- Streaming RTF 0.043 ([source](https://huggingface.co/mlboydaisuke/Nemotron-3-Diarization-CoreAI)).

**Whisper `small.en` via WhisperKit, times faster than real time**
([source](https://huggingface.co/spaces/argmaxinc/whisperkit-benchmarks/blob/main/dashboard_data/performance_data.json)):

| iPhone | Speed |
|---|---|
| 12 Pro Max | 6.1× |
| 13 | 12.8× |
| 15 Pro | 15.7× |
| 16 Pro | 20.2× |
| 17 Pro | 21.6× |

**Memory ceiling:** FluidAudio reports a Neural Engine memory limit on iOS of about 1.4 GB
for the Nemotron EN encoder ([source](https://github.com/FluidInference/FluidAudio/blob/main/Documentation/Benchmarks.md)).

## 9. Still running, and next

**Mac, AMI test split**
- `nemotron`: remaining 10 meetings.
- Nemotron-EN and Nemotron 3.5 at the 1.12 s context, whisper.cpp, Parakeet.
- Diarizer variants: the `v3-offline` preset and Sortformer v2.
- NVIDIA's tagging against the harness rule.

**Mac, NOTSOFAR far-field and close-talk mix**
- Nemotron, the current stack, whisper.cpp, pyannote.

**Scoring and speed**
- NVIDIA's protocol for Nemotron.
- The full ASR × diarizer matrix ([`research/nvidia/evaluate.py`](../research/nvidia/evaluate.py)).
- Quiet Mac speed runs.

**Phone**
- The full 14-minute AMI meeting and the 6-minute NOTSOFAR meeting, scored for accuracy
  ([`research/nvidia/device_to_hyp.py`](../research/nvidia/device_to_hyp.py)).
- Runs that separate the effect of the model from the effect of the right context.

**Possible next experiment**
- A q4_k Nemotron-EN (about 0.4 GB) for size and phone speed.

## Sources

- NVIDIA NeMo-Speech.cpp: https://github.com/NVIDIA/NeMo-Speech.cpp (commit `0f706e4`)
- Nemotron 3 Diarization model card: https://huggingface.co/nvidia/Nemotron-3-Diarization
- Nemotron 3.5 ASR model card: https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b
- Nemotron Speech Streaming EN: https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b
- NOTSOFAR-1 dataset: https://huggingface.co/datasets/microsoft/NOTSOFAR
- AMI forced-alignment labels: https://github.com/nttcslab-sp/diar-forced-alignment
- whisper.cpp v1.9.4: https://github.com/ggml-org/whisper.cpp
- sherpa-onnx v1.13.8: https://github.com/k2-fsa/sherpa-onnx
