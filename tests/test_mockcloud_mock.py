"""The mock's own guarantees: request log contents and GoReplay export,
reset, JSON persistence, the root banner, and -- most important -- that no
route can ever perform an outbound HTTP call.
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import pytest

from test_mockcloud_helpers import (
    CLIENT_KEYS,
    PARTNER,
    ROOT,
    SN_NOTEPRO,
    bearer,
    client,
    make_app,
    upload_bytes,
    user_token,
)
from mockcloud import MockState
from mockcloud.state import GOR_SEPARATOR

SDK = f"{PARTNER}/open/partner/sdk"
PKG = ROOT / "mockcloud"


def test_package_source_contains_no_http_client() -> None:
    """Static guarantee: nothing in mockcloud/ can talk to the network.
    httpx is a test-only dependency (ASGITransport) and never appears here."""
    forbidden = re.compile(
        r"^\s*(import|from)\s+(httpx|requests|aiohttp|urllib3|http\.client|urllib\.request|websockets|socket)\b",
        re.M,
    )
    calls = re.compile(r"\b(urlopen|create_connection|getaddrinfo|socket\.socket)\s*\(")
    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        text = path.read_text()
        if forbidden.search(text) or calls.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    assert len(list(PKG.rglob("*.py"))) >= 12, "the whole package was scanned"


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
        assert (await c.get("/_mock/reset")).json() == {"reset": True}
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
