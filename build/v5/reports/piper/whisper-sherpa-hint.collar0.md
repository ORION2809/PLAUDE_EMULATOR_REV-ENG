# Batch evaluation

4 meeting(s) scored

| meeting | system | cpWER | tcpWER | DER | JER | WER (label-literal) | WER (concatenated) | overlap DER | speaker-count error |
|---|---|---|---|---|---|---|---|---|---|
| synth-piper-2spk-overlap-s0102 | whisper-sherpa | 0.9389 | 0.9389 | 0.4566 | 0.6386 | 0.9389 | 0.2304 | 0.5000 | 0 |
| synth-piper-2spk-s0101 | whisper-sherpa | 0.9317 | 0.9390 | 0.7428 | 0.7674 | 0.9317 | 0.1878 | n/a | 0 |
| synth-piper-3spk-reverb-noise-s0103 | whisper-sherpa | 1.3401 | 1.3401 | 0.6756 | 0.8405 | 1.3401 | 0.4603 | n/a | -1 |
| synth-piper-4spk-s0104 | whisper-sherpa | 1.1863 | 1.2749 | 0.8388 | 0.8529 | 1.4967 | 0.1220 | n/a | 0 |
| **macro** |  | 1.0993 | 1.1233 | 0.6784 | 0.7748 | 1.1769 | 0.2501 | 0.5000 | -0.2500 |
| **micro** |  | 1.0912 | 1.1141 | 0.6645 | 0.7950 | 1.1659 | 0.2491 | 0.5000 | -0.2500 |
