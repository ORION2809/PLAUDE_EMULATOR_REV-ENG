# Batch evaluation

3 meeting(s) scored

| meeting | system | cpWER | tcpWER | DER | JER | WER (label-literal) | WER (concatenated) | overlap DER | speaker-count error |
|---|---|---|---|---|---|---|---|---|---|
| ES2004a | whisper-sherpa | 1.0633 | 1.1338 | 0.7231 | 0.7278 | 1.7953 | 0.2759 | 0.8798 | 27 |
| IS1009a | whisper-sherpa | 1.0446 | 1.2190 | 0.7550 | 0.8107 | 1.7582 | 0.3266 | 0.8470 | 30 |
| TS3003a | whisper-sherpa | 1.2210 | 1.3343 | 0.8498 | 0.8675 | 1.8153 | 0.2605 | 0.8768 | 37 |
| **macro** |  | 1.1096 | 1.2290 | 0.7760 | 0.8020 | 1.7896 | 0.2876 | 0.8679 | 31.3333 |
| **micro** |  | 1.1135 | 1.2282 | 0.7825 | 0.8020 | 1.7920 | 0.2847 | 0.8683 | 31.3333 |

Errors:

- /Users/mivi/Desktop/plaud-harness/data/corpora/ami/meetings/EN2002a: unexpected RuntimeError: Too few fallback keys provided! There are more over-/under-estimated speakers than fallback_keys in abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
