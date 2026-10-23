"""A public instance: probes can't be steered off the app, sessions and pairing are bounded."""

import asyncio
import importlib

import httpx
from fastapi.testclient import TestClient

from liara.liara_api import client as client_mod

ORIGIN = {"origin": "http://testserver"}


def test_probe_never_follows_a_redirect_off_the_app(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        if req.url.host == "evil.liara.run":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        return httpx.Response(200, text="ok")

    real = httpx.AsyncClient
    monkeypatch.setattr(client_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = asyncio.run(client_mod.probe("https://evil.liara.run/", n=2))
    assert [r["status"] for r in out] == [302, 302] and "169.254.169.254" not in seen


def test_session_opening_is_rate_limited_per_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "demo")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    codes = [TestClient(app_mod.app).post("/api/session", json={"token": ""}, headers=ORIGIN).status_code
             for _ in range(app_mod.SESSIONS_PER_IP_10_MIN + 1)]
    assert codes[:-1] == [200] * app_mod.SESSIONS_PER_IP_10_MIN and codes[-1] == 429
    page = TestClient(app_mod.app)
    app_mod.session_opens.clear()
    page.post("/api/session", json={"token": ""}, headers=ORIGIN)
    assert len(page.post("/api/pair", headers=ORIGIN).json()["code"]) == 8
    r = page.post("/api/bridge/deploy?app_name=shop-code-bug", content=b"x")
    assert r.status_code == 401                       # not paired; and a paired demo session can't upload either
