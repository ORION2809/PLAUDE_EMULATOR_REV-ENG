"""Layer 3 — evaluation harness for the plaud-harness pipeline.

The package scores a pipeline *hypothesis* (``hyp.json``) against a meeting
directory's ground truth (``meeting.json`` / ``ref.rttm`` / ``ref.stm``) with
the three libraries PROJECT.md §3 names for this layer:

* ``pyannote.metrics`` — DER, JER (:mod:`evals.metrics`)
* ``jiwer``           — plain WER with S/D/I counts
* ``meeteval``        — cpWER (headline ASR metric, decision log 2026-09-21)
  and tcpWER

and applies named CI gates (:mod:`evals.gates`, ``evals/gates.yaml``).

Evidence classes, in the sense of docs/protocol-ledger.md: nothing in this
package encodes device, SDK or cloud behaviour.  Every number the harness
*chooses* (collars, default suites, file layouts, exit codes) is labelled
HARNESS_POLICY where it is defined.  The metric definitions themselves are
the libraries' — the docstrings say which call produces which number.
"""

from __future__ import annotations

__version__ = "0.1.0"

from evals.io import (  # noqa: E402
    ContractError,
    Hypothesis,
    Meeting,
    Normalizer,
    Segment,
    Word,
    load_hypothesis,
    load_meeting,
)
from evals.metrics import MeetingReport, score_meeting  # noqa: E402

__all__ = [
    "ContractError",
    "Hypothesis",
    "Meeting",
    "MeetingReport",
    "Normalizer",
    "Segment",
    "Word",
    "load_hypothesis",
    "load_meeting",
    "score_meeting",
    "__version__",
]
