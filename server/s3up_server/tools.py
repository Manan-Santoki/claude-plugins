"""MCP tools. Every call reads the settings from the store, so changes on the settings page apply at once."""
import base64
import binascii
import functools
import ipaddress
import mimetypes
import socket
import time
from typing import Annotated
from urllib.parse import unquote, urljoin, urlparse

import httpx
from pydantic import Field
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import s3ops

INSTRUCTIONS = """Upload files to the user's own S3-compatible bucket and hand back presigned links (max 7 days).
- Files on the machine you run commands on (Claude Code): call create_upload_ticket, then run the `push` command it
  returns. That uploads straight from the machine to the bucket; never read big files into the chat.
- Content you have in the conversation: upload_text for text, upload_base64 for small binaries (< 20 MB).
- A file at a public URL: upload_from_url (the server fetches it).
- Use a descriptive prefix (project/topic) and check list_files before reusing a generic name.
- delete_file only when the user asks. Bucket keys are set on the server's settings page, never in chat.
Report each key, its size and the link."""

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
FETCH = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

MAX_INLINE = 20 * 1024 * 1024

Days = Annotated[float | None, Field(description="Link lifetime in days (max 7). Default: the server setting.")]
Prefix = Annotated[str | None, Field(description="Folder in the bucket, e.g. 'project/topic'. Default: the "
                                                  "server's default prefix (usually claude/<date>).")]


class NotConfigured(Exception):
    pass


def surfaced(fn):
    """Let the model see why a call failed (the SDK hides messages of unexpected exceptions)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(str(e) or type(e).__name__) from e
    return wrapper


def build_mcp(store, public_url):
    mcp = MCPServer(name="s3-upload", title="S3 upload", instructions=INSTRUCTIONS, version="2.0.0",
                    website_url=public_url)

    def ctx():
        s, err = store.settings()
        if err:
            raise NotConfigured(err)
        if not all(s.get(k) for k in ("endpoint", "bucket", "access_key", "secret_key")):
            raise NotConfigured(f"The bucket isn't configured yet. Open {public_url}/ and enter the S3 settings.")
        return s3ops.make_client(s), s

    def days_of(s, days):
        return float(days if days is not None else s.get("default_days") or 7)

    def prefix_of(s, prefix):
        return s3ops.expand_prefix(prefix if prefix is not None else s.get("default_prefix"))

    def report(s3, s, key, size, days):
        lines = [f"uploaded  {key}  ({s3ops.human(size)})",
                 f"  link    {s3ops.presign_get(s3, s['bucket'], key, days)}"]
        if s.get("console"):
            lines.append(f"console   {s['console']}")
        return "\n".join(lines)

    def verify(s3, s, key, size):
        remote = s3ops.remote_size(s3, s["bucket"], key)
        if remote != size:
            raise RuntimeError(f"size check failed for {key}: sent {size} bytes, bucket has {remote}")

    @mcp.tool(annotations=RO, structured_output=False)
    @surfaced
    def status() -> str:
        """Show which endpoint and bucket uploads go to, the defaults, and whether access works. No secrets."""
        s, err = store.settings()
        lines = [f"settings page  {public_url}/", f"endpoint       {s.get('endpoint') or '(not set)'}",
                 f"bucket         {s.get('bucket') or '(not set)'}", f"region         {s.get('region')}",
                 f"default prefix {s.get('default_prefix')}  (now: {prefix_of(s, None) or '/'})",
                 f"link lifetime  {s.get('default_days')} days"]
        if s.get("console"):
            lines.append(f"console        {s['console']}")
        if err:
            lines.append(f"access         ERROR: {err}")
        else:
            try:
                ctx()
                s3ops.test_access(s)
                lines.append("access         OK")
            except Exception as e:
                lines.append(f"access         ERROR: {e}")
        return "\n".join(lines)

    @mcp.tool(annotations=WRITE, structured_output=False)
    @surfaced
    def create_upload_ticket(prefix: Prefix = None, link_days: Days = None) -> str:
        """Start an upload of local files or folders (any size) from the machine you run shell commands on.

        Returns a one-off ticket URL (valid 2 hours, only for this prefix) and the command to run. The bytes go
        straight from that machine to the bucket via presigned URLs; no keys are needed there. Use this in
        Claude Code for anything on disk instead of reading the file into the conversation.
        """
        _, s = ctx()
        pfx = prefix_of(s, prefix)
        tok, expires = store.new_ticket(pfx, days_of(s, link_days))
        url = f"{public_url}/t/{tok}"
        return (f"Ticket for prefix '{pfx or '/'}' (valid until {time.strftime('%H:%M UTC', time.gmtime(expires))}):\n"
                f"{url}\n\n"
                "Run it on the machine that has the files (files and folders, any number, any size):\n"
                f"  python3 \"<s3-upload skill dir>/scripts/s3up.py\" push {url} <file-or-dir> [...]\n"
                "Without the s3-upload plugin, fetch the same script from this server first:\n"
                f"  curl -fsSL {public_url}/s3up.py -o s3up.py && python3 s3up.py push {url} <file-or-dir> [...]\n"
                "(Windows: use python or py instead of python3.) It prints each key, size and link.")

    @mcp.tool(annotations=WRITE, structured_output=False)
    @surfaced
    def upload_text(name: Annotated[str, Field(description="File name, e.g. 'notes.md'. May include sub-folders.")],
                    text: Annotated[str, Field(description="The full file content.")],
                    prefix: Prefix = None, content_type: str | None = None, link_days: Days = None) -> str:
        """Upload text you have in the conversation (notes, code, CSV, HTML, JSON, markdown) as a file."""
        s3, s = ctx()
        key = s3ops.join_key(prefix_of(s, prefix), name)
        data = text.encode("utf-8")
        ctype = content_type or mimetypes.guess_type(name)[0] or "text/plain"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        s3.put_object(Bucket=s["bucket"], Key=key, Body=data, ContentType=ctype)
        verify(s3, s, key, len(data))
        return report(s3, s, key, len(data), days_of(s, link_days))

    @mcp.tool(annotations=WRITE, structured_output=False)
    @surfaced
    def upload_base64(name: Annotated[str, Field(description="File name, e.g. 'chart.png'.")],
                      data_base64: Annotated[str, Field(description="File bytes, base64-encoded. Max 20 MB decoded.")],
                      prefix: Prefix = None, content_type: str | None = None, link_days: Days = None) -> str:
        """Upload a small binary file (image, PDF, zip) whose bytes you have, base64-encoded. For files on disk in
        Claude Code use create_upload_ticket instead."""
        s3, s = ctx()
        key = s3ops.join_key(prefix_of(s, prefix), name)
        if len(data_base64) > MAX_INLINE * 4 // 3 + 16:
            raise ValueError("too large for inline upload (20 MB max); use create_upload_ticket or upload_from_url")
        try:
            data = base64.b64decode("".join(data_base64.split()), validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"data_base64 is not valid base64: {e}")
        ctype = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        s3.put_object(Bucket=s["bucket"], Key=key, Body=data, ContentType=ctype)
        verify(s3, s, key, len(data))
        return report(s3, s, key, len(data), days_of(s, link_days))

    @mcp.tool(annotations=FETCH, structured_output=False)
    @surfaced
    def upload_from_url(url: Annotated[str, Field(description="http(s) URL of the file to copy into the bucket.")],
                        name: Annotated[str | None, Field(description="File name. Default: from the URL.")] = None,
                        prefix: Prefix = None, link_days: Days = None) -> str:
        """Have the server download a file from a URL and store it in the bucket (large files are fine)."""
        s3, s = ctx()
        limit = int(s.get("max_fetch_mb") or 10240) * 1024 * 1024
        with httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=False) as client:
            resp = open_url(client, url, bool(s.get("allow_private_urls")))
            try:
                final = str(resp.url)
                fname = name or unquote(urlparse(final).path.rstrip("/").rsplit("/", 1)[-1]) or "download"
                key = s3ops.join_key(prefix_of(s, prefix), fname)
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip() \
                    or mimetypes.guess_type(fname)[0] or "application/octet-stream"
                size = s3ops.upload_stream(s3, s["bucket"], key, resp.iter_bytes(1024 * 1024), ctype, limit)
            finally:
                resp.close()
        verify(s3, s, key, size)
        return report(s3, s, key, size, days_of(s, link_days))

    @mcp.tool(annotations=RO, structured_output=False)
    @surfaced
    def list_files(prefix: Annotated[str, Field(description="Only keys starting with this.")] = "",
                   limit: Annotated[int, Field(ge=1, le=1000)] = 100) -> str:
        """List objects in the bucket (newest first within the listed page), with size and date."""
        s3, s = ctx()
        rows, more = [], False
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=s["bucket"], Prefix=prefix or ""):
            for o in page.get("Contents", []):
                if len(rows) >= limit:
                    more = True
                    break
                rows.append(o)
            if more:
                break
        if not rows:
            return f"no objects under '{prefix or '/'}'"
        out = [f"{o['LastModified']:%Y-%m-%d %H:%M}  {s3ops.human(o['Size']):>9}  {o['Key']}" for o in rows]
        if more:
            out.append(f"... more than {limit}; narrow the prefix or raise limit")
        return "\n".join(out)

    @mcp.tool(annotations=RO, structured_output=False)
    @surfaced
    def get_link(key: Annotated[str, Field(description="Full object key, as shown by list_files.")],
                 link_days: Days = None) -> str:
        """Make a fresh presigned download link for an existing object."""
        s3, s = ctx()
        size = s3ops.remote_size(s3, s["bucket"], key)
        if size is None:
            raise ValueError(f"no such object: {key}")
        return f"{key}  ({s3ops.human(size)})\n{s3ops.presign_get(s3, s['bucket'], key, days_of(s, link_days))}"

    @mcp.tool(annotations=RO, structured_output=False)
    @surfaced
    def read_file(key: Annotated[str, Field(description="Full object key.")],
                  max_bytes: Annotated[int, Field(ge=1, le=2_000_000)] = 200_000) -> str:
        """Read a text file from the bucket (first max_bytes). Binary files return a summary and a link instead."""
        s3, s = ctx()
        r = s3.get_object(Bucket=s["bucket"], Key=key, Range=f"bytes=0-{max_bytes - 1}")
        data = r["Body"].read(max_bytes)
        total = int((r.get("ContentRange") or "/0").rsplit("/", 1)[-1] or 0) or r.get("ContentLength", len(data))
        try:
            if bytes(1) in data[:8192]:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL byte")
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            link = s3ops.presign_get(s3, s["bucket"], key, 1)
            return f"{key} is binary ({r.get('ContentType')}, {s3ops.human(total)}). Download: {link}"
        note = f"\n\n[truncated: showed {len(data)} of {total} bytes]" if total > len(data) else ""
        return text + note

    @mcp.tool(annotations=DELETE, structured_output=False)
    @surfaced
    def delete_file(key: Annotated[str, Field(description="Full object key to delete.")]) -> str:
        """Delete one object. Only when the user explicitly asks."""
        s3, s = ctx()
        if s3ops.remote_size(s3, s["bucket"], key) is None:
            raise ValueError(f"no such object: {key}")
        s3.delete_object(Bucket=s["bucket"], Key=key)
        return f"deleted {key}"

    @mcp.tool(annotations=WRITE, structured_output=False)
    @surfaced
    def update_settings(default_prefix: Annotated[str | None, Field(
                            description="Default folder template; {date}, {yyyy}, {mm}, {dd} are filled in.")] = None,
                        default_link_days: Annotated[float | None, Field(ge=0.01, le=7)] = None,
                        console: Annotated[str | None, Field(description="Web console URL shown with uploads; "
                                                                         "empty string clears it.")] = None) -> str:
        """Change non-secret defaults. Endpoint, bucket and keys can only be changed on the settings page."""
        s, err = store.settings()
        if err:
            raise NotConfigured(err)
        if default_prefix is not None:
            s["default_prefix"] = default_prefix.strip().strip("/")
        if default_link_days is not None:
            s["default_days"] = default_link_days
        if console is not None:
            s["console"] = console.strip()
        store.save_settings(s)
        return f"saved: default prefix '{s['default_prefix']}', links {s['default_days']} days, " \
               f"console '{s.get('console') or '-'}'"

    return mcp


def check_host(url, allow_private):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("only http(s) URLs are supported")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ValueError(f"can't resolve {p.hostname}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ValueError(f"{p.hostname} resolves to a private address ({ip}); enable private URLs on the "
                             "settings page to allow it")


def open_url(client, url, allow_private):
    for _ in range(6):
        check_host(url, allow_private)
        resp = client.send(client.build_request("GET", url), stream=True)
        if resp.is_redirect and resp.headers.get("location"):
            url = urljoin(url, resp.headers["location"])
            resp.close()
            continue
        if resp.status_code >= 400:
            resp.close()
            raise ValueError(f"fetch failed: HTTP {resp.status_code} from {urlparse(url).hostname}")
        return resp
    raise ValueError("too many redirects")
