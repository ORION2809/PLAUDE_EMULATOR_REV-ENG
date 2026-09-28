"""In-memory mock-cloud state with optional JSON persistence and a request log.

Nothing here is a claim about how Plaud stores anything: the shapes are what
the documented endpoints need in order to answer consistently, and they are all
HARNESS_POLICY. The persistence files live under build/mockcloud/ which
.gitignore already excludes (the whole build/ tree).

Persistence layout for `--persist PATH` (HARNESS_POLICY, review finding MC-9):

    PATH                     state.json: every table EXCEPT object bytes and the log
    <stem>.objects/<sha256>  one file per distinct object body, written once
    <stem>.log.jsonl         the request log, appended one line per entry

so a save after each request costs the size of the metadata plus what is new,
not a base64 rewrite of every stored byte. Older single-file snapshots
(objects as `data_b64`, the log inline) still load.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "plaud-harness/mockcloud-state/1"

#: GoReplay's on-disk framing (reference/upstream/goreplay/protocol.go:31,
#: :50-53): one "<type> <id> <timestamp_ns> <latency_ns>\n" header line, then
#: the raw HTTP message, records joined by "\n🐵🙈🙉\n". Type '1' = request.
GOR_SEPARATOR = "\n🐵🙈🙉\n"

#: HARNESS_POLICY: Host written into an exported record whose log entry has
#: none (entries logged before the host header was kept). `gor --output-http`
#: replaces req.Host with the target anyway (reference/upstream/goreplay/
#: output_http.go:411-413), so the value only has to make the record parse.
GOR_FALLBACK_HOST = "127.0.0.1:8787"

#: Headers the export always writes itself (framing), never from the log.
_GOR_FRAMING_HEADERS = {"host", "content-length", "transfer-encoding"}


def _key(*parts: str) -> str:
    return "/".join(parts)


@dataclass
class DeviceRecord:
    type: str
    sn: str
    owner: str | None = None            # the user-token `sub` that owns it
    bind_history: list[str] = field(default_factory=list)  # newest first
    signed_by: list[str] = field(default_factory=list)     # subs that sn-signed it
    registered_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class MultipartUpload:
    file_id: str
    upload_id: str
    filetype: str
    filesize: int
    sub: str
    part_count: int
    created_at: float
    parts: dict[int, dict[str, Any]] = field(default_factory=dict)  # n -> {etag, size}
    completed: dict[str, Any] | None = None


@dataclass
class StoredObject:
    bucket: str
    key: str
    data: bytes
    content_type: str
    etag: str
    acl: str = "private"   # "private" needs a presigned signature, "public-read" does not
    created_at: float = 0.0
    sha256: str = ""       # cached digest of `data` (the persistence blob name); filled lazily

    def digest(self) -> str:
        if not self.sha256:
            self.sha256 = hashlib.sha256(self.data).hexdigest()
        return self.sha256


@dataclass
class TranscriptionTask:
    transcription_id: str
    status: str            # one of the Celery names, openapi_transcription.json TranscriptionStatus
    file_url: str
    params: dict[str, Any]
    client_id: str
    created_at: float
    updated_at: float
    data: dict[str, Any] = field(default_factory=dict)
    message: str | None = None
    history: list[tuple[str, float]] = field(default_factory=list)
    source: str | None = None   # how the mock resolved file_url (for /_mock/state)


class MockState:
    """All mutable state. A single lock guards persistence; the ASGI app is
    single-threaded per event loop so route handlers do not lock."""

    def __init__(self, persist_path: Path | None = None) -> None:
        self.persist_path = Path(persist_path) if persist_path else None
        self._lock = threading.Lock()
        self._blobs_on_disk: set[str] = set()
        self._log_generation = 0
        self._log_saved_generation = -1
        self._log_saved_count = 0
        self.reset()
        if self.persist_path and self.persist_path.exists():
            self.load()

    # ------------------------------------------------------------------ core
    def reset(self) -> None:
        # A reset empties the log, so the next save rewrites the log file.
        self._log_generation += 1
        self.created_at = time.time()
        self.seq = 0
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.partner_tokens: dict[str, dict[str, Any]] = {}   # jti -> {client_id, exp}
        self.user_tokens: dict[str, dict[str, Any]] = {}      # jti -> {sub, user_id, client_id, exp}
        self.sdk_tokens: dict[str, dict[str, Any]] = {}       # token -> {client_id, exp}
        self.api_tokens: dict[str, dict[str, Any]] = {}       # token -> {client_id, exp}
        self.devices: dict[str, DeviceRecord] = {}            # "type/sn"
        self.keypairs: dict[str, dict[str, Any]] = {}         # sub -> {public_key, private_key, created_at}
        self.signatures: dict[str, dict[str, Any]] = {}       # signature -> {type, sn, sub, issued_at}
        self.multipart: dict[str, MultipartUpload] = {}       # upload_id
        self.objects: dict[str, StoredObject] = {}            # "bucket/key"
        self.files: dict[str, dict[str, Any]] = {}            # file_id -> {key, filetype, md5, download_url, expires_at}
        self.tasks: dict[str, TranscriptionTask] = {}
        self.meetings: dict[str, dict[str, Any]] = {}         # md5 -> {dir, file}
        self.signing_key_pem: str | None = None               # mock cloud RSA signer (PKCS#8 PEM)
        self.request_log: list[dict[str, Any]] = []

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    # --------------------------------------------------------------- devices
    def device(self, type_: str, sn: str) -> DeviceRecord | None:
        return self.devices.get(_key(type_, sn))

    def register_device(self, type_: str, sn: str, now: float) -> DeviceRecord:
        rec = self.devices.get(_key(type_, sn))
        if rec is None:
            rec = DeviceRecord(type=type_, sn=sn, registered_at=now, updated_at=now)
            self.devices[_key(type_, sn)] = rec
        return rec

    # --------------------------------------------------------------- objects
    def put_object(self, obj: StoredObject) -> None:
        self.objects[_key(obj.bucket, obj.key)] = obj

    def get_object(self, bucket: str, key: str) -> StoredObject | None:
        return self.objects.get(_key(bucket, key))

    def delete_object(self, bucket: str, key: str) -> None:
        self.objects.pop(_key(bucket, key), None)

    # ------------------------------------------------------------ persistence
    @property
    def objects_dir(self) -> Path | None:
        if not self.persist_path:
            return None
        return self.persist_path.with_name(self.persist_path.stem + ".objects")

    @property
    def log_path(self) -> Path | None:
        if not self.persist_path:
            return None
        return self.persist_path.with_name(self.persist_path.stem + ".log.jsonl")

    def snapshot(self, inline_bytes: bool = True, include_log: bool = True) -> dict[str, Any]:
        """The whole state as JSON-able data. With the defaults it is
        self-contained (object bytes as base64, the log inline); save() uses
        inline_bytes=False / include_log=False and writes those separately."""
        def obj(o: StoredObject) -> dict[str, Any]:
            out: dict[str, Any] = {
                "bucket": o.bucket, "key": o.key, "content_type": o.content_type,
                "etag": o.etag, "acl": o.acl, "created_at": o.created_at,
                "size": len(o.data), "sha256": o.digest(),
            }
            if inline_bytes:
                out["data_b64"] = base64.b64encode(o.data).decode("ascii")
            return out

        def task(t: TranscriptionTask) -> dict[str, Any]:
            return {
                "transcription_id": t.transcription_id, "status": t.status,
                "file_url": t.file_url, "params": t.params, "client_id": t.client_id,
                "created_at": t.created_at, "updated_at": t.updated_at, "data": t.data,
                "message": t.message, "history": [list(h) for h in t.history], "source": t.source,
            }

        def mp(m: MultipartUpload) -> dict[str, Any]:
            return {
                "file_id": m.file_id, "upload_id": m.upload_id, "filetype": m.filetype,
                "filesize": m.filesize, "sub": m.sub, "part_count": m.part_count,
                "created_at": m.created_at, "parts": {str(k): v for k, v in m.parts.items()},
                "completed": m.completed,
            }

        snap: dict[str, Any] = {
            "schema": SCHEMA,
            "saved_at": time.time(),
            "created_at": self.created_at,
            "seq": self.seq,
            "refresh_tokens": self.refresh_tokens,
            "partner_tokens": self.partner_tokens,
            "user_tokens": self.user_tokens,
            "sdk_tokens": self.sdk_tokens,
            "api_tokens": self.api_tokens,
            "devices": {k: vars(v) for k, v in self.devices.items()},
            "keypairs": self.keypairs,
            "signatures": self.signatures,
            "multipart": {k: mp(v) for k, v in self.multipart.items()},
            "objects": {k: obj(v) for k, v in self.objects.items()},
            "files": self.files,
            "tasks": {k: task(v) for k, v in self.tasks.items()},
            "meetings": self.meetings,
            "signing_key_pem": self.signing_key_pem,
        }
        if include_log:
            snap["request_log"] = self.request_log
        return snap

    def restore(self, snap: dict[str, Any], objects_dir: Path | None = None) -> None:
        if snap.get("schema") != SCHEMA:
            raise ValueError(f"unexpected state schema {snap.get('schema')!r}")
        self.reset()
        self.created_at = snap.get("created_at", self.created_at)
        self.seq = int(snap.get("seq", 0))
        for name in ("refresh_tokens", "partner_tokens", "user_tokens", "sdk_tokens",
                     "api_tokens", "keypairs", "signatures", "files", "meetings"):
            setattr(self, name, dict(snap.get(name, {})))
        self.devices = {k: DeviceRecord(**v) for k, v in snap.get("devices", {}).items()}
        self.multipart = {}
        for k, v in snap.get("multipart", {}).items():
            v = dict(v)
            v["parts"] = {int(n): p for n, p in v.get("parts", {}).items()}
            self.multipart[k] = MultipartUpload(**v)
        self.objects = {}
        for k, v in snap.get("objects", {}).items():
            v = dict(v)
            v.pop("size", None)
            if "data_b64" in v:
                data = base64.b64decode(v.pop("data_b64"))
            else:
                if objects_dir is None:
                    raise ValueError(f"object {k!r} is stored outside the snapshot and no objects dir was given")
                data = (objects_dir / v["sha256"]).read_bytes()
            obj = StoredObject(data=data, **v)
            if obj.sha256 and hashlib.sha256(data).hexdigest() != obj.sha256:
                raise ValueError(f"object {k!r}: stored bytes do not match their sha256")
            self.objects[k] = obj
        self.tasks = {}
        for k, v in snap.get("tasks", {}).items():
            v = dict(v)
            v["history"] = [tuple(h) for h in v.get("history", [])]
            self.tasks[k] = TranscriptionTask(**v)
        self.signing_key_pem = snap.get("signing_key_pem")
        self.request_log = list(snap.get("request_log", []))

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def save(self) -> None:
        """Persist incrementally (see the module docstring for the layout).
        Synchronous: its cost is the metadata JSON plus whatever object bodies
        and log lines are new since the previous save."""
        if not self.persist_path:
            return
        objects_dir, log_path = self.objects_dir, self.log_path
        assert objects_dir is not None and log_path is not None
        with self._lock:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            objects_dir.mkdir(exist_ok=True)
            referenced: set[str] = set()
            for o in self.objects.values():
                digest = o.digest()
                referenced.add(digest)
                if digest not in self._blobs_on_disk:
                    blob = objects_dir / digest
                    if not blob.is_file():
                        self._write_atomic(blob, o.data)
                    self._blobs_on_disk.add(digest)
            # request log: append what is new, rewrite after a reset
            if self._log_saved_generation != self._log_generation or self._log_saved_count > len(self.request_log):
                lines = "".join(json.dumps(e, sort_keys=True) + "\n" for e in self.request_log)
                self._write_atomic(log_path, lines.encode("utf-8"))
            elif self._log_saved_count < len(self.request_log):
                with log_path.open("a", encoding="utf-8") as fh:
                    for e in self.request_log[self._log_saved_count:]:
                        fh.write(json.dumps(e, sort_keys=True) + "\n")
            self._log_saved_generation = self._log_generation
            self._log_saved_count = len(self.request_log)
            snap = self.snapshot(inline_bytes=False, include_log=False)
            self._write_atomic(self.persist_path, json.dumps(snap, indent=1, sort_keys=True).encode("utf-8"))
            # drop bodies no object references any more (completed parts, reset)
            for digest in self._blobs_on_disk - referenced:
                try:
                    os.unlink(objects_dir / digest)
                except FileNotFoundError:
                    pass
            self._blobs_on_disk &= referenced

    def load(self) -> None:
        assert self.persist_path is not None
        snap = json.loads(self.persist_path.read_text())
        self.restore(snap, objects_dir=self.objects_dir)
        log_path = self.log_path
        if "request_log" not in snap and log_path is not None and log_path.is_file():
            self.request_log = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
        if self.request_log:
            self.seq = max(self.seq, max(int(e.get("seq", 0)) for e in self.request_log))
        objects_dir = self.objects_dir
        assert objects_dir is not None
        self._blobs_on_disk = {o.digest() for o in self.objects.values() if (objects_dir / o.digest()).is_file()}
        # an old single-file snapshot had its log inline: rewrite it as JSONL on the next save
        inline_log = "request_log" in snap
        self._log_saved_generation = -1 if inline_log else self._log_generation
        self._log_saved_count = 0 if inline_log else len(self.request_log)

    # ------------------------------------------------------------ request log
    def log_request(self, entry: dict[str, Any]) -> None:
        self.request_log.append(entry)

    def log_as_gor(self, entries: Iterable[dict[str, Any]] | None = None) -> str:
        """Render the request log in GoReplay's file format (type '1' requests).

        Timestamps are nanoseconds; latency is the measured handler time.
        Every record carries `Host` and a `Content-Length` equal to the UTF-8
        length of the body written after it: gor's output-http parses records
        with Go's http.ReadRequest (reference/upstream/goreplay/output_http.go:402),
        and a request with neither Content-Length nor Transfer-Encoding has an
        empty body (RFC 9112 section 6.3), so without it every replayed POST
        lost its body (review finding MC-3). Bodies that were not kept
        (object-store PUTs, bodies over MockSettings.log_body_cap, non-UTF-8
        bodies) are marked with `X-Mock-Body-Truncated: <original size>` so a
        replay never sends them as if complete.
        """
        chunks: list[str] = []
        for e in entries if entries is not None else self.request_log:
            ts_ns = int(e["ts"] * 1_000_000_000)
            latency_ns = int(e.get("duration_ms", 0.0) * 1_000_000)
            path = e["path"] + (f"?{e['query']}" if e.get("query") else "")
            logged = dict(e.get("headers", {}))
            host = next((v for k, v in logged.items() if k.lower() == "host"), None) or GOR_FALLBACK_HOST
            body = e.get("body", "")
            lines = [f"{e['method']} {path} HTTP/1.1", f"Host: {host}"]
            for name, value in logged.items():
                if name.lower() not in _GOR_FRAMING_HEADERS:
                    lines.append(f"{name}: {value}")
            if e.get("body_truncated"):
                lines.append(f"X-Mock-Body-Truncated: {int(e.get('body_size', 0))}")
            lines.append(f"Content-Length: {len(body.encode('utf-8'))}")
            raw = "\r\n".join(lines) + "\r\n\r\n" + body
            chunks.append(f"1 {e['request_id']} {ts_ns} {latency_ns}\n{raw}")
        return GOR_SEPARATOR.join(chunks) + (GOR_SEPARATOR if chunks else "")
