"""Pinned, openly licensed model files for the model-backed pipelines.

Every file a model-backed pipeline in this package loads is listed in
``ASSETS`` with its source URL, sha256, size and licence, so an operator can
re-fetch the weights byte-identically; ``require`` hashes the files a
pipeline is about to load, refuses any whose sha256 differs from the pin,
and returns the COMPUTED digests as the provenance a hypothesis records.
All of it is HARNESS_POLICY (choice of model, local layout, verification
rule); none of it is a claim about the Plaud device.

Rules (HARNESS_POLICY):

* Weights live under ``data/models/`` (git-ignored) unless
  ``$PLAUD_HARNESS_MODELS_DIR`` points elsewhere.  Nothing here downloads
  implicitly: ``fetch`` runs only from ``python -m pipeline fetch-models``
  (or a caller that asks for it), never from a pipeline's constructor.
* Downloads are plain HTTPS GETs from the pinned URL with **no
  Authorization header** (no Hugging Face token is read or sent), written to
  ``<name>.part``, checked against the pinned sha256 and only then renamed
  into place.  A mismatch deletes the partial file and raises.
* Only ungated sources are listed.  pyannote's own segmentation-3.0 repo on
  Hugging Face is gated (a click-through form); the harness uses k2-fsa's
  ONNX conversion from the sherpa-onnx GitHub release instead, which needs no
  account.  The model is MIT-licensed, which permits that redistribution.
* ``missing_reason``/``status`` (used by registry availability checks)
  check presence and size only, so ``python -m pipeline list`` stays fast.
  A pipeline constructor calls ``require``, which hashes every file it will
  load (memoised per process by device, inode, size and mtime; small.en's
  486 MB take about 0.3 s on the M1) and raises ``PipelineUnavailable`` on
  any difference from the pin, so a same-size file with other contents is
  never run or reported as the pinned weights.  ``verify`` is the CLI's
  uncached full check.  Explicit (unpinned) model paths are hashed too
  (``describe_local``) and recorded as unpinned.

Licence notes were re-checked against the cited pages on 2026-09-25; the
training-data caveats are documented, not resolved (docs/pipeline.md §11).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MODELS_DIR_ENV = "PLAUD_HARNESS_MODELS_DIR"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS_DIR = REPO_ROOT / "data" / "models"

#: HARNESS_POLICY: network timeout per request (seconds) and read chunk size.
DOWNLOAD_TIMEOUT_S = 120
_CHUNK = 1 << 20


def models_dir() -> Path:
    """The model root: ``$PLAUD_HARNESS_MODELS_DIR`` or ``<repo>/data/models``."""
    env = os.environ.get(MODELS_DIR_ENV, "").strip()
    return Path(env).expanduser() if env else DEFAULT_MODELS_DIR


@dataclass(frozen=True)
class ModelFile:
    """One file of an asset, relative to the asset's directory.

    ``url`` is a direct download; ``archive_member`` instead names the member
    of the asset's ``archive`` it is extracted from.
    """

    relpath: str
    sha256: str
    size: int
    url: str | None = None
    archive_member: str | None = None


@dataclass(frozen=True)
class Archive:
    url: str
    sha256: str
    size: int
    fmt: str = "tar.bz2"


@dataclass(frozen=True)
class ModelAsset:
    key: str
    kind: str  # "asr" | "segmentation" | "speaker-embedding"
    subdir: str  # under models_dir()
    entry: str  # what the loader is given, relative to the asset dir ("." = the dir itself)
    files: tuple[ModelFile, ...]
    licence: str
    licence_source: str
    upstream: str
    caveats: tuple[str, ...] = ()
    archive: Archive | None = None
    revision: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return models_dir() / self.subdir

    @property
    def path(self) -> Path:
        """The path handed to the library (a directory or a single file)."""
        return self.directory if self.entry in ("", ".") else self.directory / self.entry

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    def describe(self) -> dict[str, Any]:
        """The manifest entry: what SHOULD be on disk (pins only; nothing is
        read).  ``require`` adds the digests of what IS on disk."""
        d: dict[str, Any] = {
            "key": self.key,
            "kind": self.kind,
            "path": str(self.path),
            "licence": self.licence,
            "licence_source": self.licence_source,
            "upstream": self.upstream,
            "bytes": self.size,
            "files": {f.relpath: {"pinned_sha256": f.sha256, "bytes": f.size, "url": f.url or (self.archive.url if self.archive else None)} for f in self.files},
        }
        if self.revision:
            d["revision"] = self.revision
        if self.archive is not None:
            d["archive"] = {"url": self.archive.url, "sha256": self.archive.sha256, "bytes": self.archive.size}
        return d


_FW_REPO = "Systran/faster-whisper-small.en"
_FW_REV = "d1d751a5f8271d482d14ca55d9e2deeebbae577f"


def _hf(repo: str, rev: str, name: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/{rev}/{name}"


_SHERPA_SEG_TAR = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
_SHERPA_SPK = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"  # sic: the release tag is spelled this way

ASSETS: dict[str, ModelAsset] = {
    a.key: a
    for a in (
        ModelAsset(
            key="faster-whisper-small.en",
            kind="asr",
            subdir="faster-whisper/small.en",
            entry=".",
            revision=_FW_REV,
            files=(
                ModelFile("config.json", "666a9605530ac1f61fa8177f3702b4dacec9966749e42610839fcc32661d5fae", 2657, _hf(_FW_REPO, _FW_REV, "config.json")),
                ModelFile("model.bin", "62b2a45b05ee59acb4a5341b33ee35e041395d378d418a18acfe4c9e768ee37a", 483545366, _hf(_FW_REPO, _FW_REV, "model.bin")),
                ModelFile("tokenizer.json", "929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df", 2128466, _hf(_FW_REPO, _FW_REV, "tokenizer.json")),
                ModelFile("vocabulary.txt", "ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf", 422309, _hf(_FW_REPO, _FW_REV, "vocabulary.txt")),
            ),
            licence="MIT",
            licence_source="https://huggingface.co/Systran/faster-whisper-small.en (model card 'license: mit'; ungated)",
            upstream="openai/whisper small.en converted to CTranslate2 (float16 weights; quantised to int8 at load time)",
            caveats=(
                "OpenAI has not published Whisper's training data (680k hours of web audio); its licences are unknown.",
                "The upstream weights carry two licence declarations (checked 2026-09-25): OpenAI's GitHub "
                "repository https://github.com/openai/whisper is MIT (LICENSE, 'Copyright (c) 2022 OpenAI'), "
                "while the Hugging Face card of openai/whisper-small.en declares 'license: apache-2.0'.  "
                "Both are permissive; this harness records the conversion's own card (MIT) and does not "
                "resolve which upstream declaration governs.",
            ),
        ),
        ModelAsset(
            key="pyannote-segmentation-3.0-onnx",
            kind="segmentation",
            subdir="sherpa-onnx/sherpa-onnx-pyannote-segmentation-3-0",
            entry="model.onnx",
            archive=Archive(_SHERPA_SEG_TAR, "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488", 6958444),
            files=(
                ModelFile("model.onnx", "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079", 5992913,
                          archive_member="sherpa-onnx-pyannote-segmentation-3-0/model.onnx"),
                ModelFile("LICENSE", "14d7016ad68e7394d6e6b78d96cc2ae431c905287b89674cfdf021e79e62b8ba", 1061,
                          archive_member="sherpa-onnx-pyannote-segmentation-3-0/LICENSE"),
            ),
            licence="MIT (Copyright (c) 2022 CNRS; LICENSE shipped in the archive)",
            licence_source=(
                "archive LICENSE file; https://huggingface.co/pyannote/segmentation-3.0 card metadata 'license: mit' "
                "(that repo is gated; this harness did not accept its form and uses k2-fsa's ungated ONNX conversion)"
            ),
            upstream="pyannote/segmentation-3.0 (powerset, 10 s windows) exported to ONNX by k2-fsa (sherpa-onnx)",
            caveats=(
                "Trained on the combined training sets of AISHELL, AliMeeting, AMI, AVA-AVD, DIHARD, Ego4D, MSDWild, "
                "REPERE and VoxConverse (model card).  AMI test meetings are held out only if pyannote used the "
                "standard AMI split, which this harness cannot verify.",
            ),
        ),
        ModelAsset(
            key="3dspeaker-campplus-en-voxceleb",
            kind="speaker-embedding",
            subdir="sherpa-onnx",
            entry="3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
            files=(
                ModelFile("3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
                          "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b", 29596978,
                          _SHERPA_SPK + "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"),
            ),
            licence="Apache-2.0",
            licence_source="https://modelscope.cn/models/iic/speech_campplus_sv_en_voxceleb_16k (License: Apache License 2.0)",
            upstream="3D-Speaker CAM++ speaker verification, English, trained on VoxCeleb2 (5994 speakers); ONNX by k2-fsa",
            caveats=(
                "Training data VoxCeleb2.  https://www.robots.ox.ac.uk/~vgg/data/voxceleb/vox2.html (read "
                "2026-09-25) says 'The provided VoxCeleb2 metadata is licensed under a Creative Commons "
                "Attribution-ShareAlike 4.0 International License' and that the URLs and timestamps, audio "
                "files, video files and identifying metadata 'are no longer available from this website'.  "
                "It states no licence for the audio itself.  (An earlier note here quoted a CC BY 4.0 "
                "'for research purposes' statement; it is not on the current pages and was removed as unverified.)",
            ),
        ),
    )
}


# --- status / verification -----------------------------------------------------------------------


def get_asset(key: str) -> ModelAsset:
    try:
        return ASSETS[key]
    except KeyError:
        raise KeyError(f"unknown model asset {key!r}; known: {sorted(ASSETS)}") from None


def missing_reason(key: str) -> str | None:
    """None when every file of the asset is present with its pinned size
    (cheap; no hashing), else a one-line reason naming the fetch command."""
    a = get_asset(key)
    bad = []
    for f in a.files:
        p = a.directory / f.relpath
        if not p.is_file():
            bad.append(f"{f.relpath} absent")
        elif p.stat().st_size != f.size:
            bad.append(f"{f.relpath} is {p.stat().st_size} B, pinned {f.size} B")
    if not bad:
        return None
    return (
        f"model weights not fetched: {a.key} under {a.directory} ({'; '.join(bad)}); "
        f"run `python -m pipeline fetch-models {a.key}`"
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def verify(key: str) -> list[str]:
    """Hash every file; return the problems (empty list == verified)."""
    a = get_asset(key)
    problems = []
    for f in a.files:
        p = a.directory / f.relpath
        if not p.is_file():
            problems.append(f"{p}: absent")
            continue
        got = sha256_file(p)
        if got != f.sha256:
            problems.append(f"{p}: sha256 {got} != pinned {f.sha256}")
    return problems


# --- load-time check: provenance of the bytes actually loaded ------------------------------------

#: Prefix of the reason when a present file's bytes differ from its pin.
MISMATCH_PREFIX = "model weights do not match their pinned sha256: "

_DIGESTS: dict[tuple[int, int, int, int], str] = {}


def file_sha256(path: str | Path) -> str:
    """sha256 of the bytes at ``path`` (symlinks followed), memoised for this
    process by ``(st_dev, st_ino, st_size, st_mtime_ns)`` so that building a
    pipeline twice hashes its weights once.  A replacement (new inode, size or
    mtime) is re-hashed; a file rewritten in place keeping its size AND its
    mtime_ns within one process would not be (HARNESS_POLICY: accepted)."""
    st = os.stat(path)
    key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    got = _DIGESTS.get(key)
    if got is None:
        got = sha256_file(Path(path))
        _DIGESTS[key] = got
    return got


def require(key: str) -> tuple[Path, dict[str, Any]]:
    """Check a pinned asset right before a loader reads it and return
    ``(loader path, provenance)``.

    Every file is hashed (``file_sha256``) and must equal its pin.  The
    provenance is ``describe()`` with ``files[rel]["sha256"]`` set to the
    COMPUTED digest (next to ``pinned_sha256``), ``"pinned": True`` and
    ``"sha256_matches_pin": True``.  Raises ``PipelineUnavailable`` when a
    file is absent or has the wrong size (``missing_reason``) or when a
    digest differs (reason starts with ``MISMATCH_PREFIX`` and names the
    fetch command, which replaces such files).  The file could still be
    swapped between this hash and the library's own read; that window is
    not closed.
    """
    from .base import PipelineUnavailable

    reason = missing_reason(key)
    if reason:
        raise PipelineUnavailable(reason)
    a = get_asset(key)
    files: dict[str, Any] = {}
    bad: list[str] = []
    for f in a.files:
        got = file_sha256(a.directory / f.relpath)
        files[f.relpath] = {"sha256": got, "pinned_sha256": f.sha256, "bytes": f.size,
                            "url": f.url or (a.archive.url if a.archive else None)}
        if got != f.sha256:
            bad.append(f"{f.relpath} sha256 {got}, pinned {f.sha256}")
    if bad:
        raise PipelineUnavailable(
            f"{MISMATCH_PREFIX}{a.key} under {a.directory} ({'; '.join(bad)}); "
            f"run `python -m pipeline fetch-models {a.key}` to replace them"
        )
    prov = a.describe()
    prov.update({"pinned": True, "sha256_matches_pin": True, "files": files})
    return a.path, prov


def describe_local(path: str | Path) -> dict[str, Any]:
    """Provenance of an UNPINNED model given by path: a file, or every regular
    file at the top level of a directory (a CTranslate2 model directory is
    flat), each with its computed sha256 and size (symlinks followed, as in a
    Hugging Face cache snapshot)."""
    p = Path(path)
    members = sorted(q for q in p.iterdir() if q.is_file()) if p.is_dir() else [p]
    return {
        "path": str(p),
        "pinned": False,
        "files": {q.name: {"sha256": file_sha256(q), "bytes": q.stat().st_size} for q in members},
    }


def status() -> list[dict[str, Any]]:
    rows = []
    for key, a in sorted(ASSETS.items()):
        reason = missing_reason(key)
        rows.append({"key": key, "kind": a.kind, "present": reason is None, "reason": reason,
                     "bytes": a.size, "path": str(a.path), "licence": a.licence})
    return rows


# --- fetching -----------------------------------------------------------------------------------------

Opener = Callable[[str], Any]


def _default_open(url: str) -> Any:
    # A bare Request: no Authorization header, no token, no cookies.
    req = urllib.request.Request(url, headers={"User-Agent": "plaud-harness-model-fetch/1"})
    return urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S)  # noqa: S310 - pinned https URLs


def _download(url: str, dest: Path, sha256: str, size: int, opener: Opener) -> None:
    """GET ``url`` into ``dest`` via ``dest.part``; refuse a size/sha mismatch."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    n = 0
    try:
        with opener(url) as resp, open(part, "wb") as out:
            for block in iter(lambda: resp.read(_CHUNK), b""):
                out.write(block)
                h.update(block)
                n += len(block)
        if n != size or h.hexdigest() != sha256:
            raise ValueError(f"{url}: got {n} B sha256 {h.hexdigest()}, pinned {size} B sha256 {sha256}")
        os.replace(part, dest)
    finally:
        if part.exists():
            part.unlink()


def _extract_member(archive: Path, fmt: str, member: str, dest: Path, f: ModelFile) -> None:
    if fmt != "tar.bz2":
        raise ValueError(f"unsupported archive format {fmt!r}")
    part = dest.with_name(dest.name + ".part")
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            info = tar.getmember(member)  # KeyError if absent: never extract by pattern
            if not info.isfile():
                raise ValueError(f"{archive}: {member} is not a regular file")
            src = tar.extractfile(info)
            if src is None:
                raise ValueError(f"{archive}: cannot read {member}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with src, open(part, "wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
        got = sha256_file(part)
        if part.stat().st_size != f.size or got != f.sha256:
            raise ValueError(f"{member}: sha256 {got} != pinned {f.sha256}")
        os.replace(part, dest)
    finally:
        if part.exists():
            part.unlink()


def fetch(key: str, *, opener: Opener | None = None, force: bool = False, log: Callable[[str], None] = print) -> Path:
    """Download (or keep) every file of ``key``; return the loader path.

    A file already present with the pinned sha256 is kept (so fetching into a
    directory populated by hand is a verification pass).  Anything else is
    re-downloaded and checked.
    """
    a = get_asset(key)
    op = opener or _default_open
    todo = []
    for f in a.files:
        p = a.directory / f.relpath
        if not force and p.is_file() and p.stat().st_size == f.size and sha256_file(p) == f.sha256:
            log(f"{a.key}: {f.relpath} present, sha256 ok")
            continue
        todo.append(f)
    direct = [f for f in todo if f.url is not None]
    from_archive = [f for f in todo if f.archive_member is not None]
    for f in direct:
        log(f"{a.key}: downloading {f.relpath} ({f.size} B) from {f.url}")
        _download(str(f.url), a.directory / f.relpath, f.sha256, f.size, op)
    if from_archive:
        if a.archive is None:
            raise ValueError(f"{a.key}: files name archive members but the asset has no archive")
        with tempfile.TemporaryDirectory(prefix="plaud-model-") as tmp:
            arc = Path(tmp) / "archive"
            log(f"{a.key}: downloading archive ({a.archive.size} B) from {a.archive.url}")
            _download(a.archive.url, arc, a.archive.sha256, a.archive.size, op)
            for f in from_archive:
                _extract_member(arc, a.archive.fmt, str(f.archive_member), a.directory / f.relpath, f)
                log(f"{a.key}: extracted {f.relpath}, sha256 ok")
    return a.path
