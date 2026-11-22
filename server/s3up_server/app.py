"""s3-upload MCP server: one user, one bucket, reachable from Claude on any device.

Routes
  /                      settings page (password login)
  /mcp                   MCP endpoint (OAuth bearer token)
  /.well-known/...       OAuth discovery (RFC 9728 + RFC 8414)
  /register /authorize /token /revoke   OAuth 2.1 with PKCE and dynamic client registration
  /t/<ticket>/...        upload tickets used by `s3up.py push`
  /s3up.py               the push client, for machines without the plugin
"""
import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import secrets
import time
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from mcp.server.transport_security import TransportSecuritySettings

from . import s3ops
from .store import Store, h
from .tools import build_mcp

log = logging.getLogger("s3up")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CLIENT_SCRIPT = Path(os.environ.get("CLIENT_SCRIPT", Path(__file__).parent / "s3up.py"))
SESSION_TTL = 30 * 86400
SCOPE = "s3"

if not PUBLIC_URL.startswith(("https://", "http://")):
    raise SystemExit("PUBLIC_URL must be set to the server's public URL, e.g. https://s3.example.com")
if len(ADMIN_PASSWORD) < 12:
    raise SystemExit("ADMIN_PASSWORD must be set and at least 12 characters long")
DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = DATA_DIR / "secret_key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
        os.chmod(f, 0o600)
    return f.read_text().strip()


SECRET_KEY = load_secret_key()
store = Store(str(DATA_DIR / "s3up.db"), SECRET_KEY)
SECURE_COOKIE = PUBLIC_URL.startswith("https://")
MCP_URL = f"{PUBLIC_URL}/mcp"
PRM_URL = f"{PUBLIC_URL}/.well-known/oauth-protected-resource"

# ---------- password check with lockout ----------

_fails = []


def password_ok(pw):
    now = time.time()
    _fails[:] = [t for t in _fails if t > now - 900]
    if len(_fails) >= 8:
        return None  # locked out for a while
    if hmac.compare_digest(hashlib.sha256(pw.encode()).digest(), hashlib.sha256(ADMIN_PASSWORD.encode()).digest()):
        _fails.clear()
        return True
    _fails.append(now)
    return False


# ---------- web session ----------

def _sign(value):
    epoch = store.kv_get("session_epoch", "0")
    return hmac.new(SECRET_KEY.encode(), f"{value}:{epoch}".encode(), hashlib.sha256).hexdigest()


def make_session():
    exp = str(int(time.time() + SESSION_TTL))
    return f"{exp}.{_sign(exp)}"


def logged_in(request):
    raw = request.cookies.get("s3up_session", "")
    exp, _, sig = raw.partition(".")
    return bool(exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp)))


def same_origin(request):
    site = request.headers.get("sec-fetch-site")
    if site:  # modern browsers say directly whether the request came from our own pages
        return site in ("same-origin", "none")
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if origin == "null":
        origin = request.headers.get("referer") or ""
    if not origin:
        return True  # non-browser client; the SameSite=Strict cookie still protects the session
    o, p = urlparse(origin), urlparse(PUBLIC_URL)
    return (o.scheme, o.netloc) == (p.scheme, p.netloc)


# ---------- HTML ----------

CSS = """
:root{color-scheme:light dark;--bg:#f6f6f4;--fg:#1d1d1b;--mute:#6b6b66;--card:#fff;--line:#e2e1dc;--acc:#c2410c;
--ok:#15803d;--bad:#b91c1c}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--mute:#9b9b95;--card:#201f1e;--line:#33322f;
--acc:#fb923c;--ok:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:720px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:8px 0 4px}h2{font-size:16px;margin:0 0 12px}p.sub{color:var(--mute);margin:0 0 20px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:0 0 16px}
label{display:block;font-weight:600;font-size:13px;margin:12px 0 4px}label small{font-weight:400;color:var(--mute)}
input[type=text],input[type=password],input[type=url],input[type=number]{width:100%;padding:9px 10px;
border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);font:inherit}
input:focus{outline:2px solid var(--acc);outline-offset:-1px}.row{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
@media(max-width:560px){.row{grid-template-columns:1fr}}
button{margin-top:16px;padding:9px 16px;border:0;border-radius:7px;background:var(--acc);color:#fff;font:inherit;
font-weight:600;cursor:pointer}button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
button.small{margin:0;padding:5px 10px;font-size:13px}.msg{padding:10px 12px;border-radius:7px;margin:0 0 16px;
border:1px solid var(--line)}.msg.ok{border-color:var(--ok);color:var(--ok)}.msg.bad{border-color:var(--bad);
color:var(--bad)}code{font:13px ui-monospace,Menlo,Consolas,monospace;background:var(--bg);padding:2px 5px;
border-radius:4px;word-break:break-all}table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:8px 4px;border-top:1px solid var(--line);vertical-align:middle}td.r{text-align:right}
.mute{color:var(--mute)}.check{display:flex;gap:8px;align-items:center;margin-top:12px;font-size:14px}
ol{padding-left:20px;margin:0}li{margin:0 0 8px}
"""


def page(title, body, status=200):
    doc = (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
           f"content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
           f"<meta name=referrer content=same-origin><style>{CSS}</style></head><body><main>{body}</main></body></html>")
    return HTMLResponse(doc, status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                              # no form-action: the consent form redirects on to the app's callback
                                              "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                              "frame-ancestors 'none'"})


def esc(v):
    return html.escape(str(v if v is not None else ""), quote=True)


def flash(request):
    msg, kind = request.query_params.get("msg"), request.query_params.get("kind", "ok")
    return f"<div class='msg {'bad' if kind == 'bad' else 'ok'}'>{esc(msg)}</div>" if msg else ""


def back(msg, kind="ok"):
    return RedirectResponse("/?" + urlencode({"msg": msg, "kind": kind}), 303)


def login_page(error="", status=200):
    err = f"<div class='msg bad'>{esc(error)}</div>" if error else ""
    return page("s3-upload", f"""
      <h1>s3-upload</h1><p class=sub>Settings for your bucket connector.</p>{err}
      <section><form method=post action=/login><label for=pw>Password</label>
      <input id=pw type=password name=password autocomplete=current-password autofocus required>
      <button>Sign in</button></form></section>""", status)


def fmt_time(t):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) + " UTC" if t else "never"


def settings_page(request):
    s, err = store.settings()
    configured = all(s.get(k) for k in ("endpoint", "bucket", "access_key", "secret_key"))
    warn = f"<div class='msg bad'>{esc(err)}</div>" if err else ""
    secret_hint = "saved; leave blank to keep" if s.get("secret_key") else "required"
    rows = ""
    for c in store.connections():
        uris = json.loads(c["redirect_uris"] or "[]")
        where = urlparse(uris[0]).netloc if uris else ""
        rows += (f"<tr><td><b>{esc(c['name'] or 'MCP client')}</b><br><span class=mute>{esc(where)} · since "
                 f"{fmt_time(c['created'])} · last used {fmt_time(c['last_used'])}</span></td><td class=r>"
                 f"<form method=post action=/settings/revoke><input type=hidden name=grant value='{esc(c['grant_id'])}'>"
                 f"<button class='ghost small'>Revoke</button></form></td></tr>")
    conns = (f"<table>{rows}</table><form method=post action=/settings/revoke-all>"
             f"<button class=ghost>Sign out every device</button></form>") if rows \
        else "<p class=mute>Nothing connected yet.</p>"
    state = "<span style='color:var(--ok)'>connected</span>" if configured and not err \
        else "<span style='color:var(--bad)'>not configured</span>"
    return page("s3-upload settings", f"""
      <h1>s3-upload</h1><p class=sub>Bucket: <b>{esc(s.get('bucket') or '-')}</b> · {state}</p>
      {flash(request)}{warn}
      <section><h2>Connect Claude</h2><p>Connector URL: <code>{esc(MCP_URL)}</code></p><ol>
      <li><b>Claude app (web, desktop, phone):</b> Settings → Connectors → Add custom connector, paste the URL,
      then sign in here when asked. It then works on every device signed into your Claude account, and in Claude
      Code sessions logged in with that account.</li>
      <li><b>Claude Code only:</b> <code>claude mcp add --transport http --scope user s3-upload {esc(MCP_URL)}</code>,
      then run <code>/mcp</code> to sign in.</li></ol></section>
      <section><h2>Bucket</h2><form method=post action=/settings/connection>
      <label>Endpoint <small>the S3 API URL, e.g. https://s3.eu-west-1.amazonaws.com or
      https://&lt;account&gt;.r2.cloudflarestorage.com</small></label>
      <input type=url name=endpoint value='{esc(s.get('endpoint'))}' required placeholder='https://'>
      <div class=row><div><label>Bucket</label><input type=text name=bucket value='{esc(s.get('bucket'))}' required></div>
      <div><label>Region <small>auto for R2, us-east-1 for most self-hosted</small></label>
      <input type=text name=region value='{esc(s.get('region'))}'></div></div>
      <label>Access key</label><input type=text name=access_key value='{esc(s.get('access_key'))}' required
      autocomplete=off spellcheck=false>
      <label>Secret key <small>{secret_hint}</small></label>
      <input type=password name=secret_key autocomplete=new-password spellcheck=false
      {'' if s.get('secret_key') else 'required'}>
      <label>Web console URL <small>optional, shown next to uploads</small></label>
      <input type=url name=console value='{esc(s.get('console'))}'>
      <button>Test and save</button> <span class=mute>Nothing is saved unless the bucket can be listed.</span>
      </form></section>
      <section><h2>Defaults</h2><form method=post action=/settings/defaults>
      <label>Default folder <small>{{date}}, {{yyyy}}, {{mm}}, {{dd}} are filled in; Claude Code usually
      picks project/topic itself</small></label>
      <input type=text name=default_prefix value='{esc(s.get('default_prefix'))}'>
      <div class=row><div><label>Link lifetime (days, max 7)</label>
      <input type=number name=default_days min=0.01 max=7 step=0.01 value='{esc(s.get('default_days'))}'></div>
      <div><label>Max size for URL imports (MB)</label>
      <input type=number name=max_fetch_mb min=1 step=1 value='{esc(s.get('max_fetch_mb'))}'></div></div>
      <label class=check><input type=checkbox name=allow_private_urls value=1
      {'checked' if s.get('allow_private_urls') else ''}> Let “upload from URL” fetch private network addresses</label>
      <button>Save defaults</button></form></section>
      <section><h2>Connected devices and apps</h2>{conns}</section>
      <form method=post action=/logout><button class=ghost>Sign out of this page</button></form>""")


# ---------- web routes ----------

async def home(request):
    return settings_page(request) if logged_in(request) else login_page()


async def login(request):
    form = await request.form()
    ok = password_ok(form.get("password", ""))
    if ok is None:
        return login_page("Too many wrong attempts. Try again in 15 minutes.", 429)
    if not ok:
        return login_page("Wrong password.", 401)
    resp = RedirectResponse("/", 303)
    resp.set_cookie("s3up_session", make_session(), max_age=SESSION_TTL, httponly=True, secure=SECURE_COOKIE,
                    samesite="strict")
    return resp


async def logout(request):
    resp = RedirectResponse("/", 303)
    resp.delete_cookie("s3up_session")
    return resp


def guarded(fn):
    async def wrapper(request):
        if not logged_in(request):
            return RedirectResponse("/", 303)
        if not same_origin(request):
            return PlainTextResponse("cross-site request refused", 403)
        return await fn(request)
    return wrapper


@guarded
async def save_connection(request):
    form = await request.form()
    s, err = store.settings()
    if err:
        s["secret_key"] = ""
    new = dict(s)
    for k in ("endpoint", "bucket", "region", "access_key", "console"):
        new[k] = (form.get(k) or "").strip()
    new["region"] = new["region"] or "us-east-1"
    new["endpoint"] = new["endpoint"].rstrip("/")
    if (form.get("secret_key") or "").strip():
        new["secret_key"] = form["secret_key"].strip()
    if not new.get("secret_key"):
        return back("Enter the secret key.", "bad")
    try:
        await run_in_threadpool(s3ops.test_access, new)
    except Exception as e:
        return back(f"Test failed, nothing saved: {e}", "bad")
    store.save_settings(new)
    return back(f"Saved. Bucket '{new['bucket']}' is reachable.")


@guarded
async def save_defaults(request):
    form = await request.form()
    s, err = store.settings()
    if err:
        return back(err, "bad")
    try:
        days = float(form.get("default_days") or 7)
        max_mb = int(form.get("max_fetch_mb") or 10240)
    except ValueError:
        return back("Numbers only for link lifetime and max size.", "bad")
    s["default_prefix"] = (form.get("default_prefix") or "").strip().strip("/")
    s["default_days"] = min(max(days, 0.01), 7)
    s["max_fetch_mb"] = max(max_mb, 1)
    s["allow_private_urls"] = form.get("allow_private_urls") == "1"
    store.save_settings(s)
    return back("Defaults saved.")


@guarded
async def revoke(request):
    form = await request.form()
    store.revoke_grant(form.get("grant", ""))
    return back("Access revoked. That app has to sign in again.")


@guarded
async def revoke_all(request):
    store.revoke_all()
    store.kv_set("session_epoch", secrets.token_hex(8))
    resp = back("Every device and app was signed out.")
    resp.delete_cookie("s3up_session")
    return resp


# ---------- OAuth ----------

def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, mcp-protocol-version"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def oauth_error(error, desc, status=400):
    return cors(JSONResponse({"error": error, "error_description": desc}, status, headers={"Cache-Control": "no-store"}))


async def protected_resource(request):
    return cors(JSONResponse({"resource": MCP_URL, "authorization_servers": [PUBLIC_URL],
                              "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"],
                              "resource_name": "s3-upload"}))


async def auth_server_metadata(request):
    return cors(JSONResponse({
        "issuer": PUBLIC_URL, "authorization_endpoint": f"{PUBLIC_URL}/authorize",
        "token_endpoint": f"{PUBLIC_URL}/token", "registration_endpoint": f"{PUBLIC_URL}/register",
        "revocation_endpoint": f"{PUBLIC_URL}/revoke", "scopes_supported": [SCOPE],
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
    }))


def is_loopback(u):
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")


def valid_redirect(uri):
    u = urlparse(uri)
    if u.fragment or not u.scheme:
        return False
    if u.scheme == "https":
        return bool(u.netloc)
    if u.scheme == "http":
        return is_loopback(u)
    return u.scheme not in ("javascript", "data", "file", "vbscript")  # native-app custom schemes


def redirect_matches(registered, given):
    if given in registered:
        return True
    g = urlparse(given)
    if not is_loopback(g):
        return False
    # RFC 8252: loopback redirects may use any port
    return any(is_loopback(r) and r.hostname == g.hostname and r.path == g.path
               for r in map(urlparse, registered))


async def register(request):
    try:
        body = await request.json()
    except Exception:
        return oauth_error("invalid_client_metadata", "body must be JSON")
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and valid_redirect(u) for u in uris):
        return oauth_error("invalid_redirect_uri", "redirect_uris must be https, loopback http, or an app scheme")
    method = body.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        return oauth_error("invalid_client_metadata", f"unsupported token_endpoint_auth_method {method}")
    name = str(body.get("client_name") or "MCP client")[:80]
    client_id, secret = store.register_client(name, uris, method)
    out = {"client_id": client_id, "client_id_issued_at": int(time.time()), "client_name": name,
           "redirect_uris": uris, "token_endpoint_auth_method": method,
           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": SCOPE}
    if secret:
        out.update(client_secret=secret, client_secret_expires_at=0)
    log.info("registered OAuth client %s (%s)", client_id, name)
    return cors(JSONResponse(out, 201))


AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state",
               "scope", "resource")


def redirect_with(uri, **params):
    sep = "&" if urlparse(uri).query else "?"
    return RedirectResponse(uri + sep + urlencode({k: v for k, v in params.items() if v is not None}), 303)


def consent_page(p, client, error="", status=200):
    hidden = "".join(f"<input type=hidden name={k} value='{esc(p.get(k))}'>" for k in AUTH_PARAMS if p.get(k))
    where = urlparse(p["redirect_uri"]).netloc or p["redirect_uri"]
    err = f"<div class='msg bad'>{esc(error)}</div>" if error else ""
    return page("Allow access?", f"""
      <h1>Allow access to your bucket?</h1>
      <p class=sub><b>{esc(client['name'])}</b> ({esc(where)}) wants to upload, list, read and delete files in your
      bucket through this server.</p>{err}
      <section><form method=post action=/authorize>{hidden}<label for=pw>Settings password</label>
      <input id=pw type=password name=password autocomplete=current-password autofocus required>
      <button>Allow</button></form></section>""", status)


async def authorize(request):
    p = dict(request.query_params) if request.method == "GET" else dict(await request.form())
    client = store.client(p.get("client_id", ""))
    if not client:
        return page("Error", "<h1>Unknown app</h1><p>This client isn't registered. Remove the connector and add it "
                             "again.</p>", 400)
    redirect_uri = p.get("redirect_uri") or (client["redirect_uris"][0] if len(client["redirect_uris"]) == 1 else "")
    if not redirect_uri or not redirect_matches(client["redirect_uris"], redirect_uri):
        return page("Error", "<h1>Redirect not allowed</h1><p>The app's redirect URL doesn't match its "
                             "registration.</p>", 400)
    p["redirect_uri"] = redirect_uri
    state = p.get("state")
    if p.get("response_type") != "code":
        return redirect_with(redirect_uri, error="unsupported_response_type", state=state)
    if not p.get("code_challenge") or p.get("code_challenge_method", "plain") != "S256":
        return redirect_with(redirect_uri, error="invalid_request", error_description="PKCE with S256 is required",
                             state=state)
    if request.method == "GET":
        return consent_page(p, client)
    if not same_origin(request):
        return PlainTextResponse("cross-site request refused", 403)
    ok = password_ok(p.get("password", ""))
    if ok is None:
        return consent_page(p, client, "Too many wrong attempts. Try again in 15 minutes.", 429)
    if not ok:
        return consent_page(p, client, "Wrong password.", 401)
    code = store.new_code(client["client_id"], redirect_uri, p["code_challenge"], SCOPE, p.get("resource"))
    log.info("authorized client %s (%s)", client["client_id"], client["name"])
    return redirect_with(redirect_uri, code=code, state=state, iss=PUBLIC_URL)


def client_auth(request, form):
    """Returns (client, error_response)."""
    client_id, secret = form.get("client_id"), form.get("client_secret")
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("basic "):
        try:
            cid, _, sec = base64.b64decode(authz[6:]).decode().partition(":")
            client_id, secret = unquote(cid), unquote(sec)
        except Exception:
            return None, oauth_error("invalid_client", "bad Basic auth header", 401)
    client = store.client(client_id or "")
    if not client:
        return None, oauth_error("invalid_client", "unknown client", 401)
    if client["auth_method"] != "none":
        if not secret or not client["secret_hash"] or not hmac.compare_digest(h(secret), client["secret_hash"]):
            return None, oauth_error("invalid_client", "bad client secret", 401)
    return client, None


async def token(request):
    form = dict(await request.form())
    client, err = client_auth(request, form)
    if err:
        return err
    grant = form.get("grant_type")
    if grant == "authorization_code":
        code = store.take_code(form.get("code", ""))
        if not code or code["client_id"] != client["client_id"]:
            return oauth_error("invalid_grant", "code is invalid, used or expired")
        if form.get("redirect_uri") and form["redirect_uri"] != code["redirect_uri"]:
            return oauth_error("invalid_grant", "redirect_uri mismatch")
        verifier = form.get("code_verifier", "")
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not verifier or not hmac.compare_digest(digest, code["challenge"]):
            return oauth_error("invalid_grant", "PKCE verification failed")
        out = store.issue_tokens(client["client_id"], code["scope"])
    elif grant == "refresh_token":
        out = store.refresh(form.get("refresh_token", ""), client["client_id"])
        if not out:
            return oauth_error("invalid_grant", "refresh token is invalid or expired")
    else:
        return oauth_error("unsupported_grant_type", f"grant_type {grant!r} is not supported")
    return cors(JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}))


async def revoke_token(request):
    form = dict(await request.form())
    _, err = client_auth(request, form)
    if not err:
        store.revoke_token(form.get("token", ""))
    return cors(Response(status_code=200))


async def preflight(request):
    return cors(Response(status_code=204))


# ---------- upload tickets (used by `s3up.py push`) ----------

def s3_ctx():
    s, err = store.settings()
    if err or not all(s.get(k) for k in ("endpoint", "bucket", "access_key", "secret_key")):
        raise ValueError(err or "bucket not configured on the server")
    return s3ops.make_client(s), s


def ticket_or_404(request):
    t = store.ticket(request.path_params["tok"])
    if not t:
        return None, JSONResponse({"error": "ticket is unknown, expired or closed; ask Claude for a new one"}, 404)
    return t, None


async def ticket_info(request):
    t, err = ticket_or_404(request)
    if err:
        return err
    return JSONResponse({"prefix": t["prefix"], "expires": t["expires"], "days": t["days"]})


async def ticket_start(request):
    t, err = ticket_or_404(request)
    if err:
        return err
    body = await request.json()
    try:
        size = int(body["size"])
        if size < 0:
            raise ValueError
        key = s3ops.join_key(t["prefix"], str(body["name"]))
    except (KeyError, ValueError, TypeError) as e:
        return JSONResponse({"error": f"bad request: {e}"}, 400)
    ctype = str(body.get("content_type") or mimetypes.guess_type(key)[0] or "application/octet-stream")

    def plan():
        s3, s = s3_ctx()
        expires = 12 * 3600
        if size <= s3ops.SINGLE_MAX:
            url = s3.generate_presigned_url("put_object", ExpiresIn=expires,
                                            Params={"Bucket": s["bucket"], "Key": key, "ContentType": ctype})
            uid = store.add_upload(t["ticket_hash"], key, size, ctype, None)
            return {"id": uid, "key": key, "mode": "single", "url": url, "headers": {"Content-Type": ctype}}
        part = s3ops.part_size_for(size)
        mp = s3.create_multipart_upload(Bucket=s["bucket"], Key=key, ContentType=ctype)
        n = -(-size // part)
        urls = [s3.generate_presigned_url("upload_part", ExpiresIn=expires, Params={
            "Bucket": s["bucket"], "Key": key, "UploadId": mp["UploadId"], "PartNumber": i}) for i in range(1, n + 1)]
        uid = store.add_upload(t["ticket_hash"], key, size, ctype, mp["UploadId"])
        return {"id": uid, "key": key, "mode": "multipart", "part_size": part, "urls": urls}

    try:
        return JSONResponse(await run_in_threadpool(plan))
    except Exception as e:
        log.warning("ticket start failed: %s", e)
        return JSONResponse({"error": str(e)}, 502)


async def ticket_finish(request):
    t, err = ticket_or_404(request)
    if err:
        return err
    body = await request.json()
    up = store.upload(t["ticket_hash"], str(body.get("id", "")))
    if not up or up["state"] not in ("open", "done"):
        return JSONResponse({"error": "unknown or aborted upload id"}, 404)

    def finish():
        s3, s = s3_ctx()
        if up["upload_id"] and up["state"] == "open":  # "done" = a retried finish: just report again
            parts = sorted(({"PartNumber": int(p["n"]), "ETag": s3ops.clean_etag(str(p["etag"]))}
                            for p in body.get("parts", [])), key=lambda p: p["PartNumber"])
            s3.complete_multipart_upload(Bucket=s["bucket"], Key=up["key"], UploadId=up["upload_id"],
                                         MultipartUpload={"Parts": parts})
        remote = s3ops.remote_size(s3, s["bucket"], up["key"])
        if remote != up["size"]:
            raise RuntimeError(f"size check failed: sent {up['size']} bytes, bucket has {remote}")
        store.set_upload_state(up["id"], "done")
        return {"key": up["key"], "size": up["size"], "link": s3ops.presign_get(s3, s["bucket"], up["key"], t["days"]),
                "console": s.get("console") or None}

    try:
        return JSONResponse(await run_in_threadpool(finish))
    except Exception as e:
        log.warning("ticket finish failed: %s", e)
        return JSONResponse({"error": str(e)}, 502)


async def ticket_abort(request):
    t, err = ticket_or_404(request)
    if err:
        return err
    body = await request.json()
    up = store.upload(t["ticket_hash"], str(body.get("id", "")))
    if up and up["state"] == "open" and up["upload_id"]:
        def abort():
            s3, s = s3_ctx()
            s3.abort_multipart_upload(Bucket=s["bucket"], Key=up["key"], UploadId=up["upload_id"])
        with contextlib.suppress(Exception):
            await run_in_threadpool(abort)
    if up:
        store.set_upload_state(up["id"], "aborted")
    return JSONResponse({"ok": True})


async def ticket_close(request):
    store.close_ticket(request.path_params["tok"])
    return JSONResponse({"ok": True})


async def client_script(request):
    if not CLIENT_SCRIPT.exists():
        return PlainTextResponse("client script not bundled", 404)
    return FileResponse(CLIENT_SCRIPT, media_type="text/x-python", filename="s3up.py")


async def healthz(request):
    return PlainTextResponse("ok")


# ---------- MCP with bearer auth ----------

mcp = build_mcp(store, PUBLIC_URL)
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


class BearerAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            if scope["method"] == "OPTIONS":
                return await cors(Response(status_code=204))(scope, receive, send)
            headers = dict(scope.get("headers") or [])
            authz = headers.get(b"authorization", b"").decode()
            tok = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
            if not tok or not store.check_access(tok):
                resp = JSONResponse({"error": "invalid_token", "error_description": "sign in required"}, 401, headers={
                    "WWW-Authenticate": f'Bearer error="invalid_token", resource_metadata="{PRM_URL}", scope="{SCOPE}"'})
                return await cors(resp)(scope, receive, send)
        await self.app(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(app):
    log.info("s3-upload server at %s (MCP %s)", PUBLIC_URL, MCP_URL)
    async with mcp.session_manager.run():
        yield


routes = [
    Route("/", home), Route("/login", login, methods=["POST"]), Route("/logout", logout, methods=["POST"]),
    Route("/settings/connection", save_connection, methods=["POST"]),
    Route("/settings/defaults", save_defaults, methods=["POST"]),
    Route("/settings/revoke", revoke, methods=["POST"]), Route("/settings/revoke-all", revoke_all, methods=["POST"]),
    Route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET", "OPTIONS"]),
    Route("/.well-known/oauth-protected-resource/mcp", protected_resource, methods=["GET", "OPTIONS"]),
    Route("/.well-known/oauth-authorization-server", auth_server_metadata, methods=["GET", "OPTIONS"]),
    Route("/.well-known/oauth-authorization-server/mcp", auth_server_metadata, methods=["GET", "OPTIONS"]),
    Route("/.well-known/openid-configuration", auth_server_metadata, methods=["GET", "OPTIONS"]),
    Route("/register", register, methods=["POST", "OPTIONS"]),
    Route("/authorize", authorize, methods=["GET", "POST"]),
    Route("/token", token, methods=["POST", "OPTIONS"]), Route("/revoke", revoke_token, methods=["POST", "OPTIONS"]),
    Route("/t/{tok}", ticket_info), Route("/t/{tok}/start", ticket_start, methods=["POST"]),
    Route("/t/{tok}/finish", ticket_finish, methods=["POST"]), Route("/t/{tok}/abort", ticket_abort, methods=["POST"]),
    Route("/t/{tok}/close", ticket_close, methods=["POST"]),
    Route("/s3up.py", client_script), Route("/healthz", healthz),
    Mount("/", app=BearerAuth(mcp_app)),
]


class PreflightFirst:
    """Answer CORS preflights on the OAuth endpoints before routing (Route method lists would 405 them)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "OPTIONS" and (
                scope["path"].startswith("/.well-known/") or scope["path"] in ("/register", "/token", "/revoke")):
            return await cors(Response(status_code=204))(scope, receive, send)
        await self.app(scope, receive, send)


app = PreflightFirst(Starlette(routes=routes, lifespan=lifespan))
