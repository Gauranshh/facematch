import sqlite3

import numpy as np

from facematch import cluster, db
from facematch.core import MODELS, encode_emb


def test_old_v1_database_is_upgraded_in_place(tmp_path, monkeypatch):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("""CREATE TABLE sources(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, added_at REAL, last_scan REAL);
      CREATE TABLE photos(id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, path TEXT UNIQUE NOT NULL, size INTEGER, mtime INTEGER, w INTEGER, h INTEGER, nfaces INTEGER DEFAULT 0, status TEXT DEFAULT 'ok', tkey TEXT);
      CREATE TABLE faces(id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL, x INTEGER, y INTEGER, w INTEGER, h INTEGER, emb BLOB NOT NULL);
      CREATE TABLE people(id INTEGER PRIMARY KEY, name TEXT NOT NULL, emb BLOB NOT NULL, crop BLOB, created REAL);
      INSERT INTO sources(path) VALUES('/x'); INSERT INTO people(name,emb,created) VALUES('Mum', x'00', 1);""")
    c.commit(); c.close()
    monkeypatch.setattr(db, "DB_PATH", p)
    monkeypatch.setattr(db, "_local", type(db._local)(), raising=False)
    db.init()
    cols = {r[1] for r in db.conn().execute("PRAGMA table_info(people)")}
    assert {"owner_id", "srcs", "hidden", "auto"} <= cols
    assert db.conn().execute("SELECT name FROM people").fetchone()[0] == "Mum"      # data survives


def test_discovery_groups_synthetic_people(env):
    """5 'people' (random unit vectors + noise), 12 photos each -> discovery finds ~5 clean groups."""
    srv = env["server"]
    rng = np.random.default_rng(7)
    dim = MODELS["sface"]["dim"]
    c = db.conn()
    c.execute("INSERT INTO sources(path, added_at) VALUES('/synthetic', 0)")
    sid = c.execute("SELECT id FROM sources WHERE path='/synthetic'").fetchone()[0]
    centers = rng.normal(size=(5, dim)); centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    pid0 = 100000
    for k in range(5):
        for j in range(12):
            e = centers[k] + rng.normal(scale=0.03, size=dim); e /= np.linalg.norm(e)
            pid = pid0 + k * 100 + j
            c.execute("INSERT INTO photos(id,source_id,path,size,mtime,w,h,nfaces,tkey) VALUES(?,?,?,?,?,?,?,?,?)", (pid, sid, f"/synthetic/{pid}.jpg", 1, 1, 100, 100, 1, None))
            c.execute("INSERT INTO faces(photo_id,x,y,w,h,emb,q) VALUES(?,?,?,?,?,?,?)", (pid, 0, 0, 50, 50, encode_emb(e.astype(np.float32), dim), 0.9))
    c.commit()
    srv.index.invalidate()
    n = cluster.discover(srv.index)
    groups = c.execute("SELECT nphotos FROM people WHERE owner_id IS NULL AND auto=1 AND srcs LIKE ?", (f'%"{sid}"%',)).fetchall()
    assert len(groups) == 5 and all(g[0] == 12 for g in groups), (n, [g[0] for g in groups])
    # cleanup so other tests keep their expected counts
    c.execute("DELETE FROM faces WHERE photo_id>=?", (pid0,)); c.execute("DELETE FROM photos WHERE id>=?", (pid0,))
    c.execute("DELETE FROM sources WHERE id=?", (sid,)); c.commit()
    srv.index.invalidate()
