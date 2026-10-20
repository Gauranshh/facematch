"""SQLite storage. One file holds everything: libraries, photos, face embeddings, people, settings.
Schema v2; older (v1) databases are upgraded in place, so an existing index is never thrown away."""
import os
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("FACEMATCH_DATA", Path.home() / ".facematch")).expanduser()
DB_PATH = DATA_DIR / "index.db"
THUMB_DIR = DATA_DIR / "thumbs"
BACKUP_DIR = DATA_DIR / "backups"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, added_at REAL, last_scan REAL);
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, path TEXT UNIQUE NOT NULL,
  size INTEGER, mtime INTEGER, w INTEGER, h INTEGER, nfaces INTEGER DEFAULT 0,
  status TEXT DEFAULT 'ok', tkey TEXT, taken INTEGER, kind TEXT DEFAULT 'photo');
CREATE INDEX IF NOT EXISTS photos_source ON photos(source_id);
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL, x INTEGER, y INTEGER, w INTEGER, h INTEGER, emb BLOB NOT NULL,
  q REAL, t REAL);
CREATE INDEX IF NOT EXISTS faces_photo ON faces(photo_id);
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, emb BLOB NOT NULL, crop BLOB, created REAL,
  auto INTEGER DEFAULT 0, n INTEGER DEFAULT 0, nphotos INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0,
  rep_path TEXT, rep_box TEXT, rep_t REAL);
CREATE TABLE IF NOT EXISTS person_samples(
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, label INTEGER NOT NULL, emb BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS samples_person ON person_samples(person_id);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, display TEXT, pw_hash TEXT,
  role TEXT NOT NULL DEFAULT 'member', can_export INTEGER NOT NULL DEFAULT 1, all_sources INTEGER NOT NULL DEFAULT 1,
  disabled INTEGER NOT NULL DEFAULT 0, theme TEXT, created REAL, last_login REAL);
CREATE TABLE IF NOT EXISTS user_sources(user_id INTEGER NOT NULL, source_id INTEGER NOT NULL, PRIMARY KEY(user_id, source_id));
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL, expires REAL, last_seen REAL, ua TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS invites(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS shares(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT, photo_ids TEXT NOT NULL, created REAL, expires REAL,
  allow_download INTEGER DEFAULT 1, views INTEGER DEFAULT 0, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts REAL, user_id INTEGER, username TEXT, event TEXT, detail TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
"""

# columns added in v2 -> (table, column, ddl)
MIGRATE = [
    ("photos", "taken", "INTEGER"), ("photos", "kind", "TEXT DEFAULT 'photo'"),
    ("faces", "q", "REAL"), ("faces", "t", "REAL"),
    ("people", "auto", "INTEGER DEFAULT 0"), ("people", "n", "INTEGER DEFAULT 0"), ("people", "nphotos", "INTEGER DEFAULT 0"),
    ("people", "hidden", "INTEGER DEFAULT 0"), ("people", "rep_path", "TEXT"), ("people", "rep_box", "TEXT"), ("people", "rep_t", "REAL"),
    ("people", "owner_id", "INTEGER"), ("people", "from_id", "INTEGER"), ("people", "srcs", "TEXT"),
]

_local = threading.local()


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    for table, col, ddl in MIGRATE:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    c.execute("CREATE INDEX IF NOT EXISTS people_owner ON people(owner_id)")
    c.commit()
    c.close()


def conn() -> sqlite3.Connection:
    """Per-thread connection."""
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=60)
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA cache_size=-65536")
        c.row_factory = sqlite3.Row
        _local.c = c
    return c


def get_setting(k, default=None):
    r = conn().execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_setting(k, v):
    c = conn()
    c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    c.commit()


def thumb_path(tkey: str) -> Path:
    return THUMB_DIR / tkey[:2] / f"{tkey}.jpg"


def backup(keep: int = 3) -> str:
    """Consistent online copy of the index (not the thumbnails, which are re-creatable)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"index-{time.strftime('%Y%m%d-%H%M')}.db"
    src = sqlite3.connect(DB_PATH)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close(); src.close()
    for old in sorted(BACKUP_DIR.glob("index-*.db"))[:-keep]:
        old.unlink()
    set_setting("last_backup", time.time())
    return str(dst)
