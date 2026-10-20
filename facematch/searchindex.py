"""In-memory face index: embeddings are loaded from SQLite once and searched with numpy (chunked matmul).
1 million faces = ~0.5 GB RAM (128-d model) and a search takes well under a second.

Supports: filters (folder, date, group size, quality, videos), "not this person" negative examples,
and multi-person queries ("A and B in the same photo")."""
import threading
from dataclasses import dataclass

import numpy as np

from . import db
from .core import MODELS, STORE_F16_ABOVE

CHUNK = 100_000
_I64 = np.empty(0, np.int64)


@dataclass
class Filter:
    sources: list = None
    date_from: int = None
    date_to: int = None
    min_faces: int = None
    max_faces: int = None
    min_quality: float = None
    videos: bool = True
    photos: bool = True


class SearchIndex:
    def __init__(self):
        self.lock = threading.Lock()
        self.chunks = []
        self.last_face_id = 0
        self.dirty = True       # full reload needed
        self.model = "sface"
        self.version = 0        # bumped whenever the data changes (used to cache per-person counts)

    def invalidate(self):
        self.dirty = True

    @property
    def dim(self):
        return MODELS[self.model]["dim"]

    def refresh(self):
        with self.lock:
            model = db.get_setting("model", "sface")
            if model != self.model:
                self.model, self.dirty = model, True
            dim = self.dim
            f16 = dim > STORE_F16_ABOVE
            c = db.conn()
            if self.dirty:
                self.chunks, self.last_face_id, self.dirty = [], 0, False
                self.version += 1
            cur = c.execute(
                "SELECT f.id, f.photo_id, p.source_id, COALESCE(p.taken,p.mtime,0), p.nfaces, COALESCE(f.q,0.5), COALESCE(f.t,-1), f.emb "
                "FROM faces f JOIN photos p ON p.id=f.photo_id WHERE f.id>? ORDER BY f.id", (self.last_face_id,))
            while True:
                rows = cur.fetchmany(20000)
                if not rows:
                    break
                emb = np.frombuffer(b"".join(r[7] for r in rows), dtype=np.float16 if f16 else np.float32).reshape(-1, dim)
                part = dict(emb=emb, face=np.array([r[0] for r in rows], np.int64), photo=np.array([r[1] for r in rows], np.int64),
                            src=np.array([r[2] for r in rows], np.int32), taken=np.array([r[3] for r in rows], np.int64),
                            nf=np.array([r[4] for r in rows], np.int16), q=np.array([r[5] for r in rows], np.float32),
                            t=np.array([r[6] for r in rows], np.float32))
                self.last_face_id = rows[-1][0]
                self.version += 1
                if self.chunks and len(self.chunks[-1]["face"]) + len(part["face"]) <= CHUNK:
                    last = self.chunks[-1]
                    self.chunks[-1] = {k: np.concatenate([last[k], part[k]]) for k in part}
                else:
                    self.chunks.append(part)

    @property
    def n_faces(self):
        return sum(len(c["face"]) for c in self.chunks)

    # ---- helpers for clustering
    def concat(self, key):
        return np.concatenate([c[key] for c in self.chunks]) if self.chunks else _I64

    def gather(self, rows):
        """Embeddings (float32) for global row numbers."""
        sizes = np.array([len(c["face"]) for c in self.chunks])
        off = np.concatenate([[0], np.cumsum(sizes)])
        out = np.empty((len(rows), self.dim), np.float32)
        ci = np.searchsorted(off, rows, side="right") - 1
        for k in np.unique(ci):
            m = ci == k
            out[m] = self.chunks[k]["emb"][rows[m] - off[k]]
        return out

    # ---- search
    def _one(self, q, thr, flt: Filter, negs, pen):
        q = (q / (np.linalg.norm(q) + 1e-9)).astype(np.float32)
        src = np.asarray(flt.sources, np.int32) if flt.sources else None
        parts = {k: [] for k in ("photo", "s", "face", "taken", "q", "t")}
        for ch in self.chunks:
            e = ch["emb"].astype(np.float32, copy=False)
            s = e @ q
            if negs is not None and len(negs):
                smax = (e @ negs.T).max(1)      # how much it resembles a face the user rejected
                s = np.where(smax > s, -1.0, s - 0.3 * np.maximum(0.0, smax - pen))   # closer to a rejected face than to the person -> out
            m = s >= thr
            if src is not None:
                m &= np.isin(ch["src"], src)
            if flt.date_from is not None:
                m &= ch["taken"] >= flt.date_from
            if flt.date_to is not None:
                m &= ch["taken"] < flt.date_to
            if flt.min_faces is not None:
                m &= ch["nf"] >= flt.min_faces
            if flt.max_faces is not None:
                m &= ch["nf"] <= flt.max_faces
            if flt.min_quality:
                m &= ch["q"] >= flt.min_quality
            if not flt.videos:
                m &= ch["t"] < 0
            if not flt.photos:
                m &= ch["t"] >= 0
            idx = np.nonzero(m)[0]
            if len(idx):
                parts["s"].append(s[idx])
                for k in ("photo", "face", "taken", "q", "t"):
                    parts[k].append(ch[k][idx])
        if not parts["s"]:
            return None
        a = {k: np.concatenate(v) for k, v in parts.items()}
        order = np.argsort(-a["s"], kind="stable")
        a = {k: v[order] for k, v in a.items()}
        _, first = np.unique(a["photo"], return_index=True)   # best face per photo
        first.sort()
        return {k: v[first] for k, v in a.items()}

    def search(self, queries, thr, flt: Filter = None, pen=0.3):
        """queries: list of (embedding, negatives-or-None). One query = "photos of X"; several = all of them in the same photo.
        Returns dict of equally long arrays (photo, s, face, taken, q, t) sorted best-first, or None."""
        self.refresh()
        flt = flt or Filter()
        res = [self._one(q, thr, flt, n, pen) for q, n in queries]
        if any(r is None for r in res):
            return None
        if len(res) == 1:
            return res[0]
        common = res[0]["photo"]
        for r in res[1:]:
            common = np.intersect1d(common, r["photo"])
        if not len(common):
            return None
        sel = []
        for r in res:
            o = np.argsort(r["photo"])
            pos = o[np.searchsorted(r["photo"][o], common)]
            sel.append({k: v[pos] for k, v in r.items()})
        ok = np.ones(len(common), bool)
        for i in range(len(sel)):
            for j in range(i + 1, len(sel)):
                ok &= sel[i]["face"] != sel[j]["face"]       # must be different faces in the photo
        out = {k: v[ok] for k, v in sel[0].items()}
        out["s"] = np.minimum.reduce([x["s"][ok] for x in sel])
        order = np.argsort(-out["s"], kind="stable")
        return {k: v[order] for k, v in out.items()} if len(order) else None
