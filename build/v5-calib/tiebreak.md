# Word-assignment tie-break on the AMI dev split (turns at cluster_threshold 1.15)

| tie-break | macro cpWER | micro cpWER | macro DER | words decided by the tie-break | words |
|---|---:|---:|---:|---:|---:|
| floor | 1.0075 | 1.0171 | 0.5403 | 15398 | 95495 |
| latest_start **selected** | 0.9344 | 0.9377 | 0.5300 | 15398 | 95495 |
| first_seen | 1.0145 | 1.0276 | 0.5398 | 15398 | 95495 |
| previous_word | 1.0062 | 1.0161 | 0.5381 | 15398 | 95495 |

Selected: latest_start (lowest macro cpWER of the dev REFERENCE words assigned to the selected threshold's sherpa turns (a proxy: no ASR errors); ties: the default (floor) (HARNESS_POLICY)).
