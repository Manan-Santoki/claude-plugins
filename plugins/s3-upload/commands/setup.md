---
description: Connect s3-upload to your S3-compatible bucket (AWS S3, R2, MinIO, Alarik, B2…), test it, and save the settings
---

Help me connect the s3-upload plugin to my bucket.

1. Find the script: it is `skills/s3-upload/scripts/s3up.py` inside this plugin (the s3-upload skill's base directory). Run `python3 <that path> whoami` (use `python` on Windows). If it already works, tell me which endpoint and bucket it uses and stop unless I want to change it.
2. Otherwise ask me which provider I use, then show me the endpoint format from this table and what I need to fill in:
   | Provider | Endpoint | Region |
   |---|---|---|
   | AWS S3 | `https://s3.<region>.amazonaws.com` | the bucket's region |
   | Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` | `auto` |
   | MinIO / Alarik / other self-hosted | `https://<your-host>` (the S3 API, not the web console) | `us-east-1` unless set |
   | Backblaze B2 | `https://s3.<region>.backblazeb2.com` | the bucket's region |
3. Tell me to run the interactive setup myself so the secret key never goes through the chat. Give me the exact line with the real script path, prefixed with `!` so it runs in this session:
   `! python3 <script path> configure`
   It asks for the endpoint, bucket, access key, then the secret (hidden), tests access, and only saves if the test passes.
4. After I've run it, run `whoami` again, then upload a tiny test file with `put <file> --prefix s3-upload-test`, show me the link, and delete it with `rm`.

Never ask me to paste the secret key into the chat. If I do anyway, configure it non-interactively with `configure --endpoint … --bucket … --access-key … --secret-key …` and suggest rotating that key afterwards.

$ARGUMENTS
