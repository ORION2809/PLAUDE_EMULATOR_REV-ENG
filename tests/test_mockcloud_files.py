"""File Upload API + the mock object store: a 3-part multipart upload
reassembles byte-exact in PartNumber order, the DownloadUrl serves it and
expires, and the documented rejections happen.

Contract: build/docs-plaud-ai/openapi_file.json. The chunk size is shrunk to
1 KiB (HARNESS_POLICY knob) so the test moves kilobytes, not 15 MiB; the
default remains the documented 5 242 880.
"""

from __future__ import annotations

import hashlib
import random

import pytest

from test_mockcloud_helpers import (  # noqa: F401 - no_outbound_network is an autouse fixture
    PARTNER,
    bearer,
    client,
    make_app,
    no_outbound_network,
    upload_bytes,
    user_token,
)
from mockcloud.settings import DOC_CHUNK_SIZE, MockSettings

FILES = f"{PARTNER}/open/partner/files/upload"


def test_default_chunk_size_is_the_documented_5_mib() -> None:
    assert MockSettings().chunk_size == DOC_CHUNK_SIZE == 5242880


@pytest.mark.asyncio
async def test_presign_part_count_and_mock_urls() -> None:
    app, _, _ = make_app(chunk_size=1024)
    async with client(app) as c:
        tok = await user_token(c)
        r = await c.post(f"{FILES}/generate-presigned-urls", json={"filesize": 2500, "filetype": "mp3"})
        assert r.status_code == 401
        r = await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok), json={"filesize": 2500, "filetype": "mp3"})
        assert r.status_code == 200
        j = r.json()
        assert set(j) == {"FileId", "UploadId", "ChunkSize", "Parts"}
        assert j["FileId"].startswith("file_") and j["UploadId"].startswith("upload_")
        assert j["ChunkSize"] == 1024
        assert [p["PartNumber"] for p in j["Parts"]] == [1, 2, 3], "ceil(2500/1024)"
        for p in j["Parts"]:
            assert p["PresignedUrl"].startswith("http://mock/s3/plaud-bucket-mock/")
            assert "amazonaws.com" not in p["PresignedUrl"]
        # exactly one part for a file no bigger than one chunk
        r = await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok), json={"filesize": 1024, "filetype": "opus"})
        assert len(r.json()["Parts"]) == 1
        # documented filetype set is mp3|opus; the templates say wav is FILE_TYPE_INVALID
        r = await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok), json={"filesize": 10, "filetype": "wav"})
        assert r.status_code == 400 and r.json()["message"] == "FILE_TYPE_INVALID"
        r = await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok), json={"filesize": 0, "filetype": "mp3"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_three_part_upload_reassembles_byte_exact_in_part_order() -> None:
    app, _, _ = make_app(chunk_size=1024)
    rng = random.Random(1234)
    data = bytes(rng.getrandbits(8) for _ in range(2500))
    async with client(app) as c:
        tok = await user_token(c)
        r = await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok), json={"filesize": len(data), "filetype": "opus"})
        pres = r.json()
        etags: dict[int, str] = {}
        # Upload the parts OUT of order (3, 1, 2): the store must not care.
        for p in sorted(pres["Parts"], key=lambda p: [3, 1, 2].index(p["PartNumber"])):
            n = p["PartNumber"]
            chunk = data[(n - 1) * 1024: n * 1024]
            r = await c.put(p["PresignedUrl"], content=chunk)
            assert r.status_code == 200 and r.content == b""
            etag = r.headers["ETag"]
            assert etag == f'"{hashlib.md5(chunk).hexdigest()}"', "S3-style quoted MD5 ETag"
            etags[n] = etag
        # part_list also out of order, ETags quote-stripped like the templates do
        part_list = [{"PartNumber": n, "ETag": etags[n].strip('"')} for n in (2, 3, 1)]
        r = await c.post(f"{FILES}/complete-upload", headers=bearer(tok), json={
            "file_id": pres["FileId"], "upload_id": pres["UploadId"], "part_list": part_list,
            "filetype": "opus", "file_md5": hashlib.md5(data).hexdigest(),
        })
        assert r.status_code == 200, r.text
        comp = r.json()
        assert set(comp) == {"FileId", "FileType", "DownloadUrl", "FileMd5"}
        assert comp["FileId"] == pres["FileId"] and comp["FileType"] == "opus"
        assert comp["FileMd5"] == hashlib.md5(data).hexdigest()
        assert comp["DownloadUrl"].startswith("http://mock/s3/plaud-bucket-mock/")
        dl = await c.get(comp["DownloadUrl"])
        assert dl.status_code == 200
        assert dl.content == data, "byte-exact reassembly in PartNumber order"
        assert dl.headers["content-type"] == "audio/ogg"
        assert dl.headers["ETag"] == f'"{comp["FileMd5"]}"'
        # completing twice is idempotent (same DownloadUrl)
        r = await c.post(f"{FILES}/complete-upload", headers=bearer(tok), json={
            "file_id": pres["FileId"], "upload_id": pres["UploadId"], "part_list": part_list, "filetype": "opus",
        })
        assert r.status_code == 200 and r.json()["DownloadUrl"] == comp["DownloadUrl"]


@pytest.mark.asyncio
async def test_complete_rejects_wrong_etag_missing_part_and_md5_mismatch() -> None:
    app, _, _ = make_app(chunk_size=1024)
    # 2048 bytes -> 2 parts whose halves DIFFER (so part 1 and part 2 have
    # different ETags and a swapped ETag is genuinely wrong)
    data = bytes(range(256)) * 4 + bytes(reversed(range(256))) * 4
    assert hashlib.md5(data[:1024]).digest() != hashlib.md5(data[1024:]).digest()
    async with client(app) as c:
        tok = await user_token(c)
        pres = (await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok),
                             json={"filesize": len(data), "filetype": "mp3"})).json()
        p1, p2 = pres["Parts"]
        e1 = (await c.put(p1["PresignedUrl"], content=data[:1024])).headers["ETag"]
        base = {"file_id": pres["FileId"], "upload_id": pres["UploadId"], "filetype": "mp3"}
        # part 2 never uploaded
        r = await c.post(f"{FILES}/complete-upload", headers=bearer(tok), json={
            **base, "part_list": [{"PartNumber": 1, "ETag": e1}, {"PartNumber": 2, "ETag": '"deadbeef"'}]})
        assert r.status_code == 400 and "never uploaded" in r.json()["message"]
        e2 = (await c.put(p2["PresignedUrl"], content=data[1024:])).headers["ETag"]
        # wrong ETag for part 1
        r = await c.post(f"{FILES}/complete-upload", headers=bearer(tok), json={
            **base, "part_list": [{"PartNumber": 1, "ETag": e2}, {"PartNumber": 2, "ETag": e2}]})
        assert r.status_code == 400 and "ETag mismatch" in r.json()["message"]
        # md5 mismatch
        r = await c.post(f"{FILES}/complete-upload", headers=bearer(tok), json={
            **base, "part_list": [{"PartNumber": 1, "ETag": e1}, {"PartNumber": 2, "ETag": e2}],
            "file_md5": "0" * 32})
        assert r.status_code == 400 and r.json()["message"] == "FILE_MD5_MISMATCH"
        # unknown upload id
        r = await c.post(f"{FILES}/complete-upload", headers=bearer(tok), json={
            **base, "upload_id": "upload_nope", "part_list": [{"PartNumber": 1, "ETag": e1}]})
        assert r.status_code == 404
        # another user's token cannot complete this upload
        other = await user_token(c, "other-user-000002")
        r = await c.post(f"{FILES}/complete-upload", headers=bearer(other), json={
            **base, "part_list": [{"PartNumber": 1, "ETag": e1}, {"PartNumber": 2, "ETag": e2}]})
        assert r.status_code == 403
        # the honest completion still works afterwards
        r = await c.post(f"{FILES}/complete-upload", headers=bearer(tok), json={
            **base, "part_list": [{"PartNumber": 1, "ETag": e1}, {"PartNumber": 2, "ETag": e2}],
            "file_md5": hashlib.md5(data).hexdigest()})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_object_store_refuses_unsigned_forged_out_of_range_and_expired() -> None:
    app, _, clock = make_app(chunk_size=1024)
    async with client(app) as c:
        tok = await user_token(c)
        pres = (await c.post(f"{FILES}/generate-presigned-urls", headers=bearer(tok),
                             json={"filesize": 1500, "filetype": "mp3"})).json()
        url = pres["Parts"][0]["PresignedUrl"]
        # strip the signature
        r = await c.put(url.split("&X-Mock-Signature=")[0], content=b"x")
        assert r.status_code == 403 and b"AccessDenied" in r.content
        # forge the signature
        r = await c.put(url[:-4] + "0000", content=b"x")
        assert r.status_code == 403
        # re-target the signed URL at a part that was not presigned
        r = await c.put(url.replace("partNumber=1", "partNumber=7").replace("part-1", "part-7"), content=b"x")
        assert r.status_code == 403, "the signature covers the part number"
        # oversize part
        r = await c.put(url, content=bytes(1025))
        assert r.status_code == 400 and b"EntityTooLarge" in r.content
        # presign expiry
        clock.advance(3601)
        r = await c.put(url, content=b"x")
        assert r.status_code == 403 and b"expired" in r.content
        # unknown object
        r = await c.get("/s3/plaud-bucket-mock/nope.mp3")
        assert r.status_code == 404 and b"NoSuchKey" in r.content


@pytest.mark.asyncio
async def test_download_url_expires_after_24_hours() -> None:
    app, _, clock = make_app(chunk_size=1024)
    data = b"\x01\x02\x03" * 100
    async with client(app) as c:
        tok = await user_token(c)
        comp = await upload_bytes(c, tok, data, "mp3")
        assert (await c.get(comp["DownloadUrl"])).content == data
        clock.advance(24 * 3600 - 1)
        assert (await c.get(comp["DownloadUrl"])).status_code == 200
        clock.advance(2)
        r = await c.get(comp["DownloadUrl"])
        assert r.status_code == 403 and b"expired" in r.content
        # a plain GET without the presign query is also refused (private ACL)
        r = await c.get(comp["DownloadUrl"].split("?")[0])
        assert r.status_code == 403
