# Batch evaluation

4 meeting(s) scored

| meeting | system | cpWER | tcpWER | DER | JER | WER (label-literal) | WER (concatenated) | overlap DER | speaker-count error |
|---|---|---|---|---|---|---|---|---|---|
| synth-piper-2spk-overlap-s0102 | whisper-sherpa | 0.7818 | 0.7906 | 0.4260 | 0.5789 | 0.8464 | 0.3072 | 0.5000 | 0 |
| synth-piper-2spk-s0101 | whisper-sherpa | 0.9415 | 0.9439 | 0.7324 | 0.7672 | 0.9415 | 0.1756 | n/a | 0 |
| synth-piper-3spk-reverb-noise-s0103 | whisper-sherpa | 1.2744 | 1.2744 | 0.6376 | 0.7953 | 1.2744 | 0.4921 | n/a | -1 |
| synth-piper-4spk-s0104 | whisper-sherpa | 1.1774 | 1.2306 | 0.9047 | 0.8643 | 1.5033 | 0.1308 | n/a | 0 |
| **macro** |  | 1.0438 | 1.0599 | 0.6752 | 0.7514 | 1.1414 | 0.2764 | 0.5000 | -0.2500 |
| **micro** |  | 1.0277 | 1.0437 | 0.6612 | 0.7760 | 1.1259 | 0.2795 | 0.5000 | -0.2500 |
