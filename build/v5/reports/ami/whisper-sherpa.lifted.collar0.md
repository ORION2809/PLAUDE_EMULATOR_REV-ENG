# Batch evaluation

3 meeting(s) scored

| meeting | system | cpWER | tcpWER | DER | JER | WER (label-literal) | WER (concatenated) | overlap DER | speaker-count error |
|---|---|---|---|---|---|---|---|---|---|
| ES2004a | whisper-sherpa | 1.0633 | 1.1338 | 0.7553 | 0.7550 | 1.7953 | 0.2759 | 0.8805 | 27 |
| IS1009a | whisper-sherpa | 1.0446 | 1.2190 | 0.7800 | 0.8286 | 1.7582 | 0.3266 | 0.8553 | 30 |
| TS3003a | whisper-sherpa | 1.2210 | 1.3343 | 0.8649 | 0.8797 | 1.8153 | 0.2605 | 0.8874 | 37 |
| **macro** |  | 1.1096 | 1.2290 | 0.8001 | 0.8211 | 1.7896 | 0.2876 | 0.8744 | 31.3333 |
| **micro** |  | 1.1135 | 1.2282 | 0.8043 | 0.8211 | 1.7920 | 0.2847 | 0.8734 | 31.3333 |

Errors:

- /Users/mivi/Desktop/plaud-harness/data/corpora/ami/meetings/EN2002a: unexpected RuntimeError: Too few fallback keys provided! There are more over-/under-estimated speakers than fallback_keys in abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
