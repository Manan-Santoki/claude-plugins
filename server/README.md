# s3-upload server

A small self-hosted MCP server that keeps your bucket settings in one place. After you add it once as a
connector, Claude on every device (claude.ai on the web, the desktop and phone apps, and Claude Code) can upload
to your bucket, list and read files and make links. You change keys and defaults from any browser on its settings
page. It's built for one user and one bucket.

- **Settings page** (`/`): endpoint, bucket, keys, default folder, link lifetime and connected apps, behind your
  password. The secret is encrypted at rest and never shown again.
- **MCP endpoint** (`/mcp`): streamable HTTP with OAuth 2.1 (PKCE, dynamic client registration). Signing in means
  typing your settings password on the consent page. You can revoke any app from the settings page.
- **Tools:** `create_upload_ticket`, `upload_text`, `upload_base64`, `upload_from_url`, `list_files`, `get_link`,
  `read_file`, `delete_file`, `update_settings` and `status`.
- **Large local files** (Claude Code): `create_upload_ticket` returns a one-off URL that's valid for 2 hours and
  limited to one prefix. `s3up.py push <ticket> <files…>` then sends the bytes **straight to the bucket** over
  presigned URLs, using multipart in 64 MB parts, weak ETags stripped and size verified. The bytes never pass
  through this server, so Cloudflare's 100 MB body cap doesn't apply, and the machine needs no keys or boto3.
  A machine without the plugin can fetch the client from `/s3up.py`.

## Deploy on Dokploy

1. Create a **Compose** service from this repository (branch `main`) with compose path
   `./server/docker-compose.yml`.
2. Add these in the service's **Environment** tab:
   ```
   PUBLIC_URL=https://s3up.example.com
   ADMIN_PASSWORD=<12+ characters, used for the settings page and for approving connections>
   ```
   `SECRET_KEY` is optional. If you don't set it, the server generates one and keeps it in the `s3up-data` volume.
   If you set it and later change it, the saved keys can no longer be decrypted and you have to enter them again.
3. Add a **domain** pointing at service `s3up`, port `8080`, with HTTPS. Deploy.
4. Open `https://s3up.example.com`, sign in and enter the bucket settings. **Test and save** only saves settings
   that can actually list the bucket.

Anywhere else with Docker works too:
`PUBLIC_URL=… ADMIN_PASSWORD=… docker compose -f server/docker-compose.yml up -d --build`, behind any HTTPS
reverse proxy.

## Connect Claude

- **Claude app (web, desktop, phone):** Settings → Connectors → Add custom connector →
  `https://s3up.example.com/mcp`, then approve it with your password. Connectors sync across your account, so this
  also covers the phone and Claude Code sessions logged in with that account.
- **Claude Code only:** `claude mcp add --transport http --scope user s3-upload https://s3up.example.com/mcp`, then
  run `/mcp` and sign in.

Install the plugin in Claude Code (`/plugin install s3-upload@msantoki`) too. It teaches Claude to use the ticket
flow for files on disk.

## Limits

- On the phone and on claude.ai there's no local disk. Claude can upload text, content it generates, small files
  as base64 (up to 20 MB) and anything at a URL (`upload_from_url`, which by default refuses private network
  addresses).
- Presigned links last up to 7 days, an S3 limit.
- The server keeps its data in SQLite in `/data`: settings, OAuth clients and tokens, and tickets. Back up the volume
  if you don't want to sign in again after losing it.
