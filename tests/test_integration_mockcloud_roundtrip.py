"""Layer 2 -> Layer 4 -> Layer 3: the documented partner-cloud flow against the
LOCAL mock, carrying the generator's device Ogg, scored by evals.

    identity chain   Basic(client_id:secret) -> partner token -> user JWT
    bind             POST sdk/bind {type, sn} -> is_bind true; GET sdk/binding
    upload           generate-presigned-urls (>= 2 parts) -> PUT parts -> complete-upload
                     -> GET DownloadUrl is byte-exact
    transcribe       POST ai/transcriptions {file_url: DownloadUrl} -> PENDING
                     -> poll -> STARTED -> SUCCESS
    oracle path      the mock recognises the uploaded bytes (MD5) as a registered
                     meeting directory and answers with its ground truth
    -> hyp.json      mockcloud.oracle.result_to_hypothesis -> evals: DER == cpWER == 0.0

Evidence class: EMULATOR_INTEGRATION_PROVEN for our own components (mock cloud,
generator, evals); the HTTP contract shapes are the mock's DOC-EXACT /
BYTECODE_PROVEN claims (docs/mockcloud.md).  Everything runs in-process through
httpx.ASGITransport: no port, no socket, no real host.

The "oracle path" knob (docs/mockcloud.md, "Transcription results"): either
register the meeting directory with ``POST /_mock/meetings {"dir"}`` so an
ordinary presign -> PUT -> complete -> transcribe of the device file resolves
to it by MD5 (first test), or point a ``file://`` URL inside a
``local_file_roots`` directory (second test).  Either way the mock answers
through the pipeline's own oracle (``mockcloud.oracle.try_pipeline_oracle`` ->
``pipeline.oracle.OraclePipeline``) and the task's ``source`` ends in
``+pipeline-oracle``; a fallback to reading meeting.json directly would end in
``+meeting.json(<reason>)`` and fail these assertions (docs/integration.md,
"Findings": the hook used to probe names the pipeline never exported).

HARNESS_POLICY: the mock's chunk size shrunk to 20 000 bytes so the ~49 KB
file is a genuine multipart upload; task_step_s 0.02; the synthetic app
credentials and seeded SN from mockcloud/settings.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from test_mockcloud_helpers import (  # noqa: E402
    CLIENT_KEYS,
    PARTNER,
    SN_NOTEPRO,
    bearer,
    client,
    make_app,
    partner_token,
    user_token,
)

from plaudsim.handshake import normalize_historical_id  # noqa: E402

from evals import load_meeting, score_meeting  # noqa: E402
from evals.gates import evaluate, get_suite, load_gates  # noqa: E402
from evals.io import hypothesis_from_dict, load_hypothesis, write_hypothesis_files  # noqa: E402
from generator.export import DEVICE_FILES  # noqa: E402
from generator.testing import fixture_meeting  # noqa: E402
from mockcloud import MOCK_JWT_SECRET  # noqa: E402
from mockcloud import jwt as mjwt  # noqa: E402
from mockcloud.oracle import PLACEHOLDER_TEXT, result_to_hypothesis  # noqa: E402

pytestmark = pytest.mark.timeout(120)

SDK = f"{PARTNER}/open/partner/sdk"
FILES = f"{PARTNER}/open/partner/files/upload"
AI = f"{PARTNER}/open/partner/ai/transcriptions"
CHUNK = 20_000  # HARNESS_POLICY (documented default is 5 242 880)
DEVICE_REF = {"type": "notepro", "sn": SN_NOTEPRO}


@pytest.fixture(scope="module")
def meeting(tmp_path_factory) -> tuple[Path, dict]:
    return fixture_meeting(tmp_path_factory.mktemp("cloud-gen"), "smoke", 3)


@pytest.fixture(scope="module")
def ogg(meeting) -> bytes:
    d, _ = meeting
    return (d / DEVICE_FILES["ogg_opus"]).read_bytes()


async def upload_in_parts(c, tok: str, data: bytes, filetype: str = "opus") -> dict:
    """The documented three-step upload, asserting a genuine multipart (>= 2 parts)."""
    r = await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok),
                     json={"filesize": len(data), "filetype": filetype})
    assert r.status_code == 200, r.text
    pres = r.json()
    chunk = pres["ChunkSize"]
    assert chunk == CHUNK
    assert len(pres["Parts"]) == math.ceil(len(data) / chunk) >= 2, "must be a real multipart upload"
    parts = []
    for p in pres["Parts"]:
        n = p["PartNumber"]
        piece = data[(n - 1) * chunk: n * chunk]
        r = await c.put(p["PresignedUrl"], content=piece)
        assert r.status_code == 200, r.text
        assert r.headers["ETag"] == f'"{hashlib.md5(piece).hexdigest()}"'
        parts.append({"PartNumber": n, "ETag": r.headers["ETag"]})
    r = await c.post(f"{FILES}/complete-upload", headers=bearer(tok), json={
        "file_id": pres["FileId"], "upload_id": pres["UploadId"], "part_list": parts,
        "filetype": filetype, "file_md5": hashlib.md5(data).hexdigest(),
    })
    assert r.status_code == 200, r.text
    comp = r.json()
    assert comp["FileMd5"] == hashlib.md5(data).hexdigest() and comp["FileType"] == filetype
    return comp


async def poll_to_terminal(c, tid: str, *, max_polls: int = 400) -> tuple[dict, list[str]]:
    """GET the task until a terminal state, recording the distinct states seen."""
    seen: list[str] = []
    for _ in range(max_polls):
        r = await c.get(f"{AI}/{tid}", headers=CLIENT_KEYS)
        assert r.status_code == 200, r.text
        j = r.json()
        if not seen or seen[-1] != j["status"]:
            seen.append(j["status"])
        if j["status"] in ("SUCCESS", "FAILURE", "REVOKED"):
            return j, seen
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {tid} never reached a terminal state: {seen}")


def score_result(meeting_dir: Path, meeting_id: str, data: dict, out_dir: Path):
    hyp = result_to_hypothesis(data, meeting_id, system="mockcloud")
    assert hyp["schema"] == "plaud-harness/hypothesis/1"
    paths = write_hypothesis_files(hypothesis_from_dict(hyp), out_dir)  # hyp.json + hyp.rttm + hyp.stm
    assert all(p.is_file() for p in paths)
    return score_meeting(load_meeting(meeting_dir), load_hypothesis(out_dir / "hyp.json"))


@pytest.mark.asyncio
async def test_identity_bind_upload_transcribe_and_score_zero(meeting, ogg, tmp_path) -> None:
    d, m = meeting
    app, ctx, _ = make_app(chunk_size=CHUNK, task_step_s=0.02)
    async with client(app) as c:
        # --- the oracle-path knob: register the meeting directory --------------
        r = await c.post("/_mock/meetings", json={"dir": str(d)})
        assert r.status_code == 200 and r.json()["meeting_id"] == m["meeting_id"]
        assert DEVICE_FILES["ogg_opus"] in {e["file"] for e in r.json()["registered"]}

        # --- identity chain ------------------------------------------------------
        pt = await partner_token(c)
        assert pt["token_type"] == "bearer"
        tok = await user_token(c, "harness-user-0001")
        claims = mjwt.decode(tok, MOCK_JWT_SECRET)
        assert claims["user_id"] == "harness-user-0001" and claims["sub"].startswith("client_user_")
        sdk_token = normalize_historical_id(claims["sub"])
        assert re.fullmatch(r"[0-9a-f]{32}", sdk_token), "the sub survives the SDK's k3 derivation"

        # --- bind -----------------------------------------------------------------
        r = await c.post(f"{SDK}/bind", headers=bearer(tok), json=DEVICE_REF)
        assert r.status_code == 200 and r.json() == {**DEVICE_REF, "is_bind": True}
        r = await c.get(f"{SDK}/binding", headers=bearer(tok), params=DEVICE_REF)
        assert r.json()["is_bind"] is True
        assert r.json()["bind_history"] == [claims["sub"].removeprefix("client_user_")]

        # --- multipart upload of the device file ---------------------------------------
        comp = await upload_in_parts(c, tok, ogg, "opus")
        dl = await c.get(comp["DownloadUrl"])
        assert dl.status_code == 200 and dl.content == ogg, "byte-exact through the object store"
        assert dl.headers["content-type"] == "audio/ogg"

        # --- transcription task -----------------------------------------------------------
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={
            "file_url": comp["DownloadUrl"],
            "params": {"transcribe": {"language": "auto"}, "diarization": {"enabled": True}},
        })
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "PENDING" and r.json()["data"] == {}
        tid = r.json()["transcription_id"]
        assert tid.startswith("task_exec_")
        final, seen = await poll_to_terminal(c, tid)
        assert seen == ["PENDING", "STARTED", "SUCCESS"], seen
        data = final["data"]
        # the upload was recognised as the registered meeting, and the pipeline's own
        # oracle (not the meeting.json fallback) produced the ground truth
        assert ctx.state.tasks[tid].source == "objectstore+meeting+pipeline-oracle", ctx.state.tasks[tid].source

        # --- the result IS the ground truth ------------------------------------------------
        assert [s["text"] for s in data["results"]] == [s["text"] for s in m["segments"]]
        assert [(s["start"], s["end"]) for s in data["results"]] == [(s["start"], s["end"]) for s in m["segments"]]
        mapping: dict[str, str] = {}
        for ref, got in zip(m["segments"], data["results"]):
            mapping.setdefault(ref["speaker"], got["speaker_id"])
            assert mapping[ref["speaker"]] == got["speaker_id"], "one cloud label per reference speaker"
        assert set(mapping.values()) == {"Speaker 1", "Speaker 2"}
        assert data["duration"] == int(round(m["duration_s"])) and data["language"] == "en"

        # --- hyp.json -> evals ----------------------------------------------------------------
        report = score_result(d, m["meeting_id"], data, tmp_path / "hyp")
        assert report.system == "mockcloud"
        assert report.der.der == 0.0
        assert report.jer.jer == 0.0
        assert report.cpwer.error_rate == 0.0
        assert report.tcpwer.error_rate == 0.0
        assert report.speaker_count.abs_error == 0
        gate = evaluate(get_suite(load_gates(), "oracle"), report.flat(), target=m["meeting_id"])
        assert gate.passed, gate.summary()
        # the cloud's "Speaker N" labels differ from the reference's "spkN": the
        # label-literal WER pairs nothing and scores 2.0 while cpWER is 0 -- the
        # decision-log rationale for cpWER, seen end to end.
        assert report.wer_literal.wer > 0.0 and report.cpwer.error_rate == 0.0

        # --- unbind closes the loop ------------------------------------------------------------
        r = await c.post(f"{SDK}/unbind", headers=bearer(tok), json=DEVICE_REF)
        assert r.status_code == 200 and r.json()["is_bind"] is False


@pytest.mark.asyncio
async def test_file_url_inside_local_file_roots_takes_the_same_oracle_path(meeting, tmp_path) -> None:
    d, m = meeting
    app, ctx, _ = make_app(task_step_s=0.0, local_file_roots=(d,))
    async with client(app) as c:
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={
            "file_url": (d / DEVICE_FILES["ogg_opus"]).as_uri(),
            "params": {"diarization": {"enabled": True, "return_embedding": True}},
        })
        tid = r.json()["transcription_id"]
        final, _ = await poll_to_terminal(c, tid)
        assert final["status"] == "SUCCESS"
        assert ctx.state.tasks[tid].source == "file+meeting+pipeline-oracle", ctx.state.tasks[tid].source
        assert set(final["data"]["embeddings"]) == {"Speaker 1", "Speaker 2"}
        report = score_result(d, m["meeting_id"], final["data"], tmp_path / "hyp")
        assert report.der.der == 0.0 and report.cpwer.error_rate == 0.0


@pytest.mark.asyncio
async def test_falsifier_unregistered_upload_gets_the_placeholder_and_does_not_score_zero(meeting, ogg, tmp_path) -> None:
    """Without the knob the very same bytes are unknown audio: the mock answers
    its labelled placeholder, and evals refuses to call that ground truth."""
    d, m = meeting
    app, ctx, _ = make_app(chunk_size=CHUNK, task_step_s=0.0)
    async with client(app) as c:
        tok = await user_token(c)
        comp = await upload_in_parts(c, tok, ogg, "opus")
        r = await c.post(f"{AI}/", headers=CLIENT_KEYS, json={"file_url": comp["DownloadUrl"]})
        final, _ = await poll_to_terminal(c, r.json()["transcription_id"])
        assert final["status"] == "SUCCESS"
        data = final["data"]
        assert data["text"] == PLACEHOLDER_TEXT and len(data["results"]) == 1
        assert data["results"][0]["language_probability"] == 0.0
        assert abs(data["results"][0]["end"] - m["duration_s"]) <= 0.020, "duration read from the Ogg granules"
        report = score_result(d, m["meeting_id"], data, tmp_path / "hyp")
        assert report.cpwer.error_rate > 0.0
        assert report.der.der > 0.0
        assert not evaluate(get_suite(load_gates(), "oracle"), report.flat(), target=m["meeting_id"]).passed
