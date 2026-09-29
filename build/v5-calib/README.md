# Calibration evidence: the AMI dev split (published)

Outputs of `scripts/calibrate-sherpa.sh` (28 September 2026) and
`scripts/calibrate-embedding-cluster.py` (29 September), described in
`docs/pipeline.md` §11.8-§11.9 and `docs/v5-test-split.md` §3 and §7:

- `summary.md`, `selected.json`, `reports/`: sherpa-onnx `cluster_threshold`
  swept over 25 values on the 18 dev meetings, no speaker-count hint;
  `hyp/1.15/`: the selected threshold's hypotheses (the other thresholds' are
  regenerable and not published).
- `tiebreak.md`, `tiebreak.json`: the word-assignment tie-breaks compared on the
  dev reference words.
- `validation/`, `confirm/`: the replay (`pipeline/sherpa_sweep.py`) against real
  sherpa-onnx runs.
- `embedding-cluster/`: the same for `embedding-cluster`'s distance threshold
  (`selected.json`, `reports/`, the selected threshold's hypotheses, and the
  replay's validation against real runs).
- `inputs/ami-dev.SHA256SUMS`, `logs/`.

Not published (large and regenerable): the cached segmentation and embeddings
(`pre/`, `embedding-cluster/pre*/`) and the per-threshold hypotheses other than
the selected ones. No audio and no model weights are included. Paths inside the
files are the ones the run used on the author's machine, left as produced.

Attribution: AMI Meeting Corpus, the 18 meetings of the full-corpus dev split
(ES2011a-d, IB4001-IB4004, IB4010, IB4011, IS1008a-d, TS3004a-d; Mix-Headset
audio and manual annotations), CC BY 4.0, University of Edinburgh and the AMI
consortium, https://groups.inf.ed.ac.uk/ami/corpus/. Models as in
`build/v5-test/README.md`.
