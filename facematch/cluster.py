"""Auto-discovery of people: groups the faces of the whole library into identities.

Greedy centroid clustering in quality order (sharp, large faces seed the groups), then a merge pass and a final
assignment of every face. Memory use is bounded (batches), so it scales to ~1M faces.
The result is a shared *pool* of unnamed groups (owner_id NULL). Users claim a group by naming it, which creates their own
private copy, so names and corrections never leak between accounts."""
import json
import time

import numpy as np

from . import db
from .core import MODELS

BATCH = 4000
Q_SEED = 0.30          # faces below this quality never *create* a group (they can still join one)
MIN_PHOTOS = 3         # groups with fewer distinct photos are ignored


def _norm(x):
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-9)


def discover(index, progress=lambda m: None, min_photos=MIN_PHOTOS, strict=0.0):
    """Returns the number of new unnamed people found. `strict` shifts all thresholds (+ = fewer merges)."""
    index.refresh()
    model = index.model
    P = MODELS[model]
    dim = index.dim
    T_assign, T_form, T_merge, T_member = (P["assign"] + strict, P["form"] + strict, P["merge"] + strict, P["member"] + strict)
    c = db.conn()
    N = index.n_faces
    if N == 0:
        c.execute("DELETE FROM people WHERE owner_id IS NULL AND auto=1"); c.commit()
        return 0
    q_all, photo_all, face_all = index.concat("q"), index.concat("photo"), index.concat("face")

    seed_ids, n_seed = [], 0
    C = np.zeros((0, dim), np.float32)
    sums = np.zeros((0, dim), np.float64)
    cnt = np.zeros(0)
    src_all = index.concat("src")

    order = np.nonzero(q_all >= Q_SEED)[0]
    order = order[np.argsort(-q_all[order], kind="stable")]
    time.time()
    for b in range(0, len(order), BATCH):
        rows = order[b:b + BATCH]
        X = index.gather(rows)
        left = np.ones(len(rows), bool)
        if len(C):
            S = X @ C.T
            best, bs = S.argmax(1), S.max(1)
            ok = bs >= T_assign
            np.add.at(sums, best[ok], X[ok])
            np.add.at(cnt, best[ok], 1)
            left = ~ok
        L = X[left]
        if len(L):
            G = L @ L.T
            alive = np.ones(len(L), bool)
            _new_c, new_s, new_n = [], [], []
            for i in range(len(L)):          # rows are already best-quality first
                if not alive[i]:
                    continue
                mem = alive & (G[i] >= T_form)
                alive &= ~mem
                new_s.append(L[mem].sum(0)); new_n.append(int(mem.sum()))
            if new_s:
                sums = np.vstack([sums, np.array(new_s)]); cnt = np.concatenate([cnt, new_n])
        C = _norm(sums).astype(np.float32)
        progress(f"Grouping faces… {min(b + BATCH, len(order)):,} / {len(order):,} · {len(C) - n_seed:,} groups so far")

    # merge near-identical groups (auto groups merge into each other or into a named person; named never merge together)
    k = len(C)
    parent = list(range(k))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    if k > 1:
        for i0 in range(0, k, 2000):
            S = C[i0:i0 + 2000] @ C.T
            ii, jj = np.nonzero(S >= T_merge)
            for a, b_ in zip(ii + i0, jj):
                if a < b_ and not (a < n_seed and b_ < n_seed):
                    ra, rb = find(a), find(b_)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)      # seeds (lower index) win
        roots = np.array([find(i) for i in range(k)])
        for r in np.unique(roots):
            m = roots == r
            if m.sum() > 1:
                sums[r] = sums[m].sum(0); cnt[r] = cnt[m].sum()
        keep = np.unique(roots)
        {int(r): n for n, r in enumerate(keep)}
        sums, cnt = sums[keep], cnt[keep]
        n_seed = int(np.sum(keep < n_seed))
        seed_ids = [seed_ids[int(r)] for r in keep if r < len(seed_ids)]
        C = _norm(sums).astype(np.float32)
    progress("Assigning every face…")

    # final assignment of ALL faces (incl. blurry ones) to the nearest group
    k = len(C)
    counts = np.zeros(k, np.int64)
    pair_keys, src_keys = [], []
    best_score = np.full(k, -1.0)
    best_row = np.zeros(k, np.int64)
    for i0 in range(0, N, 10000):
        rows = np.arange(i0, min(N, i0 + 10000))
        X = index.gather(rows)
        S = X @ C.T
        bi, bs = S.argmax(1), S.max(1)
        ok = bs >= T_member
        counts += np.bincount(bi[ok], minlength=k)
        pair_keys.append(bi[ok].astype(np.int64) * (1 << 40) + photo_all[rows[ok]])
        src_keys.append(src_all[rows[ok]].astype(np.int64))
        sc = 0.5 * q_all[rows] + 0.5 * bs              # representative face: sharp AND typical
        for g in np.unique(bi[ok]):
            m = ok & (bi == g)
            j = int(np.argmax(np.where(m, sc, -1)))
            if sc[j] > best_score[g]:
                best_score[g], best_row[g] = sc[j], rows[j]
    keys = np.unique(np.concatenate(pair_keys)) if pair_keys else np.empty(0, np.int64)
    nphotos = np.bincount((keys >> 40).astype(np.int64), minlength=k)
    # photos per (group, source) -> lets restricted users see correct counts for just their folders
    src_counts = {}
    if len(keys):
        gp = np.concatenate(pair_keys)
        sk = np.concatenate(src_keys)
        trip = np.unique(np.stack([gp, sk], 1), axis=0)          # unique (group<<40|photo, source) pairs
        for key, s_ in trip:
            d = src_counts.setdefault(int(key >> 40), {})
            d[int(s_)] = d.get(int(s_), 0) + 1

    # write results (shared pool; user-owned people are never touched)
    c.execute("DELETE FROM people WHERE owner_id IS NULL AND auto=1")
    new = 0
    for g in range(k):
        if nphotos[g] < min_photos:
            continue
        fid = int(face_all[best_row[g]])
        r = c.execute("SELECT p.path, f.x, f.y, f.w, f.h, f.t FROM faces f JOIN photos p ON p.id=f.photo_id WHERE f.id=?", (fid,)).fetchone()
        c.execute("INSERT INTO people(name, emb, created, auto, n, nphotos, rep_path, rep_box, rep_t, srcs) VALUES('',?,?,1,?,?,?,?,?,?)",
                  (C[g].astype(np.float32).tobytes(), time.time(), int(counts[g]), int(nphotos[g]),
                   r["path"] if r else None, json.dumps([r["x"], r["y"], r["w"], r["h"]]) if r else None, r["t"] if r else None,
                   json.dumps(src_counts.get(g, {}))))
        new += 1
    c.commit()
    return new
