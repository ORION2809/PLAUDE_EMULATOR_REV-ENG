"""docs/evals.md must not contradict the code, the libraries or the repository.

Two review findings (2026-09-25) were documentation errors: the tcpWER
default was attributed to meeteval (EV-9), and the status line said the
harness had never been run on generator output (EV-11).  These checks pin
the corrected statements to the facts they rest on.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO))

DOC = (REPO / "docs" / "evals.md").read_text(encoding="utf-8")
METRICS_SRC = (REPO / "evals" / "metrics.py").read_text(encoding="utf-8")


def test_tcp_collar_default_is_harness_policy_because_meeteval_has_none() -> None:
    """EV-9.  meeteval 0.4.3's tcp_word_error_rate takes ``collar`` as a required
    keyword and its CLI declares --collar required; only the 5 s *recommendation*
    is meeteval's (meeteval/wer/preprocess.py:398)."""
    from meeteval.wer.wer.time_constrained import tcp_word_error_rate

    assert inspect.signature(tcp_word_error_rate).parameters["collar"].default is inspect.Parameter.empty
    for text in (DOC, METRICS_SRC):
        assert "meeteval's own default" not in text and "meeteval's own CLI default" not in text
        assert "preprocess.py:398" in text
    uem_bullet = DOC.split("**Scoring region (UEM):**")[1].split("\n* ")[0]
    assert "silent" not in uem_bullet and "with a warning" in uem_bullet  # pyannote warns (utils.py:200-202)
    assert "silently" not in METRICS_SRC.split("def to_uem")[1].split("def to_seglst")[0]


def test_status_reflects_that_generator_output_is_scored() -> None:
    """EV-11.  tests/test_v4_e2e.py and docker/job.sh run generator output through evals."""
    assert "from evals" in (REPO / "tests" / "test_v4_e2e.py").read_text(encoding="utf-8")
    assert "python -m evals batch" in (REPO / "docker" / "job.sh").read_text(encoding="utf-8")
    assert "has not yet been run on generator output" not in DOC
    assert "No generator integration" not in DOC
    for cited in ("tests/test_v4_e2e.py", "docker/job.sh", "tests/test_integration_mockcloud_roundtrip.py"):
        assert cited in DOC, cited
