"""The page's routes (home card, ticket) and the gaps found in the first live code-fix run with Claude Code.
REPLAY only, no credit."""

import importlib

import pytest
from fastapi.testclient import TestClient

from liara.agent.incident import Incident
from liara.agent.policy import Session
from liara.agent.redact import Redactor
from liara.liara_api import LabelReplay, LiaraClient
from liara.mcp import liara_sre_mcp as mcp
from liara.sre.engine import troubleshoot

pytestmark = pytest.mark.anyio
ORIGIN = {"origin": "http://testserver"}
APP = "l0-dyar-6b46"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "d_fixed")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LIARA_AGENT_KILL", raising=False)

    async def no_network(url, n=5, **_):
        return [{"status": 500, "ms": 50}] * n

    import liara.sre.collect as collect
    monkeypatch.setattr(collect, "probe", no_network)
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    page = TestClient(app_mod.app)
    assert page.post("/api/session", json={"token": "replay-token-123"}, headers=ORIGIN).status_code == 200
    return app_mod, page


def test_app_list_and_home_card(web):
    _, page = web
    apps = page.get("/api/apps").json()["apps"]
    assert apps and apps[0]["name"] == APP
    o = page.get(f"/api/apps/{APP}/overview").json()
    assert o["plan"] == "small-g2" and o["plan_ram_gb"] == 0.5 and o["price_month"] == round(1041.66 * 720)
    assert o["ram_pct"] is not None and 0 < o["ram_pct"] < 100 and o["last_release"]["tag"]
    assert page.get("/api/apps/..%2Fx/overview").status_code in (400, 404)


def test_bridge_deploy_adopts_the_boards_code_fix_and_card_states_the_contract(web):
    app_mod, page = web
    code = page.post("/api/pair", headers=ORIGIN).json()["code"]
    token = TestClient(app_mod.app).post("/api/bridge/pair", json={"code": code}).json()["token"]
    s = next(iter(app_mod.sessions.values()))
    inc = Incident(APP, "پرداخت خطا می‌ده")
    inc.move("diagnosing")
    inc.move("waiting_user")
    contract = {"route": "/checkout", "probes": 5, "max_status": 399, "log_window_s": 120, "new_container": True,
                "memory_flat": False}
    inc.board = {"headline": "خطا با نسخه‌ی v8 شروع شده", "recommended": {"hypothesis": "P1", "option": 0},
                 "hypotheses": [{"id": "P1", "fixes": [{"kind": "coding_agent", "contract": contract}]}]}
    s.incidents[inc.id] = inc
    data = mcp.pack(__import__("pathlib").Path(__file__).resolve().parents[1] / "demo-apps" / "demo-shop" / "v1")[0]
    res = TestClient(app_mod.app).post(f"/api/bridge/deploy?app_name={APP}", content=data,
                                       headers={"Authorization": f"Bearer {token}"}).json()
    assert res["status"] == "waiting_for_user"
    assert inc.fix["hypothesis"] == "P1" and inc.fix["tool"] == "deploy" and inc.state == "waiting_coding_agent"
    act = s.actions[res["pending_action_id"]]
    assert "/checkout" in act.card.success and "۵ از ۵" in act.card.success       # the diagnosis's own test
    assert "خطا با نسخه‌ی v8" in act.why
    assert "۲۰" in act.card.cost and "20" not in act.card.cost                      # Persian digits


async def test_repeated_crash_is_one_evidence_line_with_a_count():
    t = LabelReplay("v2_regression")
    s = Session("s", LiaraClient("tok", transport=t), Redactor())

    async def prober(url, n):
        return [{"status": 500, "ms": 60}] * n

    board = await troubleshoot(s, "dastyar-demo-shop", "پرداخت خطا می‌ده", prober=prober, now=t.summary()["at"])
    errors = [e["text"] for e in board["evidence"] if e["kind"] == "error"]
    assert len(errors) == len(set(errors)) and any("بار" in e for e in errors)
    files = board["hypotheses"][0]["files"]
    assert len(files) == len({(f["path"], f["line"]) for f in files}) == 1
    assert board["id"] and len(board["id"]) == 12


def test_mcp_tells_the_agent_to_re_pair_after_a_restart():
    srv = mcp.Server(mcp.Backend("http://127.0.0.1:9"))
    srv.backend.token = "stale-token"
    srv.tool_check_capability = lambda a: {"status": "error", "http": 401, "error": "not_paired"}
    res = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "check_capability", "arguments": {"name": "x"}}})
    text = res["result"]["content"][0]["text"]
    assert res["result"]["isError"] and "new code" in text and srv.backend.token is None


def test_page_has_the_one_screen_parts(web):
    _, page = web
    html = page.get("/").text
    for part in ("hRamBar", "hCpuBar", "checkBtn", "pairBtn", "askText", "feed", "banner"):
        assert f'id="{part}"' in html
    js = page.get("/app.js").text
    assert "innerHTML" not in js                       # untrusted text only ever goes in as textContent


# ---------- L5 review fixes ----------

async def test_old_board_is_refused_and_errors_have_messages(tmp_path):
    from liara.agent.budget import Budget
    from liara.agent.policy import ActionLog, Policy
    from liara.sre.engine import propose_fix
    t = LabelReplay("v2_regression")
    s = Session("s", LiaraClient("tok", transport=t), Redactor())

    async def prober(url, n):
        return [{"status": 500, "ms": 60}] * n

    b1 = await troubleshoot(s, "dastyar-demo-shop", "پرداخت خطا می‌ده", prober=prober, now=t.summary()["at"])
    b2 = await troubleshoot(s, "dastyar-demo-shop", "پرداخت خطا می‌ده", prober=prober, now=t.summary()["at"])
    pol = Policy(Budget(tmp_path), ActionLog(tmp_path / "a.jsonl"))
    old = await propose_fix(pol, s, b2["incident"], "P1", 0, board_id=b1["id"])
    assert old["error"] == "old_board" and old["message"]
    res = await propose_fix(pol, s, b2["incident"], "P1", 0, board_id=b2["id"])
    assert res["status"] == "handoff" and "server.js line 37" in res["brief"]       # brief of the chosen cause
    for bad in (await propose_fix(pol, s, "inc-nope", "P1", 0), await propose_fix(pol, s, b2["incident"], "P9", 0)):
        assert bad["status"] == "error" and bad["message"]


def test_coding_agent_deploy_does_not_hijack_an_env_fix():
    from liara.sre.engine import adopt_code_fix
    inc = Incident(APP, "x")
    inc.move("diagnosing")
    inc.move("waiting_user")
    inc.board = {"recommended": {"hypothesis": "P2", "option": 0}, "hypotheses": [
        {"id": "P2", "fixes": [{"kind": "action", "tool": "set_envs", "contract": {"route": "/"}}]},
        {"id": "P1", "fixes": [{"kind": "coding_agent", "contract": {"route": "/checkout"}}]}]}
    inc.fix = {"hypothesis": "P2", "option": 0, "tool": "set_envs", "contract": {"route": "/"}, "action_id": "a"}
    assert adopt_code_fix(inc) is None and inc.fix["tool"] == "set_envs"
    inc.fix = None
    assert adopt_code_fix(inc)["hypothesis"] == "P1"


def test_ticket_lists_none_when_nothing_was_tried():
    from liara.sre.engine import support_ticket
    s = Session("s", LiaraClient("tok"), Redactor())
    inc = Incident(APP, "x")
    inc.board = {"headline": "h", "evidence": [], "ruled_out": []}
    s.incidents[inc.id] = inc
    text = support_ticket(s, APP)["text"]
    assert "کارهای امتحان‌شده:\n  - هیچ" in text


def test_overview_keeps_the_app_name_even_if_a_secret_contains_it(web):
    app_mod, page = web
    s = next(iter(app_mod.sessions.values()))
    s.redactor.add(APP)                                 # e.g. APP_NAME=<app name> stored as an env value
    o = page.get(f"/api/apps/{APP}/overview").json()
    assert o["name"] == APP and o["url"].endswith(".liara.run")
    assert page.get("/api/actions").json()["bridge"] is False


def test_judge_mode_answers_three_recorded_scenarios_and_refuses_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "demo")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LIARA_DEV", "1")
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    page = TestClient(app_mod.app)
    assert page.post("/api/session", json={"token": "any"}, headers=ORIGIN).json()["replay"] is True
    assert [a["name"] for a in page.get("/api/apps").json()["apps"]] == ["shop-missing-env", "shop-code-bug", "shop-memory-leak"]
    tops = {app: page.post("/api/troubleshoot", json={"app": app, "text": text}, headers=ORIGIN).json()["hypotheses"][0]["id"]
            for app, text in [("shop-missing-env", "پرداخت خطا می‌ده"), ("shop-code-bug", "پرداخت خطا می‌ده"),
                              ("shop-memory-leak", "کند شده")]}
    assert tops == {"shop-missing-env": "P2", "shop-code-bug": "P1", "shop-memory-leak": "P3"}
    card = page.post("/api/apps/shop-code-bug/zero-downtime", headers=ORIGIN).json()
    act = next(a for a in page.get("/api/actions").json()["actions"] if a["id"] == card["pending_action_id"])
    res = page.post(f"/api/actions/{act['id']}/approve", json={"args_hash": act["args_hash"]}, headers=ORIGIN).json()
    assert res["status"] == "failed" and "نمایش ضبط‌شده" in res["message"]


def test_judge_approves_the_recorded_fix_and_sees_a_recorded_before_after(tmp_path, monkeypatch):
    """Judge mode: the judge is the human — approve the card, the write is simulated (said on the card), and the
    verify runs the real verify() over a real after-recording; nothing else can be written, live caps untouched."""
    monkeypatch.setenv("LIARA_REPLAY", "demo")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    page = TestClient(app_mod.app)
    page.post("/api/session", json={"token": "any"}, headers=ORIGIN)
    for app, text, (hyp, opt), route in [("shop-missing-env", "پرداخت خطا می‌ده", ("P2", 0), "/checkout"),
                                         ("shop-code-bug", "پرداخت خطا می‌ده", ("P1", 1), "/checkout"),
                                         ("shop-memory-leak", "سایت قطع می‌شه", ("P3", 0), "/")]:
        board = page.post("/api/troubleshoot", json={"app": app, "text": text}, headers=ORIGIN).json()
        values = {"PAYMENT_API_URL": "https://pay.example.ir/api"} if hyp == "P2" else None
        fix = page.post(f"/api/incidents/{board['incident']}/fix", headers=ORIGIN,
                        json={"hypothesis": hyp, "option": opt, "values": values, "board_id": board["id"]}).json()
        assert fix["status"] == "waiting_for_user"
        act = next(a for a in page.get("/api/actions").json()["actions"] if a["id"] == fix["pending_action_id"])
        assert act["demo"] is True
        assert page.post(f"/api/actions/{act['id']}/approve", json={"args_hash": act["args_hash"]},
                         headers=ORIGIN).json()["status"] == "done"
        v = page.post(f"/api/incidents/{board['incident']}/verify", headers=ORIGIN).json()
        assert v["ok"] and not v["provisional"] and v["status"] == "resolved" and "ضبط واقعی" in v["recorded"]
        assert v["before"]["codes"] != v["after"]["codes"] or hyp == "P3"
        assert v["after"]["route"] == route and all(c == 200 for c in v["after"]["codes"])
        if hyp == "P3":
            assert v["after"]["slope_mb_min"] < 1 < v["before"]["slope_mb_min"]
        again = page.post("/api/troubleshoot", json={"app": app, "text": "الان درسته؟"}, headers=ORIGIN).json()
        assert again["hypotheses"][0]["id"] == "H0"                     # the after-state reads as healthy
    # anything that wasn't recorded stays refused, and judge clicks never touch the live daily caps
    card = page.post("/api/apps/shop-code-bug/zero-downtime", headers=ORIGIN).json()
    act = next(a for a in page.get("/api/actions").json()["actions"] if a["id"] == card["pending_action_id"])
    res = page.post(f"/api/actions/{act['id']}/approve", json={"args_hash": act["args_hash"]}, headers=ORIGIN).json()
    assert res["status"] == "failed" and "نمایش ضبط‌شده" in res["message"]
    assert app_mod.policy.budget.today()["deploys"] == 0


def test_judge_mode_only_the_recorded_fix_counts_and_unfixed_verify_stays_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "demo")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    page = TestClient(app_mod.app)
    page.post("/api/session", json={"token": "any"}, headers=ORIGIN)
    board = page.post("/api/troubleshoot", json={"app": "shop-missing-env", "text": "پرداخت خطا"}, headers=ORIGIN).json()
    fix = page.post(f"/api/incidents/{board['incident']}/fix", headers=ORIGIN,
                    json={"hypothesis": "P2", "option": 0, "values": {"OTHER": "x"}, "board_id": board["id"]}).json()
    act = next(a for a in page.get("/api/actions").json()["actions"] if a["id"] == fix["pending_action_id"])
    res = page.post(f"/api/actions/{act['id']}/approve", json={"args_hash": act["args_hash"]}, headers=ORIGIN).json()
    assert res["status"] == "failed"                                   # a variable that wasn't the one dropped
    # the code-fix option changes nothing in demo: verify reads the recording (no network, no wait) and says so
    board = page.post("/api/troubleshoot", json={"app": "shop-code-bug", "text": "پرداخت خطا"}, headers=ORIGIN).json()
    page.post(f"/api/incidents/{board['incident']}/fix", headers=ORIGIN,
              json={"hypothesis": "P1", "option": 0, "board_id": board["id"]})
    v = page.post(f"/api/incidents/{board['incident']}/verify", headers=ORIGIN).json()
    assert v["ok"] is False and 500 in v["after"]["codes"]


def test_healthy_answer_names_the_checked_page_and_points_elsewhere_for_non_problems(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "demo")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    from liara.liara_api import replay
    monkeypatch.setitem(replay.DEMO_APPS, "shop-healthy", "v1_healthy")
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    page = TestClient(app_mod.app)
    page.post("/api/session", json={"token": "any"}, headers=ORIGIN)

    def headline(text):
        return page.post("/api/troubleshoot", json={"app": "shop-healthy", "text": text}, headers=ORIGIN).json()["headline"]

    assert "/checkout" in headline("صفحه‌ی پرداخت خطا می‌ده") and "هر بار درست جواب داد" in headline("صفحه‌ی پرداخت خطا می‌ده")
    assert "دامنه، پروژه‌ی تازه و هزینه" in headline("دامنه‌ام رو وصل کن")
    assert "کند نیست" in headline("site kond shode")
