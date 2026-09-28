# Batch evaluation

4 meeting(s) scored

| meeting | system | cpWER | tcpWER | DER | JER | WER (label-literal) | WER (concatenated) | overlap DER | speaker-count error |
|---|---|---|---|---|---|---|---|---|---|
| synth-piper-2spk-overlap-s0102 | whisper-sherpa | 0.9564 | 0.9703 | 0.5671 | 0.6608 | 1.7941 | 0.3072 | 0.5943 | 6 |
| synth-piper-2spk-s0101 | whisper-sherpa | 0.8976 | 0.9122 | 0.8333 | 0.7693 | 0.9634 | 0.1756 | n/a | 4 |
| synth-piper-3spk-reverb-noise-s0103 | whisper-sherpa | 1.0590 | 1.1179 | 0.6188 | 0.6805 | 1.2200 | 0.4921 | n/a | 7 |
| synth-piper-4spk-s0104 | whisper-sherpa | 1.1840 | 1.2882 | 0.9673 | 0.8540 | 1.2195 | 0.1308 | n/a | 6 |
| **macro** |  | 1.0242 | 1.0722 | 0.7466 | 0.7411 | 1.2992 | 0.2764 | 0.5943 | 5.7500 |
| **micro** |  | 1.0224 | 1.0688 | 0.7366 | 0.7561 | 1.3392 | 0.2795 | 0.5943 | 5.7500 |
