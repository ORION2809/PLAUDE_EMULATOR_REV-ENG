"""Transcription API: task lifecycle over the Celery state names, the
documented SUCCESS result schema, ground truth for a meeting directory (both
via a file:// url and via an upload through the mock store), the placeholder
for unknown audio, and the guarantee that an external URL is never fetched.

Contract: build/docs-plaud-ai/openapi_transcription.json (+ -model.json).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from test_mockcloud_helpers import CLIENT_KEYS, PARTNER, client, make_app, upload_bytes, user_token, write_meeting_dir
from mockcloud.oracle import (
    EMBEDDING_DIM,
    PLACEHOLDER_TEXT,
    ogg_opus_duration_s,
    result_from_meeting,
    result_to_hypothesis,
)

AI = f"{PARTNER}/open/partner/ai/transcriptions"
CELERY_STATES = {"PENDING", "RECEIVED", "STARTED", "PROGRESS", "SUCCESS", "FAILURE", "REVOKED"}
RESULT_KEYS = {"start", "end", "text", "language", "language_probability"}


def make_ogg_opus(path: Path, seconds: float, seed: int = 7) -> bytes:
    rng = np.random.default_rng(seed)
    sf.write(path, (rng.standard_normal(int(16000 * seconds)) * 0.1).astype("float32"), 16000,
             format="OGG", subtype="OPUS")
    return path.read_bytes()


async def poll(c, tid: str, ctx) -> dict:
    await ctx.drain()
    r = await c.get(f"{AI}/{tid}", headers=CLIENT_KEYS)
    assert r.status_code == 200, r.text
    return r.json()


# --- pure pieces ------------------------------------------------------------

def test_ogg_opus_duration_matches_what_was_written(tmp_path: Path) -> None:
    for secs in (0.5, 2.0, 7.25):
        data = make_ogg_opus(tmp_path / f"{secs}.ogg", secs)
        assert ogg_opus_duration_s(data) == pytest.approx(secs, abs=1e-3)
        assert sf.info(tmp_path / f"{secs}.ogg").duration == pytest.approx(secs, abs=1e-3)
    assert ogg_opus_duration_s(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None
    assert ogg_opus_duration_s(b"") is None
    assert ogg_opus_duration_s(b"OggS" + bytes(23)) is None, "truncated page"


def test_result_from_meeting_is_the_ground_truth_in_the_documented_shape(tmp_path: Path) -> None:
    _, meeting = write_meeting_dir(tmp_path)
    params = {"diarization": {"enabled": True, "return_embedding": True}}
    data = result_from_meeting(meeting, params)
    assert set(data) == {"text", "language", "duration", "results", "embeddings"}
    assert data["duration"] == 5 and isinstance(data["duration"], int)
    assert data["language"] == "en"
    assert [r["text"] for r in data["results"]] == ["hello there", "hi how are you", "fine thanks"]
    assert [(r["start"], r["end"]) for r in data["results"]] == [(0.0, 1.5), (1.2, 3.0), (3.4, 4.9)]
    assert [r["speaker_id"] for r in data["results"]] == ["Speaker 1", "Speaker 2", "Speaker 1"]
    assert data["text"] == "hello there hi how are you fine thanks"
    assert set(data["embeddings"]) == {"Speaker 1", "Speaker 2"}
    for vec in data["embeddings"].values():
        assert len(vec) == EMBEDDING_DIM
        assert sum(v * v for v in vec) == pytest.approx(1.0, abs=1e-4)
    assert data["embeddings"]["Speaker 1"] != data["embeddings"]["Speaker 2"]
    # diarization off: no speaker_id, no embeddings; explicit language echoed
    plain = result_from_meeting(meeting, {"transcribe": {"language": "de-DE"}})
    assert "embeddings" not in plain
    assert all("speaker_id" not in r for r in plain["results"])
    assert plain["language"] == "de" and plain["results"][0]["language"] == "de-DE"
    # round-trip into the shared hypothesis contract
    hyp = result_to_hypothesis(data, meeting["meeting_id"])
    assert hyp["schema"] == "plaud-harness/hypothesis/1" and hyp["meeting_id"] == "mtg-0001"
    assert [(s["speaker"], s["start"], s["end"], s["text"]) for s in hyp["segments"]] == [
        ("Speaker 1", 0.0, 1.5, "hello there"), ("Speaker 2", 1.2, 3.0, "hi how are you"),
        ("Speaker 1", 3.4, 4.9, "fine thanks")]


# --- the API ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_requires_client_id_and_api_key_headers() -> None:
    app, _, _ = make_app()
    async with client(app) as c:
        body = {"file_url": "file:///nope.ogg"}
        assert (await c.post(f"{AI}/", json=body)).status_code == 401
        assert (await c.post(f"{AI}/", headers={"X-Client-Id": CLIENT_KEYS["X-Client-Id"]}, json=body)).status_code == 401
        bad = {**CLIENT_KEYS, "X-Client-Api-Key": "wrong"}
        assert (await c.post(f"{AI}/", headers=bad, json=body)).status_code == 401
        # a user Bearer token is the wrong credential for the AI surface
        tok = await user_token(c)
        assert (await c.post(f"{AI}/", headers={"Authorization": f"Bearer {tok}"}, json=body)).status_code == 401
        assert (await c.post(f"{AI}/", headers=CLIENT_KEYS, json={})).status_code == 422
        r = await c.get(f"{AI}/task_exec_unknown", headers=CLIENT_KEYS)
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_lifecycle_walks_pending_started_success(tmp_path: Path) -> None:
    ogg = make_ogg_opus(tmp_path / "a.ogg", 2.0)
    app, ctx, _ = make_app(task_step_s=0.05, local_file_roots=(tmp_path,))
    async with client(app) as c:
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={"file_url": (tmp_path / "a.ogg").as_uri()})
        assert r.status_code == 200
        j = r.json()
        assert set(j) == {"transcription_id", "status", "data"}
        assert j["transcription_id"].startswith("task_exec_") and j["status"] == "PENDING" and j["data"] == {}
        tid = j["transcription_id"]
        seen = []
        for _ in range(200):
            s = (await c.get(f"{AI}/{tid}", headers=CLIENT_KEYS)).json()["status"]
            if not seen or seen[-1] != s:
                seen.append(s)
            if s in ("SUCCESS", "FAILURE", "REVOKED"):
                break
            await asyncio.sleep(0.01)
        assert seen == ["PENDING", "STARTED", "SUCCESS"], seen
        assert set(seen) <= CELERY_STATES
        task = ctx.state.tasks[tid]
        assert [h[0] for h in task.history] == ["PENDING", "STARTED", "SUCCESS"]
        assert task.history[0][1] <= task.history[1][1] <= task.history[2][1]
        data = (await c.get(f"{AI}/{tid}", headers=CLIENT_KEYS)).json()["data"]
        assert data["duration"] == 2 and data["results"][0]["end"] == pytest.approx(2.0, abs=1e-3)
        assert data["text"] == PLACEHOLDER_TEXT, "no meeting.json beside the file -> placeholder"
        assert len(ogg) > 0


@pytest.mark.asyncio
async def test_meeting_dir_file_url_returns_ground_truth(tmp_path: Path) -> None:
    audio = make_ogg_opus(tmp_path / "rec.ogg", 5.25)
    mdir, meeting = write_meeting_dir(tmp_path, device_audio=audio)
    app, ctx, _ = make_app(local_file_roots=(tmp_path,))
    async with client(app) as c:
        params = {"transcribe": {"language": "auto", "model": "plaud-fast-whisper"},
                  "vad": {"decode_silence": False},
                  "diarization": {"enabled": True, "return_embedding": False}}
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS,
                         json={"file_url": (mdir / "device" / "recording.ogg").as_uri(), "params": params})
        tid = r.json()["transcription_id"]
        j = await poll(c, tid, ctx)
        assert j["status"] == "SUCCESS"
        assert set(j["data"]) == {"text", "language", "duration", "results"}
        assert [r["text"] for r in j["data"]["results"]] == [s["text"] for s in meeting["segments"]]
        assert [r["speaker_id"] for r in j["data"]["results"]] == ["Speaker 1", "Speaker 2", "Speaker 1"]
        for seg in j["data"]["results"]:
            assert RESULT_KEYS | {"speaker_id"} == set(seg)
        assert ctx.state.tasks[tid].source.startswith("file+meeting")


@pytest.mark.asyncio
async def test_uploaded_meeting_audio_is_recognised_by_md5_and_answered_with_ground_truth(tmp_path: Path) -> None:
    audio = make_ogg_opus(tmp_path / "rec.ogg", 5.25)
    mdir, meeting = write_meeting_dir(tmp_path, device_audio=audio)
    app, ctx, _ = make_app(chunk_size=2048)
    async with client(app) as c:
        r = await c.post("/_mock/meetings", json={"dir": str(mdir)})
        assert r.status_code == 200 and r.json()["meeting_id"] == "mtg-0001"
        assert [e["file"] for e in r.json()["registered"]] == ["device/recording.ogg"], "only files that exist"
        tok = await user_token(c)
        comp = await upload_bytes(c, tok, audio, "opus")
        assert len(audio) > 2048, "the fixture really is multipart"
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={
            "file_url": comp["DownloadUrl"],
            "params": {"diarization": {"enabled": True, "return_embedding": True}}})
        tid = r.json()["transcription_id"]
        j = await poll(c, tid, ctx)
        assert j["status"] == "SUCCESS"
        assert j["data"]["duration"] == 5
        assert [r["text"] for r in j["data"]["results"]] == [s["text"] for s in meeting["segments"]]
        assert len(j["data"]["embeddings"]["Speaker 2"]) == EMBEDDING_DIM
        assert ctx.state.tasks[tid].source.startswith("objectstore+meeting")
        # the same bytes re-uploaded under a different filetype still match
        comp2 = await upload_bytes(c, tok, audio, "mp3")
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={"file_url": comp2["DownloadUrl"]})
        j = await poll(c, r.json()["transcription_id"], ctx)
        assert j["status"] == "SUCCESS" and all("speaker_id" not in s for s in j["data"]["results"])


@pytest.mark.asyncio
async def test_external_urls_fail_without_any_fetch_and_expired_download_urls_fail(tmp_path: Path) -> None:
    app, ctx, clock = make_app(chunk_size=2048)
    async with client(app) as c:
        for url in ("https://plaud-bucket.s3.amazonaws.com/some/key.mp3", "http://127.0.0.1:1/x.mp3",
                    "ftp://example.invalid/x.mp3"):
            r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={"file_url": url})
            j = await poll(c, r.json()["transcription_id"], ctx)
            assert j["status"] == "FAILURE" and j["data"] == {}
            assert "no outbound HTTP" in j["message"]
        # file:// outside the allowed roots (none configured here)
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={"file_url": (tmp_path / "x.ogg").as_uri()})
        j = await poll(c, r.json()["transcription_id"], ctx)
        assert j["status"] == "FAILURE" and "local_file_roots" in j["message"]
        # a DownloadUrl from this mock that has expired is refused like S3 would
        tok = await user_token(c)
        comp = await upload_bytes(c, tok, b"\x00" * 3000, "mp3")
        clock.advance(24 * 3600 + 1)
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={"file_url": comp["DownloadUrl"]})
        j = await poll(c, r.json()["transcription_id"], ctx)
        assert j["status"] == "FAILURE" and "expired" in j["message"]


@pytest.mark.asyncio
async def test_tasks_are_scoped_to_the_client_id_that_created_them(tmp_path: Path) -> None:
    from mockcloud import PartnerApp
    other = PartnerApp("mock_client_0002", "mock_secret_0002_SYNTHETIC", "mock_api_key_0002_SYNTHETIC")
    from mockcloud import DEFAULT_PARTNER_APP as APP
    app, ctx, _ = make_app(partner_apps=(APP, other))
    async with client(app) as c:
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={"file_url": "https://example.invalid/a.mp3"})
        tid = r.json()["transcription_id"]
        await ctx.drain()
        r = await c.get(f"{AI}/{tid}", headers={"X-Client-Id": other.client_id, "X-Client-Api-Key": other.api_key})
        assert r.status_code == 404
        assert (await c.get(f"{AI}/{tid}", headers=CLIENT_KEYS)).status_code == 200
