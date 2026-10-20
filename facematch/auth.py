"""Accounts, sessions and access control.

* Passwords: scrypt (stdlib) with a per-user salt.  Sessions: random 256-bit tokens, only their SHA-256 is stored.
* Cookie: HttpOnly + SameSite=Lax (blocks cross-site POSTs, i.e. CSRF), Secure when served over HTTPS.
* Login throttling per (IP, username) and per IP.  Everything notable is written to the audit log.
* Invite links let an admin add someone without ever knowing their password."""
import hashlib
import hmac
import os
import secrets
import threading
import time

from flask import g, request

from . import db

COOKIE = "fm_session"
SESSION_DAYS = 30
MIN_PW = 8
ROLES = ("admin", "member")
_lock = threading.Lock()
_fails: dict = {}      # key -> [count, first_ts, locked_until]


class AuthError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- passwords / tokens
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def check_new_password(pw: str):
    if len(pw or "") < MIN_PW:
        raise AuthError(f"Use at least {MIN_PW} characters for the password.")
    if len(pw) > 200:
        raise AuthError("That password is too long.")


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip() -> str:
    if os.environ.get("FACEMATCH_TRUST_PROXY") == "1":
        xf = request.headers.get("X-Forwarded-For", "")
        if xf:
            return xf.split(",")[0].strip()
    return request.remote_addr or "?"


def is_https() -> bool:
    return request.is_secure or (os.environ.get("FACEMATCH_TRUST_PROXY") == "1" and request.headers.get("X-Forwarded-Proto") == "https")


# ---------------------------------------------------------------- audit
def audit(event: str, detail: str = "", user=None):
    try:
        c = db.conn()
        u = user or getattr(g, "user", None)
        c.execute("INSERT INTO audit(ts,user_id,username,event,detail,ip) VALUES(?,?,?,?,?,?)",
                  (time.time(), u["id"] if u else None, u["username"] if u else None, event, detail[:500], client_ip() if request else None))
        c.execute("DELETE FROM audit WHERE id < (SELECT MAX(id) FROM audit) - 20000")
        c.commit()
    except Exception:
        pass


# ---------------------------------------------------------------- users
def _row_user(r):
    if not r:
        return None
    d = dict(r)
    d.pop("pw_hash", None)
    return d


def user_count() -> int:
    return db.conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def clean_username(u: str) -> str:
    u = (u or "").strip()
    if not (2 <= len(u) <= 32) or not all(ch.isalnum() or ch in "._-@" for ch in u):
        raise AuthError("Usernames are 2–32 characters: letters, numbers, . _ - @")
    return u


def create_user(username, display="", role="member", password=None, can_export=True, all_sources=True, sources=()):
    username = clean_username(username)
    if role not in ROLES:
        raise AuthError("Unknown role.")
    c = db.conn()
    if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise AuthError("That username is taken.")
    pw = None
    if password:
        check_new_password(password)
        pw = hash_password(password)
    cur = c.execute("INSERT INTO users(username,display,pw_hash,role,can_export,all_sources,created) VALUES(?,?,?,?,?,?,?)",
                    (username, (display or username).strip()[:60], pw, role, int(can_export), int(all_sources), time.time()))
    set_user_sources(cur.lastrowid, sources)
    c.commit()
    return cur.lastrowid


def set_user_sources(uid, sources):
    c = db.conn()
    c.execute("DELETE FROM user_sources WHERE user_id=?", (uid,))
    ids = {r[0] for r in c.execute("SELECT id FROM sources")}
    for s in set(int(x) for x in (sources or ()) if int(x) in ids):
        c.execute("INSERT INTO user_sources(user_id,source_id) VALUES(?,?)", (uid, s))


def allowed_sources(user):
    """None = every folder; otherwise a (possibly empty) list of source ids."""
    if user["role"] == "admin" or user["all_sources"]:
        return None
    return [r[0] for r in db.conn().execute("SELECT source_id FROM user_sources WHERE user_id=?", (user["id"],))]


def issue_invite(uid, kind="invite", days=7) -> str:
    c = db.conn()
    c.execute("DELETE FROM invites WHERE user_id=?", (uid,))
    tok = secrets.token_urlsafe(24)
    c.execute("INSERT INTO invites(id,user_id,kind,expires) VALUES(?,?,?,?)", (_th(tok), uid, kind, time.time() + days * 86400))
    c.commit()
    return tok


def invite_info(tok):
    c = db.conn()
    r = c.execute("SELECT i.kind, i.expires, u.id, u.username, u.display FROM invites i JOIN users u ON u.id=i.user_id WHERE i.id=?", (_th(tok),)).fetchone()
    if not r or r["expires"] < time.time():
        raise AuthError("This link has expired or was already used. Ask the admin for a new one.", 404)
    return dict(r)


def redeem_invite(tok, password):
    info = invite_info(tok)
    check_new_password(password)
    c = db.conn()
    c.execute("UPDATE users SET pw_hash=?, disabled=0 WHERE id=?", (hash_password(password), info["id"]))
    c.execute("DELETE FROM invites WHERE id=?", (_th(tok),))
    c.execute("DELETE FROM sessions WHERE user_id=?", (info["id"],))
    c.commit()
    return info["id"]


# ---------------------------------------------------------------- sessions
def _throttle_check(ip, uname):
    now = time.time()
    with _lock:
        for k in ((ip, uname.lower()), (ip, "*")):
            f = _fails.get(k)
            if f and f[2] > now:
                raise AuthError(f"Too many attempts. Try again in {int(f[2] - now) + 1} seconds.", 429)


def _throttle_fail(ip, uname):
    now = time.time()
    with _lock:
        for k, limit in (((ip, uname.lower()), 5), ((ip, "*"), 30)):
            f = _fails.setdefault(k, [0, now, 0])
            if now - f[1] > 900:
                f[:] = [0, now, 0]
            f[0] += 1
            if f[0] >= limit:
                f[2] = now + min(900, 30 * 2 ** (f[0] - limit))


def _throttle_ok(ip, uname):
    with _lock:
        _fails.pop((ip, uname.lower()), None)


def login(username, password):
    ip = client_ip()
    username = (username or "").strip()
    _throttle_check(ip, username)
    r = db.conn().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    ok = bool(r and r["pw_hash"] and verify_password(password or "", r["pw_hash"]))
    if not r or not r["pw_hash"]:
        verify_password("x", "scrypt$16384$8$1$" + "00" * 16 + "$" + "00" * 32)   # equalise timing
    if not ok or r["disabled"]:
        _throttle_fail(ip, username)
        audit("login_failed", username)
        raise AuthError("Wrong username or password." if not (r and r["disabled"] and ok) else "This account is disabled.", 401)
    _throttle_ok(ip, username)
    return r, new_session(r["id"])


def new_session(uid) -> str:
    tok = secrets.token_urlsafe(32)
    now = time.time()
    c = db.conn()
    c.execute("INSERT INTO sessions(id,user_id,created,expires,last_seen,ua,ip) VALUES(?,?,?,?,?,?,?)",
              (_th(tok), uid, now, now + SESSION_DAYS * 86400, now, (request.headers.get("User-Agent") or "")[:200], client_ip()))
    c.execute("UPDATE users SET last_login=? WHERE id=?", (now, uid))
    c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    c.commit()
    return tok


def end_session(tok):
    c = db.conn()
    c.execute("DELETE FROM sessions WHERE id=?", (_th(tok),))
    c.commit()


def kill_sessions(uid, keep_tok=None):
    c = db.conn()
    if keep_tok:
        c.execute("DELETE FROM sessions WHERE user_id=? AND id!=?", (uid, _th(keep_tok)))
    else:
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.commit()


def current_user():
    if hasattr(g, "user"):
        return g.user
    g.user = None
    tok = request.cookies.get(COOKIE)
    if tok:
        c = db.conn()
        r = c.execute("SELECT u.*, s.last_seen sl FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires>?", (_th(tok), time.time())).fetchone()
        if r and not r["disabled"]:
            g.user = _row_user(r)
            g.session_token = tok
            now = time.time()
            if now - (r["sl"] or 0) > 300:
                c.execute("UPDATE sessions SET last_seen=?, expires=? WHERE id=?", (now, now + SESSION_DAYS * 86400, _th(tok)))
                c.commit()
    return g.user
