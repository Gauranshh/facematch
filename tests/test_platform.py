"""Open sign-up, personal uploads, quotas, privacy between users, and account deletion."""
import io
import os
import time

from PIL import Image

from conftest import Client


def _jpeg(lib, name="ref.jpg"):
    return (io.BytesIO((lib / name).read_bytes()), name)


def _wait_indexed(env, n, who, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = who.get("/api/my/photos").get_json()
        if d["total"] >= n and not d["pending"]:
            return d
        time.sleep(0.5)
    raise AssertionError("upload was not indexed in time")


def _signup(env, name, consent=True):
    c = Client(env["app"])
    r = c.post("/api/auth/signup", json=dict(username=name, display=name.title(), password="a-long-password-1", consent=consent))
    return c, r


def test_signup_is_closed_by_default(env):
    assert env["anon"].get("/api/auth/state").get_json()["signup_open"] is False
    assert _signup(env, "nobody")[1].status_code == 403


def test_signup_needs_consent_and_a_good_password(env):
    env["admin"].post("/api/settings", json=dict(signup=True))
    assert env["anon"].get("/api/auth/state").get_json()["signup_open"] is True
    assert _signup(env, "kim", consent=False)[1].status_code == 400
    c = Client(env["app"])
    assert c.post("/api/auth/signup", json=dict(username="kim", password="short", consent=True)).status_code == 400
    assert c.post("/api/auth/signup", json=dict(username="admin", password="a-long-password-1", consent=True)).status_code == 400   # taken


def test_new_member_is_isolated_and_can_upload_and_search(env):
    lib = env["lib"]
    a, r = _signup(env, "ana")
    assert r.status_code == 200, r.get_json()
    me = a.get("/api/me").get_json()
    assert me["role"] == "member" and me["uploads"] is True
    assert a.get("/api/status").get_json()["library"]["photos"] == 0           # sees none of the shared folders

    up = a.post("/api/upload", data={"photos": [_jpeg(lib), _jpeg(lib)]}, content_type="multipart/form-data")
    assert up.status_code == 200 and up.get_json()["saved"] == 2
    d = _wait_indexed(env, 2, a)
    assert d["used"] > 0 and d["quota"] > 0

    with open(lib / "ref.jpg", "rb") as f:
        ref = a.post("/api/reference", data=dict(face=(f, "r.jpg")), content_type="multipart/form-data").get_json()["ref"]
    res = a.post("/api/search", json=dict(queries=[dict(ref=ref)])).get_json()
    assert res["count"] == 2 and res["photos_indexed"] == 2
    page = a.get(f"/api/search/{res['token']}/page").get_json()["items"]
    assert all(i["dir"] == "" for i in page)

    # another new member cannot see Ana's photos, thumbs or originals — and neither can members who may "see every folder"
    b, _ = _signup(env, "ben")
    ids = [i["id"] for i in page]
    for pid in ids:
        assert b.get(f"/thumb/{pid}").status_code == 404 and b.get(f"/photo/{pid}").status_code == 404
    with open(lib / "ref.jpg", "rb") as f:
        refb = b.post("/api/reference", data=dict(face=(f, "r.jpg")), content_type="multipart/form-data").get_json()["ref"]
    assert b.post("/api/search", json=dict(queries=[dict(ref=refb)])).get_json()["count"] == 0
    sam = Client(env["app"])
    assert sam.post("/api/auth/login", json=dict(username="sam", password="samspassword1")).status_code == 200
    uid = next(u["id"] for u in env["admin"].get("/api/admin/users").get_json()["users"] if u["username"] == "sam")
    env["admin"].patch(f"/api/admin/users/{uid}", json=dict(all_sources=True))
    assert sam.get(f"/thumb/{ids[0]}").status_code == 404
    env["admin"].patch(f"/api/admin/users/{uid}", json=dict(all_sources=False, sources=[env["ids"]["a"]]))
    assert env["admin"].get(f"/photo/{ids[0]}").status_code == 200                 # admins can see everything (stated in the privacy notice)


def test_discovered_groups_never_reveal_other_peoples_faces(env):
    srv, db = env["server"], env["db"]
    c = db.conn()
    own = [r[0] for r in c.execute("SELECT own_source FROM users WHERE username IN ('ana','ben')")]
    srv.start_discover()
    t0 = time.time()
    while srv.DISC["state"] == "running" and time.time() - t0 < 60:
        time.sleep(0.3)
    ben = Client(env["app"])
    assert ben.post("/api/auth/login", json=dict(username="ben", password="a-long-password-1")).status_code == 200
    for p in ben.get("/api/people?show=discovered").get_json()["people"]:
        assert ben.get(f"/api/people/{p['id']}/crop").status_code in (200, 404)
    assert own and len(own) == 2


def test_upload_rules(env):
    lib = env["lib"]
    a = Client(env["app"])
    assert a.post("/api/auth/login", json=dict(username="ana", password="a-long-password-1")).status_code == 200
    r = a.post("/api/upload", data={"photos": [(io.BytesIO(b"nope"), "fake.jpg"), (io.BytesIO(b"x"), "notes.txt")]}, content_type="multipart/form-data").get_json()
    assert r["saved"] == 0 and len(r["rejected"]) == 2
    # path tricks end up as plain file names inside the user's own folder
    ok = a.post("/api/upload", data={"photos": [(io.BytesIO((lib / "ref.jpg").read_bytes()), "../../evil.jpg")]}, content_type="multipart/form-data").get_json()
    assert ok["saved"] == 1
    root = env["db"].conn().execute("SELECT s.path FROM users u JOIN sources s ON s.id=u.own_source WHERE u.username='ana'").fetchone()[0]
    assert all(os.path.dirname(os.path.join(root, f)) == root for f in os.listdir(root))
    # quota
    env["admin"].post("/api/settings", json=dict(quota_mb=10))
    big = io.BytesIO()
    Image.effect_noise((2200, 2200), 90).convert("RGB").save(big, "JPEG", quality=100)
    r = a.post("/api/upload", data={"photos": [(io.BytesIO(big.getvalue()), "big1.jpg"), (io.BytesIO(big.getvalue()), "big2.jpg"), (io.BytesIO(big.getvalue()), "big3.jpg")]}, content_type="multipart/form-data").get_json()
    assert any("storage is full" in x["why"] for x in r["rejected"])
    env["admin"].post("/api/settings", json=dict(quota_mb=2048))


def test_signup_rate_limit(env):
    env["server"]._signup_hits.clear()
    codes = [Client(env["app"]).post("/api/auth/signup", json=dict(username=f"bot{i}", password="a-long-password-1", consent=True)).status_code for i in range(7)]
    assert 429 in codes
    env["server"]._signup_hits.clear()


def test_delete_own_photo_and_account(env):
    a = Client(env["app"])
    assert a.post("/api/auth/login", json=dict(username="ana", password="a-long-password-1")).status_code == 200
    d = a.get("/api/my/photos").get_json()
    assert d["total"] >= 2
    pid = d["items"][0]["id"]
    assert a.post("/api/my/photos/delete", json=dict(ids=[pid])).get_json()["deleted"] == 1
    assert a.post("/api/my/photos/delete", json=dict(ids=[1, 2, 3])).get_json()["deleted"] == 0       # other people's photos: untouched
    assert a.post("/api/me/delete", json=dict(password="wrong-password")).status_code == 400
    root = env["db"].conn().execute("SELECT s.path FROM users u JOIN sources s ON s.id=u.own_source WHERE u.username='ana'").fetchone()[0]
    assert os.path.isdir(root)
    assert a.post("/api/me/delete", json=dict(password="a-long-password-1")).status_code == 200
    assert a.get("/api/me").status_code == 401
    assert not os.path.exists(root)
    c = env["db"].conn()
    assert c.execute("SELECT COUNT(*) FROM users WHERE username='ana'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM photos WHERE path LIKE ?", (root + "%",)).fetchone()[0] == 0
    # shared library is unaffected
    assert env["admin"].get("/api/status").get_json()["library"]["photos"] >= 7


def test_last_admin_cannot_delete_themselves(env):
    assert env["admin"].post("/api/me/delete", json=dict(password="correct horse 1")).status_code in (400,)
