---
name: s3-upload
description: Upload files or folders (videos, renders, reports, builds, screenshots, datasets) to the user's S3-compatible bucket (AWS S3, MinIO, Cloudflare R2, Alarik, Backblaze B2, Wasabi…) and return shareable presigned links; also list, re-link or delete uploads. Use when the user says "upload this", "put it on S3 / the bucket", "send me the file", or when a deliverable is too big for a chat attachment.
---

# s3-upload

One Python script, `<skill-dir>/scripts/s3up.py` (`<skill-dir>` = the base directory printed when this skill
loads). It needs only Python 3.9+; on first run it creates a private venv with boto3 in
`~/.cache/s3-upload/venv` and re-runs itself there. Use `python3` on Linux/macOS, `python` or `py` on Windows.

Call it directly (`python3 <skill-dir>/scripts/s3up.py put …`), or define a function: a `S3UP="python3 …"`
variable does not word-split in zsh.

```sh
s3up() { python3 "<skill-dir>/scripts/s3up.py" "$@"; }
s3up whoami                              # is it configured? which endpoint/bucket? (prints no secrets)
s3up put <file-or-dir> [...]             # default prefix: <cwd name>/<YYYY-MM-DD>/
s3up put video.mp4 --prefix project/topic
s3up put big.mp4 --days 2                # link lifetime (max 7 days)
s3up ls [prefix]
s3up link <key>                          # fresh 7-day link for an existing object
s3up rm <key>                            # delete, ONLY when the user asks
```

`put` verifies each upload's size on the server, then prints the key, size, a presigned download link (the
bucket can stay private; links last up to 7 days) and a web-console URL if one is configured.

## Setup (once per machine)

If `whoami` says it is not configured, ask the user to run the interactive setup themselves, so the secret never
enters the chat: `! python3 <skill-dir>/scripts/s3up.py configure`. It prompts for the endpoint, bucket and access
key, reads the secret hidden, tests access, and only saves if the test passes.

Endpoint examples:

| Provider | Endpoint | Region |
|---|---|---|
| AWS S3 | `https://s3.<region>.amazonaws.com` | the bucket's region |
| Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` | `auto` |
| MinIO / Alarik / other self-hosted | `https://<your-host>` (the S3 API, not the console) | `us-east-1` unless set |
| Backblaze B2 | `https://s3.<region>.backblazeb2.com` | the bucket's region |

Non-interactive (CI, or when the user pastes keys anyway):
`configure --endpoint … --bucket … --access-key … --secret-key … [--region …] [--console URL]`. If keys were
pasted into the chat, suggest rotating them afterwards. A key read off a screenshot may have look-alike
characters (I/l/1, O/0): a `SignatureDoesNotMatch` with a recognised access key points at the secret.

## Credentials

Stored in `~/.config/s3-upload/config.json` (mode 600), or overridden by env vars `S3UP_ENDPOINT`, `S3UP_BUCKET`,
`S3UP_ACCESS_KEY`, `S3UP_SECRET_KEY`, `S3UP_REGION`, `S3UP_CONSOLE` (and `S3UP_CONFIG` for another file path).
**Never print, echo, cat, log, commit or paste the keys**, and never write them into this plugin.

## How to report

Give the user each key, its size and the presigned link, plus the console link if configured. Upload the
original full-quality file unless told otherwise. Use a descriptive prefix (project/topic), not the bucket root,
and check `ls` before reusing a generic name so nothing is overwritten silently.

## Endpoint quirks already handled

Self-hosted and proxied S3 servers often break stock boto3; the script works around it:
- streamed CRC checksums are turned off (they cause "Payload hash mismatch" on many servers);
- files over 95 MB use a hand-rolled multipart upload in 64 MB parts that strips weak `W/` ETags (a proxy such as
  Cloudflare rewrites them, which makes boto3's own multipart fail with "ETag mismatch");
- single requests stay under Cloudflare's 100 MB body cap;
- object size is checked via listing, because HEAD can return 403 behind some proxies.
