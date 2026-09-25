"""In-memory mock-cloud state with optional JSON persistence and a request log.

Nothing here is a claim about how Plaud stores anything: the shapes are what
the documented endpoints need in order to answer consistently, and they are all
HARNESS_POLICY. Bytes (object-store contents, the mock signing key) survive
persistence as base64. The persistence file lives under build/mockcloud/ which
.gitignore already excludes (the whole build/ tree).
"""

from __future__ import annotations

import base64
import json
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
        self.reset()
        if self.persist_path and self.persist_path.exists():
            self.load()

    # ------------------------------------------------------------------ core
    def reset(self) -> None:
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

    # ------------------------------------------------------------ persistence
    def snapshot(self) -> dict[str, Any]:
        def obj(o: StoredObject) -> dict[str, Any]:
            return {
                "bucket": o.bucket, "key": o.key, "content_type": o.content_type,
                "etag": o.etag, "acl": o.acl, "created_at": o.created_at,
                "data_b64": base64.b64encode(o.data).decode("ascii"),
            }

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

        return {
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
            "request_log": self.request_log,
        }

    def restore(self, snap: dict[str, Any]) -> None:
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
            data = base64.b64decode(v.pop("data_b64"))
            self.objects[k] = StoredObject(data=data, **v)
        self.tasks = {}
        for k, v in snap.get("tasks", {}).items():
            v = dict(v)
            v["history"] = [tuple(h) for h in v.get("history", [])]
            self.tasks[k] = TranscriptionTask(**v)
        self.signing_key_pem = snap.get("signing_key_pem")
        self.request_log = list(snap.get("request_log", []))

    def save(self) -> None:
        if not self.persist_path:
            return
        with self._lock:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.snapshot(), indent=1, sort_keys=True))
            tmp.replace(self.persist_path)

    def load(self) -> None:
        assert self.persist_path is not None
        self.restore(json.loads(self.persist_path.read_text()))

    # ------------------------------------------------------------ request log
    def log_request(self, entry: dict[str, Any]) -> None:
        self.request_log.append(entry)

    def log_as_gor(self, entries: Iterable[dict[str, Any]] | None = None) -> str:
        """Render the request log in GoReplay's file format (type '1' requests).

        Timestamps are nanoseconds; latency is the measured handler time. Bodies
        beyond MockSettings.log_body_cap are absent and marked in a header so a
        replay does not silently send a truncated body.
        """
        chunks: list[str] = []
        for e in entries if entries is not None else self.request_log:
            ts_ns = int(e["ts"] * 1_000_000_000)
            latency_ns = int(e.get("duration_ms", 0.0) * 1_000_000)
            path = e["path"] + (f"?{e['query']}" if e.get("query") else "")
            lines = [f"{e['method']} {path} HTTP/1.1"]
            headers = dict(e.get("headers", {}))
            if e.get("body_truncated"):
                headers["X-Mock-Body-Truncated"] = str(e.get("body_size", 0))
            for name, value in headers.items():
                lines.append(f"{name}: {value}")
            body = e.get("body", "")
            raw = "\r\n".join(lines) + "\r\n\r\n" + body
            chunks.append(f"1 {e['request_id']} {ts_ns} {latency_ns}\n{raw}")
        return GOR_SEPARATOR.join(chunks) + (GOR_SEPARATOR if chunks else "")
