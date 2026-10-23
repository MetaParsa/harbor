"""The watcher: background health + resource checks, one alert when a problem starts and one when it clears."""

import asyncio
import importlib

from fastapi.testclient import TestClient

ORIGIN = {"origin": "http://testserver"}


def demo_page(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "demo")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    page = TestClient(app_mod.app)
    page.post("/api/session", json={"token": "any"}, headers=ORIGIN)
    return app_mod, page, next(iter(app_mod.sessions.values()))


def test_watcher_flags_each_recorded_fault_then_clears_it_after_the_fix(tmp_path, monkeypatch):
    from liara.sre import watcher
    app_mod, page, s = demo_page(tmp_path, monkeypatch)
    asyncio.run(watcher.run_once(s))
    st = page.get("/api/watch").json()
    problems = {app: r["problems"] for app, r in st["apps"].items()}
    assert any("۵xx" in p and "/checkout" in p for p in problems["shop-missing-env"])
    assert any("۵xx" in p for p in problems["shop-code-bug"])
    assert any("OOM" in p for p in problems["shop-memory-leak"])
    assert len(st["alerts"]) == sum(len(p) for p in problems.values()) and not any(a["ok"] for a in st["alerts"])
    asyncio.run(watcher.run_once(s))                                 # same state: no repeated alerts
    assert len(page.get("/api/watch").json()["alerts"]) == len(st["alerts"])

    board = page.post("/api/troubleshoot", json={"app": "shop-memory-leak", "text": "قطع می‌شه"}, headers=ORIGIN).json()
    fix = page.post(f"/api/incidents/{board['incident']}/fix", headers=ORIGIN,
                    json={"hypothesis": "P3", "option": 0, "board_id": board["id"]}).json()
    act = next(a for a in page.get("/api/actions").json()["actions"] if a["id"] == fix["pending_action_id"])
    page.post(f"/api/actions/{act['id']}/approve", json={"args_hash": act["args_hash"]}, headers=ORIGIN)
    asyncio.run(watcher.run_once(s))
    after = page.get("/api/watch").json()
    assert after["apps"]["shop-memory-leak"]["problems"] == []
    assert any(a["ok"] and a["app"] == "shop-memory-leak" and "برطرف شد" in a["text"] for a in after["alerts"])


def test_watcher_needs_the_page_session(tmp_path, monkeypatch):
    _, page, _ = demo_page(tmp_path, monkeypatch)
    assert TestClient(page.app).get("/api/watch").status_code == 401


def test_hosted_mode_empty_key_is_judge_mode_and_a_key_is_live(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "auto")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    page = TestClient(app_mod.app)
    assert page.get("/health").json()["both"] is True
    assert page.post("/api/session", json={"token": ""}, headers=ORIGIN).json()["replay"] is True
    assert next(iter(app_mod.sessions.values())).demo is not None
    live = TestClient(app_mod.app)

    class NoNetwork(Exception):
        pass

    async def refuse(*_a, **_k):
        raise NoNetwork
    monkeypatch.setattr(app_mod.LiaraClient, "teams", refuse)
    try:
        live.post("/api/session", json={"token": "a-real-looking-key"}, headers=ORIGIN)
    except NoNetwork:
        pass                                    # a real key goes to the real Liara API (stopped here: no network)
    else:
        raise AssertionError("a real key must open a live client")
