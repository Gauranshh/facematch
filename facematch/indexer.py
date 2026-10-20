"""Background indexer: walks library folders, detects faces with a pool of worker processes, stores embeddings.

* Incremental: files already indexed (same size + mtime) are skipped, so re-scans take seconds.
* Resumable: results are committed in small batches; stopping/crashing loses at most a few seconds of work.
* Gentle: workers run at low CPU priority and use (CPU count - 1) processes by default, so the PC stays responsive.
* Reads the EXIF capture date; optionally samples faces from videos.
"""
import hashlib
import multiprocessing as mp
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import db
from .core import IMAGE_EXTS, MODELS, VIDEO_EXTS, encode_emb

DETAIL = {"fast": 960, "balanced": 1280, "thorough": 1920}   # detection resolution (longest side, px)
THUMB_SIDE = int(os.environ.get("FACEMATCH_THUMB", "320"))
MIN_BYTES = int(os.environ.get("FACEMATCH_MIN_BYTES", str(20 * 1024)))      # skip icons / tiny web graphics
MIN_VIDEO_BYTES = 200 * 1024
MAX_PIXELS = 400_000_000
VIDEO_STEP_S, VIDEO_MAX_SAMPLES = 4.0, 48
SKIP_DIRS = {"$recycle.bin", "system volume information", "node_modules", ".thumbnails", "@eadir", "__macosx"}


# ----------------------------------------------------------------------------- worker side
_W = {}


def tkey_for(path: str) -> str:
    return hashlib.md5(path.encode("utf-8", "surrogatepass")).hexdigest()[:20]


def _init_worker(side=1280, model="sface"):
    _W["side"] = side
    try:
        os.nice(10)
    except Exception:
        pass
    import cv2
    cv2.setNumThreads(1)
    from .core import FaceMatcher
    _W["m"] = FaceMatcher(model)
    _W["dim"] = MODELS[model]["dim"]


def exif_taken(im):
    """EXIF capture time as epoch seconds, or None."""
    try:
        ex = im.getexif()
        v = None
        try:
            ifd = ex.get_ifd(0x8769)
            v = ifd.get(0x9003) or ifd.get(0x9004)
        except Exception:
            pass
        v = v or ex.get(0x0132)
        if v:
            if isinstance(v, bytes):
                v = v.decode("ascii", "ignore")
            t = time.mktime(time.strptime(str(v).strip()[:19], "%Y:%m:%d %H:%M:%S"))
            if 631152000 < t < time.time() + 172800:
                return int(t)
    except Exception:
        pass
    return None


def _save_thumb(cv2, path, bgr):
    tk = tkey_for(path)
    tp = db.thumb_path(tk)
    tp.parent.mkdir(parents=True, exist_ok=True)
    h, w = bgr.shape[:2]
    s = THUMB_SIDE / max(h, w)
    small = cv2.resize(bgr, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else bgr
    cv2.imwrite(str(tp), small, [cv2.IMWRITE_JPEG_QUALITY, 72])
    return tk


def process_file(path: str):
    """Runs in a worker process. Returns a plain dict (picklable)."""
    ext = os.path.splitext(path)[1].lower()
    out = {"path": path, "status": "ok", "faces": [], "w": 0, "h": 0, "kind": "video" if ext in VIDEO_EXTS else "photo"}
    try:
        st = os.stat(path)
        out["size"], out["mtime"] = st.st_size, int(st.st_mtime)
        if out["kind"] == "video":
            _process_video(path, out)
        else:
            _process_image(path, out)
        if out.get("taken") is None:
            out["taken"] = out["mtime"]
    except Exception as e:  # unreadable / corrupt
        out["status"] = "error"
        out["err"] = str(e)[:200]
    return out


def _process_image(path, out):
    import cv2
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None
    dim = _W["dim"]
    with Image.open(path) as im:
        ow, oh = im.size
        if ow * oh > MAX_PIXELS:
            out["status"] = "toolarge"
            return
        out["taken"] = exif_taken(im)
        if im.format == "JPEG":
            k = _W["side"] / max(ow, oh)   # decode at 1/2, 1/4 or 1/8 size (longest side stays >= detail px): much faster
            if k < 1:
                im.draft("RGB", (int(ow * k), int(oh * k)))
        dw, dh = im.size
        ratio = ow / dw
        im = ImageOps.exif_transpose(im).convert("RGB")
    bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    out["w"], out["h"] = int(round(w * ratio)), int(round(h * ratio))
    faces = _W["m"].embed_faces(bgr, _W["side"])
    for f in faces:
        x, y, fw, fh = f.box
        out["faces"].append(((int(x * ratio), int(y * ratio), int(fw * ratio), int(fh * ratio)), encode_emb(f.emb, dim), f.q, None))
    if faces:  # thumbnails only for photos that can ever be a search result
        out["tkey"] = _save_thumb(cv2, path, bgr)


def _process_video(path, out):
    import cv2
    dim = _W["dim"]
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        out["status"] = "error"
        out["err"] = "cannot open video"
        return
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        dur = n / fps if n > 0 else 30.0
        W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        out["w"], out["h"] = W, H
        step = max(VIDEO_STEP_S, dur / VIDEO_MAX_SAMPLES)
        kept, best_frame, best_n = [], None, 0
        for t in np.arange(0, max(dur, 0.1), step):
            cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000)
            ok, fr = cap.read()
            if not ok:
                continue
            H, W = fr.shape[:2]
            out["w"], out["h"] = W, H
            faces = _W["m"].embed_faces(fr, _W["side"])
            new = 0
            for f in faces:
                if kept and max(float(np.dot(f.emb, e)) for _, e, _, _ in kept) > 0.85:
                    continue   # same face as an earlier sample of this video
                kept.append((f.box, f.emb, f.q, float(t)))
                new += 1
            if new and len(faces) >= best_n:
                best_frame, best_n = fr, len(faces)
    finally:
        cap.release()
    out["faces"] = [(b, encode_emb(e, dim), q, t) for b, e, q, t in kept]
    if best_frame is not None:
        out["tkey"] = _save_thumb(cv2, path, best_frame)


# ----------------------------------------------------------------------------- controller side
def parse_excludes(text: str):
    names, paths = set(), []
    for line in (text or "").splitlines():
        line = line.strip().strip('"').strip()
        if not line:
            continue
        if os.sep in line or "/" in line or (len(line) > 1 and line[1] == ":"):
            paths.append(os.path.normcase(os.path.abspath(line)).rstrip(os.sep))
        else:
            names.add(line.lower())
    return names, paths


class Indexer:
    """Runs jobs from a queue: ('source', id) = scan a whole folder, ('paths', id, [paths]) = just these files (live updates)."""

    def __init__(self, on_change=None, on_finish=None):
        self.on_change = on_change or (lambda: None)   # stored faces were removed/changed -> reload search index
        self.on_finish = on_finish or (lambda changed: None)
        self.lock = threading.Lock()
        self.thread = None
        self._active = False
        self.jobs = []
        self.pause_ev = threading.Event()
        self.stop_ev = threading.Event()
        self._changed = False
        self.s = dict(state="idle", message="", total=0, done=0, errors=0, faces=0, rate=0.0, source=None, eta=None, live=0)

    # ---- public controls
    def status(self):
        d = dict(self.s)
        d["paused"] = self.pause_ev.is_set()
        d["running"] = self._active
        d["queued"] = len(self.jobs)
        return d

    def _kick(self):
        """Caller holds self.lock."""
        if not self._active:
            self._active = True
            self.stop_ev.clear()
            self.pause_ev.clear()
            self._changed = False
            self.s.update(state="scanning", message="Starting…", total=0, done=0, errors=0, rate=0.0, eta=None, faces=0)
            self.thread = threading.Thread(target=self._run, daemon=True, name="indexer")
            self.thread.start()

    def start(self, source_ids=None):
        with self.lock:
            ids = list(source_ids) if source_ids else [r["id"] for r in db.conn().execute("SELECT id FROM sources")]
            for i in ids:
                if ("source", i) not in self.jobs:
                    self.jobs.append(("source", i))
            was = self._active
            self._kick()
            return not was

    def add_paths(self, source_id, paths):
        """Live update: index / refresh / remove these specific files."""
        with self.lock:
            for j in self.jobs:
                if j[0] == "paths" and j[1] == source_id:
                    j[2].update(paths)
                    break
            else:
                self.jobs.append(("paths", source_id, set(paths)))
            if ("source", source_id) in self.jobs:
                return   # a full scan is already pending and will see them
            self._kick()

    def pause(self):
        self.pause_ev.set()

    def resume(self):
        self.pause_ev.clear()

    def stop(self):
        with self.lock:
            self.jobs.clear()
        self.stop_ev.set()
        self.pause_ev.clear()

    def wait(self, timeout=None):
        """Block until the queue is drained; returns True if it finished within `timeout` seconds."""
        end = None if timeout is None else time.time() + timeout
        while True:
            t = self.thread
            if t is None or (not t.is_alive() and not self._active):
                return True
            t.join(0.2)
            if end is not None and time.time() > end:
                return False

    # ---- internals
    def _set(self, **kw):
        self.s.update(kw)

    def _run(self):
        try:
            while True:
                with self.lock:
                    if not self.jobs or self.stop_ev.is_set():
                        self._active = False
                        break
                    job = self.jobs.pop(0)
                row = db.conn().execute("SELECT * FROM sources WHERE id=?", (job[1],)).fetchone()
                if not row:
                    continue
                try:
                    if job[0] == "source":
                        self._do_source(row)
                    else:
                        self._do_paths(row, sorted(job[2]))
                except Exception as e:  # noqa: BLE001
                    self._set(message=f"Indexer error: {e}")
            self._set(state="idle", message="Stopped" if self.stop_ev.is_set() else "Up to date", rate=0.0, eta=None, source=None)
        except Exception as e:  # noqa: BLE001
            self._active = False
            self._set(state="idle", message=f"Indexer error: {e}", rate=0.0)
        finally:
            self._active = False
            try:
                self.on_finish(self._changed and not self.stop_ev.is_set())
            except Exception:
                pass

    def _walk(self, root: str, with_video: bool):
        """Yield (path, size, mtime, is_video) for media files; collects errors in self._walk_errors."""
        names, paths = parse_excludes(db.get_setting("exclude", ""))
        stack = [root]
        errors = []
        self._walk_errors = errors
        while stack:
            if self.stop_ev.is_set():
                return
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.name.startswith(".") or e.name.startswith("@"):
                                continue
                            if e.is_dir(follow_symlinks=False):
                                low = e.name.lower()
                                if low in SKIP_DIRS or low in names:
                                    continue
                                if paths and os.path.normcase(e.path).rstrip(os.sep) in paths:
                                    continue
                                stack.append(e.path)
                            else:
                                ext = os.path.splitext(e.name)[1].lower()
                                if ext in IMAGE_EXTS:
                                    st = e.stat()
                                    if st.st_size >= MIN_BYTES:
                                        yield e.path, st.st_size, int(st.st_mtime), False
                                elif with_video and ext in VIDEO_EXTS:
                                    st = e.stat()
                                    if st.st_size >= MIN_VIDEO_BYTES:
                                        yield e.path, st.st_size, int(st.st_mtime), True
                        except OSError:
                            errors.append(e.path)
            except OSError:
                errors.append(d)

    def _do_paths(self, src, paths):
        """Live job: handle just these files (new, changed, or removed)."""
        c = db.conn()
        with_video = db.get_setting("videos", "0") == "1"
        names, xpaths = parse_excludes(db.get_setting("exclude", ""))
        todo, gone = [], []
        for p in paths:
            ext = os.path.splitext(p)[1].lower()
            rel_parts = [x.lower() for x in Path(p).parts]
            if any(x in SKIP_DIRS or x in names for x in rel_parts[:-1]) or any(os.path.normcase(p).startswith(x + os.sep) for x in xpaths):
                continue
            ok_ext = ext in IMAGE_EXTS or (with_video and ext in VIDEO_EXTS)
            try:
                st = os.stat(p)
            except OSError:
                if c.execute("SELECT 1 FROM photos WHERE path=?", (p,)).fetchone():
                    gone.append(p)
                continue
            if not ok_ext or st.st_size < (MIN_VIDEO_BYTES if ext in VIDEO_EXTS else MIN_BYTES) or not os.path.isfile(p):
                continue
            row = c.execute("SELECT size, mtime FROM photos WHERE path=?", (p,)).fetchone()
            if not row or (row["size"], row["mtime"]) != (st.st_size, int(st.st_mtime)):
                todo.append(p)
        if gone:
            self._delete_paths(gone)
        if todo and not self.stop_ev.is_set():
            was_idle = self.s["total"] == self.s["done"]
            if was_idle:
                self.s.update(total=0, done=0, errors=0, faces=0)
            self._set(total=self.s["total"] + len(todo), state="indexing", message=f"Adding {len(todo)} new photo{'s' if len(todo) != 1 else ''}", source=src["path"])
            self._index(src["id"], todo)

    def _do_source(self, src):
        c = db.conn()
        root = src["path"]
        self._set(source=root, state="scanning", message=f"Scanning {root}…")
        if not os.path.isdir(root):
            self._set(message=f"{root} is not connected — skipped")
            return
        with_video = db.get_setting("videos", "0") == "1"
        known = {r["path"]: (r["size"], r["mtime"]) for r in c.execute("SELECT path,size,mtime FROM photos WHERE source_id=?", (src["id"],))}
        todo, seen, found = [], set(), 0
        for p, size, mt, _v in self._walk(root, with_video):
            found += 1
            seen.add(p)
            if known.get(p) != (size, mt):
                todo.append(p)
            if found % 2000 == 0:
                self._set(message=f"Scanning {root}… {found:,} files found, {len(todo):,} new")
        if self.stop_ev.is_set():
            return
        if not self._walk_errors:   # drop photos that vanished (only if the whole folder was readable)
            gone = [p for p in known if p not in seen]
            if gone:
                self._delete_paths(gone)
        self._backfill_dates(src["id"])
        if self.stop_ev.is_set():
            return
        self._set(total=self.s["total"] + len(todo), state="indexing", message=f"Indexing {root}")
        if todo:
            self._index(src["id"], todo)
        if not self.stop_ev.is_set():
            c.execute("UPDATE sources SET last_scan=? WHERE id=?", (time.time(), src["id"]))
            c.commit()

    def _backfill_dates(self, source_id):
        """Photos indexed by an older version have no capture date yet: read just the EXIF header (fast)."""
        from PIL import Image
        c = db.conn()
        rows = c.execute("SELECT id,path,mtime FROM photos WHERE source_id=? AND taken IS NULL", (source_id,)).fetchall()
        if not rows:
            return

        def one(r):
            t = None
            try:
                with Image.open(r["path"]) as im:
                    t = exif_taken(im)
            except Exception:
                pass
            return r["id"], t or r["mtime"] or 0

        self._set(state="scanning")
        with ThreadPoolExecutor(6) as ex:
            for i in range(0, len(rows), 1000):
                if self.stop_ev.is_set():
                    return
                self._set(message=f"Reading photo dates… {i:,} / {len(rows):,}")
                res = list(ex.map(one, rows[i:i + 1000]))
                c.executemany("UPDATE photos SET taken=? WHERE id=?", [(t, i_) for i_, t in res])
                c.commit()
        self._changed = True
        self.on_change()

    def _delete_paths(self, paths):
        c = db.conn()
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            q = ",".join("?" * len(chunk))
            rows = c.execute(f"SELECT id, tkey FROM photos WHERE path IN ({q})", chunk).fetchall()
            for r in rows:
                if r["tkey"]:
                    try:
                        db.thumb_path(r["tkey"]).unlink()
                    except OSError:
                        pass
            ids = [r["id"] for r in rows]
            if ids:
                q2 = ",".join("?" * len(ids))
                c.execute(f"DELETE FROM faces WHERE photo_id IN ({q2})", ids)
                c.execute(f"DELETE FROM photos WHERE id IN ({q2})", ids)
        c.commit()
        self._changed = True
        self.on_change()

    def _index(self, source_id, todo):
        n_workers = int(db.get_setting("workers", max(1, (os.cpu_count() or 2) - 1)))
        side = DETAIL.get(db.get_setting("detail", "balanced"), 1280)
        model = db.get_setting("model", "sface")
        ctx = mp.get_context("spawn")
        pool = ctx.Pool(n_workers, initializer=_init_worker, initargs=(side, model))
        results = queue.Queue()
        inflight = 0
        window = n_workers * 6
        batch, t_batch = [], time.time()
        t0, done0 = time.time(), self.s["done"]
        changed = False
        try:
            it = iter(todo)
            exhausted = False
            while not (exhausted and inflight == 0):
                if self.stop_ev.is_set():
                    break
                while self.pause_ev.is_set() and not self.stop_ev.is_set():
                    self._set(state="paused", message="Paused")
                    inflight -= self._drain(results, batch, source_id)
                    time.sleep(0.3)
                self._set(state="indexing", message=f"Indexing {self.s['source']}")
                while not exhausted and inflight < window and not self.pause_ev.is_set():
                    try:
                        p = next(it)
                    except StopIteration:
                        exhausted = True
                        break
                    pool.apply_async(process_file, (p,), callback=results.put,
                                     error_callback=lambda e, p=p: results.put({"path": p, "status": "error", "faces": [], "err": str(e)}))
                    inflight += 1
                try:
                    r = results.get(timeout=0.5)
                except queue.Empty:
                    continue
                inflight -= 1
                batch.append(r)
                if len(batch) >= 64 or time.time() - t_batch > 2:
                    changed |= self._commit(batch, source_id)
                    batch, t_batch = [], time.time()
                    el = max(1e-3, time.time() - t0)
                    rate = (self.s["done"] - done0) / el
                    left = self.s["total"] - self.s["done"]
                    self._set(rate=round(rate, 1), eta=int(left / rate) if rate > 0 else None)
            while True:
                try:
                    batch.append(results.get_nowait())
                except queue.Empty:
                    break
            changed |= self._commit(batch, source_id)
        finally:
            pool.terminate()
            pool.join()
            if changed:
                self._changed = True
                self.on_change()

    def _drain(self, results, batch, source_id):
        n = 0
        try:
            while True:
                batch.append(results.get_nowait())
                n += 1
        except queue.Empty:
            pass
        if batch:
            self._commit(batch, source_id)
            batch.clear()
        return n

    def _commit(self, batch, source_id):
        if not batch:
            return False
        c = db.conn()
        replaced = False
        for r in batch:
            st = r.get("status", "ok")
            if st == "error":
                self.s["errors"] += 1
            old = c.execute("SELECT id FROM photos WHERE path=?", (r["path"],)).fetchone()
            if old:
                c.execute("DELETE FROM faces WHERE photo_id=?", (old["id"],))
                replaced = True
            c.execute(
                "INSERT INTO photos(source_id,path,size,mtime,w,h,nfaces,status,tkey,taken,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, w=excluded.w, h=excluded.h, "
                "nfaces=excluded.nfaces, status=excluded.status, tkey=excluded.tkey, taken=excluded.taken, kind=excluded.kind",
                (source_id, r["path"], r.get("size", 0), r.get("mtime", 0), r.get("w", 0), r.get("h", 0), len(r["faces"]), st,
                 r.get("tkey"), r.get("taken"), r.get("kind", "photo")))
            pid = c.execute("SELECT id FROM photos WHERE path=?", (r["path"],)).fetchone()[0]
            if r["faces"]:
                c.executemany("INSERT INTO faces(photo_id,x,y,w,h,emb,q,t) VALUES(?,?,?,?,?,?,?,?)",
                              [(pid, *box, emb, q, t) for box, emb, q, t in r["faces"]])
                self.s["faces"] += len(r["faces"])
            self.s["done"] += 1
        c.commit()
        if replaced:
            self.on_change()
        return True
