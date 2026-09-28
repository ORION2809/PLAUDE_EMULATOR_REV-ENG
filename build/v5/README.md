# V5 evidence (published)

Outputs of `scripts/run-v5.sh` from the 25 September 2026 run described in
`docs/v5-results.md`: scoring reports (`reports/`), system hypotheses (`hyp/`),
per-run logs (`logs/`), gate checks (`gates/`), input hashes (`inputs/`) and the
summaries. No audio and no model weights are included.

Attribution:
- AMI Meeting Corpus (ES2004a, IS1009a, TS3003a, EN2002a; Mix-Headset audio and
  manual annotations), CC BY 4.0, University of Edinburgh and the AMI consortium,
  https://groups.inf.ed.ac.uk/ami/corpus/. The hypotheses under `hyp/ami/` are
  machine transcripts and diarizations of that audio.
- Synthetic Piper meetings (`hyp/piper*`) were rendered with the Piper voice
  `en_US-libritts_r-medium` (LibriTTS-R, CC BY 4.0; see docs/generator.md for the
  unresolved licence question about its base voice).
- Transcripts produced with faster-whisper `small.en` (MIT) and diarization with
  sherpa-onnx (Apache-2.0) models listed in docs/pipeline.md.

Regenerate with `scripts/run-v5.sh`; references are rebuilt from the AMI
annotations by `python -m evals.ami` (see docs/evals.md).
