#!/usr/bin/env python3
"""Upload files to an S3-compatible bucket and print review links.

Usage:
  s3up.py configure [--endpoint URL --bucket NAME --access-key KEY [--secret-key KEY] ...]
                                                              save + test credentials (prompts for anything missing)
  s3up.py whoami                                              show endpoint/bucket and test access (no secrets)
  s3up.py put <file-or-dir> [...] [--prefix PATH] [--days N]  upload; prints key + presigned link per file
  s3up.py ls [PREFIX]                                         list objects
  s3up.py link <key> [--days N]                               fresh presigned link for an existing object
  s3up.py rm <key>                                            delete one object

Works on Linux, macOS and Windows with only Python 3.9+: on first run it creates a private venv with boto3
(~/.cache/s3-upload/venv) and re-runs itself there.

Credentials: ~/.config/s3-upload/config.json (mode 600), or S3UP_CONFIG=<path>. Environment variables
S3UP_ENDPOINT, S3UP_BUCKET, S3UP_ACCESS_KEY, S3UP_SECRET_KEY, S3UP_REGION, S3UP_CONSOLE override the file.
"""
import argparse, datetime, getpass, json, mimetypes, os, subprocess, sys
from pathlib import Path

HOME = Path.home()
CONFIG = Path(os.environ.get("S3UP_CONFIG") or HOME / ".config" / "s3-upload" / "config.json")
VENV = HOME / ".cache" / "s3-upload" / "venv"
SINGLE_MAX = 95 * 1024 * 1024  # Cloudflare-fronted endpoints cap request bodies at 100 MB
PART = 64 * 1024 * 1024
FIELDS = ["endpoint", "bucket", "region", "access_key", "secret_key", "console"]


# ---------- bootstrap: make sure boto3 is importable, using a private venv if needed ----------

def venv_python():
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_boto3():
    try:
        import boto3  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("S3UP_IN_VENV"):
        sys.exit("boto3 still missing inside the venv; delete ~/.cache/s3-upload/venv and retry")
    py = venv_python()
    if not py.exists():
        print("s3up: first run, creating ~/.cache/s3-upload/venv with boto3 ...", file=sys.stderr)
        VENV.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    if subprocess.call([str(py), "-c", "import boto3"], stderr=subprocess.DEVNULL) != 0:
        subprocess.check_call([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", "boto3"])
    env = dict(os.environ, S3UP_IN_VENV="1")
    sys.exit(subprocess.call([str(py), os.path.abspath(__file__), *sys.argv[1:]], env=env))


# ---------- config + client ----------

def load_config(required=True):
    c = {}
    if CONFIG.exists():
        c = json.loads(CONFIG.read_text())
    for f in FIELDS:
        v = os.environ.get("S3UP_" + f.upper())
        if v:
            c[f] = v
    c.setdefault("region", "us-east-1")
    missing = [f for f in ("endpoint", "bucket", "access_key", "secret_key") if not c.get(f)]
    if required and missing:
        sys.exit(f"s3up is not configured ({', '.join(missing)} missing). Run: python {Path(__file__).name} configure")
    return c


def make_client(c):
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", endpoint_url=c["endpoint"], region_name=c.get("region", "us-east-1"),
        aws_access_key_id=c["access_key"], aws_secret_access_key=c["secret_key"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      # newer boto3 streams CRC checksums by default; many self-hosted S3 servers reject them
                      request_checksum_calculation="when_required", response_checksum_validation="when_required"),
    )


def client():
    c = load_config()
    return make_client(c), c


# ---------- helpers ----------

def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def presign(s3, bucket, key, days):
    secs = max(60, min(int(days * 86400), 7 * 86400))  # S3 caps presigned links at 7 days
    return s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=secs)


def remote_size(s3, bucket, key):
    # HEAD requests can come back 403 through a proxy, so size comes from a listing
    for o in s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=5).get("Contents", []):
        if o["Key"] == key:
            return o["Size"]
    return None


def upload(s3, bucket, path, key, ctype):
    """Single PUT when small; otherwise a hand-rolled multipart upload.

    boto3's managed transfer can't be used behind Cloudflare: part ETags come back weak (W/"..."), boto3
    echoes them verbatim, and the server rejects CompleteMultipartUpload with "ETag mismatch".
    """
    size = path.stat().st_size
    if size <= SINGLE_MAX:
        with open(path, "rb") as fh:
            s3.put_object(Bucket=bucket, Key=key, Body=fh, ContentType=ctype)
        return
    mp = s3.create_multipart_upload(Bucket=bucket, Key=key, ContentType=ctype)
    parts = []
    try:
        with open(path, "rb") as fh:
            n = 1
            while True:
                chunk = fh.read(PART)
                if not chunk:
                    break
                r = s3.upload_part(Bucket=bucket, Key=key, UploadId=mp["UploadId"], PartNumber=n, Body=chunk)
                etag = r["ETag"]
                parts.append({"PartNumber": n, "ETag": etag[2:] if etag.startswith("W/") else etag})
                print(f"  part {n}/{-(-size // PART)}", file=sys.stderr)
                n += 1
        s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=mp["UploadId"], MultipartUpload={"Parts": parts})
    except BaseException:
        s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=mp["UploadId"])
        raise


def default_prefix():
    project = Path.cwd().name or "misc"
    return f"{project}/{datetime.date.today().isoformat()}"


# ---------- commands ----------

def cmd_configure(a):
    c = load_config(required=False)
    given = {"endpoint": a.endpoint, "bucket": a.bucket, "region": a.region, "access_key": a.access_key,
             "secret_key": a.secret_key, "console": a.console}
    for k, v in given.items():
        if v:
            c[k] = v
    interactive = sys.stdin.isatty()
    for k in ("endpoint", "bucket", "access_key", "secret_key"):
        if not c.get(k):
            if not interactive:
                sys.exit(f"missing --{k.replace('_', '-')}")
            c[k] = (getpass.getpass(f"{k}: ") if k == "secret_key" else input(f"{k}: ")).strip()
    s3 = make_client(c)
    try:
        s3.list_objects_v2(Bucket=c["bucket"], MaxKeys=1)
    except Exception as e:
        sys.exit(f"test failed, nothing saved: {e}")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077) if hasattr(os, "umask") else None
    try:
        CONFIG.write_text(json.dumps({k: c[k] for k in FIELDS if c.get(k)}, indent=2))
    finally:
        if old is not None:
            os.umask(old)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass
    print(f"saved {CONFIG}; access to bucket '{c['bucket']}' OK")


def cmd_whoami(a):
    s3, c = client()
    s3.list_objects_v2(Bucket=c["bucket"], MaxKeys=1)
    print(f"endpoint {c['endpoint']}\nbucket   {c['bucket']}\nconfig   {CONFIG}\naccess   OK")
    if c.get("console"):
        print(f"console  {c['console']}")


def cmd_put(a):
    s3, c = client()
    prefix = (a.prefix if a.prefix is not None else default_prefix()).strip("/")
    files = []
    for p in map(Path, a.paths):
        if p.is_dir():
            files += [(f, f"{p.name}/{f.relative_to(p).as_posix()}") for f in sorted(p.rglob("*")) if f.is_file()]
        elif p.is_file():
            files.append((p, p.name))
        else:
            sys.exit(f"not found: {p}")
    for f, rel in files:
        key = f"{prefix}/{rel}" if prefix else rel
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        upload(s3, c["bucket"], f, key, ctype)
        remote = remote_size(s3, c["bucket"], key)
        if remote != f.stat().st_size:
            sys.exit(f"size mismatch for {key}: local {f.stat().st_size}, remote {remote}")
        print(f"uploaded  {key}  ({human(f.stat().st_size)})")
        print(f"  link    {presign(s3, c['bucket'], key, a.days)}")
    if c.get("console"):
        print(f"console   {c['console']}  (open folder: {prefix or '/'})")


def cmd_ls(a):
    s3, c = client()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=c["bucket"], Prefix=a.prefix or ""):
        for o in page.get("Contents", []):
            print(f"{o['LastModified']:%Y-%m-%d %H:%M}  {human(o['Size']):>9}  {o['Key']}")


def cmd_link(a):
    s3, c = client()
    if remote_size(s3, c["bucket"], a.key) is None:
        sys.exit(f"no such object: {a.key}")
    print(presign(s3, c["bucket"], a.key, a.days))


def cmd_rm(a):
    s3, c = client()
    s3.delete_object(Bucket=c["bucket"], Key=a.key)
    print(f"deleted {a.key}")


def main():
    ensure_boto3()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("configure")
    for f in FIELDS:
        p.add_argument("--" + f.replace("_", "-"), dest=f)
    p.set_defaults(fn=cmd_configure)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    p = sub.add_parser("put"); p.add_argument("paths", nargs="+"); p.add_argument("--prefix"); p.add_argument("--days", type=float, default=7)
    p.set_defaults(fn=cmd_put)
    p = sub.add_parser("ls"); p.add_argument("prefix", nargs="?"); p.set_defaults(fn=cmd_ls)
    p = sub.add_parser("link"); p.add_argument("key"); p.add_argument("--days", type=float, default=7); p.set_defaults(fn=cmd_link)
    p = sub.add_parser("rm"); p.add_argument("key"); p.set_defaults(fn=cmd_rm)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
