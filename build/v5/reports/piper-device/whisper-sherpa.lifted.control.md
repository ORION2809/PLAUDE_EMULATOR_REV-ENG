# Batch evaluation

4 meeting(s) scored

| meeting | system | cpWER | tcpWER | DER | JER | WER (label-literal) | WER (concatenated) | overlap DER | speaker-count error |
|---|---|---|---|---|---|---|---|---|---|
| synth-piper-2spk-overlap-s0102 | whisper-sherpa | 0.9564 | 0.9703 | 0.5609 | 0.6730 | 1.7941 | 0.3072 | 0.6102 | 6 |
| synth-piper-2spk-s0101 | whisper-sherpa | 0.8976 | 0.9122 | 0.7285 | 0.7511 | 0.9634 | 0.1756 | n/a | 4 |
| synth-piper-3spk-reverb-noise-s0103 | whisper-sherpa | 1.0590 | 1.1179 | 0.5656 | 0.6639 | 1.2200 | 0.4921 | n/a | 7 |
| synth-piper-4spk-s0104 | whisper-sherpa | 1.1840 | 1.2882 | 0.8728 | 0.8461 | 1.2195 | 0.1308 | n/a | 6 |
| **macro** |  | 1.0242 | 1.0722 | 0.6819 | 0.7335 | 1.2992 | 0.2764 | 0.6102 | 5.7500 |
| **micro** |  | 1.0224 | 1.0688 | 0.6860 | 0.7477 | 1.3392 | 0.2795 | 0.6102 | 5.7500 |
