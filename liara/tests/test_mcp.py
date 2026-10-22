"""L7 coding-agent bridge: the stdlib MCP server + the backend's pairing/bridge routes. REPLAY only, no credit."""

import importlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from liara.liara_api import package_dir
from liara.mcp import liara_sre_mcp as mcp

ORIGIN = {"origin": "http://testserver"}
DEMO_V1 = Path(__file__).resolve().parents[1] / "demo-apps" / "demo-shop" / "v1"
APP = "l0-dyar-6b46"            # the app in the L0 REPLAY recordings


class ClientBackend(mcp.Backend):
    """mcp.Backend over FastAPI's TestClient instead of urllib (same paths, headers and bodies)."""

    def __init__(self, client: TestClient):
        super().__init__("http://testserver")
        self.client = client

    def call(self, method, path, body=None, *, raw=None, timeout=60):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if raw is not None:
            r = self.client.request(method, path, content=raw, headers={**headers, "Content-Type": "application/gzip"})
        else:
            r = self.client.request(method, path, json=body, headers=headers)
        out = r.json() if r.content else {}
        return out if r.status_code < 400 else {"status": "error", "http": r.status_code, **out}


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "d_fixed")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LIARA_AGENT_KILL", raising=False)

    async def no_network(url, n=5, **_):
        return [{"status": 200, "ms": 120}] * n

    import liara.sre.collect as collect
    monkeypatch.setattr(collect, "probe", no_network)
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    page = TestClient(app_mod.app)
    assert page.post("/api/session", json={"token": "replay-token-123"}, headers=ORIGIN).status_code == 200
    return app_mod, page


def names(data: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        return set(t.getnames())


def test_bridge_packer_matches_backend_packer(tmp_path):
    for rel in ("main.py", "src/a.py", "src/sub/b.py", "data/x.sql", "app/data/m.py", "node_modules/z/i.js",
                ".env", "logs/e.log", "build/o.js", "keep.txt"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    (tmp_path / ".gitignore").write_bytes("﻿/data\nsrc/*.py\nbuild/\n*.log\n!keep.txt\n".encode())
    assert names(mcp.pack(tmp_path)[0]) == names(package_dir(tmp_path)[0])
    assert names(mcp.pack(DEMO_V1)[0]) == {"server.js", "package.json"}


def test_jsonrpc_handshake_and_tool_list():
    srv = mcp.Server(mcp.Backend("http://127.0.0.1:9"))
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["serverInfo"]["name"] == "liara-sre"
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = {t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert tools == {"pair", "troubleshoot", "deploy_project", "wait_for_approval", "verify_fix", "check_capability",
                     "support_ticket", "ci_setup"}
    assert not any("approve" in t or "confirm" in t or "reject" in t for t in tools)       # no way to approve
    res = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "troubleshoot", "arguments": {"app": "x", "symptom": "y"}}})
    assert res["result"]["isError"] and "not_paired" in res["result"]["content"][0]["text"]
    assert srv.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601


def test_pair_troubleshoot_deploy_approve_in_page_wait(web):
    app_mod, page = web
    code = page.post("/api/pair", headers=ORIGIN).json()["code"]
    srv = mcp.Server(ClientBackend(TestClient(app_mod.app)), sleep=lambda s: None)
    assert srv.tool_pair({"code": code})["status"] == "paired"
    assert srv.tool_pair({"code": code}).get("http") == 401                 # single use

    board = srv.tool_troubleshoot({"app": APP, "symptom": "سایتم خطا می‌ده", "project_path": str(DEMO_V1)})
    assert board["incident_id"].startswith("inc-") and board["headline"] and "web page" in board["next"]

    res = srv.tool_deploy_project({"path": str(DEMO_V1), "app": APP, "message": "fix checkout"})
    assert res["status"] == "waiting_for_user" and res["packed"]["files"] == 2, res
    act_id = res["pending_action_id"]
    assert srv.tool_wait_for_approval({"action_id": act_id, "timeout_s": 1})["status"] == "pending"

    # the bridge token can't approve: approval routes need the page's cookie + Origin
    token = srv.backend.token
    stranger = TestClient(app_mod.app)
    act = page.get("/api/actions").json()["actions"][-1]
    assert act["id"] == act_id and act["caller"] == "mcp"
    for headers in ({"Authorization": f"Bearer {token}"}, {"Authorization": f"Bearer {token}", **ORIGIN}):
        r = stranger.post(f"/api/actions/{act_id}/approve", json={"args_hash": act["args_hash"]}, headers=headers)
        assert r.status_code == 401
    # the user taps the card in the page
    assert page.post(f"/api/actions/{act_id}/approve", json={"args_hash": act["args_hash"]}, headers=ORIGIN).json()["status"] == "done"
    done = srv.tool_wait_for_approval({"action_id": act_id, "timeout_s": 5})
    assert done["status"] == "done" and done["result"]["state"] == "READY"


def test_pairing_codes_expire_and_brute_force_is_braked(web, monkeypatch):
    app_mod, page = web
    bridge = TestClient(app_mod.app)
    for _ in range(20):
        assert bridge.post("/api/bridge/pair", json={"code": "WRONG1"}).status_code == 401
    assert bridge.post("/api/bridge/pair", json={"code": "WRONG1"}).status_code == 429
    app_mod.pair_failures.clear()
    code = page.post("/api/pair", headers=ORIGIN).json()["code"]
    app_mod.pair_codes[code] = (app_mod.pair_codes[code][0], 0)            # expired
    assert bridge.post("/api/bridge/pair", json={"code": code}).status_code == 401


def test_logout_unpairs_and_bridge_needs_token(web):
    app_mod, page = web
    code = page.post("/api/pair", headers=ORIGIN).json()["code"]
    token = TestClient(app_mod.app).post("/api/bridge/pair", json={"code": code}).json()["token"]
    bridge = TestClient(app_mod.app)
    auth = {"Authorization": f"Bearer {token}"}
    assert bridge.get("/api/bridge/capability/top_up", headers=auth).json()["api"] == "none"
    assert bridge.get("/api/bridge/capability/top_up").status_code == 401
    assert page.delete("/api/session", headers=ORIGIN).status_code == 200
    assert bridge.get("/api/bridge/capability/top_up", headers=auth).status_code == 401


def test_deploy_rejects_non_gzip_and_bad_names(web):
    app_mod, page = web
    code = page.post("/api/pair", headers=ORIGIN).json()["code"]
    token = TestClient(app_mod.app).post("/api/bridge/pair", json={"code": code}).json()["token"]
    c = TestClient(app_mod.app)
    auth = {"Authorization": f"Bearer {token}"}
    assert c.post(f"/api/bridge/deploy?app_name={APP}", content=b"not a tarball", headers=auth).status_code == 400
    assert c.post("/api/bridge/deploy?app_name=../x", content=b"\x1f\x8b", headers=auth).status_code == 400


def test_stdio_process_speaks_json_rpc():
    script = Path(mcp.__file__)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "check_capability", "arguments": {"name": "top_up"}}}]
    p = subprocess.run([sys.executable, "-I", str(script), "--url", "http://127.0.0.1:9"],
                       input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    replies = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[2]["result"]["isError"] and "not_paired" in replies[2]["result"]["content"][0]["text"]


# ---------- review fixes (2026-10-22) ----------

def pair_token(app_mod, page):
    code = page.post("/api/pair", headers=ORIGIN).json()["code"]
    return TestClient(app_mod.app).post("/api/bridge/pair", json={"code": code}).json()["token"]


def test_odd_headers_and_names_never_500(web):
    app_mod, page = web
    token = pair_token(app_mod, page)
    c = TestClient(app_mod.app)
    assert c.get("/api/bridge/capability/top_up", headers={"Authorization": b"Bearer caf\xe9"}).status_code == 401
    auth = {"Authorization": f"Bearer {token}"}
    assert c.post(f"/api/bridge/deploy?app_name={APP}", content=b"\x1f\x8bxx",
                  headers={**auth, "content-length": "abc"}).status_code in (400, 422)
    assert c.post("/api/bridge/troubleshoot", json={"app": APP + "\n", "symptom": "x"}, headers=auth).status_code == 400


def test_upload_cap_streams_and_rate_limit(web, monkeypatch):
    app_mod, page = web
    token = pair_token(app_mod, page)
    c = TestClient(app_mod.app)
    auth = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(app_mod, "MAX_SOURCE_BYTES", 10)

    def chunks():
        yield b"\x1f\x8b" + b"x" * 8
        yield b"y" * 8                                        # chunked: no content-length to trust

    assert c.post(f"/api/bridge/deploy?app_name={APP}", content=chunks(), headers=auth).status_code == 413
    monkeypatch.setattr(app_mod, "MAX_SOURCE_BYTES", 128 * 1024 * 1024)
    data = mcp.pack(DEMO_V1)[0]
    codes = [c.post(f"/api/bridge/deploy?app_name={APP}", content=data, headers=auth).status_code for _ in range(6)]
    assert codes[-1] == 429 and all(x == 200 for x in codes[:5])


def test_bridge_does_not_keep_the_session_alive_and_repairing_replaces_tokens(web):
    app_mod, page = web
    t1 = pair_token(app_mod, page)
    s = next(iter(app_mod.sessions.values()))
    seen = __import__("time").time() - 60
    s.last_seen = seen
    c = TestClient(app_mod.app)
    assert c.get("/api/bridge/capability/top_up", headers={"Authorization": f"Bearer {t1}"}).status_code == 200
    assert s.last_seen == seen                               # only the user's page keeps it alive
    s.last_seen = __import__("time").time()
    t2 = pair_token(app_mod, page)
    assert c.get("/api/bridge/capability/top_up", headers={"Authorization": f"Bearer {t1}"}).status_code == 401
    assert c.get("/api/bridge/capability/top_up", headers={"Authorization": f"Bearer {t2}"}).status_code == 200
    app_mod.sessions.clear()
    app_mod.get_session(None)                                 # sweep
    assert not app_mod.bridge_tokens and not app_mod.pair_codes


def test_no_card_deploy_still_verifiable(web):
    app_mod, page = web
    token = pair_token(app_mod, page)
    from liara.agent.incident import Incident
    s = next(iter(app_mod.sessions.values()))
    s.ctx.created_apps.add(APP)                               # created through the assistant → first builds are T1
    inc = Incident(APP, "x")
    inc.move("diagnosing")
    inc.move("waiting_coding_agent")
    inc.fix = {"hypothesis": "P1", "option": 0, "contract": {"route": "/"}, "action_id": None, "started_at": None}
    s.incidents[inc.id] = inc
    res = TestClient(app_mod.app).post(f"/api/bridge/deploy?app_name={APP}", content=mcp.pack(DEMO_V1)[0],
                                       headers={"Authorization": f"Bearer {token}"}).json()
    assert res["status"] == "done" and inc.fix["tool"] == "deploy" and inc.fix["started_at"]


def test_mcp_server_survives_bad_input_and_refuses_cleartext_remote():
    assert mcp.Backend("http://example.com").call("GET", "/x")["error"] == "bad_url"
    assert mcp.Backend("http://127.0.0.1:9").url_error is None and mcp.Backend("https://x.liara.run").url_error is None
    srv = mcp.Server(mcp.Backend("http://127.0.0.1:9"))
    srv.backend.token = "t"

    def boom(a):
        raise RuntimeError("x")

    srv.tool_check_capability = boom
    res = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "check_capability", "arguments": {"name": "x"}}})
    assert res["result"]["isError"] and "internal" in res["result"]["content"][0]["text"]
    srv.tool_check_capability = lambda a: {"status": "refused"}
    res = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "check_capability", "arguments": {"name": "x"}}})
    assert res["result"]["isError"]
    init = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}})
    assert init["result"]["protocolVersion"] == mcp.PROTOCOLS[0]


def test_stdio_process_survives_non_object_lines():
    p = subprocess.run([sys.executable, "-I", str(Path(mcp.__file__)), "--url", "http://127.0.0.1:9"],
                       input='[]\n1\nnot json\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n', capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    replies = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    assert [r.get("error", {}).get("code") for r in replies[:3]] == [-32600] * 3 and replies[3] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_packers_agree_on_vendor_and_env_files(tmp_path):
    for rel in ("index.php", "composer.json", "vendor/lib.php", ".env.local", ".env.example", "src/x.php"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    got = names(mcp.pack(tmp_path)[0])
    assert got == names(package_dir(tmp_path)[0]) == {"index.php", "composer.json", ".env.example", "src/x.php"}


def test_ci_setup_returns_the_workflow_without_any_secret(web):
    app_mod, page = web
    token = pair_token(app_mod, page)
    c = TestClient(app_mod.app)
    res = c.post("/api/bridge/ci", json={"app": APP, "branch": "main"}, headers={"Authorization": f"Bearer {token}"}).json()
    assert res["path"] == ".github/workflows/liara.yml" and res["secret_name"] == "LIARA_API_TOKEN"
    assert f'--app="{APP}"' in res["content"] and "secrets.LIARA_API_TOKEN" in res["content"]
    assert "replay-token" not in res["content"] and "--detach" not in res["content"]
    assert any(t["name"] == "ci_setup" for t in mcp.TOOLS)
