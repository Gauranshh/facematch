"""End to end: index real images, search, and make sure a restricted member never sees the other folder."""
import os


def _m(env):
    """A freshly signed-in member (other tests may have signed the shared one out)."""
    c = type(env["admin"])(env["app"])
    assert c.post("/api/auth/login", json=dict(username="sam", password="samspassword1")).status_code == 200
    return c


def _ref(client, lib):
    with open(lib / "ref.jpg", "rb") as f:
        r = client.post("/api/reference", data=dict(face=(f, "ref.jpg")), content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["ref"]


def _search(client, ref, **kw):
    r = client.post("/api/search", json=dict(queries=[dict(ref=ref)], **kw))
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    items = client.get(f"/api/search/{d['token']}/page?limit=200").get_json()["items"]
    return d, items


def test_library_indexed(env):
    st = env["admin"].get("/api/status").get_json()["library"]
    assert st["photos"] == 7 and st["faces"] >= 6


def test_admin_finds_all_matching_photos(env):
    ref = _ref(env["admin"], env["lib"])
    d, items = _search(env["admin"], ref)
    names = {i["name"] for i in items}
    assert d["count"] == 6 and not any("coffee" in n for n in names)
    assert any(n.startswith("SECRET") for n in names)


def test_member_only_sees_allowed_folder(env):
    m = _m(env)
    ref = _ref(m, env["lib"])
    d, items = _search(m, ref)
    assert d["count"] == 4 and all(i["name"].startswith("IMG_") for i in items)
    assert all(i["dir"] == "" for i in items)                       # server paths are not revealed
    assert m.get("/api/status").get_json()["library"]["photos"] == 5
    # asking for the forbidden folder explicitly returns nothing rather than leaking
    d2, _ = _search(m, ref, sources=[env["ids"]["b"]])
    assert d2["count"] == 0


def test_member_cannot_fetch_forbidden_files(env):
    c = env["db"].conn()
    secret = c.execute("SELECT id FROM photos WHERE path LIKE '%SECRET_0%'").fetchone()[0]
    ok = c.execute("SELECT id FROM photos WHERE path LIKE '%IMG_0%'").fetchone()[0]
    assert _m(env).get(f"/thumb/{secret}").status_code == 404
    assert _m(env).get(f"/photo/{secret}").status_code == 404
    assert _m(env).get(f"/thumb/{ok}").status_code == 200
    assert env["admin"].get(f"/photo/{secret}").status_code == 200


def test_result_tokens_belong_to_one_user(env):
    ref = _ref(env["admin"], env["lib"])
    tok = env["admin"].post("/api/search", json=dict(queries=[dict(ref=ref)])).get_json()["token"]
    m = _m(env)
    assert m.get(f"/api/search/{tok}/page").status_code == 410      # results are private to the user who searched
    r = m.post("/api/search", json=dict(queries=[dict(ref=ref)]))   # nor can an upload id be borrowed
    assert r.status_code == 400


def test_export_permission_and_zip(env):
    a, m = env["admin"], _m(env)
    ref = _ref(m, env["lib"])
    tok = m.post("/api/search", json=dict(queries=[dict(ref=ref)])).get_json()["token"]
    r = m.post(f"/api/search/{tok}/zip", data=dict(all="1", layout="flat"))
    assert r.status_code == 200 and r.data[:2] == b"PK"
    uid = next(u["id"] for u in a.get("/api/admin/users").get_json()["users"] if u["username"] == "sam")
    a.patch(f"/api/admin/users/{uid}", json=dict(can_export=False))
    assert m.post(f"/api/search/{tok}/zip", data=dict(all="1")).status_code == 403
    assert m.post(f"/api/search/{tok}/share", json=dict(all=True)).status_code == 403
    a.patch(f"/api/admin/users/{uid}", json=dict(can_export=True))


def test_people_are_private_and_pool_is_scoped(env):
    a, m = env["admin"], _m(env)
    ref = _ref(a, env["lib"])
    assert a.post("/api/people", json=dict(ref=ref, name="Alice")).status_code == 200
    assert [p["name"] for p in a.get("/api/people?show=named").get_json()["people"]] == ["Alice"]
    assert m.get("/api/people?show=named").get_json()["people"] == []
    pid = a.get("/api/people?show=named").get_json()["people"][0]["id"]
    assert m.get(f"/api/people/{pid}/crop").status_code == 404
    assert m.patch(f"/api/people/{pid}", json=dict(name="hacked")).status_code == 404
    assert m.delete(f"/api/people/{pid}").status_code == 404
    assert a.get("/api/people?show=named").get_json()["people"][0]["nphotos"] == 6
    # a member searching "by person" with someone else's id gets a clean error, not their data
    r = m.post("/api/search", json=dict(queries=[dict(person=pid)]))
    assert r.status_code in (400, 404)


def test_share_links(env):
    m, anon = _m(env), env["anon"]
    ref = _ref(m, env["lib"])
    tok = m.post("/api/search", json=dict(queries=[dict(ref=ref)])).get_json()["token"]
    r = m.post(f"/api/search/{tok}/share", json=dict(all=True, days=1, title="Trip"))
    assert r.status_code == 200
    s = r.get_json()
    meta = anon.get(f"/api/public/share/{s['token']}").get_json()
    assert meta["title"] == "Trip" and len(meta["items"]) == 4
    pid = meta["items"][0]["id"]
    assert anon.get(f"/s/{s['token']}/thumb/{pid}").status_code == 200
    assert anon.get(f"/s/{s['token']}/photo/{pid}").status_code == 200
    secret = env["db"].conn().execute("SELECT id FROM photos WHERE path LIKE '%SECRET_0%'").fetchone()[0]
    assert anon.get(f"/s/{s['token']}/photo/{secret}").status_code == 404       # only the shared photos
    assert anon.get(f"/s/{s['token']}/zip").data[:2] == b"PK"
    assert anon.get("/api/public/share/not-a-token").status_code == 404
    assert env["admin"].delete(f"/api/shares/{s['token']}").status_code == 200
    assert anon.get(f"/api/public/share/{s['token']}").status_code == 404        # revoked
    assert anon.get(f"/s/{s['token']}/photo/{pid}").status_code == 404


def test_audit_log_records_security_events(env):
    ev = {e["event"] for e in env["admin"].get("/api/admin/audit?limit=200").get_json()["events"]}
    assert {"setup", "login", "user_created", "share_created", "share_revoked", "export_zip"} <= ev
    assert os.path.exists(env["db"].DB_PATH)
