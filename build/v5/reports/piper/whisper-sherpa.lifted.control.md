# Batch evaluation

4 meeting(s) scored

| meeting | system | cpWER | tcpWER | DER | JER | WER (label-literal) | WER (concatenated) | overlap DER | speaker-count error |
|---|---|---|---|---|---|---|---|---|---|
| synth-piper-2spk-overlap-s0102 | whisper-sherpa | 1.0052 | 1.0209 | 0.5874 | 0.6778 | 1.7295 | 0.2304 | 0.6305 | 7 |
| synth-piper-2spk-s0101 | whisper-sherpa | 0.9122 | 0.9366 | 0.7666 | 0.7151 | 0.9561 | 0.1878 | n/a | 5 |
| synth-piper-3spk-reverb-noise-s0103 | whisper-sherpa | 1.0408 | 1.1156 | 0.5727 | 0.6684 | 1.4603 | 0.4603 | n/a | 7 |
| synth-piper-4spk-s0104 | whisper-sherpa | 1.0266 | 1.2195 | 0.7772 | 0.8150 | 1.3437 | 0.1220 | n/a | 6 |
| **macro** |  | 0.9962 | 1.0732 | 0.6760 | 0.7191 | 1.3724 | 0.2501 | 0.6305 | 6.2500 |
| **micro** |  | 0.9984 | 1.0725 | 0.6759 | 0.7319 | 1.4043 | 0.2491 | 0.6305 | 6.2500 |
