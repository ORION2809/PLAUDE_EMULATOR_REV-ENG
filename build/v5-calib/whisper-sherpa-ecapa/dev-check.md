# whisper-sherpa-ecapa on the AMI dev split (no hint)

| turns | macro DER | JER | confusion | miss | speaker-count error | reference-word cpWER (floor / latest_start) |
|---|---:|---:|---:|---:|---:|---|
| sherpa-count+ecapa | 0.4314 | 0.5167 | 0.1959 | 0.1848 | -0.44 | 0.6145 / 0.6145 |
| ecapa-alone-0.55 | 0.5295 | 0.6554 | 0.2954 | 0.1829 | -1.44 | 0.8828 / 0.8829 |
| sherpa-alone-1.15 | 0.5431 | 0.7078 | 0.4520 | 0.0484 | -0.44 | 1.0075 / 0.9344 |

Tie-break for sherpa-count+ecapa: floor (lowest macro cpWER of the reference-word proxy at 4 decimals; ties: the default (floor)).

