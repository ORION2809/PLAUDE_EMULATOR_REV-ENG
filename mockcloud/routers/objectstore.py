"""The mock's stand-in for S3 (`PUT`/`GET` on presigned URLs).

The docs describe the real thing only from the client side: "PUT the raw bytes
of each chunk directly to its presigned S3 URL (no auth ...). Read the ETag
response header" (openapi_file.json description) and "https://plaud-bucket.
s3.amazonaws.com/..." examples. Everything about this store is HARNESS_POLICY
modelled on public S3 behaviour (INFERRED): a part's ETag is the quoted MD5 of
its bytes, an unsigned or expired presigned URL is 403 AccessDenied, and
errors come back as the S3 XML error document.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Request, Response

from ..common import MockHTTPError, ctx_of
from ..state import StoredObject

router = APIRouter(tags=["Mock object store"])


def s3_error(status: int, code: str, message: str) -> Response:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
    )
    return Response(content=body, status_code=status, media_type="application/xml")


@router.put("/s3/{bucket}/{key:path}")
async def put_object(bucket: str, key: str, request: Request) -> Response:
    ctx = ctx_of(request)
    q = request.query_params
    upload_id = q.get("uploadId")
    part_number = q.get("partNumber")
    try:
        ctx.check_signed_query("PUT", bucket, key, q, upload_id or "", part_number or "")
    except MockHTTPError as exc:
        return s3_error(403, "AccessDenied", exc.message)
    data = await request.body()
    etag = f'"{hashlib.md5(data).hexdigest()}"'
    if upload_id:
        mp = ctx.state.multipart.get(upload_id)
        if mp is None:
            return s3_error(404, "NoSuchUpload", "unknown uploadId")
        try:
            n = int(part_number or "")
        except ValueError:
            return s3_error(400, "InvalidArgument", "partNumber must be an integer")
        if not 1 <= n <= mp.part_count:
            return s3_error(400, "InvalidPart", f"partNumber {n} outside 1..{mp.part_count}")
        if len(data) > ctx.settings.chunk_size:
            # DOC: "You must send <ChunkSize of data to each PresignedUrl"
            # (plaud-embedded_file-api-overview.md:22) but also "PUT up to the
            # ChunkSize of raw bytes" (:80). The mock follows "up to": exactly
            # ChunkSize is accepted, more is refused. Code HARNESS_POLICY.
            return s3_error(400, "EntityTooLarge", "part exceeds ChunkSize")
        mp.parts[n] = {"etag": etag, "size": len(data)}
    ctx.state.put_object(StoredObject(
        bucket=bucket, key=key, data=data, content_type="application/octet-stream",
        etag=etag, acl="private", created_at=ctx.now(),
    ))
    return Response(status_code=200, headers={"ETag": etag})


@router.get("/s3/{bucket}/{key:path}")
async def get_object(bucket: str, key: str, request: Request) -> Response:
    ctx = ctx_of(request)
    obj = ctx.state.get_object(bucket, key)
    if obj is None:
        return s3_error(404, "NoSuchKey", "the specified key does not exist")
    if obj.acl != "public-read":
        try:
            ctx.check_signed_query("GET", bucket, key, request.query_params)
        except MockHTTPError as exc:
            return s3_error(403, "AccessDenied", exc.message)
    headers: dict[str, Any] = {"ETag": obj.etag, "Content-Length": str(len(obj.data))}
    return Response(content=obj.data, status_code=200, media_type=obj.content_type, headers=headers)
