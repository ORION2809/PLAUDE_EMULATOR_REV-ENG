"""Template citations in mockcloud/ and docs/mockcloud.md point at real lines
(review finding MC-6: several cited lines past the end of a 270-line file).

Every `<file>.kt|.swift|.ts:<line>` cite is resolved under reference/plaud-org
and must fall inside the file; the cites the review corrected must also land
on the line that carries the quoted construct. reference/plaud-org is fetched
locally only (./scripts/fetch-references.sh, proprietary; never in CI), so the
test skips without it under the documented `sdk-aar` gate (tests/conftest.py).
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORG = ROOT / "reference" / "plaud-org"
SOURCES = sorted((ROOT / "mockcloud").rglob("*.py")) + [ROOT / "docs" / "mockcloud.md"]
CITE = re.compile(r"([\w./-]*[\w-]+\.(?:kt|swift|ts)):(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)")

#: (file suffix, line) -> text that line must contain.
ANCHORS = {
    ("TranscriptionManager.kt", 158): 'replace("\\"", "")',
    ("TranscriptionManager.kt", 225): 'optJSONArray("results")',
    ("TranscriptionManager.kt", 231): 'optString("message", status)',
    ("PlaudAPIService.swift", 118): 'replacingOccurrences(of: "\\"", with: "")',
    ("PlaudAPIService.swift", 452): '"speaker_id"',
    ("FileDetailActivity.kt", 238): '"speaker_id"',
    ("presign/route.ts", 28): "filesize <= 0",
    ("presign/route.ts", 31): '"opus"',
}


def _require_references() -> None:
    if not (ORG / "plaud-sdk-public").is_dir():
        pytest.skip("SDK not fetched: reference/plaud-org/plaud-sdk-public absent (template sources for the cite check)")


@functools.lru_cache(maxsize=1)
def _index() -> dict[str, list[Path]]:
    """basename -> files under reference/plaud-org (template sources only)."""
    out: dict[str, list[Path]] = {}
    for dirpath, _, names in os.walk(ORG):
        for n in names:
            if n.endswith((".kt", ".swift", ".ts")):
                out.setdefault(n, []).append(Path(dirpath) / n)
    return out


def _files_for(cited: str) -> list[Path]:
    tail = cited.split("...")[-1].strip("/")
    tail = tail.removeprefix("reference/plaud-org/")
    return [p for p in _index().get(Path(tail).name, []) if p.as_posix().endswith("/" + tail)]


def _cites() -> list[tuple[str, str, list[tuple[int, int]]]]:
    """(source file, cited path, [(first, last) line ranges])."""
    out = []
    for src in SOURCES:
        for m in CITE.finditer(src.read_text()):
            ranges: list[tuple[int, int]] = []
            for part in m.group(2).split(","):
                a, _, b = part.partition("-")
                ranges.append((int(a), int(b or a)))
            out.append((str(src.relative_to(ROOT)), m.group(1), ranges))
    return out


def _line(path: Path, n: int) -> str:
    return path.read_text(errors="replace").splitlines()[n - 1]


def test_every_template_cite_is_inside_its_file() -> None:
    _require_references()
    cites = _cites()
    assert len(cites) >= 20, "the scan found the cites"
    bad = []
    for src, cited, ranges in cites:
        files = _files_for(cited)
        if not files:
            bad.append(f"{src}: {cited} not found under reference/plaud-org")
            continue
        last = max(b for _, b in ranges)
        if any(a > b for a, b in ranges) or not any(
                last <= len(f.read_text(errors="replace").splitlines()) for f in files):
            bad.append(f"{src}: {cited}:{ranges} is past the end of the file")
    assert bad == []


def test_the_corrected_cites_land_on_the_quoted_construct() -> None:
    _require_references()
    cites = [(c, r) for _, c, ranges in _cites() for r in ranges]
    for (suffix, n), needle in ANCHORS.items():
        assert any(c.endswith(suffix) and a <= n <= b for c, (a, b) in cites), f"{suffix}:{n} is not cited"
        files = [f for f in _files_for(Path(suffix).name) if f.as_posix().endswith(suffix)]
        assert files, suffix
        assert any(needle in _line(f, n) for f in files), f"{suffix}:{n} does not contain {needle!r}"
