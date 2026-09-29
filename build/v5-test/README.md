# V5 evidence: the full AMI test split (published)

Outputs of `scripts/run-v5.sh` for the 28 September 2026 run described in
`docs/v5-test-split.md`, and of `scripts/calibrate-embedding-cluster.py
--test-root` for the `embedding-cluster` systems (29 September): scoring reports
(`reports/`), system hypotheses and their derived views (`hyp/`), per-run logs
(`logs/`, `infer-*.log`, `score.log`), gate checks (`gates/`), the transcript
cache the runs shared (`asr-cache/`), the NIST md-eval cross-check
(`md-eval-crosscheck.json`) and the summaries (`summary.md`, `summary.json`). No
audio and no model weights are included. Paths inside the files are the ones the
run used on the author's machine, left as produced.

Attribution:
- AMI Meeting Corpus, the 16 meetings of the full-corpus test split (EN2002a-d,
  ES2004a-d, IS1009a-d, TS3003a-d; Mix-Headset audio and manual annotations),
  CC BY 4.0, University of Edinburgh and the AMI consortium,
  https://groups.inf.ed.ac.uk/ami/corpus/. `hyp/ami/oracle/` is the reference
  converted by `python -m evals.ami`; every other hypothesis is a machine
  transcript or diarization of that audio.
- Transcripts produced with faster-whisper `small.en` (MIT); diarization with
  sherpa-onnx (Apache-2.0) and the models listed in docs/pipeline.md §11.2;
  `embedding-cluster` with SpeechBrain `spkrec-ecapa-voxceleb` (Apache-2.0).

Regenerate with the commands in `docs/v5-test-split.md` §11.
