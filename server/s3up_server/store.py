"""SQLite persistence: encrypted settings, OAuth clients/codes/tokens, upload tickets."""
import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clients (
  client_id TEXT PRIMARY KEY, secret_hash TEXT, name TEXT, redirect_uris TEXT NOT NULL,
  auth_method TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS codes (
  code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL,
  scope TEXT, resource TEXT, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, grant_id TEXT NOT NULL,
  scope TEXT, expires REAL NOT NULL, created REAL NOT NULL, last_used REAL);
CREATE INDEX IF NOT EXISTS tokens_grant ON tokens(grant_id);
CREATE TABLE IF NOT EXISTS tickets (
  ticket_hash TEXT PRIMARY KEY, prefix TEXT NOT NULL, days REAL NOT NULL, expires REAL NOT NULL,
  closed INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY, ticket_hash TEXT NOT NULL, key TEXT NOT NULL, size INTEGER NOT NULL,
  content_type TEXT NOT NULL, upload_id TEXT, state TEXT NOT NULL, created REAL NOT NULL);
"""

SETTINGS_DEFAULTS = {
    "endpoint": "", "bucket": "", "region": "us-east-1", "access_key": "", "secret_key": "", "console": "",
    "default_prefix": "claude/{date}", "default_days": 7, "allow_private_urls": False,
    "max_fetch_mb": 10240,
}
ACCESS_TTL = 7 * 86400
REFRESH_TTL = 180 * 86400
CODE_TTL = 600
TICKET_TTL = 2 * 3600


def h(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(n=32):
    return secrets.token_urlsafe(n)


class Store:
    def __init__(self, path, secret_key):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        key = hashlib.sha256(b"s3up-settings:" + secret_key.encode()).digest()
        self.fernet = Fernet(base64.urlsafe_b64encode(key))

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    # ---------- settings ----------

    def settings(self):
        """Returns (settings, error). error is set when stored settings can't be decrypted."""
        row = self.one("SELECT v FROM kv WHERE k='settings'")
        s = dict(SETTINGS_DEFAULTS)
        if not row:
            return s, None
        try:
            s.update(json.loads(self.fernet.decrypt(row["v"].encode())))
        except InvalidToken:
            return s, "Saved settings can't be decrypted (SECRET_KEY changed). Enter the bucket keys again."
        return s, None

    def save_settings(self, s):
        blob = self.fernet.encrypt(json.dumps(s).encode()).decode()
        self.q("INSERT INTO kv(k, v) VALUES('settings', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (blob,))

    def kv_get(self, k, default=None):
        row = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return row["v"] if row else default

    def kv_set(self, k, v):
        self.q("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # ---------- OAuth clients ----------

    def register_client(self, name, redirect_uris, auth_method):
        client_id = "c_" + new_secret(16)
        secret = new_secret() if auth_method != "none" else None
        self.q("INSERT INTO clients VALUES(?,?,?,?,?,?)",
               (client_id, h(secret) if secret else None, name, json.dumps(redirect_uris), auth_method, time.time()))
        # dynamic registration is open, so keep the table small: drop old clients that never got a token
        self.q("""DELETE FROM clients WHERE created < ? AND client_id NOT IN (SELECT client_id FROM tokens)""",
               (time.time() - 86400,))
        return client_id, secret

    def client(self, client_id):
        row = self.one("SELECT * FROM clients WHERE client_id=?", (client_id,))
        if not row:
            return None
        d = dict(row)
        d["redirect_uris"] = json.loads(d["redirect_uris"])
        return d

    # ---------- codes + tokens ----------

    def new_code(self, client_id, redirect_uri, challenge, scope, resource):
        code = new_secret()
        self.q("INSERT INTO codes VALUES(?,?,?,?,?,?,?)",
               (h(code), client_id, redirect_uri, challenge, scope, resource, time.time() + CODE_TTL))
        return code

    def take_code(self, code):
        with self.lock:
            row = self.one("SELECT * FROM codes WHERE code_hash=?", (h(code),))
            self.q("DELETE FROM codes WHERE code_hash=? OR expires < ?", (h(code), time.time()))
        if not row or row["expires"] < time.time():
            return None
        return dict(row)

    def issue_tokens(self, client_id, scope, grant_id=None):
        grant_id = grant_id or new_secret(12)
        access, refresh, now = new_secret(), new_secret(), time.time()
        self.q("INSERT INTO tokens VALUES(?,?,?,?,?,?,?,NULL)",
               (h(access), "access", client_id, grant_id, scope, now + ACCESS_TTL, now))
        self.q("INSERT INTO tokens VALUES(?,?,?,?,?,?,?,NULL)",
               (h(refresh), "refresh", client_id, grant_id, scope, now + REFRESH_TTL, now))
        self.q("DELETE FROM tokens WHERE expires < ?", (now,))
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "scope": scope}

    def refresh(self, refresh_token, client_id):
        with self.lock:
            row = self.one("SELECT * FROM tokens WHERE token_hash=? AND kind='refresh'", (h(refresh_token),))
            if not row or row["expires"] < time.time() or row["client_id"] != client_id:
                return None
            # rotate: the old refresh token and its access tokens die, a new pair takes over the same grant
            self.q("DELETE FROM tokens WHERE grant_id=?", (row["grant_id"],))
            return self.issue_tokens(client_id, row["scope"], row["grant_id"])

    def check_access(self, token):
        row = self.one("SELECT * FROM tokens WHERE token_hash=? AND kind='access'", (h(token),))
        if not row or row["expires"] < time.time():
            return None
        if not row["last_used"] or row["last_used"] < time.time() - 300:
            self.q("UPDATE tokens SET last_used=? WHERE token_hash=?", (time.time(), row["token_hash"]))
        return dict(row)

    def revoke_token(self, token):
        row = self.one("SELECT grant_id FROM tokens WHERE token_hash=?", (h(token),))
        if row:
            self.q("DELETE FROM tokens WHERE grant_id=?", (row["grant_id"],))

    def connections(self):
        return [dict(r) for r in self.q("""
            SELECT t.grant_id, c.name, c.redirect_uris, MIN(t.created) AS created, MAX(t.last_used) AS last_used
            FROM tokens t LEFT JOIN clients c ON c.client_id = t.client_id
            WHERE t.expires > ? GROUP BY t.grant_id ORDER BY created DESC""", (time.time(),))]

    def revoke_grant(self, grant_id):
        self.q("DELETE FROM tokens WHERE grant_id=?", (grant_id,))

    def revoke_all(self):
        self.q("DELETE FROM tokens")
        self.q("DELETE FROM codes")
        self.q("UPDATE tickets SET closed=1")

    # ---------- upload tickets ----------

    def new_ticket(self, prefix, days):
        tok = new_secret()
        now = time.time()
        self.q("DELETE FROM tickets WHERE expires < ?", (now - 86400,))
        self.q("DELETE FROM uploads WHERE created < ?", (now - 2 * 86400,))
        self.q("INSERT INTO tickets VALUES(?,?,?,?,0,?)", (h(tok), prefix, days, now + TICKET_TTL, now))
        return tok, now + TICKET_TTL

    def ticket(self, tok):
        row = self.one("SELECT * FROM tickets WHERE ticket_hash=?", (h(tok),))
        if not row or row["closed"] or row["expires"] < time.time():
            return None
        return dict(row)

    def close_ticket(self, tok):
        self.q("UPDATE tickets SET closed=1 WHERE ticket_hash=?", (h(tok),))

    def add_upload(self, ticket_hash, key, size, ctype, upload_id):
        uid = "u_" + new_secret(12)
        self.q("INSERT INTO uploads VALUES(?,?,?,?,?,?,'open',?)",
               (uid, ticket_hash, key, size, ctype, upload_id, time.time()))
        return uid

    def upload(self, ticket_hash, uid):
        row = self.one("SELECT * FROM uploads WHERE id=? AND ticket_hash=?", (uid, ticket_hash))
        return dict(row) if row else None

    def set_upload_state(self, uid, state):
        self.q("UPDATE uploads SET state=? WHERE id=?", (state, uid))
