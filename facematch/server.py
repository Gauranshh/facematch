"""FaceMatch web server (Flask, served by waitress).

Accounts + per-folder access control on every endpoint, background indexing, live folder watching,
in-memory face search. Heavy work happens in background processes / numpy, so the website stays responsive."""
import base64
import io
import json
import logging
import logging.handlers
import os
import secrets
import shutil
import string
import threading
import time
import uuid
import zipfile
from collections import OrderedDict, defaultdict, deque
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
from flask import Flask, Response, abort, g, jsonify, request, send_file, send_from_directory

from . import __version__, auth, cluster, db
from .core import FaceMatcher, MODELS, decode_embs, load_image, model_available
from .indexer import Indexer
from .searchindex import Filter, SearchIndex
from .watcher import Watcher

STATIC = Path(__file__).resolve().parent.parent / "static"
START_TS = time.time()
SETUP_CODE = ""
log = logging.getLogger("facematch")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # reference uploads only; the library is never uploaded
app.config["JSON_SORT_KEYS"] = False

index = SearchIndex()
_matchers = {}
_matcher_lock = threading.Lock()
REFS: "OrderedDict[str, dict]" = OrderedDict()
RESULTS: "OrderedDict[str, dict]" = OrderedDict()
JOBS: dict = {}
DISC = dict(state="idle", message="", found=0, finished=0)
MODEL_JOB = dict(state="idle", message="", progress=0)
_stats_cache = {"t": 0, "v": None}
_count_cache: dict = {}
_pub_hits = defaultdict(deque)


def cur_model():
    m = db.get_setting("model", "sface")
    return m if m in MODELS else "sface"


def matcher():
    m = cur_model()
    if m not in _matchers:
        _matchers.clear()
        _matchers[m] = FaceMatcher(m)
    return _matchers[m]


def _lru(d, key, val, cap):
    d[key] = val
    while len(d) > cap:
        d.popitem(last=False)


# ------------------------------------------------------------------ security plumbing
PUBLIC_PREFIXES = ("/static/", "/s/", "/api/auth/", "/api/public/")
PUBLIC_EXACT = {"/", "/healthz", "/manifest.webmanifest", "/sw.js", "/favicon.ico", "/privacy", "/offline"}


def me():
    return auth.current_user()


def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        u = me()
        if not u or u["role"] != "admin":
            return jsonify(error="Only an admin can do that."), 403
        return f(*a, **k)
    return w


def allowed():
    """None = all folders, else list of source ids this user may see."""
    return auth.allowed_sources(me())


def can_see_source(sid):
    a = allowed()
    return a is None or sid in a


def can_export():
    u = me()
    return bool(u and (u["role"] == "admin" or u["can_export"]))


@app.errorhandler(auth.AuthError)
def _auth_err(e):
    return jsonify(error=str(e)), e.code


@app.before_request
def _gate():
    p = request.path
    if request.method not in ("GET", "HEAD", "OPTIONS"):   # CSRF defence in depth (cookie is also SameSite=Lax)
        o = request.headers.get("Origin")
        if o and urlparse(o).netloc != request.host:
            return jsonify(error="Cross-site request blocked."), 403
    if p in PUBLIC_EXACT or p.startswith(PUBLIC_PREFIXES):
        return None
    if not me():
        return jsonify(error="Please sign in.", auth=True), 401
    return None


@app.after_request
def _headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("X-Frame-Options", "DENY")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    r.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if r.mimetype == "text/html":
        r.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self'; "
                                                "style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; "
                                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.path.startswith("/api/") or request.path == "/":
        r.headers["Cache-Control"] = "no-store"
    return r


def _set_cookie(resp, tok):
    resp.set_cookie(auth.COOKIE, tok, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="Lax", secure=auth.is_https(), path="/")
    return resp


# ------------------------------------------------------------------ pages
@app.get("/")
def home():
    return send_from_directory(STATIC, "index.html")


@app.get("/privacy")
def privacy_page():
    return send_from_directory(STATIC, "privacy.html")


@app.get("/offline")
def offline_page():
    return send_from_directory(STATIC, "offline.html")


@app.get("/static/<path:p>")
def static_files(p):
    return send_from_directory(STATIC, p, max_age=300)


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def sw():
    r = send_from_directory(STATIC, "sw.js", mimetype="application/javascript")
    r.headers["Service-Worker-Allowed"] = "/"
    return r


@app.get("/healthz")
def healthz():
    try:
        db.conn().execute("SELECT 1").fetchone()
        return jsonify(ok=True, version=__version__)
    except Exception:
        return jsonify(ok=False), 500


# ------------------------------------------------------------------ auth API
def _is_local_request():
    return request.remote_addr in ("127.0.0.1", "::1") and not request.headers.get("X-Forwarded-For")


def _public_user(u):
    return dict(uploads=_uploads_on(), id=u["id"], username=u["username"], display=u["display"], role=u["role"], can_export=bool(u["can_export"] or u["role"] == "admin"),
                theme=json.loads(u["theme"]) if u.get("theme") else None)


@app.get("/api/auth/state")
def auth_state():
    u = me()
    return jsonify(setup_needed=auth.user_count() == 0, user=_public_user(u) if u else None, version=__version__,
                   setup_code_required=not _is_local_request(), signup_open=_signup_open() and auth.user_count() > 0)


@app.post("/api/auth/setup")
def auth_setup():
    if auth.user_count() > 0:
        return jsonify(error="Setup is already done."), 409
    d = request.get_json(silent=True) or {}
    if not _is_local_request() and not (SETUP_CODE and secrets.compare_digest(str(d.get("code", "")), SETUP_CODE)):
        return jsonify(error="Enter the setup code printed in the server console."), 403
    uid = auth.create_user(d.get("username"), d.get("display"), "admin", d.get("password"))
    c = db.conn()
    c.execute("UPDATE people SET owner_id=? WHERE owner_id IS NULL AND auto=0 AND name!=''", (uid,))   # adopt people saved before accounts existed
    c.execute("UPDATE people SET owner_id=?, auto=0 WHERE owner_id IS NULL AND auto=1 AND name!=''", (uid,))
    c.execute("DELETE FROM people WHERE owner_id IS NULL AND auto=0")
    c.commit()
    tok = auth.new_session(uid)
    g.user = None
    u = db.conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    auth.audit("setup", "first admin created", dict(id=uid, username=u["username"]))
    return _set_cookie(jsonify(ok=True), tok)


@app.post("/api/auth/login")
def auth_login():
    d = request.get_json(silent=True) or {}
    row, tok = auth.login(d.get("username"), d.get("password"))
    auth.audit("login", "", dict(id=row["id"], username=row["username"]))
    return _set_cookie(jsonify(ok=True), tok)


@app.post("/api/auth/logout")
def auth_logout():
    tok = request.cookies.get(auth.COOKIE)
    if tok:
        auth.audit("logout", "", me())
        auth.end_session(tok)
    r = jsonify(ok=True)
    r.delete_cookie(auth.COOKIE, path="/")
    return r


@app.get("/api/auth/invite/<tok>")
def invite_get(tok):
    i = auth.invite_info(tok)
    return jsonify(username=i["username"], display=i["display"], kind=i["kind"])


@app.post("/api/auth/invite/<tok>")
def invite_redeem(tok):
    pw = (request.get_json(silent=True) or {}).get("password", "")
    uid = auth.redeem_invite(tok, pw)
    row = db.conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    session = auth.new_session(uid)
    auth.audit("invite_redeemed", "", dict(id=uid, username=row["username"]))
    return _set_cookie(jsonify(ok=True), session)


@app.get("/api/me")
def api_me():
    return jsonify(_public_user(me()))


@app.patch("/api/me")
def api_me_patch():
    d = request.get_json(silent=True) or {}
    c, u = db.conn(), me()
    if "display" in d:
        c.execute("UPDATE users SET display=? WHERE id=?", ((d["display"] or u["username"]).strip()[:60], u["id"]))
    if "theme" in d and isinstance(d["theme"], dict):
        c.execute("UPDATE users SET theme=? WHERE id=?", (json.dumps({k: str(v)[:20] for k, v in d["theme"].items()}), u["id"]))
    c.commit()
    return jsonify(ok=True)


@app.post("/api/me/password")
def api_me_password():
    d = request.get_json(silent=True) or {}
    u = me()
    row = db.conn().execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()
    if not auth.verify_password(d.get("old", ""), row["pw_hash"] or ""):
        return jsonify(error="Your current password is not right."), 400
    auth.check_new_password(d.get("new", ""))
    c = db.conn()
    c.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(d["new"]), u["id"]))
    c.commit()
    auth.kill_sessions(u["id"], keep_tok=g.session_token)   # sign out every other device
    auth.audit("password_changed", "", u)
    return jsonify(ok=True)


# ------------------------------------------------------------------ admin: users, overview, audit
def _user_row(r, c):
    srcs = [x[0] for x in c.execute("SELECT source_id FROM user_sources WHERE user_id=?", (r["id"],))]
    pend = c.execute("SELECT 1 FROM invites WHERE user_id=? AND expires>?", (r["id"], time.time())).fetchone()
    return dict(id=r["id"], username=r["username"], display=r["display"], role=r["role"], can_export=bool(r["can_export"]),
                all_sources=bool(r["all_sources"]), sources=srcs, disabled=bool(r["disabled"]), last_login=r["last_login"],
                created=r["created"], pending_invite=bool(pend) and not r["pw_hash"], has_password=bool(r["pw_hash"]))


@app.get("/api/admin/users")
@admin_required
def admin_users():
    c = db.conn()
    return jsonify(users=[_user_row(r, c) for r in c.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE")])


@app.post("/api/admin/users")
@admin_required
def admin_user_add():
    d = request.get_json(silent=True) or {}
    uid = auth.create_user(d.get("username"), d.get("display"), d.get("role", "member"), None, bool(d.get("can_export", True)),
                           bool(d.get("all_sources", True)), d.get("sources") or ())
    tok = auth.issue_invite(uid)
    auth.audit("user_created", d.get("username", ""), me())
    return jsonify(ok=True, id=uid, invite=f"/#invite/{tok}")


@app.patch("/api/admin/users/<int:uid>")
@admin_required
def admin_user_patch(uid):
    d = request.get_json(silent=True) or {}
    c = db.conn()
    r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        abort(404)
    admins = c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0]
    demoting = (d.get("role") == "member" and r["role"] == "admin") or (d.get("disabled") and r["role"] == "admin")
    if demoting and admins <= 1:
        return jsonify(error="There must always be at least one active admin."), 400
    if "role" in d and d["role"] in auth.ROLES:
        c.execute("UPDATE users SET role=? WHERE id=?", (d["role"], uid))
    for k, col in (("display", "display"), ("can_export", "can_export"), ("all_sources", "all_sources"), ("disabled", "disabled")):
        if k in d:
            c.execute(f"UPDATE users SET {col}=? WHERE id=?", ((str(d[k]).strip()[:60] if k == "display" else int(bool(d[k]))), uid))
    if "sources" in d:
        auth.set_user_sources(uid, d["sources"])
    c.commit()
    if d.get("disabled"):
        auth.kill_sessions(uid)
    auth.audit("user_updated", r["username"], me())
    return jsonify(ok=True)


@app.post("/api/admin/users/<int:uid>/invite")
@admin_required
def admin_user_invite(uid):
    r = db.conn().execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        abort(404)
    tok = auth.issue_invite(uid, "reset")
    auth.audit("password_reset_link", r["username"], me())
    return jsonify(ok=True, invite=f"/#invite/{tok}")


@app.delete("/api/admin/users/<int:uid>")
@admin_required
def admin_user_del(uid):
    c = db.conn()
    r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        abort(404)
    if uid == me()["id"]:
        return jsonify(error="You can't delete your own account."), 400
    if r["role"] == "admin" and c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
        return jsonify(error="There must always be at least one admin."), 400
    _purge_user(uid)
    auth.audit("user_deleted", r["username"], me())
    return jsonify(ok=True)


def _sample_thumb_bytes():
    c = db.conn()
    n = c.execute("SELECT COUNT(*) FROM photos WHERE tkey IS NOT NULL").fetchone()[0]
    rows = c.execute("SELECT tkey FROM photos WHERE tkey IS NOT NULL ORDER BY RANDOM() LIMIT 150").fetchall()
    sizes = [db.thumb_path(r[0]).stat().st_size for r in rows if db.thumb_path(r[0]).exists()]
    return int(n * (sum(sizes) / len(sizes))) if sizes else 0


@app.get("/api/admin/overview")
@admin_required
def admin_overview():
    c = db.conn()
    st = _stats()
    years = {r[0]: r[1] for r in c.execute(
        "SELECT strftime('%Y', COALESCE(taken,mtime), 'unixepoch', 'localtime') y, COUNT(*) FROM photos WHERE nfaces>0 GROUP BY y ORDER BY y") if r[0]}
    du = shutil.disk_usage(db.DATA_DIR)
    return jsonify(
        version=__version__, uptime=int(time.time() - START_TS), photos=st["photos"], faces=st["faces"], videos=st["videos"],
        with_faces=c.execute("SELECT COUNT(*) FROM photos WHERE nfaces>0").fetchone()[0], by_year=years,
        pool_people=c.execute("SELECT COUNT(*) FROM people WHERE owner_id IS NULL AND auto=1").fetchone()[0],
        named_people=c.execute("SELECT COUNT(*) FROM people WHERE owner_id IS NOT NULL AND name!=''").fetchone()[0],
        users=c.execute("SELECT COUNT(*) FROM users").fetchone()[0], shares=c.execute("SELECT COUNT(*) FROM shares WHERE revoked=0 AND expires>?", (time.time(),)).fetchone()[0],
        db_bytes=db.DB_PATH.stat().st_size, thumb_bytes=_sample_thumb_bytes(), disk_free=du.free, disk_total=du.total,
        model=cur_model(), watch={s["id"]: watcher.mode(s["id"]) for s in st["sources"]}, last_backup=float(db.get_setting("last_backup", 0) or 0))


@app.get("/api/admin/audit")
@admin_required
def admin_audit():
    off, lim = int(request.args.get("offset", 0)), min(200, int(request.args.get("limit", 50)))
    rows = db.conn().execute("SELECT ts,username,event,detail,ip FROM audit ORDER BY id DESC LIMIT ? OFFSET ?", (lim, off)).fetchall()
    return jsonify(events=[dict(r) for r in rows])


# ------------------------------------------------------------------ status / library
def _stats():
    if time.time() - _stats_cache["t"] < 5 and _stats_cache["v"]:
        return _stats_cache["v"]
    c = db.conn()
    per = {r["source_id"]: (r["n"], r["f"], r["e"], r["v"]) for r in c.execute(
        "SELECT source_id, COUNT(*) n, COALESCE(SUM(nfaces),0) f, SUM(status!='ok') e, SUM(kind='video') v FROM photos GROUP BY source_id")}
    srcs = []
    for r in c.execute("SELECT * FROM sources ORDER BY id"):
        n, f, e, v = per.get(r["id"], (0, 0, 0, 0))
        srcs.append(dict(id=r["id"], path=r["path"], photos=n, faces=f, problems=e or 0, videos=v or 0, last_scan=r["last_scan"],
                         online=os.path.isdir(r["path"]), watch=watcher.mode(r["id"]), owner_id=r["owner_id"]))
    yr = c.execute("SELECT MIN(COALESCE(taken,mtime)), MAX(COALESCE(taken,mtime)) FROM photos WHERE COALESCE(taken,mtime)>631152000").fetchone()
    y = lambda t: time.localtime(t).tm_year if t else None  # noqa: E731
    v = dict(sources=srcs, photos=sum(s["photos"] for s in srcs), faces=sum(s["faces"] for s in srcs),
             videos=sum(s["videos"] for s in srcs), year_min=y(yr[0]), year_max=y(yr[1]))
    _stats_cache.update(t=time.time(), v=v)
    return v


def _settings():
    g_ = db.get_setting
    return dict(workers=int(g_("workers", max(1, (os.cpu_count() or 2) - 1))), detail=g_("detail", "balanced"),
                autoscan_hours=float(g_("autoscan_hours", 24)), videos=g_("videos", "0") == "1", exclude=g_("exclude", ""),
                auto_discover=g_("auto_discover", "1") == "1", backup=g_("backup", "1") == "1", watch=g_("watch", "1") == "1",
                sharing=g_("sharing", "1") == "1", signup=_signup_open(), uploads=_uploads_on(), quota_mb=int(_quota_bytes() / 1048576),
                last_backup=float(g_("last_backup", 0) or 0), model=cur_model())


@app.get("/api/status")
def status():
    m, _u, a = cur_model(), me(), allowed()
    L, admin = _stats(), me()["role"] == "admin"
    srcs = [s for s in L["sources"] if a is None or s["id"] in a]
    if admin:
        lib = dict(L, sources=srcs)
        idx = indexer.status()
    else:
        lib = dict(sources=[dict(id=s["id"], label="My photos" if s.get("owner_id") else (os.path.basename(s["path"].rstrip("/\\")) or s["path"]), photos=s["photos"], faces=s["faces"], videos=s["videos"]) for s in srcs],
                   photos=sum(s["photos"] for s in srcs), faces=sum(s["faces"] for s in srcs), videos=sum(s["videos"] for s in srcs),
                   year_min=L["year_min"], year_max=L["year_max"])
        i = indexer.status()
        idx = dict(running=i["running"], state=i["state"], done=i["done"], total=i["total"], paused=i["paused"])
    out = dict(index=idx, library=lib, default_threshold=MODELS[m]["thr"], version=__version__, sharing=db.get_setting("sharing", "1") == "1", uploads=_uploads_on(),
               models=[dict(id=k, label=v["label"], available=model_available(k)) for k, v in MODELS.items()], discover=DISC, model_job=MODEL_JOB,
               settings=_settings() if admin else dict(model=m), cpus=os.cpu_count())
    return jsonify(out)


@app.post("/api/sources")
@admin_required
def add_source():
    path = ((request.get_json(silent=True) or {}).get("path") or "").strip().strip('"')
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return jsonify(error=f"Folder not found: {path}"), 400
    c = db.conn()
    try:
        c.execute("INSERT INTO sources(path, added_at) VALUES(?,?)", (path, time.time()))
        c.commit()
    except Exception:
        return jsonify(error="That folder is already in your library."), 400
    sid = c.execute("SELECT id FROM sources WHERE path=?", (path,)).fetchone()[0]
    _stats_cache["t"] = 0
    if db.get_setting("watch", "1") == "1":
        watcher.watch(sid, path)
    indexer.start([sid])
    auth.audit("source_added", path, me())
    return jsonify(ok=True, id=sid)


@app.delete("/api/sources/<int:sid>")
@admin_required
def del_source(sid):
    if indexer.status()["running"]:
        return jsonify(error="Stop indexing first, then remove the folder."), 409
    watcher.unwatch(sid)
    auth.audit("source_removed", str(sid), me())

    def work():
        c = db.conn()
        tkeys = [r[0] for r in c.execute("SELECT tkey FROM photos WHERE source_id=? AND tkey IS NOT NULL", (sid,))]
        c.execute("DELETE FROM faces WHERE photo_id IN (SELECT id FROM photos WHERE source_id=?)", (sid,))
        c.execute("DELETE FROM photos WHERE source_id=?", (sid,))
        c.execute("DELETE FROM user_sources WHERE source_id=?", (sid,))
        c.execute("DELETE FROM sources WHERE id=?", (sid,))
        c.commit()
        index.invalidate()
        _stats_cache["t"] = 0
        for t in tkeys:
            try:
                db.thumb_path(t).unlink()
            except OSError:
                pass
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/index/<action>")
@admin_required
def index_ctl(action):
    d = request.get_json(silent=True) or {}
    if action == "start":
        indexer.start(d.get("source_ids"))
    elif action == "pause":
        indexer.pause()
    elif action == "resume":
        indexer.resume()
    elif action == "stop":
        indexer.stop()
    else:
        abort(404)
    return jsonify(ok=True)


@app.post("/api/settings")
@admin_required
def settings():
    d = request.get_json(silent=True) or {}
    if "workers" in d:
        db.set_setting("workers", max(1, min(int(d["workers"]), (os.cpu_count() or 2))))
    if d.get("detail") in ("fast", "balanced", "thorough"):
        db.set_setting("detail", d["detail"])
    if "autoscan_hours" in d:
        db.set_setting("autoscan_hours", max(0.0, float(d["autoscan_hours"])))
    if "quota_mb" in d:
        db.set_setting("quota_mb", max(10, min(int(float(d["quota_mb"])), 10_000_000)))
    for k in ("videos", "auto_discover", "backup", "watch", "sharing", "signup", "uploads"):
        if k in d:
            db.set_setting(k, "1" if d[k] else "0")
    if "watch" in d:
        watcher.set_enabled(bool(d["watch"]))
    if "exclude" in d:
        db.set_setting("exclude", str(d["exclude"])[:4000])
    auth.audit("settings_changed", ",".join(d.keys()), me())
    return jsonify(ok=True)


@app.post("/api/backup")
@admin_required
def backup_now():
    try:
        return jsonify(ok=True, file=db.backup())
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 500


@app.get("/api/fs")
@admin_required
def fs():
    p = request.args.get("path", "")
    if not p:
        if os.name == "nt":
            roots = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
        else:
            roots = [str(Path.home()), "/"]
            for base in ("/media", "/mnt", "/Volumes", f"/run/media/{os.environ.get('USER', '')}"):
                if os.path.isdir(base):
                    roots += [os.path.join(base, n) for n in sorted(os.listdir(base)) if os.path.isdir(os.path.join(base, n))]
            if os.path.isdir("/photos"):
                roots.insert(0, "/photos")
        return jsonify(path="", parent=None, dirs=[dict(name=r, path=r) for r in roots])
    p = os.path.abspath(p)
    try:
        names = sorted((n for n in os.listdir(p) if not n.startswith(".") and os.path.isdir(os.path.join(p, n))), key=str.lower)
    except OSError as e:
        return jsonify(error=str(e)), 400
    par = os.path.dirname(p)
    return jsonify(path=p, parent=par if par != p else "", dirs=[dict(name=n, path=os.path.join(p, n)) for n in names])


# ------------------------------------------------------------------ recognition model switching
def _model_switch(target):
    from .models_dl import download_accurate
    job = MODEL_JOB
    try:
        if not model_available(target):
            job.update(state="downloading", message="Downloading the accurate model (175 MB)…", progress=0)
            download_accurate(lambda a, t: job.update(progress=round(100 * a / t) if t else 0))
        job.update(state="switching", message="Stopping indexing…", progress=100)
        indexer.stop()
        indexer.wait(120)
        c = db.conn()
        old_ids = [r[0] for r in c.execute("SELECT id FROM people WHERE owner_id IS NOT NULL")]
        crops = {}
        for pid in old_ids:                      # keep the people the user cares about: re-derive their embedding from their face crop
            b = _person_crop(pid)
            if b:
                crops[pid] = b
        job.update(message="Resetting the face index for the new model…")
        db.set_setting("model", target)
        c.execute("DELETE FROM faces")
        c.execute("UPDATE photos SET size=-1, nfaces=0")
        c.execute("DELETE FROM person_samples")
        c.execute("DELETE FROM people WHERE owner_id IS NULL AND auto=1")
        c.commit()
        index.invalidate()
        _matchers.clear()
        lost = 0
        m = matcher()
        for pid in old_ids:
            ok = False
            if pid in crops:
                img = cv2.imdecode(np.frombuffer(crops[pid], np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    big = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                    fs = m.embed_faces(big, 640)
                    if fs:
                        f = max(fs, key=lambda x: x.box[2] * x.box[3])
                        c.execute("UPDATE people SET emb=? WHERE id=?", (f.emb.astype(np.float32).tobytes(), pid))
                        ok = True
            if not ok:
                c.execute("DELETE FROM people WHERE id=?", (pid,))
                lost += 1
        c.commit()
        _stats_cache["t"] = 0
        job.update(state="done", message=f"Switched to {MODELS[target]['label']}. Re-analysing your library now…" + (f" ({lost} saved people could not be carried over — re-add them.)" if lost else ""))
        indexer.start()
    except Exception as e:  # noqa: BLE001
        job.update(state="error", message=f"Could not switch: {e}")


@app.post("/api/model")
@admin_required
def set_model():
    target = (request.get_json(silent=True) or {}).get("model")
    if target not in MODELS or target == cur_model():
        return jsonify(error="Nothing to change."), 400
    if MODEL_JOB["state"] in ("downloading", "switching"):
        return jsonify(error="A model change is already running."), 409
    MODEL_JOB.update(state="starting", message="Starting…", progress=0)
    threading.Thread(target=_model_switch, args=(target,), daemon=True).start()
    return jsonify(ok=True)



# ------------------------------------------------------------------ reference photos
def _crop_jpeg(img, box, size=160, margin=0.35):
    x, y, w, h = box
    m = int(max(w, h) * margin)
    x0, y0 = max(0, x - m), max(0, y - m)
    c = img[y0:y + h + m, x0:x + w + m]
    if c.size == 0:
        return None
    s = size / max(c.shape[:2])
    c = cv2.resize(c, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()




@app.post("/api/reference")
def reference():
    photos, notes = [], []
    for f in request.files.getlist("face")[:8]:
        raw = f.read()
        try:
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
            img = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
        except Exception:
            notes.append(f"{f.filename}: not a readable image")
            continue
        try:
            with _matcher_lock:
                faces = matcher().embed_faces(img, 1600)
        except FileNotFoundError as e:
            return jsonify(error=str(e)), 400
        if not faces:
            notes.append(f"{f.filename}: no face found")
            continue
        faces.sort(key=lambda t: -t.box[2] * t.box[3])
        photos.append(dict(name=f.filename, faces=[dict(emb=x.emb, crop=_crop_jpeg(img, x.box)) for x in faces]))
    if not photos:
        return jsonify(error="No face found in the photo(s). Use a clear, front-facing picture. " + " ".join(notes)), 400
    rid = uuid.uuid4().hex[:12]
    _lru(REFS, rid, dict(photos=photos, uid=me()["id"]), 30)
    return jsonify(ref=rid, notes=notes, photos=[dict(name=p["name"], crops=[base64.b64encode(f["crop"]).decode() for f in p["faces"]]) for p in photos])


def _ref_embedding(rid, picks):
    r = REFS.get(rid)
    if not r or r["uid"] != me()["id"]:
        raise KeyError("Reference expired — please upload the photo again.")
    embs = [p["faces"][min(int((picks or {}).get(str(i), 0)), len(p["faces"]) - 1)]["emb"] for i, p in enumerate(r["photos"])]
    e = np.mean(embs, axis=0)
    return e / np.linalg.norm(e)


def _face_embs(ids):
    """Embeddings of faces the user marked ✓ / ✗ — only faces from folders they are allowed to see."""
    ids = [int(i) for i in (ids or [])][:400]
    if not ids:
        return []
    a = allowed()
    dim = MODELS[cur_model()]["dim"]
    rows = db.conn().execute(f"SELECT f.emb, p.source_id FROM faces f JOIN photos p ON p.id=f.photo_id WHERE f.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return [decode_embs(r["emb"], dim)[0].astype(np.float32) for r in rows if a is None or r["source_id"] in a]


# ------------------------------------------------------------------ people (private per user) + shared discovered pool
def _pool_visible(row, a):
    if a is None:
        return True
    try:
        keys = [int(k) for k in json.loads(row["srcs"] or "{}")]
        return bool(keys) and all(k in a for k in keys)   # a group that also contains someone else's photos is never shown
    except Exception:
        return False


def _pool_count(row, a):
    try:
        m = json.loads(row["srcs"] or "{}")
    except Exception:
        return row["nphotos"] or 0
    return row["nphotos"] if a is None else sum(v for k, v in m.items() if int(k) in a)


def _person_row(pid):
    """A person this user owns, or a discovered (pool) person they are allowed to see. 404 otherwise."""
    r = db.conn().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    u = me()
    if not r:
        abort(404)
    if r["owner_id"] == u["id"]:
        return r
    if r["owner_id"] is None and r["auto"] == 1 and _pool_visible(r, allowed()):
        return r
    abort(404)


def _owned_person(pid):
    r = _person_row(pid)
    if r["owner_id"] != me()["id"]:
        abort(404)
    return r


def person_count(row):
    """Number of photos (within the user's folders) that currently match this person. Cached per index version."""
    a = allowed()
    key = (row["id"], index.version, tuple(sorted(a)) if a is not None else None, row["emb"][:8])
    if key in _count_cache:
        return _count_cache[key]
    m = cur_model()
    e = np.frombuffer(row["emb"], np.float32)
    if len(e) != MODELS[m]["dim"]:
        return 0
    f = Filter(sources=(a or [-1]) if a is not None else None)
    r = index.search([(e, None)], MODELS[m]["member"], f)
    n = 0 if r is None else int(len(r["photo"]))
    if len(_count_cache) > 4000:
        _count_cache.clear()
    _count_cache[key] = n
    return n


def _person_vectors(pid):
    r = _person_row(pid)
    dim = MODELS[cur_model()]["dim"]
    e = np.frombuffer(r["emb"], np.float32)
    if len(e) != dim:
        raise KeyError("This person was saved with a different recognition model — please re-add them.")
    pos, neg = [], []
    if r["owner_id"] is not None:
        for s in db.conn().execute("SELECT label, emb FROM person_samples WHERE person_id=?", (pid,)):
            (pos if s["label"] else neg).append(np.frombuffer(s["emb"], np.float32))
    return e, pos, neg


def _build_queries(d):
    """-> list of (embedding, negatives ndarray or None) with 'this is them / not them' feedback folded in."""
    items = d.get("queries")
    if not items:
        items = [dict(person=d["person"]) if d.get("person") else dict(ref=d.get("ref"), picks=d.get("picks"))]
    s_pos, s_neg = (_face_embs(d.get("pos")), _face_embs(d.get("neg"))) if len(items) == 1 else ([], [])
    out = []
    for it in items[:4]:
        if it.get("person"):
            e, pos, neg = _person_vectors(int(it["person"]))
        else:
            e, pos, neg = _ref_embedding(it.get("ref"), it.get("picks")), [], []
        pos, neg = pos + s_pos, neg + s_neg
        if pos:
            e = e + 0.7 * np.mean(pos, axis=0)
            e = e / np.linalg.norm(e)
        out.append((e, np.array(neg, np.float32) if neg else None))
    return out


def _pcard(r, count):
    return dict(id=r["id"], name=r["name"], nphotos=count, hidden=bool(r["hidden"]), pool=r["owner_id"] is None,
                fb=(db.conn().execute("SELECT COUNT(*) FROM person_samples WHERE person_id=?", (r["id"],)).fetchone()[0] if r["owner_id"] is not None else 0))


def _discovered(u, a):
    """Pool groups visible to this user, minus the ones they already named or hid (matched by face similarity)."""
    c = db.conn()
    dim = MODELS[cur_model()]["dim"]
    pool = [r for r in c.execute("SELECT id,emb,srcs,nphotos,name,hidden,owner_id,auto FROM people WHERE owner_id IS NULL AND auto=1") if _pool_visible(r, a)]
    mine = [np.frombuffer(r["emb"], np.float32) for r in c.execute("SELECT emb FROM people WHERE owner_id=? AND (name!='' OR hidden=1)", (u["id"],))]
    mine = [e for e in mine if len(e) == dim]
    if pool and mine:
        P = np.array([np.frombuffer(r["emb"], np.float32) for r in pool])
        keep = (P @ np.array(mine).T).max(1) < 0.6
        pool = [r for r, k in zip(pool, keep) if k]
    out = [(r, _pool_count(r, a)) for r in pool]
    out = [x for x in out if x[1] > 0]
    out.sort(key=lambda x: -x[1])
    return out


@app.get("/api/people")
def people_list():
    u, a = me(), allowed()
    show = request.args.get("show", "named")
    off, lim = int(request.args.get("offset", 0)), min(300, int(request.args.get("limit", 60)))
    c = db.conn()
    named_n = c.execute("SELECT COUNT(*) FROM people WHERE owner_id=? AND name!='' AND hidden=0", (u["id"],)).fetchone()[0]
    hidden_n = c.execute("SELECT COUNT(*) FROM people WHERE owner_id=? AND hidden=1", (u["id"],)).fetchone()[0]
    disc = _discovered(u, a)
    if show == "discovered":
        page_ = disc[off:off + lim]
        people, total = [_pcard(r, n) for r, n in page_], len(disc)
    else:
        where = "name!='' AND hidden=0" if show == "named" else "hidden=1"
        total = named_n if show == "named" else hidden_n
        rows = c.execute(f"SELECT * FROM people WHERE owner_id=? AND {where} ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?", (u["id"], lim, off)).fetchall()
        people = [_pcard(r, person_count(r)) for r in rows]
    return jsonify(people=people, total=total, counts=dict(named=named_n, discovered=len(disc), hidden=hidden_n))


@app.post("/api/people")
def people_add():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()[:60]
    if not name:
        return jsonify(error="Please give this person a name."), 400
    try:
        emb = _ref_embedding(d.get("ref"), d.get("picks"))
    except KeyError as e:
        return jsonify(error=str(e.args[0])), 400
    r = REFS[d["ref"]]["photos"][0]["faces"][int((d.get("picks") or {}).get("0", 0))]
    c = db.conn()
    cur = c.execute("INSERT INTO people(name, emb, crop, created, auto, owner_id) VALUES(?,?,?,?,0,?)", (name, emb.astype(np.float32).tobytes(), r["crop"], time.time(), me()["id"]))
    _save_samples(cur.lastrowid, d.get("pos"), d.get("neg"))
    c.commit()
    return jsonify(ok=True, id=cur.lastrowid)


@app.post("/api/people/claim")
def people_claim():
    """Name (or hide) a discovered person: creates the user's own private copy."""
    d = request.get_json(silent=True) or {}
    pool = _person_row(int(d.get("pool_id", 0)))
    if pool["owner_id"] is not None:
        return jsonify(error="Already yours."), 400
    name = (d.get("name") or "").strip()[:60]
    hide = bool(d.get("hidden"))
    if not name and not hide:
        return jsonify(error="Please give this person a name."), 400
    crop = _person_crop(pool["id"])
    c = db.conn()
    cur = c.execute("INSERT INTO people(name,emb,crop,created,auto,owner_id,from_id,hidden,n,nphotos,rep_path,rep_box,rep_t) VALUES(?,?,?,?,0,?,?,?,?,?,?,?,?)",
                    (name, pool["emb"], crop, time.time(), me()["id"], pool["id"], int(hide and not name), pool["n"], pool["nphotos"],
                     pool["rep_path"], pool["rep_box"], pool["rep_t"]))
    c.commit()
    return jsonify(ok=True, id=cur.lastrowid)


def _save_samples(pid, pos, neg):
    c = db.conn()
    for label, ids in ((1, pos), (0, neg)):
        for e in _face_embs(ids):
            c.execute("INSERT INTO person_samples(person_id,label,emb) VALUES(?,?,?)", (pid, label, e.tobytes()))
    for label, cap in ((1, 60), (0, 200)):   # keep the newest few
        c.execute("DELETE FROM person_samples WHERE person_id=? AND label=? AND id NOT IN "
                  "(SELECT id FROM person_samples WHERE person_id=? AND label=? ORDER BY id DESC LIMIT ?)", (pid, label, pid, label, cap))


@app.post("/api/people/<int:pid>/feedback")
def people_feedback(pid):
    _owned_person(pid)
    d = request.get_json(silent=True) or {}
    _save_samples(pid, d.get("pos"), d.get("neg"))
    db.conn().commit()
    _count_cache.clear()
    return jsonify(ok=True)


@app.delete("/api/people/<int:pid>/feedback")
def people_feedback_reset(pid):
    _owned_person(pid)
    c = db.conn()
    c.execute("DELETE FROM person_samples WHERE person_id=?", (pid,))
    c.commit()
    return jsonify(ok=True)


@app.patch("/api/people/<int:pid>")
def people_patch(pid):
    _owned_person(pid)
    d = request.get_json(silent=True) or {}
    c = db.conn()
    if "name" in d:
        c.execute("UPDATE people SET name=? WHERE id=?", ((d["name"] or "").strip()[:60], pid))
    if "hidden" in d:
        c.execute("UPDATE people SET hidden=? WHERE id=?", (1 if d["hidden"] else 0, pid))
    c.commit()
    return jsonify(ok=True)


@app.delete("/api/people/<int:pid>")
def people_del(pid):
    _owned_person(pid)
    c = db.conn()
    c.execute("DELETE FROM person_samples WHERE person_id=?", (pid,))
    c.execute("DELETE FROM people WHERE id=?", (pid,))
    c.commit()
    return jsonify(ok=True)


@app.post("/api/people/merge")
def people_merge():
    ids = [int(i) for i in (request.get_json(silent=True) or {}).get("ids", [])]
    if len(ids) != 2:
        return jsonify(error="Select exactly two people to merge."), 400
    rows = [_owned_person(i) for i in ids]
    rows.sort(key=lambda r: (r["name"] == "",))       # prefer the named one as the survivor
    a, b = rows
    c = db.conn()
    ea, eb = np.frombuffer(a["emb"], np.float32), np.frombuffer(b["emb"], np.float32)
    e = (max(a["n"], 1) * ea + max(b["n"], 1) * eb)
    e = (e / np.linalg.norm(e)).astype(np.float32)
    c.execute("UPDATE people SET emb=?, n=n+?, nphotos=nphotos+?, hidden=0 WHERE id=?", (e.tobytes(), b["n"], b["nphotos"], a["id"]))
    c.execute("UPDATE person_samples SET person_id=? WHERE person_id=?", (a["id"], b["id"]))
    c.execute("DELETE FROM people WHERE id=?", (b["id"],))
    c.commit()
    return jsonify(ok=True, id=a["id"])


def _person_crop(pid):
    """JPEG bytes of the person's face; generated on first use from the representative photo and cached."""
    c = db.conn()
    r = c.execute("SELECT crop, rep_path, rep_box, rep_t FROM people WHERE id=?", (pid,)).fetchone()
    if not r:
        return None
    if r["crop"]:
        return r["crop"]
    if not r["rep_path"] or not r["rep_box"]:
        return None
    box = json.loads(r["rep_box"])
    jpg = None
    try:
        if os.path.exists(r["rep_path"]):
            if r["rep_t"] is not None:
                cap = cv2.VideoCapture(r["rep_path"])
                cap.set(cv2.CAP_PROP_POS_MSEC, float(r["rep_t"]) * 1000)
                ok, img = cap.read()
                cap.release()
                img = img if ok else None
            else:
                img = load_image(r["rep_path"])
            if img is not None:
                jpg = _crop_jpeg(img, box)
        if jpg is None:   # original not reachable (drive unplugged?): fall back to the cached thumbnail
            p = c.execute("SELECT tkey, w FROM photos WHERE path=?", (r["rep_path"],)).fetchone()
            if p and p["tkey"] and db.thumb_path(p["tkey"]).exists():
                th = cv2.imread(str(db.thumb_path(p["tkey"])))
                s = th.shape[1] / max(p["w"], 1)
                jpg = _crop_jpeg(th, [int(v * s) for v in box], size=120)
    except Exception:
        jpg = None
    if jpg:
        c.execute("UPDATE people SET crop=? WHERE id=?", (jpg, pid))
        c.commit()
    return jpg




@app.get("/api/people/<int:pid>/crop")
def people_crop(pid):
    _person_row(pid)
    b = _person_crop(pid)
    if not b:
        abort(404)
    return Response(b, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


def _discover_thread(strict):
    DISC.update(state="running", message="Starting…")
    try:
        n = cluster.discover(index, lambda m: DISC.update(message=m), strict=strict)
        DISC.update(state="idle", message=f"Found {n} groups of faces" if n else "No new people found", found=n, finished=time.time())
    except Exception as e:  # noqa: BLE001
        log.exception("people discovery failed")
        DISC.update(state="idle", message=f"People scan failed: {e}")


def start_discover(strict=0.0):
    if DISC["state"] == "running":
        return False
    threading.Thread(target=_discover_thread, args=(strict,), daemon=True).start()
    return True


@app.post("/api/people/discover")
@admin_required
def people_discover():
    if indexer.status()["running"]:
        return jsonify(error="Wait until indexing has finished (or pause it), then scan for people."), 409
    return jsonify(ok=True, started=start_discover(float((request.get_json(silent=True) or {}).get("strict", 0))))


# ------------------------------------------------------------------ search
def _filter(d):
    a, req = allowed(), d.get("sources") or None
    if a is None:
        srcs = req
    else:
        srcs = [s for s in (req or a) if s in a] or [-1]     # [-1] matches nothing
    f = Filter(sources=srcs, date_from=d.get("date_from"), date_to=d.get("date_to"),
               min_faces=d.get("min_faces"), max_faces=d.get("max_faces"), videos=d.get("videos", True) is not False)
    if d.get("hide_low_quality"):
        f.min_quality = 0.35
    return f


def _my_results(tok):
    r = RESULTS.get(tok)
    if not r or r.get("uid") != me()["id"]:
        return None
    return r


@app.post("/api/search")
def search():
    d = request.get_json(silent=True) or {}
    try:
        qs = _build_queries(d)
    except KeyError as e:
        return jsonify(error=str(e.args[0])), 400
    m = cur_model()
    thr = float(d.get("threshold", MODELS[m]["thr"]))
    t0 = time.time()
    r = index.search(qs, thr, _filter(d), pen=MODELS[m]["pen"])
    if r is None:
        r = {k: np.empty(0) for k in ("photo", "s", "face", "taken", "q", "t")}
    sort = d.get("sort", "best")
    if sort in ("newest", "oldest", "quality") and len(r["photo"]):
        key = {"newest": -r["taken"], "oldest": r["taken"], "quality": -r["q"]}[sort]
        o = np.argsort(key, kind="stable")
        r = {k: v[o] for k, v in r.items()}
    tok = uuid.uuid4().hex[:12]
    r["uid"] = me()["id"]
    _lru(RESULTS, tok, r, 24)
    a = allowed()
    scope = _stats()["photos"] if a is None else sum(s["photos"] for s in _stats()["sources"] if s["id"] in a)
    return jsonify(token=tok, count=int(len(r["photo"])), ms=round((time.time() - t0) * 1000), photos_indexed=scope,
                   indexing=indexer.status()["running"], together=len(qs) > 1)


@app.get("/api/search/<tok>/page")
def page(tok):
    r = _my_results(tok)
    if not r:
        return jsonify(error="expired"), 410
    off = max(0, int(request.args.get("offset", 0)))
    lim = min(200, int(request.args.get("limit", 60)))
    sl = slice(off, off + lim)
    P, S, F, T, Q, V = (r[k][sl] for k in ("photo", "s", "face", "taken", "q", "t"))
    if not len(P):
        return jsonify(items=[])
    c = db.conn()
    admin = me()["role"] == "admin"
    pid = [int(x) for x in P]
    rows = {x["id"]: x for x in c.execute(f"SELECT id,path,w,h,source_id FROM photos WHERE id IN ({','.join('?' * len(pid))})", pid)}
    fid = [int(x) for x in F]
    boxes = {x["id"]: (x["x"], x["y"], x["w"], x["h"]) for x in c.execute(f"SELECT id,x,y,w,h FROM faces WHERE id IN ({','.join('?' * len(fid))})", fid)}
    items = []
    for p, s, f, t, q, v in zip(pid, S, fid, T, Q, V):
        row = rows.get(p)
        if not row:
            continue
        items.append(dict(id=p, f=f, name=os.path.basename(row["path"]), dir=os.path.dirname(row["path"]) if admin else "", w=row["w"], h=row["h"],
                          taken=int(t), score=round(float(s), 3), q=round(float(q), 2), box=boxes.get(f), vid=bool(v >= 0), t=float(v) if v >= 0 else None))
    return jsonify(items=items)


@app.get("/thumb/<int:pid>")
def thumb(pid):
    r = db.conn().execute("SELECT tkey, source_id FROM photos WHERE id=?", (pid,)).fetchone()
    if not r or not r["tkey"] or not can_see_source(r["source_id"]):
        abort(404)
    p = db.thumb_path(r["tkey"])
    if not p.exists():
        abort(404)
    resp = send_file(p, mimetype="image/jpeg", max_age=86400)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


def _send_original(r):
    p = r["path"]
    if not os.path.exists(p):
        return Response("The original file is not available (is the drive connected?)", 404)
    ext = os.path.splitext(p)[1].lower()
    if r["kind"] == "video" or ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        resp = send_file(p, conditional=True, max_age=3600)
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp
    img = load_image(p)
    if img is None:
        abort(404)
    return send_file(io.BytesIO(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()), mimetype="image/jpeg")


@app.get("/photo/<int:pid>")
def photo(pid):
    r = db.conn().execute("SELECT path, kind, source_id FROM photos WHERE id=?", (pid,)).fetchone()
    if not r or not can_see_source(r["source_id"]):
        abort(404)
    return _send_original(r)


# ------------------------------------------------------------------ export (copy to folder / zip)
def _selected_ids(tok, d):
    r = _my_results(tok)
    if not r:
        abort(410)
    P = r["photo"]
    if d.get("all"):
        ids = P[~np.isin(P, np.array(d.get("exclude") or [], np.int64))]
    else:
        ids = P[np.isin(P, np.array(d.get("ids") or [], np.int64))]
    return [int(x) for x in ids]


def _selected(tok, d):
    c = db.conn()
    out, ids = [], _selected_ids(tok, d)
    for i in range(0, len(ids), 900):
        ch = ids[i:i + 900]
        out += [dict(path=x["path"], taken=x["t"], root=x["root"]) for x in c.execute(
            f"SELECT p.path, COALESCE(p.taken,p.mtime) t, s.path root FROM photos p JOIN sources s ON s.id=p.source_id "
            f"WHERE p.id IN ({','.join('?' * len(ch))})", ch)]
    return out


def _rel_name(f, layout, seen):
    p = Path(f["path"])
    if layout == "year":
        rel = Path(str(time.localtime(f["taken"] or 0).tm_year)) / p.name
    elif layout == "structure":
        try:
            rel = p.relative_to(f["root"])
        except ValueError:
            rel = Path(p.name)
    else:
        rel = Path(p.name)
    key = str(rel)
    seen[key] = seen.get(key, 0) + 1
    if seen[key] > 1:
        rel = rel.with_name(f"{rel.stem}_{seen[key]}{rel.suffix}")
    return rel


def _csv(s):
    return [int(x) for x in s.split(",") if x.strip()]


@app.post("/api/search/<tok>/export")
def export(tok):
    if not can_export():
        return jsonify(error="Your account can view photos but not export them."), 403
    if me()["role"] != "admin":
        return jsonify(error="Saving to a folder on the server is for admins. Use “Download .zip” instead."), 403
    d = request.get_json(silent=True) or {}
    out = (d.get("out") or "").strip().strip('"')
    if not out:
        return jsonify(error="Enter a destination folder."), 400
    files = _selected(tok, d)
    if not files:
        return jsonify(error="No photos selected."), 400
    try:
        os.makedirs(out, exist_ok=True)
    except OSError as e:
        return jsonify(error=str(e)), 400
    layout = d.get("layout", "flat")
    jid = uuid.uuid4().hex[:10]
    job = JOBS[jid] = dict(total=len(files), done=0, missing=0, state="running", out=out, uid=me()["id"])
    auth.audit("export_folder", f"{len(files)} files -> {out}", me())

    def work():
        seen = {}
        for f in files:
            try:
                dst = Path(out) / _rel_name(f, layout, seen)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f["path"], dst)
            except OSError:
                job["missing"] += 1
            job["done"] += 1
        job["state"] = "done"
    threading.Thread(target=work, daemon=True).start()
    return jsonify(job=jid)


@app.get("/api/jobs/<jid>")
def job_status(jid):
    j = JOBS.get(jid)
    if not j or j.get("uid") != me()["id"]:
        return jsonify({"state": "unknown"})
    return jsonify({k: v for k, v in j.items() if k != "uid"})


class _Sink:
    def __init__(self):
        self.buf, self.pos = [], 0

    def write(self, b):
        self.buf.append(bytes(b)); self.pos += len(b); return len(b)

    def flush(self):
        pass

    def tell(self):
        return self.pos

    def take(self):
        b = b"".join(self.buf); self.buf = []; return b


def _zip_response(files, layout, name="matching_photos.zip"):
    def gen():
        sink = _Sink()
        z = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
        seen = {}
        for it in files:
            if not os.path.exists(it["path"]):
                continue
            n = str(_rel_name(it, layout, seen)).replace("\\", "/")
            zi = zipfile.ZipInfo(n, time.localtime(max(it["taken"] or 0, 315532800))[:6])
            zi.compress_type = zipfile.ZIP_STORED
            try:
                with open(it["path"], "rb") as fh, z.open(zi, "w", force_zip64=True) as dst:
                    while True:
                        b = fh.read(1 << 20)
                        if not b:
                            break
                        dst.write(b)
                        yield sink.take()
            except OSError:
                continue
            yield sink.take()
        z.close()
        yield sink.take()
    return Response(gen(), mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/search/<tok>/zip")
def zip_dl(tok):
    if not can_export():
        return jsonify(error="Your account can view photos but not export them."), 403
    f = request.form
    d = dict(all=f.get("all") == "1", exclude=_csv(f.get("exclude", "")), ids=_csv(f.get("ids", "")))
    files = _selected(tok, d)
    auth.audit("export_zip", f"{len(files)} files", me())
    return _zip_response(files, f.get("layout", "flat"))


# ------------------------------------------------------------------ share links (public, read-only, expiring)
def _rate_ok(limit=240, per=60):
    ip, now = auth.client_ip(), time.time()
    q = _pub_hits[ip]
    while q and now - q[0] > per:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    if len(_pub_hits) > 5000:
        _pub_hits.clear()
    return True


@app.post("/api/search/<tok>/share")
def share_create(tok):
    if db.get_setting("sharing", "1") != "1":
        return jsonify(error="Share links are turned off by the admin."), 403
    if not can_export():
        return jsonify(error="Your account can't create share links."), 403
    d = request.get_json(silent=True) or {}
    ids = _selected_ids(tok, d)[:5000]
    if not ids:
        return jsonify(error="Select some photos first."), 400
    days = max(1, min(90, int(d.get("days", 7))))
    token = secrets.token_urlsafe(14)
    c = db.conn()
    c.execute("INSERT INTO shares(token,user_id,title,photo_ids,created,expires,allow_download) VALUES(?,?,?,?,?,?,?)",
              (token, me()["id"], (d.get("title") or "Shared photos").strip()[:80], json.dumps(ids), time.time(), time.time() + days * 86400, int(bool(d.get("allow_download", True)))))
    c.commit()
    auth.audit("share_created", f"{len(ids)} photos, {days} days", me())
    return jsonify(ok=True, token=token, url=f"/s/{token}", count=len(ids))


@app.get("/api/shares")
def shares_list():
    u, c = me(), db.conn()
    q = "SELECT s.*, u.username FROM shares s JOIN users u ON u.id=s.user_id " + ("" if u["role"] == "admin" else "WHERE s.user_id=? ") + "ORDER BY s.created DESC LIMIT 200"
    rows = c.execute(q, () if u["role"] == "admin" else (u["id"],)).fetchall()
    return jsonify(shares=[dict(token=r["token"], title=r["title"], count=len(json.loads(r["photo_ids"])), created=r["created"], expires=r["expires"],
                                views=r["views"], revoked=bool(r["revoked"]), by=r["username"], allow_download=bool(r["allow_download"])) for r in rows])


@app.delete("/api/shares/<token>")
def share_revoke(token):
    u, c = me(), db.conn()
    r = c.execute("SELECT user_id FROM shares WHERE token=?", (token,)).fetchone()
    if not r or (r["user_id"] != u["id"] and u["role"] != "admin"):
        abort(404)
    c.execute("UPDATE shares SET revoked=1 WHERE token=?", (token,))
    c.commit()
    auth.audit("share_revoked", token[:6], u)
    return jsonify(ok=True)


def _share_or_404(token):
    if not _rate_ok():
        abort(429)
    r = db.conn().execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    if not r or r["revoked"] or r["expires"] < time.time() or db.get_setting("sharing", "1") != "1":
        abort(404)
    return r


@app.get("/s/<token>")
def share_page(token):
    return send_from_directory(STATIC, "share.html")


@app.get("/api/public/share/<token>")
def share_meta(token):
    r = _share_or_404(token)
    ids = json.loads(r["photo_ids"])
    c = db.conn()
    items = []
    for i in range(0, len(ids), 900):
        ch = ids[i:i + 900]
        items += [dict(id=x["id"], name=os.path.basename(x["path"]), w=x["w"], h=x["h"], taken=x["t"], vid=x["kind"] == "video", thumb=bool(x["tkey"]))
                  for x in c.execute(f"SELECT id,path,w,h,kind,tkey,COALESCE(taken,mtime) t FROM photos WHERE id IN ({','.join('?' * len(ch))})", ch)]
    items.sort(key=lambda x: -(x["taken"] or 0))
    c.execute("UPDATE shares SET views=views+1 WHERE token=?", (token,))
    c.commit()
    return jsonify(title=r["title"], expires=r["expires"], allow_download=bool(r["allow_download"]), items=items)


def _share_photo_row(token, pid):
    r = _share_or_404(token)
    if pid not in set(json.loads(r["photo_ids"])):
        abort(404)
    row = db.conn().execute("SELECT path, kind, tkey FROM photos WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    return r, row


@app.get("/s/<token>/thumb/<int:pid>")
def share_thumb(token, pid):
    _, row = _share_photo_row(token, pid)
    if not row["tkey"] or not db.thumb_path(row["tkey"]).exists():
        abort(404)
    resp = send_file(db.thumb_path(row["tkey"]), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.get("/s/<token>/photo/<int:pid>")
def share_photo(token, pid):
    _, row = _share_photo_row(token, pid)
    return _send_original(row)


@app.get("/s/<token>/zip")
def share_zip(token):
    r = _share_or_404(token)
    if not r["allow_download"]:
        abort(403)
    ids = json.loads(r["photo_ids"])
    c, files = db.conn(), []
    for i in range(0, len(ids), 900):
        ch = ids[i:i + 900]
        files += [dict(path=x["path"], taken=x["t"], root=x["root"]) for x in c.execute(
            f"SELECT p.path, COALESCE(p.taken,p.mtime) t, s.path root FROM photos p JOIN sources s ON s.id=p.source_id WHERE p.id IN ({','.join('?' * len(ch))})", ch)]
    return _zip_response(files, "flat", "shared_photos.zip")


# ------------------------------------------------------------------ open sign-up, personal uploads, account deletion
MAX_UPLOAD_FILE = 40 * 1024 * 1024
_signup_hits = defaultdict(deque)


def _signup_open():
    return db.get_setting("signup", os.environ.get("FACEMATCH_SIGNUP", "0")) == "1"


def _uploads_on():
    return db.get_setting("uploads", "1") == "1"


def _quota_bytes():
    return int(float(db.get_setting("quota_mb", os.environ.get("FACEMATCH_QUOTA_MB", 2048))) * 1048576)


def _dir_stats(root):
    n = size = 0
    try:
        for e in os.scandir(root):
            if e.is_file():
                n += 1
                size += e.stat().st_size
    except OSError:
        pass
    return n, size


def ensure_private_source(uid):
    """Every account gets its own private upload folder; only that person (and admins) can see photos in it."""
    c = db.conn()
    r = c.execute("SELECT own_source FROM users WHERE id=?", (uid,)).fetchone()
    if r and r["own_source"]:
        row = c.execute("SELECT id, path FROM sources WHERE id=?", (r["own_source"],)).fetchone()
        if row:
            Path(row["path"]).mkdir(parents=True, exist_ok=True)
            return row["id"], row["path"]
    path = str(db.DATA_DIR / "users" / str(uid) / "photos")
    os.makedirs(path, exist_ok=True)
    c.execute("INSERT OR IGNORE INTO sources(path, added_at, owner_id) VALUES(?,?,?)", (path, time.time(), uid))
    sid = c.execute("SELECT id FROM sources WHERE path=?", (path,)).fetchone()[0]
    c.execute("UPDATE users SET own_source=? WHERE id=?", (sid, uid))
    c.execute("INSERT OR IGNORE INTO user_sources(user_id, source_id) VALUES(?,?)", (uid, sid))
    c.commit()
    _stats_cache["t"] = 0
    if db.get_setting("watch", "1") == "1":
        watcher.watch(sid, path)
    return sid, path


@app.post("/api/auth/signup")
def auth_signup():
    if not _signup_open() or auth.user_count() == 0:
        return jsonify(error="Sign-up is closed. Ask the admin for an invite link."), 403
    d = request.get_json(silent=True) or {}
    if not d.get("consent"):
        return jsonify(error="Please tick the box to agree to the privacy notice."), 400
    ip, now = auth.client_ip(), time.time()
    q = _signup_hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= 5:
        return jsonify(error="Too many sign-ups from this network. Try again in an hour."), 429
    uid = auth.create_user(d.get("username"), d.get("display"), "member", d.get("password"), True, False, ())
    q.append(now)
    c = db.conn()
    c.execute("UPDATE users SET consent=? WHERE id=?", (now, uid))
    c.commit()
    ensure_private_source(uid)
    tok = auth.new_session(uid)
    g.user = None
    auth.audit("signup", "self-registered", dict(id=uid, username=auth.clean_username(d.get("username"))))
    return _set_cookie(jsonify(ok=True), tok)


@app.post("/api/upload")
def upload_photos():
    if not _uploads_on():
        return jsonify(error="Uploads are turned off."), 403
    u = me()
    files = request.files.getlist("photos")[:40]
    if not files:
        return jsonify(error="Choose some photos first."), 400
    from PIL import Image
    from werkzeug.utils import secure_filename
    from .core import IMAGE_EXTS
    sid, root = ensure_private_source(u["id"])
    _, used = _dir_stats(root)
    quota = None if u["role"] == "admin" else _quota_bytes()
    if shutil.disk_usage(db.DATA_DIR).free < 500 * 1048576:
        return jsonify(error="The server is almost out of disk space."), 507
    saved, rejected = [], []
    for f in files:
        nm = secure_filename(f.filename or "") or "photo"
        ext = os.path.splitext(nm)[1].lower()
        if ext not in IMAGE_EXTS:
            rejected.append(dict(name=nm, why="not a supported photo type"))
            continue
        data = f.read(MAX_UPLOAD_FILE + 1)
        if len(data) > MAX_UPLOAD_FILE:
            rejected.append(dict(name=nm, why="larger than 40 MB"))
            continue
        if quota is not None and used + len(data) > quota:
            rejected.append(dict(name=nm, why="your storage is full"))
            continue
        try:
            Image.open(io.BytesIO(data)).verify()
        except Exception:
            rejected.append(dict(name=nm, why="not a valid image"))
            continue
        dst = Path(root) / f"{uuid.uuid4().hex[:8]}_{nm}"
        dst.write_bytes(data)
        used += len(data)
        saved.append(str(dst))
    if saved:
        indexer.add_paths(sid, saved)
        auth.audit("upload", f"{len(saved)} photos", u)
    return jsonify(saved=len(saved), rejected=rejected, used=used, quota=quota)


def _own_source(u):
    r = db.conn().execute("SELECT s.id, s.path FROM users x JOIN sources s ON s.id=x.own_source WHERE x.id=?", (u["id"],)).fetchone()
    return (r["id"], r["path"]) if r else (None, None)


@app.get("/api/my/photos")
def my_photos():
    u = me()
    sid, root = _own_source(u)
    off, lim = max(0, int(request.args.get("offset", 0))), min(200, int(request.args.get("limit", 60)))
    files, used = _dir_stats(root) if root else (0, 0)
    items, total = [], 0
    if sid:
        c = db.conn()
        total = c.execute("SELECT COUNT(*) FROM photos WHERE source_id=?", (sid,)).fetchone()[0]
        for r in c.execute("SELECT id, path, nfaces, status FROM photos WHERE source_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (sid, lim, off)):
            nm = os.path.basename(r["path"])
            items.append(dict(id=r["id"], name=nm[9:] if len(nm) > 9 and nm[8] == "_" else nm, faces=r["nfaces"], ok=r["status"] == "ok"))
    return jsonify(items=items, total=total, pending=max(0, files - total), used=used, quota=None if u["role"] == "admin" else _quota_bytes(), uploads=_uploads_on())


def _delete_photos(sid, ids):
    c = db.conn()
    n = 0
    for pid in ids:
        r = c.execute("SELECT path, tkey FROM photos WHERE id=? AND source_id=?", (pid, sid)).fetchone()
        if not r:
            continue
        c.execute("DELETE FROM faces WHERE photo_id=?", (pid,))
        c.execute("DELETE FROM photos WHERE id=?", (pid,))
        for f in (r["path"], str(db.thumb_path(r["tkey"])) if r["tkey"] else None):
            try:
                f and os.unlink(f)
            except OSError:
                pass
        n += 1
    c.commit()
    index.invalidate()
    _count_cache.clear()
    _stats_cache["t"] = 0
    return n


@app.post("/api/my/photos/delete")
def my_photos_delete():
    sid, _ = _own_source(me())
    ids = [int(i) for i in (request.get_json(silent=True) or {}).get("ids", [])][:500]
    if not sid or not ids:
        return jsonify(error="Nothing to delete."), 400
    n = _delete_photos(sid, ids)
    auth.audit("photos_deleted", f"{n} photos", me())
    return jsonify(ok=True, deleted=n)


def _purge_user(uid):
    """Remove a person completely: account, private photos and their thumbnails, saved people, share links."""
    c = db.conn()
    own = c.execute("SELECT own_source FROM users WHERE id=?", (uid,)).fetchone()
    sid = own[0] if own else None
    if sid:
        watcher.unwatch(sid)
        tk = [r[0] for r in c.execute("SELECT tkey FROM photos WHERE source_id=? AND tkey IS NOT NULL", (sid,))]
        c.execute("DELETE FROM faces WHERE photo_id IN (SELECT id FROM photos WHERE source_id=?)", (sid,))
        c.execute("DELETE FROM photos WHERE source_id=?", (sid,))
        c.execute("DELETE FROM sources WHERE id=?", (sid,))
        c.execute("DELETE FROM user_sources WHERE source_id=?", (sid,))
        for t in tk:
            try:
                db.thumb_path(t).unlink()
            except OSError:
                pass
        shutil.rmtree(db.DATA_DIR / "users" / str(uid), ignore_errors=True)
    for sql in ("DELETE FROM person_samples WHERE person_id IN (SELECT id FROM people WHERE owner_id=?)", "DELETE FROM people WHERE owner_id=?",
                "DELETE FROM sessions WHERE user_id=?", "DELETE FROM invites WHERE user_id=?", "DELETE FROM shares WHERE user_id=?",
                "DELETE FROM user_sources WHERE user_id=?", "DELETE FROM users WHERE id=?"):
        c.execute(sql, (uid,))
    c.commit()
    index.invalidate()
    _count_cache.clear()
    _stats_cache["t"] = 0


@app.post("/api/me/delete")
def me_delete():
    u = me()
    d = request.get_json(silent=True) or {}
    row = db.conn().execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()
    if not auth.verify_password(d.get("password", ""), row["pw_hash"] or ""):
        return jsonify(error="That password isn't right."), 400
    if u["role"] == "admin" and db.conn().execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
        return jsonify(error="You are the only admin. Make someone else an admin first."), 400
    auth.audit("account_deleted", u["username"], u)
    _purge_user(u["id"])
    r = jsonify(ok=True)
    r.delete_cookie(auth.COOKIE, path="/")
    return r


# ------------------------------------------------------------------ housekeeping
def _after_index(changed):
    _stats_cache["t"] = 0
    if changed and db.get_setting("auto_discover", "1") == "1":
        start_discover()


indexer = Indexer(on_change=index.invalidate, on_finish=_after_index)
watcher = Watcher(indexer)


def _background_loop():
    time.sleep(8)
    last_scan = time.time()
    while True:
        try:
            hrs = float(db.get_setting("autoscan_hours", 24))
            if hrs > 0 and time.time() - last_scan > hrs * 3600 and not indexer.status()["running"]:
                last_scan = time.time()
                indexer.start()
            if db.get_setting("backup", "1") == "1" and time.time() - float(db.get_setting("last_backup", 0) or 0) > 86400 \
                    and _stats()["photos"] > 0:
                db.backup()
            now = time.time()
            c = db.conn()
            c.execute("DELETE FROM shares WHERE expires < ?", (now - 30 * 86400,))
            c.execute("DELETE FROM invites WHERE expires < ?", (now,))
            c.commit()
        except Exception:
            log.exception("background task failed")
        time.sleep(30)


def _setup_logging():
    d = db.DATA_DIR / "logs"
    d.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger("facematch")
    root.setLevel(logging.INFO)
    if not root.handlers:
        fh = logging.handlers.RotatingFileHandler(d / "facematch.log", maxBytes=2_000_000, backupCount=5)
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)


def init_app(start_threads=True):
    global SETUP_CODE
    db.init()
    _setup_logging()
    if auth.user_count() == 0:
        u, p = os.environ.get("FACEMATCH_ADMIN_USER"), os.environ.get("FACEMATCH_ADMIN_PASSWORD")
        if u and p:
            uid = auth.create_user(u, u, "admin", p)
            c = db.conn()
            c.execute("UPDATE people SET owner_id=? WHERE owner_id IS NULL AND auto=0 AND name!=''", (uid,))
            c.commit()
            log.info("admin account '%s' created from environment", u)
        else:
            SETUP_CODE = secrets.token_urlsafe(6)
    if start_threads:
        threading.Thread(target=_background_loop, daemon=True).start()
        threading.Thread(target=lambda: (matcher(), index.refresh()), daemon=True).start()  # warm up
        watcher.start()
    return app


def shutdown():
    try:
        watcher.stop()
        indexer.stop()
    except Exception:
        pass
