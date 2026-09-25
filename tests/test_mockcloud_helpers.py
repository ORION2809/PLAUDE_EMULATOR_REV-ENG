"""Shared helpers for the mockcloud tests (no tests live here).

Every test drives the FastAPI app in-process through httpx's ASGITransport:
no socket is opened, no port is bound, and nothing leaves the process.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from mockcloud import DEFAULT_PARTNER_APP as APP  # noqa: E402
from mockcloud import MockSettings, MockState, create_app  # noqa: E402

PARTNER = "/developer/api"
BASIC = base64.b64encode(f"{APP.client_id}:{APP.secret_key}".encode()).decode()
CLIENT_KEYS = {"X-Client-Id": APP.client_id, "X-Client-Api-Key": APP.api_key}
SN_NOTEPRO = "8810000000000001"
SN_NOTEPINS = "8820000000000001"


class Clock:
    """Deterministic clock injected into MockSettings.clock."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_app(**overrides: Any):
    """(app, ctx, clock) with fast worker steps and a small chunk size unless
    overridden. `state` may be passed to reuse a persisted MockState."""
    clock = overrides.pop("clock", None) or Clock()
    state = overrides.pop("state", None)
    settings = MockSettings(clock=clock, task_step_s=overrides.pop("task_step_s", 0.0), **overrides)
    app = create_app(settings, state or MockState())
    return app, app.state.mock, clock


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")


async def partner_token(c: httpx.AsyncClient) -> dict[str, Any]:
    r = await c.post(
        f"{PARTNER}/oauth/partner/access-token",
        headers={"Authorization": f"Basic {BASIC}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def user_token(c: httpx.AsyncClient, user_id: str = "harness-user-0001", expires_in: int | None = 86400) -> str:
    pt = (await partner_token(c))["access_token"]
    body: dict[str, Any] = {"user_id": user_id}
    if expires_in is not None:
        body["expires_in"] = expires_in
    r = await c.post(f"{PARTNER}/open/partner/users/access-token", headers={"Authorization": f"Bearer {pt}"}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def upload_bytes(c: httpx.AsyncClient, token: str, data: bytes, filetype: str = "opus") -> dict[str, Any]:
    """Full documented 3-step upload; returns the complete-upload response."""
    r = await c.post(
        f"{PARTNER}/open/partner/files/upload/generate-presigned-urls",
        headers=bearer(token), json={"filesize": len(data), "filetype": filetype},
    )
    assert r.status_code == 200, r.text
    pres = r.json()
    chunk = pres["ChunkSize"]
    parts = []
    for p in pres["Parts"]:
        n = p["PartNumber"]
        r = await c.put(p["PresignedUrl"], content=data[(n - 1) * chunk: n * chunk])
        assert r.status_code == 200, r.text
        parts.append({"PartNumber": n, "ETag": r.headers["ETag"]})
    r = await c.post(
        f"{PARTNER}/open/partner/files/upload/complete-upload",
        headers=bearer(token),
        json={"file_id": pres["FileId"], "upload_id": pres["UploadId"], "part_list": parts, "filetype": filetype},
    )
    assert r.status_code == 200, r.text
    return r.json()


def write_meeting_dir(root: Path, meeting_id: str = "mtg-0001", device_audio: bytes = b"") -> tuple[Path, dict[str, Any]]:
    """A minimal meeting directory per the shared Layer 2->3 contract."""
    mdir = root / meeting_id
    (mdir / "device").mkdir(parents=True)
    (mdir / "stems").mkdir()
    segments = [
        {"speaker": "spk0", "start": 0.0, "end": 1.5, "text": "hello there",
         "words": [{"w": "hello", "start": 0.0, "end": 0.6}, {"w": "there", "start": 0.7, "end": 1.5}]},
        {"speaker": "spk1", "start": 1.2, "end": 3.0, "text": "hi how are you",
         "words": [{"w": "hi", "start": 1.2, "end": 1.5}, {"w": "how", "start": 1.6, "end": 1.9},
                   {"w": "are", "start": 2.0, "end": 2.3}, {"w": "you", "start": 2.4, "end": 3.0}]},
        {"speaker": "spk0", "start": 3.4, "end": 4.9, "text": "fine thanks",
         "words": [{"w": "fine", "start": 3.4, "end": 3.9}, {"w": "thanks", "start": 4.0, "end": 4.9}]},
    ]
    meeting = {
        "schema": "plaud-harness/meeting/1",
        "meeting_id": meeting_id,
        "sample_rate": 16000,
        "duration_s": 5.25,
        "channels": 1,
        "speakers": [{"id": "spk0", "voice": "v0", "position_m": [1.0, 1.0, 1.2]},
                     {"id": "spk1", "voice": "v1", "position_m": [2.0, 1.0, 1.2]}],
        "segments": segments,
        "audio": {"mix_wav": "mix.wav", "stems": {"spk0": "stems/spk0.wav", "spk1": "stems/spk1.wav"},
                  "device": {"ogg_opus": "device/recording.ogg"}},
        "generator": {"name": "test-fixture", "version": "0", "seed": 1, "scenario": {}},
    }
    (mdir / "meeting.json").write_text(json.dumps(meeting, indent=1))
    (mdir / "device" / "recording.ogg").write_bytes(device_audio)
    lines = [f"SPEAKER {meeting_id} 1 {s['start']:.3f} {s['end'] - s['start']:.3f} <NA> <NA> {s['speaker']} <NA> <NA>"
             for s in segments]
    (mdir / "ref.rttm").write_text("\n".join(lines) + "\n")
    (mdir / "ref.stm").write_text(
        "\n".join(f"{meeting_id} 1 {s['speaker']} {s['start']:.3f} {s['end']:.3f} {s['text']}" for s in segments) + "\n"
    )
    return mdir, meeting
