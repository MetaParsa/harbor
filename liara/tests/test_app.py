"""Web routes: approvals need this session's cookie and a same-origin request (test_mcp_cannot_approve)."""

import importlib

import pytest
from fastapi.testclient import TestClient

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "d_fixed")
    monkeypatch.setenv("LIARA_DEV", "1")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LIARA_AGENT_KILL", raising=False)
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    return app_mod, TestClient(app_mod.app)


def connect(c):
    r = c.post("/api/session", json={"token": "replay-token-123"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["team"] == "hackathon"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    return r


def propose(c):
    r = c.post("/api/dev/propose", json={"tool": "restart", "args": {"app": "l0-dyar-6b46"}}, headers=ORIGIN)
    assert r.json()["status"] == "waiting_for_user"
    act = c.get("/api/actions").json()["actions"][0]
    assert "args" not in act and act["args_hash"]
    return act


def test_mcp_cannot_approve(web):
    app_mod, c = web
    connect(c)
    act = propose(c)
    outsider = TestClient(app_mod.app)                     # a coding agent / curl: knows the id and hash, no cookie
    assert outsider.post(f"/api/actions/{act['id']}/approve", json={"args_hash": act["args_hash"]},
                         headers=ORIGIN).status_code == 401
    # even with the cookie, a request without the page's Origin (a script) is refused
    assert c.post(f"/api/actions/{act['id']}/approve", json={"args_hash": act["args_hash"]}).status_code == 403
    assert c.post(f"/api/actions/{act['id']}/approve", json={"args_hash": act["args_hash"]},
                  headers={"origin": "http://evil.example"}).status_code == 403
    ok = c.post(f"/api/actions/{act['id']}/approve", json={"args_hash": act["args_hash"]}, headers=ORIGIN)
    assert ok.json()["status"] == "done"


def test_token_kept_in_memory_only(web, tmp_path):
    app_mod, c = web
    connect(c)
    propose(c)
    on_disk = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in tmp_path.rglob("*") if p.is_file())
    assert "replay-token-123" not in on_disk
    assert "replay-token-123" not in c.get("/api/log").text


def test_dev_route_off_by_default(web, monkeypatch):
    app_mod, c = web
    connect(c)
    monkeypatch.setenv("LIARA_DEV", "0")
    assert c.post("/api/dev/propose", json={"tool": "restart", "args": {}}).status_code == 404


def test_page_and_health(web):
    _, c = web
    page = c.get("/")
    assert "دستیار دیپلوی" in page.text and "<script>" not in page.text      # no inline script: CSP blocks it
    assert page.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "EventSource" in c.get("/app.js").text
    assert c.get("/health").json() == {"ok": True, "killed": False, "demo": False, "both": False}   # no counters for strangers


def test_login_needs_page_origin(web):
    _, c = web
    assert c.post("/api/session", json={"token": "x-token-1234"}).status_code == 403
    assert c.post("/api/session", json={"token": "x-token-1234"}, headers={"origin": "http://evil.example"}).status_code == 403


def test_busy_session_is_not_swept(web):
    import asyncio
    import time

    app_mod, c = web
    connect(c)
    sid = c.cookies.get("lsid")
    s = app_mod.sessions[sid]
    s.last_seen = time.time() - app_mod.SESSION_IDLE_S - 60
    s.subscribers.append(asyncio.Queue())            # an open page
    app_mod.get_session("someone-else")
    assert sid in app_mod.sessions
    s.subscribers.clear()
    app_mod.get_session("someone-else")
    assert sid not in app_mod.sessions


def test_unknown_paths_and_malformed_bodies_answer_in_persian(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    import importlib

    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    from fastapi.testclient import TestClient
    c = TestClient(app_mod.app)
    r = c.get("/api/apps/a%2Fb/overview")
    assert r.status_code == 404 and "وجود نداره" in r.json()["message"]
    r = c.post("/api/session", json={"nope": 1}, headers={"origin": "http://testserver"})
    assert r.status_code == 422 and "token" in r.json()["fields"] and "نادرسته" in r.json()["message"]
