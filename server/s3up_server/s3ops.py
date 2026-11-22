"""S3 helpers shared by the MCP tools and the upload-ticket endpoints.

Same workarounds as the plugin's local script: no streamed CRC checksums, path-style addressing, weak ETags
stripped before CompleteMultipartUpload, single requests under Cloudflare's 100 MB body cap, and object size
read from a listing because HEAD can be blocked behind a proxy.
"""
import datetime
import math

import boto3
from botocore.config import Config

SINGLE_MAX = 95 * 1024 * 1024
PART = 64 * 1024 * 1024
MAX_PARTS = 10000
MAX_LINK_DAYS = 7


def make_client(c):
    return boto3.client(
        "s3", endpoint_url=c["endpoint"], region_name=c.get("region") or "us-east-1",
        aws_access_key_id=c["access_key"], aws_secret_access_key=c["secret_key"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required", response_checksum_validation="when_required",
                      connect_timeout=15, read_timeout=120, retries={"max_attempts": 3, "mode": "standard"}),
    )


def test_access(c):
    make_client(c).list_objects_v2(Bucket=c["bucket"], MaxKeys=1)


def link_seconds(days):
    return max(60, min(int(float(days) * 86400), MAX_LINK_DAYS * 86400))


def presign_get(s3, bucket, key, days):
    return s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=link_seconds(days))


def remote_size(s3, bucket, key):
    for o in s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=5).get("Contents", []):
        if o["Key"] == key:
            return o["Size"]
    return None


def clean_etag(etag):
    return etag[2:] if etag.startswith("W/") else etag


def part_size_for(size):
    return max(PART, math.ceil(size / MAX_PARTS))


def upload_stream(s3, bucket, key, chunks, ctype, limit=None):
    """Upload an iterator of byte chunks: one PUT if it fits, else a hand-rolled multipart upload.

    Returns the number of bytes uploaded. `limit` aborts uploads that grow past it.
    """
    buf = bytearray()
    it = iter(chunks)
    total = 0
    done = False
    for chunk in it:
        buf += chunk
        total += len(chunk)
        if limit and total > limit:
            raise ValueError(f"source is larger than the {limit // (1024 * 1024)} MB limit")
        if len(buf) > SINGLE_MAX:
            break
    else:
        done = True
    if done:
        s3.put_object(Bucket=bucket, Key=key, Body=bytes(buf), ContentType=ctype)
        return total
    mp = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType=ctype)
    parts, n = [], 1
    try:
        def flush(data):
            nonlocal n
            r = s3.upload_part(Bucket=bucket, Key=key, UploadId=mp["UploadId"], PartNumber=n, Body=data)
            parts.append({"PartNumber": n, "ETag": clean_etag(r["ETag"])})
            n += 1

        while len(buf) >= PART:
            flush(bytes(buf[:PART]))
            del buf[:PART]
        for chunk in it:
            buf += chunk
            total += len(chunk)
            if limit and total > limit:
                raise ValueError(f"source is larger than the {limit // (1024 * 1024)} MB limit")
            while len(buf) >= PART:
                flush(bytes(buf[:PART]))
                del buf[:PART]
        if buf:
            flush(bytes(buf))
        s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=mp["UploadId"], MultipartUpload={"Parts": parts})
    except BaseException:
        try:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=mp["UploadId"])
        except Exception:
            pass
        raise
    return total


def expand_prefix(template):
    today = datetime.date.today()
    return (template or "").replace("{date}", today.isoformat()).replace("{yyyy}", f"{today:%Y}") \
        .replace("{mm}", f"{today:%m}").replace("{dd}", f"{today:%d}").strip("/")


def clean_key(key):
    key = (key or "").replace("\\", "/").lstrip("/")
    parts = key.split("/")
    if not key or any(p in ("", ".", "..") for p in parts[:-1]) or parts[-1] in ("", ".", ".."):
        raise ValueError(f"invalid object key: {key!r}")
    return key


def join_key(prefix, name):
    prefix = (prefix or "").replace("\\", "/").strip("/")
    return clean_key(f"{prefix}/{name}" if prefix else name)


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
