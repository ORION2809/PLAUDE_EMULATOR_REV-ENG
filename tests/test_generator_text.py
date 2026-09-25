"""Layer 2 text source: deterministic, clean lowercase tokens."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from generator.text import VOCABULARY, TextSource, normalise_tokens

TOKEN = re.compile(r"^[a-z]+$")


def test_vocabulary_is_letters_only_and_pronounceable() -> None:
    assert len(VOCABULARY) >= 100
    for w in VOCABULARY:
        assert TOKEN.match(w), w
        assert any(c in "aeiouy" for c in w), f"{w} has no vowel letter for the formant backend"


def test_same_seed_gives_same_utterances_and_different_seeds_differ() -> None:
    a = [TextSource(seed=5).utterance(8) for _ in range(3)]
    b = [TextSource(seed=5).utterance(8) for _ in range(3)]
    c = [TextSource(seed=6).utterance(8) for _ in range(3)]
    assert a == b
    assert a != c


def test_utterance_has_exact_token_count_in_contract_form() -> None:
    src = TextSource(seed=1)
    for n in (1, 4, 14):
        text = src.utterance(n)
        tokens = text.split(" ")
        assert len(tokens) == n
        assert text == " ".join(tokens) == text.lower()
        assert all(TOKEN.match(t) for t in tokens)
    with pytest.raises(ValueError):
        src.utterance(0)


def test_corpus_windows_are_contiguous_and_normalised(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.txt"
    corpus.write_text("Hello, WORLD! This is a small corpus; it has punctuation -- and 12 numbers.\n")
    src = TextSource(seed=2, corpus_path=corpus)
    tokens = normalise_tokens(corpus.read_text())
    assert tokens == ["hello", "world", "this", "is", "a", "small", "corpus", "it", "has", "punctuation", "and", "numbers"]
    for _ in range(10):
        words = src.words(4)
        joined = " ".join(words)
        assert joined in " ".join(tokens), f"{joined!r} is not a contiguous window"
    # asking for more than the corpus holds wraps instead of failing
    assert len(src.words(30)) == 30


def test_empty_or_tiny_corpus_is_rejected(tmp_path: Path) -> None:
    corpus = tmp_path / "empty.txt"
    corpus.write_text("!!! 123\n")
    with pytest.raises(ValueError):
        TextSource(seed=1, corpus_path=corpus)


def test_normalise_tokens_known_answer() -> None:
    assert normalise_tokens("  A  b-c  D1e ") == ["a", "bc", "de"]
    assert normalise_tokens("") == []
