"""The mock's own guarantees: request log contents and GoReplay export,
reset, JSON persistence, the root banner, and -- most important -- that no
route performs an outbound network call (checked statically over the source
and, for every mockcloud test, by blocking socket connects at runtime).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import random
import socket
from pathlib import Path

import pytest

from test_mockcloud_helpers import (  # noqa: F401 - no_outbound_network is an autouse fixture
    CLIENT_KEYS,
    PARTNER,
    ROOT,
    SN_NOTEPRO,
    bearer,
    client,
    make_app,
    no_outbound_network,
    upload_bytes,
    user_token,
    write_meeting_dir,
)
from mockcloud import MockState
from mockcloud.state import GOR_SEPARATOR, StoredObject

SDK = f"{PARTNER}/open/partner/sdk"
PKG = ROOT / "mockcloud"

# --- static network check (AST, review finding MC-12) ---------------------------

#: Modules that can open a connection (or load one dynamically). Any import of
#: them, or of a submodule, anywhere in mockcloud/ is an offence.
NETWORK_MODULES = (
    "socket", "ssl", "http.client", "urllib.request", "urllib3", "httpx", "requests", "aiohttp",
    "websockets", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc.client", "grpc",
    "asyncio.streams", "importlib",
)
#: Callables / attributes that open a connection or import dynamically, in any
#: spelling: bare name, attribute (asyncio.open_connection, loop.sock_connect)
#: or from-import.
NETWORK_NAMES = {
    "urlopen", "create_connection", "getaddrinfo", "open_connection", "open_unix_connection",
    "sock_connect", "connect_ex", "create_datagram_endpoint", "create_unix_connection",
    "HTTPConnection", "HTTPSConnection", "import_module", "__import__",
}


def _is_network_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in NETWORK_MODULES)


def network_offenders(source: str) -> list[str]:
    """Every import, name, attribute or module-name string in `source` that
    could open a network connection (see NETWORK_MODULES / NETWORK_NAMES)."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out += [f"import {a.name}" for a in node.names if _is_network_module(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mod = node.module or ""
            if _is_network_module(mod):
                out.append(f"from {mod} import ...")
            out += [f"from {mod} import {a.name}" for a in node.names
                    if _is_network_module(f"{mod}.{a.name}") or a.name in NETWORK_NAMES]
        elif isinstance(node, ast.Attribute) and node.attr in NETWORK_NAMES:
            out.append(f".{node.attr}")
        elif isinstance(node, ast.Name) and node.id in NETWORK_NAMES:
            out.append(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _is_network_module(node.value):
            out.append(f"string {node.value!r}")
    return out


def test_the_static_scan_catches_the_known_bypasses() -> None:
    """Each of these slipped past the old line regex (review demo d10)."""
    bypasses = [
        "from http import client\nclient.HTTPSConnection('h')",
        "import asyncio\nasyncio.open_connection('h', 1)",
        "from asyncio import open_connection",
        "import importlib\nimportlib.import_module('urllib.request')",
        "loop.sock_connect(s, ('h', 1))",
        "__import__('socket')",
        "from urllib import request",
        "import urllib.request as u",
        "import http.client",
        "x = 'socket'",
    ]
    for src in bypasses:
        assert network_offenders(src), src
    assert network_offenders("import asyncio, hashlib, hmac\nasyncio.sleep(0)\nfrom ..common import ctx_of") == []


def test_package_source_contains_no_network_client() -> None:
    """Static check over every module in mockcloud/. httpx is a test-only
    dependency (ASGITransport) and never appears there."""
    files = sorted(PKG.rglob("*.py"))
    offenders = {str(p.relative_to(ROOT)): network_offenders(p.read_text()) for p in files}
    assert {k: v for k, v in offenders.items() if v} == {}
    assert len(files) >= 12, "the whole package was scanned"


def test_every_mockcloud_test_runs_with_outbound_connects_blocked() -> None:
    with pytest.raises(AssertionError, match="outbound network"):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(AssertionError, match="outbound network"):
        socket.getaddrinfo("example.invalid", 443)
    s = socket.socket()
    try:
        with pytest.raises(AssertionError, match="outbound network"):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()


@pytest.mark.asyncio
async def test_full_flow_opens_no_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Runtime guarantee: with every socket connect forbidden, the whole
    documented flow still completes, because ASGITransport is in-process and
    the mock resolves file_url locally."""

    def boom(*_a, **_k):  # noqa: ANN001
        raise AssertionError("mockcloud attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    app, ctx, _ = make_app(chunk_size=1024)
    async with client(app) as c:
        tok = await user_token(c)
        assert (await c.post(f"{SDK}/bind", headers=bearer(tok), json={"type": "notepro", "sn": SN_NOTEPRO})).status_code == 200
        assert (await c.post(f"{SDK}/gen-key", headers=bearer(tok))).status_code == 200
        comp = await upload_bytes(c, tok, bytes(range(256)) * 10, "mp3")
        r = await c.post(f"{PARTNER}/open/partner/ai/transcriptions/", headers=CLIENT_KEYS,
                         json={"file_url": comp["DownloadUrl"]})
        await ctx.drain()
        j = (await c.get(f"{PARTNER}/open/partner/ai/transcriptions/{r.json()['transcription_id']}",
                         headers=CLIENT_KEYS)).json()
        assert j["status"] == "SUCCESS"
        # and an external URL is answered without any connection attempt
        r = await c.post(f"{PARTNER}/open/partner/ai/transcriptions/", headers=CLIENT_KEYS,
                         json={"file_url": "https://platform-us.plaud.ai/never"})
        await ctx.drain()
        assert ctx.state.tasks[r.json()["transcription_id"]].status == "FAILURE"


@pytest.mark.asyncio
async def test_request_log_records_every_non_mock_request_with_ids() -> None:
    app, ctx, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c)  # 2 requests
        r = await c.post(f"{SDK}/bind", headers={**bearer(tok), "X-Request-Id": "client-supplied-42"},
                         json={"type": "notepro", "sn": "8819999999999999"})
        assert r.status_code == 404
        assert r.headers["x-request-id"] == "client-supplied-42", "echoed"
        r = await c.get("/_mock/log")
        log = r.json()
        assert log["count"] == 3 and len(log["entries"]) == 3
        assert [e["method"] for e in log["entries"]] == ["POST", "POST", "POST"]
        assert [e["path"] for e in log["entries"]] == [
            f"{PARTNER}/oauth/partner/access-token", f"{PARTNER}/open/partner/users/access-token", f"{SDK}/bind"]
        assert [e["status"] for e in log["entries"]] == [200, 200, 404]
        assert [e["seq"] for e in log["entries"]] == [1, 2, 3]
        assert log["entries"][2]["request_id"] == "client-supplied-42"
        assert log["entries"][0]["request_id"].startswith("mockreq_")
        assert json.loads(log["entries"][1]["body"])["user_id"] == "harness-user-0001"
        assert all(e["duration_ms"] >= 0 for e in log["entries"])
        # /_mock/* itself is never logged
        assert (await c.get("/_mock/log")).json()["count"] == 3
        # object-store PUT bodies are not retained, only their size
        comp_tok = tok
        await upload_bytes(c, comp_tok, b"\xff" * 700, "mp3")
        puts = [e for e in (await c.get("/_mock/log")).json()["entries"] if e["method"] == "PUT"]
        assert len(puts) == 1 and puts[0]["body"] == "" and puts[0]["body_size"] == 700


@pytest.mark.asyncio
async def test_log_exports_in_goreplay_file_format() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        await user_token(c)
        text = (await c.get("/_mock/log", params={"format": "gor"})).text
        records = [r for r in text.split(GOR_SEPARATOR) if r]
        assert len(records) == 2
        header, _, http = records[0].partition("\n")
        # protocol.go:50-53 -> "<type> <id> <timestamp_ns> <latency_ns>"
        kind, rid, ts_ns, latency = header.split(" ")
        assert kind == "1" and rid.startswith("mockreq_") and int(ts_ns) > 10**18 and int(latency) >= 0
        assert http.startswith(f"POST {PARTNER}/oauth/partner/access-token HTTP/1.1\r\n")
        assert "\r\n\r\n" in http
        assert "authorization: Basic " in http
        assert text.endswith(GOR_SEPARATOR)


@pytest.mark.asyncio
async def test_reset_clears_state_and_reseeds_devices() -> None:
    app, ctx, _ = make_app()
    async with client(app) as c:
        tok = await user_token(c)
        assert (await c.post(f"{SDK}/bind", headers=bearer(tok), json={"type": "notepro", "sn": SN_NOTEPRO})).status_code == 200
        # POST only (MC-5): a GET from any web page must not wipe the mock
        assert (await c.get("/_mock/reset")).status_code == 405
        assert ctx.state.device("notepro", SN_NOTEPRO).owner is not None, "the GET changed nothing"
        assert (await c.post("/_mock/reset")).json() == {"reset": True}
        assert (await c.get("/_mock/log")).json()["count"] == 0
        # seeded devices are back, unbound; the old token no longer matters for state
        r = await c.get(f"{SDK}/binding", headers=bearer(tok), params={"type": "notepro", "sn": SN_NOTEPRO})
        assert r.json() == {"is_bind": None, "bind_history": []}
        assert (await c.post("/_mock/reset")).status_code == 200


@pytest.mark.asyncio
async def test_json_persistence_round_trips_bindings_objects_and_log(tmp_path: Path) -> None:
    persist = tmp_path / "build" / "mockcloud" / "state.json"
    app, ctx, _ = make_app(chunk_size=1024, state=MockState(persist_path=persist))
    async with client(app) as c:
        tok = await user_token(c)
        assert (await c.post(f"{SDK}/bind", headers=bearer(tok), json={"type": "notepro", "sn": SN_NOTEPRO})).status_code == 200
        sig = (await c.post(f"{SDK}/sn-sign", headers=bearer(tok), json={"type": "notepro", "sn": SN_NOTEPRO})).json()["signature"]
        comp = await upload_bytes(c, tok, b"persist-me" * 300, "opus")
    assert persist.exists()
    snap = json.loads(persist.read_text())
    assert snap["schema"] == "plaud-harness/mockcloud-state/1"
    # a brand-new app process loads the same file
    app2, ctx2, _ = make_app(chunk_size=1024, state=MockState(persist_path=persist))
    async with client(app2) as c:
        r = await c.get(f"{SDK}/binding", headers=bearer(tok), params={"type": "notepro", "sn": SN_NOTEPRO})
        assert r.status_code == 200 and r.json()["is_bind"] is True and len(r.json()["bind_history"]) == 1
        dl = await c.get(comp["DownloadUrl"])
        assert dl.status_code == 200 and dl.content == b"persist-me" * 300
        # the signature survives and still authenticates version/latest
        r = await c.get(f"{SDK}/version/latest", headers={"X-Device-Signature": sig},
                        params={"type": "notepro", "sn": SN_NOTEPRO})
        assert r.status_code == 200
        assert (await c.get("/_mock/log")).json()["count"] >= 6
    assert ctx2.state.signing_key_pem == ctx.state.signing_key_pem


@pytest.mark.asyncio
async def test_root_and_health_are_unmistakably_a_mock() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        for path in ("/", "/_mock/health"):
            j = (await c.get(path)).json()
            assert j["mock"] is True and "synthetic" in j["warning"]


def test_cli_parser_defaults() -> None:
    from mockcloud.__main__ import build_parser
    args = build_parser().parse_args(["--port", "8787", "--persist"])
    assert args.port == 8787 and args.host == "127.0.0.1"
    assert args.persist.endswith("build/mockcloud/state.json")
    assert build_parser().parse_args([]).persist is None


# --- GoReplay export replays with its bodies (review finding MC-3) ---------------

@pytest.mark.asyncio
async def test_goreplay_export_parses_as_http_and_delivers_every_logged_body() -> None:
    """Parse every exported record with h11 (an RFC 9112 parser, as Go's
    http.ReadRequest is for gor's output-http): each needs Host, and the body a
    parser delivers must be exactly the logged body."""
    h11 = pytest.importorskip("h11")
    app, ctx, _ = make_app(chunk_size=1024)
    async with client(app) as c:
        tok = await user_token(c)
        await c.post(f"{SDK}/bind", headers=bearer(tok), json={"type": "notepro", "sn": SN_NOTEPRO})
        await upload_bytes(c, tok, bytes(range(256)) * 6, "mp3")
        # a non-UTF-8 body inside the cap: logged as dropped AND marked
        await c.post(f"{SDK}/bind", headers={**bearer(tok), "Content-Type": "application/json"}, content=b"\xff\xfe{")
        text = (await c.get("/_mock/log", params={"format": "gor"})).text
    log = ctx.state.request_log
    records = [r for r in text.split(GOR_SEPARATOR) if r]
    assert len(records) == len(log) >= 6
    for rec, entry in zip(records, log):
        _, _, raw = rec.partition("\n")
        conn = h11.Connection(our_role=h11.SERVER)
        conn.receive_data(raw.encode("utf-8"))
        req = conn.next_event()
        assert isinstance(req, h11.Request), (entry["path"], req)
        body = b""
        while True:
            ev = conn.next_event()
            if isinstance(ev, h11.Data):
                body += ev.data
                continue
            assert isinstance(ev, h11.EndOfMessage), (entry["path"], ev)
            break
        assert req.method.decode() == entry["method"]
        assert body == entry["body"].encode("utf-8"), entry["path"]
        hdrs = {k.decode().lower(): v.decode() for k, v in req.headers}
        assert hdrs["host"] and int(hdrs["content-length"]) == len(body)
        if entry["body_truncated"]:
            assert hdrs["x-mock-body-truncated"] == str(entry["body_size"])
        else:
            assert "x-mock-body-truncated" not in hdrs
    posts_with_body = [e for e in log if e["method"] == "POST" and e["body"]]
    assert len(posts_with_body) >= 4, "JSON bodies really are replayed"
    puts = [e for e in log if e["method"] == "PUT"]
    assert puts and all(e["body_truncated"] and e["body"] == "" for e in puts)
    undecodable = log[-1]
    assert undecodable["status"] in (400, 422) and undecodable["body"] == ""
    assert undecodable["body_truncated"] is True and undecodable["body_undecodable"] is True


# --- admin surface stays inside the meeting dir (review finding MC-5) ------------

@pytest.mark.asyncio
async def test_register_meeting_reads_only_files_inside_the_meeting_dir(tmp_path: Path) -> None:
    mdir, meeting = write_meeting_dir(tmp_path / "m", device_audio=b"device-bytes")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"not part of any meeting")
    meeting["audio"]["mix_wav"] = "../../outside.bin"
    meeting["audio"]["stems"] = {"spk0": str(outside)}
    (mdir / "meeting.json").write_text(json.dumps(meeting))
    app, ctx, _ = make_app()
    async with client(app) as c:
        r = await c.post("/_mock/meetings", json={"dir": str(mdir)})
        assert r.status_code == 200
        j = r.json()
    assert [e["file"] for e in j["registered"]] == ["device/recording.ogg"]
    assert sorted(e["file"] for e in j["skipped"]) == sorted(["../../outside.bin", str(outside)])
    secret_md5 = hashlib.md5(outside.read_bytes()).hexdigest()
    assert secret_md5 not in ctx.state.meetings and secret_md5 not in r.text
    assert all(str(outside) != v["file"] for v in ctx.state.meetings.values())


# --- in-flight tasks across a restart (review finding MC-4) ----------------------

@pytest.mark.asyncio
async def test_each_transition_is_persisted_and_a_restart_resumes_in_flight_tasks(tmp_path: Path) -> None:
    persist = tmp_path / "build" / "state.json"
    (tmp_path / "a.bin").write_bytes(b"unknown audio")
    url = (tmp_path / "a.bin").as_uri()
    AI = f"{PARTNER}/open/partner/ai/transcriptions"
    app, ctx, _ = make_app(task_step_s=0.2, local_file_roots=(tmp_path,), state=MockState(persist_path=persist))
    async with client(app) as c:
        tid = (await c.post(f"{AI}/", headers=CLIENT_KEYS, json={"file_url": url})).json()["transcription_id"]
        for _ in range(200):
            if ctx.state.tasks[tid].status == "STARTED":
                break
            await asyncio.sleep(0.01)
        assert ctx.state.tasks[tid].status == "STARTED"
        assert json.loads(persist.read_text())["tasks"][tid]["status"] == "STARTED", "STARTED hit the file at once"
        # the process stops mid-walk: asyncio cancels the worker at its sleep
        handles = list(ctx.worker_tasks)
        for h in handles:
            h.cancel()
        await asyncio.gather(*handles, return_exceptions=True)
    assert json.loads(persist.read_text())["tasks"][tid]["status"] == "STARTED"

    # a new process: the first request (ASGITransport sends no lifespan) resumes it
    app2, ctx2, _ = make_app(task_step_s=0.0, local_file_roots=(tmp_path,), state=MockState(persist_path=persist))
    async with client(app2) as c:
        await c.get(f"{AI}/{tid}", headers=CLIENT_KEYS)
        await ctx2.drain()
        j = (await c.get(f"{AI}/{tid}", headers=CLIENT_KEYS)).json()
    assert j["status"] == "SUCCESS", j
    assert [h[0] for h in ctx2.state.tasks[tid].history] == ["PENDING", "STARTED", "SUCCESS"]
    assert json.loads(persist.read_text())["tasks"][tid]["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_the_lifespan_hook_resumes_restored_tasks_without_any_request(tmp_path: Path) -> None:
    persist = tmp_path / "state.json"
    st = MockState(persist_path=persist)
    from mockcloud.state import TranscriptionTask
    st.tasks["task_exec_restored"] = TranscriptionTask(
        transcription_id="task_exec_restored", status="PENDING", file_url="https://example.invalid/x.mp3",
        params={}, client_id="mock_client_0001", created_at=0.0, updated_at=0.0, history=[("PENDING", 0.0)])
    st.save()
    app, ctx, _ = make_app(task_step_s=0.0, state=MockState(persist_path=persist))
    async with app.router.lifespan_context(app):
        await ctx.drain()
    task = ctx.state.tasks["task_exec_restored"]
    assert task.status == "FAILURE" and "no outbound HTTP" in (task.message or "")


# --- persistence cost (review finding MC-9) --------------------------------------

@pytest.mark.asyncio
async def test_persistence_keeps_bytes_out_of_state_json_and_drops_merged_parts(tmp_path: Path) -> None:
    persist = tmp_path / "state.json"
    rng = random.Random(9)
    data = bytes(rng.getrandbits(8) for _ in range(20 * 1024))
    app, ctx, _ = make_app(chunk_size=1024, state=MockState(persist_path=persist))
    async with client(app) as c:
        tok = await user_token(c)
        comp = await upload_bytes(c, tok, data, "mp3")
        objects = (await c.get("/_mock/state")).json()["objects"]
    assert list(objects) == [f"plaud-bucket-mock/{comp['FileId']}.mp3"], "the 20 part objects were discarded"
    text = persist.read_text()
    assert "data_b64" not in text and "request_log" not in json.loads(text)
    assert len(text) < len(data), "state.json no longer carries the stored bytes"
    blobs = sorted((tmp_path / "state.objects").iterdir())
    assert [b.read_bytes() for b in blobs] == [data], "one body file, the merged object, parts collected"
    lines = (tmp_path / "state.log.jsonl").read_text().splitlines()
    assert [json.loads(x)["seq"] for x in lines] == [e["seq"] for e in ctx.state.request_log]
    # a new process serves the same bytes and continues the log numbering
    app2, ctx2, _ = make_app(chunk_size=1024, state=MockState(persist_path=persist))
    async with client(app2) as c:
        assert (await c.get(comp["DownloadUrl"])).content == data
        await c.post("/_mock/reset")
    assert list((tmp_path / "state.objects").iterdir()) == [], "reset collects the body files"
    assert (tmp_path / "state.log.jsonl").read_text() == ""


def test_a_single_file_snapshot_from_before_the_split_still_loads(tmp_path: Path) -> None:
    persist = tmp_path / "state.json"
    old = MockState()
    old.put_object(StoredObject(bucket="b", key="k", data=b"old-bytes", content_type="x", etag='"e"'))
    old.request_log.append({"seq": 7, "request_id": "r", "ts": 1.0, "method": "GET", "path": "/", "body": ""})
    old.seq = 7
    snap = old.snapshot()
    assert "data_b64" in snap["objects"]["b/k"] and snap["request_log"], "the self-contained form"
    persist.write_text(json.dumps(snap))
    st = MockState(persist_path=persist)
    assert st.get_object("b", "k").data == b"old-bytes" and len(st.request_log) == 1
    assert st.next_seq() == 8
    st.save()
    assert "data_b64" not in persist.read_text()
    again = MockState(persist_path=persist)
    assert again.get_object("b", "k").data == b"old-bytes"
    assert [e["seq"] for e in again.request_log] == [7], "the inline log moved to the JSONL file"


# --- the per-track requirements file (review finding MC-10) ----------------------

def _pins(path: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, _, ver = line.partition("==")
        out[name.split("[")[0].strip().lower()] = ver.strip() or None
    return out


def test_requirements_file_installs_what_mockcloud_and_its_tests_import() -> None:
    mine = _pins(ROOT / "requirements" / "mockcloud.txt")
    everything = _pins(ROOT / "requirements" / "all.txt")
    for dep in ("fastapi", "uvicorn", "cryptography", "httpx", "pytest", "pytest-asyncio", "pytest-timeout",
                "numpy", "soundfile"):
        assert dep in mine, f"requirements/mockcloud.txt does not install {dep}"
        assert mine[dep] == everything.get(dep), f"{dep}: pin differs from requirements/all.txt"
