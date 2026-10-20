"""Shared fixtures: one throw-away data directory and a small public-domain photo library per test session."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="facematch-test-")
os.environ["FACEMATCH_DATA"] = str(Path(_tmp) / "data")
os.environ["FACEMATCH_NO_BROWSER"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

ORIGIN = {"Origin": "http://localhost"}


class Client:
    """Thin wrapper around Flask's test client that sends the Origin header the CSRF check expects."""

    def __init__(self, app):
        self.c = app.test_client()

    def _kw(self, kw):
        kw.setdefault("headers", {}).update(ORIGIN)
        kw.setdefault("base_url", "http://localhost")
        return kw

    def get(self, url, **kw): return self.c.get(url, **self._kw(kw))
    def post(self, url, **kw): return self.c.post(url, **self._kw(kw))
    def patch(self, url, **kw): return self.c.patch(url, **self._kw(kw))
    def delete(self, url, **kw): return self.c.delete(url, **self._kw(kw))


@pytest.fixture(scope="session")
def lib(tmp_path_factory):
    """Photos: 4 of the same face in folder 'a', 2 in folder 'b' (private), and 1 face-less picture."""
    from PIL import Image, ImageEnhance, ImageOps
    from skimage import data
    root = tmp_path_factory.mktemp("photos")
    (root / "a").mkdir(); (root / "b").mkdir()
    face = Image.fromarray(data.astronaut())
    for i in range(4):
        im = ImageOps.mirror(face) if i % 2 else face
        ImageEnhance.Brightness(im).enhance(0.8 + 0.1 * i).save(root / "a" / f"IMG_{i}.jpg")
    for i in range(2):
        face.resize((600, 600)).save(root / "b" / f"SECRET_{i}.jpg")
    Image.fromarray(data.coffee()).save(root / "a" / "coffee.jpg")
    face.save(root / "ref.jpg")
    return root


@pytest.fixture(scope="session")
def env(lib):
    """A running app with an admin, a member limited to folder 'a', and everything indexed."""
    from facematch import auth, db, server
    app = server.init_app(start_threads=False)
    admin = Client(app)
    r = admin.post("/api/auth/setup", json=dict(username="admin", display="Admin", password="correct horse 1"))
    assert r.status_code == 200, r.get_json()
    ids = {}
    for name in ("a", "b"):
        r = admin.post("/api/sources", json=dict(path=str(lib / name)))
        assert r.status_code == 200, r.get_json()
        ids[name] = r.get_json()["id"]
    assert server.indexer.wait(180)
    r = admin.post("/api/admin/users", json=dict(username="sam", display="Sam", role="member", all_sources=False, sources=[ids["a"]]))
    assert r.status_code == 200
    invite = r.get_json()["invite"].split("/")[-1]
    member = Client(app)
    r = member.post(f"/api/auth/invite/{invite}", json=dict(password="samspassword1"))
    assert r.status_code == 200, r.get_json()
    yield dict(app=app, admin=admin, member=member, anon=Client(app), ids=ids, lib=lib, server=server, db=db, auth=auth)
    server.shutdown()
