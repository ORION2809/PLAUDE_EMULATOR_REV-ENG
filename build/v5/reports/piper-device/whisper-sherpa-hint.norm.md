# Batch evaluation

4 meeting(s) scored

| meeting | system | cpWER | tcpWER | DER | JER | WER (label-literal) | WER (concatenated) | overlap DER | speaker-count error |
|---|---|---|---|---|---|---|---|---|---|
| synth-piper-2spk-overlap-s0102 | whisper-sherpa | 0.7836 | 0.7923 | 0.4071 | 0.5884 | 0.8482 | 0.3089 | 0.5000 | 0 |
| synth-piper-2spk-s0101 | whisper-sherpa | 0.9439 | 0.9463 | 0.6298 | 0.7490 | 0.9439 | 0.1780 | n/a | 0 |
| synth-piper-3spk-reverb-noise-s0103 | whisper-sherpa | 1.2789 | 1.2789 | 0.5743 | 0.7799 | 1.2789 | 0.4966 | n/a | -1 |
| synth-piper-4spk-s0104 | whisper-sherpa | 1.1796 | 1.2328 | 0.8095 | 0.8563 | 1.5055 | 0.1308 | n/a | 0 |
| **macro** |  | 1.0465 | 1.0626 | 0.6052 | 0.7434 | 1.1441 | 0.2786 | 0.5000 | -0.2500 |
| **micro** |  | 1.0304 | 1.0464 | 0.6105 | 0.7672 | 1.1285 | 0.2816 | 0.5000 | -0.2500 |
