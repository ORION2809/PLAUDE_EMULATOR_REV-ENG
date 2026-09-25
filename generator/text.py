"""Deterministic text source for synthetic meetings.

Produces lowercase letter-only tokens separated by single spaces, which is
what the shared contract requires generators to emit. Two modes:

* seeded vocabulary sampling (default, offline, no files), and
* a plain-text corpus file supplied by the caller, from which contiguous
  windows are cut.

Everything here is HARNESS_POLICY: the vocabulary, the Zipf-like weighting
and the window sampling are harness choices with no device or product
evidence behind them. The words are chosen to be pronounceable by the
formant backend (letters only, every token has at least one vowel letter).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np

# HARNESS_POLICY: a small, fixed, letters-only vocabulary. Ordered; index
# order is part of the deterministic output.
VOCABULARY: tuple[str, ...] = (
    "the", "and", "we", "you", "that", "this", "with", "for", "have", "not",
    "meeting", "agenda", "budget", "project", "release", "customer", "design",
    "review", "update", "status", "timeline", "quarter", "report", "issue",
    "feature", "device", "battery", "audio", "record", "upload", "summary",
    "transcript", "speaker", "channel", "signal", "noise", "sample", "frame",
    "packet", "stream", "storage", "cloud", "server", "client", "network",
    "schedule", "deadline", "priority", "decision", "action", "owner", "risk",
    "metric", "target", "result", "test", "check", "verify", "measure",
    "number", "value", "table", "column", "figure", "chart", "model", "data",
    "point", "level", "plan", "goal", "step", "next", "last", "first", "second",
    "week", "month", "today", "tomorrow", "morning", "afternoon", "later",
    "again", "still", "maybe", "really", "quite", "very", "about", "around",
    "between", "before", "after", "during", "under", "over", "into", "from",
    "think", "agree", "suggest", "propose", "confirm", "cancel", "delay",
    "finish", "start", "begin", "close", "open", "send", "receive", "share",
    "write", "read", "listen", "speak", "answer", "question", "problem",
    "solution", "reason", "example", "detail", "option", "version", "change",
    "small", "large", "early", "late", "quick", "slow", "simple", "complex",
    "ready", "done", "good", "better", "clear", "sure", "okay", "right",
    "people", "team", "manager", "engineer", "office", "room", "table",
    "phone", "laptop", "screen", "camera", "microphone", "speaker", "cable",
    "power", "charge", "memory", "search", "index", "note", "page", "file",
    "folder", "name", "title", "label", "item", "list", "queue", "job",
    "minute", "hour", "second", "moment", "time", "period", "cycle", "phase",
)


def _seed_int(*parts: object) -> int:
    material = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "little")


def normalise_tokens(text: str) -> list[str]:
    """Lowercase, keep letters only, drop empties; the generator's clean form."""
    out = []
    for raw in text.lower().split():
        token = re.sub(r"[^a-z]", "", raw)
        if token:
            out.append(token)
    return out


class TextSource:
    """Seeded utterance generator.

    `utterance(n)` returns exactly n lowercase tokens joined by single spaces.
    With a corpus the tokens are a contiguous window of that corpus; without
    one they are sampled from VOCABULARY with Zipf-like weights (rank^-0.8,
    HARNESS_POLICY) so common words repeat the way they do in speech.
    """

    def __init__(
        self,
        seed: int,
        corpus_path: str | Path | None = None,
        vocabulary: tuple[str, ...] | None = None,
    ) -> None:
        self.seed = int(seed)
        self.vocabulary = tuple(vocabulary or VOCABULARY)
        if not self.vocabulary:
            raise ValueError("vocabulary must not be empty")
        self.corpus: list[str] | None = None
        if corpus_path is not None:
            tokens = normalise_tokens(Path(corpus_path).read_text(encoding="utf-8"))
            if len(tokens) < 2:
                raise ValueError(f"corpus {corpus_path} yields fewer than two tokens")
            self.corpus = tokens
        self._rng = np.random.default_rng(_seed_int("text", self.seed))
        ranks = np.arange(1, len(self.vocabulary) + 1, dtype=np.float64)
        weights = ranks ** -0.8
        self._weights = weights / weights.sum()

    def words(self, n: int) -> list[str]:
        n = int(n)
        if n < 1:
            raise ValueError("n must be >= 1")
        if self.corpus is not None:
            if n >= len(self.corpus):
                reps = n // len(self.corpus) + 1
                pool = self.corpus * reps
            else:
                pool = self.corpus
            start = int(self._rng.integers(0, max(1, len(pool) - n + 1)))
            return list(pool[start : start + n])
        idx = self._rng.choice(len(self.vocabulary), size=n, p=self._weights)
        return [self.vocabulary[int(i)] for i in idx]

    def utterance(self, n: int) -> str:
        return " ".join(self.words(n))


__all__ = ["VOCABULARY", "TextSource", "normalise_tokens"]
