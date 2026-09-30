# Claude Code plugins

## s3-upload

Ask Claude Code to "upload this to the bucket" and it puts files (videos, builds, reports, screenshots, whole
folders) in your S3-compatible bucket and hands back presigned links you can open or share.

- Works with AWS S3, Cloudflare R2, MinIO, Alarik, Backblaze B2, Wasabi and other S3-compatible servers,
  including ones behind a proxy like Cloudflare.
- Large files: multipart uploads that survive proxies (weak ETags, 100 MB body caps, blocked HEAD requests).
- Every upload is verified on the server before the link is returned.
- Keys stay on your machine in `~/.config/s3-upload/config.json` (mode 600), never in the plugin or the chat.
- Only needs Python 3.9+; it installs boto3 into its own venv on first use. Linux, macOS and Windows.

### Install

```sh
claude plugin marketplace add Manan-Santoki/claude-plugins
claude plugin install s3-upload@msantoki
```

Or from inside Claude Code: `/plugin marketplace add Manan-Santoki/claude-plugins`, then
`/plugin install s3-upload@msantoki`.

### Configure (once per machine)

Run `/s3-upload:setup` in Claude Code: it walks you through your provider's endpoint, has you type the secret
into the terminal (never the chat), tests the connection and does a test upload. Or run the setup yourself:

```sh
python3 ~/.claude/plugins/cache/msantoki/s3-upload/*/skills/s3-upload/scripts/s3up.py configure
```

It asks for the endpoint, bucket and access key, reads the secret without echoing it, tests access, and saves only
if the test passes.

| Provider | Endpoint | Region |
|---|---|---|
| AWS S3 | `https://s3.<region>.amazonaws.com` | the bucket's region |
| Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` | `auto` |
| MinIO / Alarik / other self-hosted | `https://<your-host>` (the S3 API, not the web console) | `us-east-1` unless set |
| Backblaze B2 | `https://s3.<region>.backblazeb2.com` | the bucket's region |

Environment variables (`S3UP_ENDPOINT`, `S3UP_BUCKET`, `S3UP_ACCESS_KEY`, `S3UP_SECRET_KEY`, `S3UP_REGION`) override
the file, which is handy in CI.

### Use

Just ask: "upload the build to S3", "put these screenshots in the bucket under release/1.4", "give me a fresh link
for yesterday's video". Or call the script directly:

```sh
s3up.py put video.mp4 --prefix project/demo     # upload, print a presigned link (7 days max)
s3up.py ls project/                              # list
s3up.py link project/demo/video.mp4              # new link for an existing file
s3up.py whoami                                   # check the setup (prints no secrets)
```

## License

MIT
