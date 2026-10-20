"""Live detection of new / changed / deleted photos using the operating system's file-change notifications
(inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows) via `watchdog`.
Falls back to polling when notifications are unavailable (network shares, inotify limit reached)."""
import logging
import os
import threading
import time

from . import db
from .core import IMAGE_EXTS, VIDEO_EXTS

log = logging.getLogger("facematch.watcher")
DEBOUNCE = float(os.environ.get("FACEMATCH_WATCH_DEBOUNCE", "6"))     # seconds of quiet before a file is picked up
POLL_EVERY = 60


class Watcher:
    def __init__(self, indexer):
        self.indexer = indexer
        self.obs = {}          # source_id -> (observer, mode)
        self.pending = {}      # source_id -> {path: last_event_ts}
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.thread = None

    # ---- lifecycle
    def start(self):
        self.stop_ev.clear()
        if db.get_setting("watch", "1") == "1":
            for r in db.conn().execute("SELECT id, path FROM sources"):
                self.watch(r["id"], r["path"])
        self.thread = threading.Thread(target=self._loop, daemon=True, name="watcher")
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        for sid in list(self.obs):
            self.unwatch(sid)

    def mode(self, sid):
        o = self.obs.get(sid)
        return o[1] if o else "off"

    def watch(self, sid, path):
        if sid in self.obs or not os.path.isdir(path):
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
            from watchdog.observers.polling import PollingObserver
        except Exception:
            log.warning("watchdog is not installed; live detection disabled")
            return
        outer = self

        class H(FileSystemEventHandler):
            def on_any_event(self, ev):
                if ev.is_directory:
                    return
                for p in {getattr(ev, "src_path", None), getattr(ev, "dest_path", None)}:
                    if p:
                        outer._event(sid, os.fsdecode(p))

        for cls, mode, kw in ((Observer, "live", {}), (PollingObserver, "poll", {"timeout": POLL_EVERY})):
            try:
                o = cls(**kw)
                o.schedule(H(), path, recursive=True)
                o.daemon = True
                o.start()
                self.obs[sid] = (o, mode)
                log.info("watching %s (%s)", path, mode)
                return
            except Exception as e:  # noqa: BLE001  (e.g. inotify watch limit)
                log.warning("cannot watch %s with %s: %s", path, mode, e)

    def unwatch(self, sid):
        o = self.obs.pop(sid, None)
        if o:
            try:
                o[0].stop()
                o[0].join(2)
            except Exception:
                pass
        self.pending.pop(sid, None)

    def set_enabled(self, on):
        if on:
            for r in db.conn().execute("SELECT id, path FROM sources"):
                self.watch(r["id"], r["path"])
        else:
            for sid in list(self.obs):
                self.unwatch(sid)

    # ---- events -> debounced batches -> indexer
    def _event(self, sid, path):
        ext = os.path.splitext(path)[1].lower()
        if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
            return
        if os.path.basename(path).startswith("."):
            return
        with self.lock:
            self.pending.setdefault(sid, {})[path] = time.time()

    def _loop(self):
        while not self.stop_ev.wait(2):
            now = time.time()
            ready = {}
            with self.lock:
                for sid, d in self.pending.items():
                    for p, t in list(d.items()):
                        if now - t >= DEBOUNCE:
                            ready.setdefault(sid, []).append(p)
                            del d[p]
            for sid, paths in ready.items():
                stable = []
                for p in paths:                       # a file that is still being copied keeps growing: wait for it
                    try:
                        a = os.stat(p).st_size
                        time.sleep(0.5)
                        b = os.stat(p).st_size
                    except OSError:
                        stable.append(p)               # deleted/moved away: the indexer handles it
                        continue
                    if a == b:
                        stable.append(p)
                    else:
                        self._event(sid, p)
                if stable:
                    log.info("live update: %d file(s)", len(stable))
                    self.indexer.add_paths(sid, stable)
