import pytest

from facematch import auth


def test_password_hash_roundtrip_and_rules():
    h = auth.hash_password("correct horse 1")
    assert h.startswith("scrypt$") and "correct horse" not in h
    assert auth.verify_password("correct horse 1", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")
    with pytest.raises(auth.AuthError):
        auth.check_new_password("short")


def test_every_api_needs_login(env):
    for url in ("/api/status", "/api/people", "/api/me", "/api/shares", "/thumb/1", "/photo/1", "/api/admin/users"):
        assert env["anon"].get(url).status_code == 401, url
    assert env["anon"].get("/healthz").status_code == 200
    assert env["anon"].get("/").status_code == 200


def test_cross_site_posts_are_rejected(env):
    r = env["admin"].c.post("/api/me/password", json={}, headers={"Origin": "https://evil.example"}, base_url="http://localhost")
    assert r.status_code == 403


def test_security_headers(env):
    r = env["anon"].get("/")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"


def test_setup_cannot_run_twice(env):
    r = env["anon"].post("/api/auth/setup", json=dict(username="evil", password="correct horse 1"))
    assert r.status_code == 409


def test_member_is_not_admin(env):
    m = env["member"]
    for url in ("/api/admin/users", "/api/admin/overview", "/api/admin/audit", "/api/fs"):
        assert m.get(url).status_code == 403, url
    assert m.post("/api/sources", json=dict(path="/tmp")).status_code == 403
    assert m.post("/api/index/start", json={}).status_code == 403
    assert m.post("/api/settings", json=dict(sharing=False)).status_code == 403
    assert m.post("/api/model", json=dict(model="arcface")).status_code == 403


def test_login_logout_and_disabled_account(env):
    a = env["admin"]
    c = type(a)(env["app"])
    assert c.post("/api/auth/login", json=dict(username="SAM", password="samspassword1")).status_code == 200   # case-insensitive
    assert c.get("/api/me").get_json()["username"] == "sam"
    c.post("/api/auth/logout")
    assert c.get("/api/me").status_code == 401
    uid = next(u["id"] for u in a.get("/api/admin/users").get_json()["users"] if u["username"] == "sam")
    c2 = type(a)(env["app"])
    assert c2.post("/api/auth/login", json=dict(username="sam", password="samspassword1")).status_code == 200
    a.patch(f"/api/admin/users/{uid}", json=dict(disabled=True))
    assert c2.get("/api/me").status_code == 401           # existing sessions die immediately
    assert type(a)(env["app"]).post("/api/auth/login", json=dict(username="sam", password="samspassword1")).status_code == 401
    a.patch(f"/api/admin/users/{uid}", json=dict(disabled=False))


def test_cannot_demote_or_delete_last_admin(env):
    a = env["admin"]
    assert a.patch("/api/admin/users/1", json=dict(role="member")).status_code == 400
    assert a.delete("/api/admin/users/1").status_code == 400


def test_password_change_signs_out_other_devices(env):
    a = env["admin"]
    other = type(a)(env["app"])
    assert other.post("/api/auth/login", json=dict(username="admin", password="correct horse 1")).status_code == 200
    assert a.post("/api/me/password", json=dict(old="nope", new="another pass 22")).status_code == 400
    assert a.post("/api/me/password", json=dict(old="correct horse 1", new="correct horse 1")).status_code == 200
    assert other.get("/api/me").status_code == 401
    assert a.get("/api/me").status_code == 200


def test_login_throttle_locks_out(env):
    c = type(env["admin"])(env["app"])
    codes = [c.post("/api/auth/login", json=dict(username="ghost", password="x" * 9)).status_code for _ in range(7)]
    assert 429 in codes and codes[0] == 401
    auth._fails.clear()
