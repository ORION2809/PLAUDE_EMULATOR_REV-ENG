# Batch evaluation

10 meeting(s) scored

| meeting | system | cpWER | tcpWER | DER | JER | WER (label-literal) | WER (concatenated) | overlap DER | speaker-count error |
|---|---|---|---|---|---|---|---|---|---|
| ES2004a | whisper-sherpa | 1.0633 | 1.1338 | 0.7553 | 0.7550 | 1.7953 | 0.2759 | 0.8805 | 27 |
| ES2004b | whisper-sherpa | 1.0167 | 1.0650 | 0.6682 | 0.6578 | 1.8243 | 0.2639 | 0.8690 | 42 |
| ES2004c | whisper-sherpa | 1.0085 | 1.0805 | 0.6575 | 0.6862 | 1.7969 | 0.2795 | 0.8928 | 44 |
| IS1009a | whisper-sherpa | 1.0446 | 1.2190 | 0.7800 | 0.8286 | 1.7582 | 0.3266 | 0.8553 | 30 |
| IS1009b | whisper-sherpa | 1.1215 | 1.1997 | 0.7150 | 0.7135 | 1.7908 | 0.2763 | 0.9622 | 46 |
| IS1009c | whisper-sherpa | 1.1467 | 1.2418 | 0.7990 | 0.6775 | 1.8111 | 0.2444 | 0.9535 | 35 |
| IS1009d | whisper-sherpa | 1.1754 | 1.3283 | 0.8172 | 0.8161 | 1.8032 | 0.2695 | 0.9467 | 47 |
| TS3003a | whisper-sherpa | 1.2210 | 1.3343 | 0.8649 | 0.8797 | 1.8153 | 0.2605 | 0.8874 | 37 |
| TS3003b | whisper-sherpa | 1.0207 | 1.1340 | 0.7467 | 0.6640 | 1.7811 | 0.3038 | 0.9116 | 46 |
| TS3003c | whisper-sherpa | 1.0884 | 1.1708 | 0.7255 | 0.6528 | 1.7599 | 0.3049 | 0.9812 | 38 |
| **macro** |  | 1.0907 | 1.1907 | 0.7529 | 0.7331 | 1.7936 | 0.2805 | 0.9140 | 39.2000 |
| **micro** |  | 1.0815 | 1.1736 | 0.7392 | 0.7331 | 1.7960 | 0.2782 | 0.9134 | 39.2000 |

Errors:

- /Users/mivi/Desktop/plaud-harness/data/corpora/ami/test/EN2002a: unexpected RuntimeError: Too few fallback keys provided! There are more over-/under-estimated speakers than fallback_keys in abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
- /Users/mivi/Desktop/plaud-harness/data/corpora/ami/test/EN2002b: unexpected RuntimeError: Too few fallback keys provided! There are more over-/under-estimated speakers than fallback_keys in abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
- /Users/mivi/Desktop/plaud-harness/data/corpora/ami/test/EN2002c: unexpected RuntimeError: Too few fallback keys provided! There are more over-/under-estimated speakers than fallback_keys in abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
- /Users/mivi/Desktop/plaud-harness/data/corpora/ami/test/EN2002d: unexpected RuntimeError: Too few fallback keys provided! There are more over-/under-estimated speakers than fallback_keys in abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
- /Users/mivi/Desktop/plaud-harness/data/corpora/ami/test/ES2004d: unexpected RuntimeError: Too few fallback keys provided! There are more over-/under-estimated speakers than fallback_keys in abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
- /Users/mivi/Desktop/plaud-harness/data/corpora/ami/test/TS3003d: unexpected RuntimeError: Too few fallback keys provided! There are more over-/under-estimated speakers than fallback_keys in abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
