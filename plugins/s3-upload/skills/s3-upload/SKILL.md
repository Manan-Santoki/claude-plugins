---
name: s3-upload
description: Upload files or folders (videos, renders, reports, builds, screenshots, datasets) to the owner's S3 bucket for later review and return shareable links; also list, re-link or delete uploads. Use when the owner says "upload this", "put it on S3 / the bucket", "send me the file", or when a deliverable is too big for an artifact (>15 MB) or a chat attachment.
---

# s3-upload

One Python script, `<skill-dir>/scripts/s3up.py` (`<skill-dir>` = the base directory printed when this skill
loads). It needs only Python 3.9+; on first run it creates a private venv with boto3 in
`~/.cache/s3-upload/venv` and re-runs itself there. Use `python3` on Linux/macOS, `python` or `py` on Windows.

```sh
S3UP="python3 <skill-dir>/scripts/s3up.py"
$S3UP whoami                              # is it configured? which endpoint/bucket? (prints no secrets)
$S3UP put <file-or-dir> [...]             # default prefix: <cwd name>/<YYYY-MM-DD>/
$S3UP put video.mp4 --prefix project/topic
$S3UP put big.mp4 --days 2                # link lifetime (max 7 days)
$S3UP ls [prefix]
$S3UP link <key>                          # fresh 7-day link for an existing object
$S3UP rm <key>                            # delete, ONLY when the owner asks
```

`put` verifies each upload's size on the server, then prints the key, size, a presigned download link (the
bucket is private; links last up to 7 days) and the web-console URL if one is configured.

## Credentials

Stored in `~/.config/s3-upload/config.json` (mode 600) or overridden by env vars `S3UP_ENDPOINT`,
`S3UP_BUCKET`, `S3UP_ACCESS_KEY`, `S3UP_SECRET_KEY`, `S3UP_REGION`, `S3UP_CONSOLE`. **Never print, echo, cat,
log, commit or paste the keys**, and never write them into this plugin.

If `whoami` says it is not configured, ask the owner to run the interactive setup themselves (so the secret
never enters the chat), e.g. `! python3 <skill-dir>/scripts/s3up.py configure` — it prompts for endpoint,
bucket and access key, reads the secret hidden, tests access and only then saves. If they paste keys into the
chat anyway, run `configure --endpoint … --bucket … --access-key … --secret-key … [--console URL]`
non-interactively, and suggest rotating that key later. A key read off a screenshot may have look-alike
characters (I/l/1, O/0); a `SignatureDoesNotMatch` with a recognised access key points at the secret.

## How to report

Give the owner each key, its size and the presigned link, plus the console link. Upload the original
full-quality file (not a compressed web copy) unless told otherwise. Use a descriptive prefix (project/topic),
not the bucket root, and check `ls` before reusing a generic name so nothing is overwritten silently.

## Endpoint quirks already handled

Behind Cloudflare, S3 servers break stock boto3 in four ways; the script works around all of them: streamed CRC
checksums ("Payload hash mismatch") are turned off; files over 95 MB use a hand-rolled multipart upload in
64 MB parts that strips weak `W/` ETags ("ETag mismatch"); request bodies stay under the 100 MB cap; object
size is checked via listing because HEAD can return 403.
