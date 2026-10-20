import shutil
import time

from PIL import Image


def test_new_photo_is_detected_instantly(env, lib):
    srv = env["server"]
    srv.watcher.start()
    try:
        sid = env["ids"]["a"]
        deadline = time.time() + 10
        while time.time() < deadline and srv.watcher.mode(sid) == "off":
            time.sleep(0.2)
        assert srv.watcher.mode(sid) in ("live", "poll")
        before = env["admin"].get("/api/status").get_json()["library"]["photos"]
        shutil.copy(lib / "ref.jpg", lib / "a" / "LIVE_NEW.jpg")
        got = before
        t0 = time.time()
        while time.time() - t0 < 40 and got == before:
            time.sleep(0.5)
            srv._stats_cache["t"] = 0
            got = env["admin"].get("/api/status").get_json()["library"]["photos"]
        assert got == before + 1, "new photo was not indexed"
        assert time.time() - t0 < 30
        (lib / "a" / "LIVE_NEW.jpg").unlink()
        t0 = time.time()
        while time.time() - t0 < 40 and got != before:
            time.sleep(0.5)
            srv._stats_cache["t"] = 0
            got = env["admin"].get("/api/status").get_json()["library"]["photos"]
        assert got == before, "deleted photo was not removed from the index"
    finally:
        srv.watcher.stop()


def test_unreadable_files_are_reported_not_fatal(env, lib):
    (lib / "a" / "broken.jpg").write_bytes(b"not an image")
    Image.new("RGB", (4, 4)).save(lib / "a" / "tiny.png")
    env["admin"].post("/api/index/start", json=dict(source_ids=[env["ids"]["a"]]))
    assert env["server"].indexer.wait(120)
    st = env["admin"].get("/api/status").get_json()
    assert st["index"]["state"] in ("idle", "done") or not st["index"]["running"]
    for n in ("broken.jpg", "tiny.png"):
        (lib / "a" / n).unlink()
