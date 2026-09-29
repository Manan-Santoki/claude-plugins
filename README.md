# msantoki Claude Code plugins

## s3-upload

Ask Claude to "upload this to the bucket" and it puts files in your S3-compatible bucket and hands back
presigned links. Handles Cloudflare-fronted endpoints (checksums, weak ETags, 100 MB body cap, HEAD 403).

Install:

```sh
claude plugin marketplace add <this repo or folder>
claude plugin install s3-upload@msantoki
```

Configure once per machine (prompts; the secret is read hidden, tested, then saved to
`~/.config/s3-upload/config.json`):

```sh
python3 ~/.claude/plugins/cache/msantoki/s3-upload/*/skills/s3-upload/scripts/s3up.py configure
```

or just ask Claude to "set up s3-upload" and run the command it gives you. Needs Python 3.9+ only; boto3 is
installed into a private venv at `~/.cache/s3-upload/venv` on first use.
