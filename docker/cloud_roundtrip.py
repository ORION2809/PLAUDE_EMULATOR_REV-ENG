#!/usr/bin/env python3
"""Push one generated device recording through the LOCAL mock cloud over HTTP and
write what comes back as a contract hyp.json (HARNESS_POLICY tooling; the
`cloud-roundtrip` step of docker/job.sh).

    python docker/cloud_roundtrip.py --url http://mockcloud:8787 \\
        --meeting-dir build/compose/job/meetings/synth-smoke-s0001 --out build/compose/job/hyp/cloud

    register    POST /_mock/meetings {"dir"}   the mock fingerprints the meeting's audio files (the
                                               oracle-path knob, docs/mockcloud.md) so an upload of
                                               those exact bytes is answered with its ground truth
    identity    Basic(client_id:secret_key) -> partner token -> user token (JWT)
    bind        POST sdk/bind {type, sn}      -> is_bind true
    presign     generate-presigned-urls        must be a genuine multipart upload (>= --min-parts)
    upload      PUT every part                 ETag == quoted MD5 of the part
    complete    complete-upload                FileMd5 == MD5 of the file
    download    GET DownloadUrl                byte-exact
    transcribe  POST ai/transcriptions/        PENDING, then polled to a terminal state
    hypothesis  mockcloud.oracle.result_to_hypothesis -> <out>/<meeting_id>/hyp.json (+ .rttm/.stm)
    unbind      POST sdk/unbind                leaves the mock's binding as it was

Prints one `CLOUD_STEP <step> ...` line per step and a final `CLOUD_OK ...` line;
on failure `CLOUD_FAIL step=<step> <reason>` on stderr and exit 1 (exit 2: the
meeting directory is unusable). Scoring the hyp.json is the caller's job
(docker/job.sh runs `python -m evals score --suite oracle`).

Credentials: the mock's SYNTHETIC partner app from mockcloud/settings.py
(DEFAULT_PARTNER_APP, "not a real portal credential"); no token is ever printed.
Only the --url given is contacted, with the standard library's urllib; the mock
itself performs no outbound traffic (docs/mockcloud.md).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PARTNER = "/developer/api"
SDK = f"{PARTNER}/open/partner/sdk"
FILES = f"{PARTNER}/open/partner/files/upload"
AI = f"{PARTNER}/open/partner/ai/transcriptions"
TERMINAL = ("SUCCESS", "FAILURE", "REVOKED")
#: HARNESS_POLICY: the job's partner user, and the mock's seeded notepro SN
#: (mockcloud/settings.py seed_devices: the openapi_binding.json example SN).
DEFAULT_USER_ID = "harness-job-0001"
DEFAULT_DEVICE_TYPE = "notepro"
DEFAULT_SN = "8810000000000001"
REQUEST_TIMEOUT_S = 10.0


class CloudFail(RuntimeError):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(reason)
        self.step = step
        self.reason = reason


class Http:
    """Tiny JSON-over-urllib client for one base URL."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None,
             json_body: Any = None) -> tuple[int, dict[str, str], bytes]:
        if not url.startswith(("http://", "https://")):
            url = self.base + url
        hdrs = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:  # noqa: S310 - the mock only
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read()

    def json(self, step: str, method: str, url: str, **kw: Any) -> Any:
        status, _, raw = self.call(method, url, **kw)
        if status != 200:
            raise CloudFail(step, f"{method} {urllib.parse.urlsplit(url).path} -> HTTP {status}: {raw[:300]!r}")
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise CloudFail(step, f"{method} {url} did not return JSON: {exc}") from exc


def say(step: str, **fields: Any) -> None:
    detail = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"CLOUD_STEP {step} {detail}".rstrip(), flush=True)


def device_recording(meeting_dir: Path) -> tuple[dict[str, Any], Path, bytes]:
    """(meeting.json, the primary device file, its bytes), checked against meeting.json."""
    meeting_json = meeting_dir / "meeting.json"
    if not meeting_json.is_file():
        raise CloudFail("input", f"{meeting_dir} has no meeting.json")
    meeting = json.loads(meeting_json.read_text())
    audio = meeting.get("audio") or {}
    primary = audio.get("device_primary", "ogg_opus")
    rel = (audio.get("device") or {}).get(primary, "device/recording.ogg")
    path = meeting_dir / rel
    if not path.is_file():
        raise CloudFail("input", f"{path} (audio.device.{primary}) does not exist")
    data = path.read_bytes()
    want = ((audio.get("device_details") or {}).get(primary) or {}).get("sha256")
    if want and hashlib.sha256(data).hexdigest() != want:
        raise CloudFail("input", f"{path} sha256 differs from meeting.json audio.device_details.{primary}")
    return meeting, path, data


def roundtrip(base: str, meeting_dir: Path, out_root: Path, *, timeout: float, min_parts: int,
              user_id: str, device_type: str, sn: str) -> dict[str, Any]:
    from mockcloud.oracle import result_to_hypothesis
    from mockcloud.settings import DEFAULT_PARTNER_APP as APP  # SYNTHETIC partner app

    t0 = time.monotonic()
    http = Http(base)
    meeting, path, data = device_recording(meeting_dir)
    meeting_id = str(meeting["meeting_id"])
    md5 = hashlib.md5(data).hexdigest()
    filetype = "opus" if path.suffix in (".ogg", ".opus") else "mp3"

    reg = http.json("register", "POST", "/_mock/meetings", json_body={"dir": str(meeting_dir.resolve())})
    rel = str(path.relative_to(meeting_dir))
    if rel not in {e.get("file") for e in reg.get("registered", [])}:
        raise CloudFail("register", f"the mock did not fingerprint {rel}: {reg}")
    say("register", meeting_id=meeting_id, files=len(reg["registered"]))

    basic = base64.b64encode(f"{APP.client_id}:{APP.secret_key}".encode()).decode()
    partner = http.json("identity", "POST", f"{PARTNER}/oauth/partner/access-token", body=b"",
                        headers={"Authorization": f"Basic {basic}",
                                 "Content-Type": "application/x-www-form-urlencoded"})
    user = http.json("identity", "POST", f"{PARTNER}/open/partner/users/access-token",
                     headers={"Authorization": f"Bearer {partner['access_token']}"},
                     json_body={"user_id": user_id})
    bearer = {"Authorization": f"Bearer {user['access_token']}"}
    say("identity", user_id=user_id, token_type=user.get("token_type"))

    device = {"type": device_type, "sn": sn}
    bound = http.json("bind", "POST", f"{SDK}/bind", headers=bearer, json_body=device)
    if bound.get("is_bind") is not True:
        raise CloudFail("bind", f"bind answered {bound}")
    say("bind", type=device_type, sn=sn)
    try:
        pres = http.json("presign", "POST", f"{FILES}/generate-presigned-urls", headers=bearer,
                         json_body={"filesize": len(data), "filetype": filetype})
        chunk, parts = int(pres["ChunkSize"]), pres["Parts"]
        if len(parts) < min_parts:
            raise CloudFail("presign", f"{len(data)} bytes at ChunkSize {chunk} is {len(parts)} part(s), fewer "
                                       f"than {min_parts}: start the mock with a smaller --chunk-size "
                                       "(docker-compose.yml and scripts/local-up.sh use 20000)")
        say("presign", bytes=len(data), chunk=chunk, parts=len(parts))

        done = []
        for p in parts:
            n = int(p["PartNumber"])
            piece = data[(n - 1) * chunk: n * chunk]
            status, headers, raw = http.call("PUT", p["PresignedUrl"], body=piece,
                                             headers={"Content-Type": "application/octet-stream"})
            if status != 200:
                raise CloudFail("upload", f"PUT part {n} -> HTTP {status}: {raw[:200]!r}")
            etag = headers.get("etag", "")
            if etag != f'"{hashlib.md5(piece).hexdigest()}"':
                raise CloudFail("upload", f"part {n} ETag {etag!r} is not the quoted MD5 of the part")
            done.append({"PartNumber": n, "ETag": etag})
        say("upload", parts=len(done))

        comp = http.json("complete", "POST", f"{FILES}/complete-upload", headers=bearer, json_body={
            "file_id": pres["FileId"], "upload_id": pres["UploadId"], "part_list": done,
            "filetype": filetype, "file_md5": md5})
        if comp.get("FileMd5") != md5:
            raise CloudFail("complete", f"FileMd5 {comp.get('FileMd5')} != {md5}")
        say("complete", md5=md5)

        status, _, got = http.call("GET", comp["DownloadUrl"])
        if status != 200 or got != data:
            raise CloudFail("download", f"GET DownloadUrl -> HTTP {status}, {len(got)} bytes, "
                                        f"byte-exact={got == data}")
        say("download", bytes=len(got), byte_exact=True)

        keys = {"X-Client-Id": APP.client_id, "X-Client-Api-Key": APP.api_key}
        task = http.json("transcribe", "POST", f"{AI}/", headers=keys, json_body={
            "file_url": comp["DownloadUrl"],
            "params": {"transcribe": {"language": "auto"}, "diarization": {"enabled": True}}})
        tid = task["transcription_id"]
        seen = [task["status"]]
        deadline = time.monotonic() + timeout
        while task["status"] not in TERMINAL:
            if time.monotonic() > deadline:
                raise CloudFail("transcribe", f"task {tid} not terminal after {timeout:.0f}s: {seen}")
            time.sleep(0.05)
            task = http.json("transcribe", "GET", f"{AI}/{tid}", headers=keys)
            if task["status"] != seen[-1]:
                seen.append(task["status"])
        if task["status"] != "SUCCESS":
            raise CloudFail("transcribe", f"task {tid} ended {task['status']}: {task.get('message')}")
        source = (http.json("transcribe", "GET", "/_mock/state").get("tasks", {}).get(tid) or {}).get("source", "?")
        if not str(source).startswith("objectstore+meeting"):
            raise CloudFail("transcribe", f"the mock did not recognise the upload as the registered meeting "
                                          f"(source={source}); its answer would be the placeholder")
        say("transcribe", id=tid, states=",".join(seen), source=source)

        from evals.io import hypothesis_from_dict, write_hypothesis_files

        hyp = result_to_hypothesis(task["data"], meeting_id, system="mockcloud")
        out_dir = out_root / meeting_id
        written = write_hypothesis_files(hypothesis_from_dict(hyp), out_dir)
        say("hypothesis", path=out_dir / "hyp.json", segments=len(hyp["segments"]),
            files=",".join(sorted(p.name for p in written)))
    finally:
        try:
            http.json("unbind", "POST", f"{SDK}/unbind", headers=bearer, json_body=device)
            say("unbind", type=device_type, sn=sn)
        except CloudFail as exc:  # reported, never masks the real failure
            print(f"CLOUD_WARN step=unbind {exc.reason}", file=sys.stderr, flush=True)

    return {"meeting_id": meeting_id, "bytes": len(data), "parts": len(parts), "chunk": chunk,
            "states": ",".join(seen), "source": source, "task": tid, "hyp": out_dir / "hyp.json",
            "elapsed_s": f"{time.monotonic() - t0:.2f}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python docker/cloud_roundtrip.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="mock cloud base URL, e.g. http://mockcloud:8787")
    parser.add_argument("--meeting-dir", required=True, type=Path, help="a plaud-harness/meeting/1 directory")
    parser.add_argument("--out", required=True, type=Path, help="hypothesis root; writes <out>/<meeting_id>/hyp.json")
    parser.add_argument("--timeout", type=float, default=60.0, help="bound on the transcription task (seconds)")
    parser.add_argument("--min-parts", type=int, default=2, help="refuse an upload with fewer parts (default 2)")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--device-type", default=DEFAULT_DEVICE_TYPE)
    parser.add_argument("--sn", default=DEFAULT_SN)
    args = parser.parse_args(argv)
    try:
        res = roundtrip(args.url, args.meeting_dir, args.out, timeout=args.timeout, min_parts=args.min_parts,
                        user_id=args.user_id, device_type=args.device_type, sn=args.sn)
    except CloudFail as exc:
        print(f"CLOUD_FAIL step={exc.step} {exc.reason}", file=sys.stderr, flush=True)
        return 2 if exc.step == "input" else 1
    except (OSError, KeyError, ValueError) as exc:  # connection refused, a malformed answer, ...
        print(f"CLOUD_FAIL step=transport {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    print("CLOUD_OK " + " ".join(f"{k}={v}" for k, v in res.items()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
