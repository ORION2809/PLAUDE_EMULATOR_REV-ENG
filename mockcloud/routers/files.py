"""File Upload API -- build/docs-plaud-ai/openapi_file.json and
plaud-embedded_file-api-overview.md (DOC-EXACT paths, bodies, response keys
including their CamelCase). The presigned URLs point at this mock's own object
store (routers/objectstore.py) instead of plaud-bucket.s3.amazonaws.com.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..common import MockHTTPError, ctx_of, new_id, require_user_token
from ..settings import MOCK_BUCKET
from ..state import MultipartUpload, StoredObject

router = APIRouter(tags=["File Upload API"])
PREFIX = "/developer/api/open/partner/files/upload"

#: DOC-EXACT: "File extension of the audio file. ("mp3" or "opus")"
#: (openapi_file.json GeneratePresignedUrlsRequest.filetype); the template
#: comments say WAV is rejected as FILE_TYPE_INVALID
#: (TranscriptionManager.kt:100, SyncManager.kt:790 -- OFFICIAL_SOURCE claims).
ALLOWED_FILETYPES = ("mp3", "opus")

CONTENT_TYPES = {"mp3": "audio/mpeg", "opus": "audio/ogg", "wav": "audio/wav"}


class GeneratePresignedUrlsRequest(BaseModel):
    filesize: int
    filetype: str


class CompletedPart(BaseModel):
    PartNumber: int
    ETag: str


class CompleteUploadRequest(BaseModel):
    file_id: str
    upload_id: str
    part_list: list[CompletedPart] = Field(min_length=1)
    filetype: str
    file_md5: str | None = None


def part_key(file_id: str, n: int) -> str:
    return f"{file_id}/part-{n}"


def object_key(file_id: str, filetype: str) -> str:
    return f"{file_id}.{filetype}"


@router.post(f"{PREFIX}/generate-presigned-urls")
async def generate_presigned_urls(request: Request, body: GeneratePresignedUrlsRequest) -> dict[str, Any]:
    """{FileId, UploadId, ChunkSize, Parts[{PartNumber, PresignedUrl}]} --
    DOC-EXACT keys. Part count = ceil(filesize / ChunkSize) ("Determines how
    many parts are returned" -- INFERRED arithmetic). filesize <= 0 -> 400 and
    a filetype outside mp3|opus -> 400 FILE_TYPE_INVALID (HARNESS_POLICY codes;
    the demo backend validates the same two things, embedded-capacitor
    nextjs-demo/app/api/transcription/presign/route.ts:30-35)."""
    ctx = ctx_of(request)
    claims = require_user_token(request)
    if body.filesize <= 0:
        raise MockHTTPError(400, "filesize must be a positive byte count")
    if body.filetype not in ALLOWED_FILETYPES:
        raise MockHTTPError(400, "FILE_TYPE_INVALID")
    chunk = ctx.settings.chunk_size
    count = max(1, math.ceil(body.filesize / chunk))
    file_id = new_id("file_")
    upload_id = new_id("upload_")
    now = ctx.now()
    exp = str(int(now + ctx.settings.presign_ttl_s))
    base = ctx.base_url(request)
    parts = []
    for n in range(1, count + 1):
        key = part_key(file_id, n)
        sig = ctx.sign("PUT", MOCK_BUCKET, key, upload_id, str(n), exp)
        parts.append({
            "PartNumber": n,
            "PresignedUrl": (
                f"{base}s3/{MOCK_BUCKET}/{key}?uploadId={upload_id}&partNumber={n}"
                f"&X-Mock-Expires={exp}&X-Mock-Signature={sig}"
            ),
        })
    ctx.state.multipart[upload_id] = MultipartUpload(
        file_id=file_id, upload_id=upload_id, filetype=body.filetype, filesize=body.filesize,
        sub=str(claims["sub"]), part_count=count, created_at=now,
    )
    return {"FileId": file_id, "UploadId": upload_id, "ChunkSize": chunk, "Parts": parts}


@router.post(f"{PREFIX}/complete-upload")
async def complete_upload(request: Request, body: CompleteUploadRequest) -> dict[str, Any]:
    """{FileId, FileType, DownloadUrl, FileMd5} -- DOC-EXACT keys; DownloadUrl
    valid 24 h (DOC-EXACT). Parts are concatenated in ascending PartNumber
    regardless of the order in part_list (S3 CompleteMultipartUpload semantics,
    INFERRED). ETags are compared with surrounding quotes stripped because both
    templates strip them (TranscriptionManager.kt:161, PlaudAPIService.swift:105).
    Unknown upload -> 404, unknown/unuploaded part or ETag mismatch -> 400,
    file_md5 mismatch -> 400 FILE_MD5_MISMATCH (all HARNESS_POLICY codes)."""
    ctx = ctx_of(request)
    claims = require_user_token(request)
    mp = ctx.state.multipart.get(body.upload_id)
    if mp is None or mp.file_id != body.file_id:
        raise MockHTTPError(404, "NoSuchUpload: unknown upload_id / file_id pair")
    if mp.sub != claims["sub"]:
        raise MockHTTPError(403, "upload belongs to a different user token")
    if body.filetype != mp.filetype:
        raise MockHTTPError(400, "filetype differs from generate-presigned-urls")
    if mp.completed is not None:
        return mp.completed  # HARNESS_POLICY: completing twice is idempotent
    ordered = sorted(body.part_list, key=lambda p: p.PartNumber)
    seen: set[int] = set()
    chunks: list[bytes] = []
    for p in ordered:
        if p.PartNumber in seen:
            raise MockHTTPError(400, f"InvalidPart: PartNumber {p.PartNumber} listed twice")
        seen.add(p.PartNumber)
        stored = mp.parts.get(p.PartNumber)
        if stored is None:
            raise MockHTTPError(400, f"InvalidPart: part {p.PartNumber} was never uploaded")
        if p.ETag.strip('"') != stored["etag"].strip('"'):
            raise MockHTTPError(400, f"InvalidPart: ETag mismatch for part {p.PartNumber}")
        obj = ctx.state.get_object(MOCK_BUCKET, part_key(mp.file_id, p.PartNumber))
        assert obj is not None
        chunks.append(obj.data)
    data = b"".join(chunks)
    md5 = hashlib.md5(data).hexdigest()
    if body.file_md5 and body.file_md5.lower() != md5:
        raise MockHTTPError(400, "FILE_MD5_MISMATCH")
    key = object_key(mp.file_id, mp.filetype)
    now = ctx.now()
    ctx.state.put_object(StoredObject(
        bucket=MOCK_BUCKET, key=key, data=data,
        content_type=CONTENT_TYPES.get(mp.filetype, "application/octet-stream"),
        etag=f'"{md5}"', acl="private", created_at=now,
    ))
    exp = str(int(now + ctx.settings.download_url_ttl_s))
    sig = ctx.sign("GET", MOCK_BUCKET, key, exp)
    download_url = f"{ctx.base_url(request)}s3/{MOCK_BUCKET}/{key}?X-Mock-Expires={exp}&X-Mock-Signature={sig}"
    result = {"FileId": mp.file_id, "FileType": mp.filetype, "DownloadUrl": download_url, "FileMd5": md5}
    mp.completed = result
    ctx.state.files[mp.file_id] = {
        "key": key, "filetype": mp.filetype, "md5": md5, "download_url": download_url,
        "expires_at": float(exp), "size": len(data), "sub": mp.sub,
    }
    return result
