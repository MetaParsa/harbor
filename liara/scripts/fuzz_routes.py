"""Run: uv run --frozen python liara/scripts/fuzz_routes.py

Throw bad input at every route (judge mode, no network). Any 5xx or non-JSON error is a bug; every 4xx should carry
a Persian `message`. Prints a summary and the offenders."""
import itertools
import os
import re
import sys
import tempfile

os.environ["LIARA_REPLAY"] = "demo"
os.environ["LIARA_DATA_DIR"] = tempfile.mkdtemp()
os.environ["LIARA_DEV"] = "1"
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from fastapi.testclient import TestClient  # noqa: E402

import liara.app as m  # noqa: E402

O = {"origin": "http://testserver"}
PERSIAN = re.compile(r"[؀-ۿ]")
c = TestClient(m.app, raise_server_exceptions=False)
anon = TestClient(m.app, raise_server_exceptions=False)
c.post("/api/session", json={"token": "judge"}, headers=O)
code = c.post("/api/pair", headers=O).json()["code"]
bt = c.post("/api/bridge/pair", json={"code": code}).json()["token"]
B = {"Authorization": f"Bearer {bt}"}

BAD_STR = ["", " ", "../../etc/passwd", "A" * 5000, "shop-code-bug\n", "<b>x</b>", "🙂", "null", "-1", "UPPER-Case", "a" * 70]
BAD_JSON = [None, {}, [], "x", 1, {"app": 1}, {"app": None, "text": None}, {"app": "shop-code-bug", "text": 5}]
results, bad = 0, []


def check(label, r, allow=(200,)):
    global results
    results += 1
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code >= 500:
        bad.append((label, r.status_code, r.text[:160]))
    elif r.status_code >= 400:
        msg = (body or {}).get("message", "") if isinstance(body, dict) else ""
        if not PERSIAN.search(msg or ""):
            bad.append((label, r.status_code, "no Persian message: " + r.text[:120]))


# path params
from urllib.parse import quote  # noqa: E402
for raw in BAD_STR:
    s = quote(raw, safe='')
    for path in (f"/api/apps/{s}/overview", f"/api/apps/{s}/ticket", f"/api/projects/{s}", f"/api/projects/{s}/patch",
                 f"/api/projects/{s}/liara.json", f"/api/projects/{s}/workflow.yml", f"/api/bridge/actions/{s}",
                 f"/api/bridge/capability/{s}"):
        h = B if "/bridge/" in path else {}
        check(f"GET {path[:60]}", c.get(path, headers=h))
    for path in (f"/api/apps/{s}/zero-downtime", f"/api/incidents/{s}/verify", f"/api/actions/{s}/reject"):
        check(f"POST {path[:60]}", c.post(path, headers=O))
    check(f"approve {s[:20]}", c.post(f"/api/actions/{s}/approve", json={"args_hash": s}, headers=O))
# bodies
for body, path in itertools.product(BAD_JSON, ["/api/troubleshoot", "/api/domains", "/api/domains/check", "/api/domains/ssl",
                                              "/api/incidents/x/fix", "/api/projects/x/create", "/api/session"]):
    check(f"POST {path} {str(body)[:30]}", c.post(path, json=body, headers=O))
for s in BAD_STR:
    check(f"troubleshoot app={s[:20]}", c.post("/api/troubleshoot", json={"app": s, "text": "x"}, headers=O))
    check(f"domain={s[:20]}", c.post("/api/domains", json={"app": "shop-code-bug", "domain": s}, headers=O))
    check(f"bridge troubleshoot {s[:20]}", c.post("/api/bridge/troubleshoot", json={"app": s, "symptom": s}, headers=B))
    check(f"bridge ci {s[:20]}", c.post("/api/bridge/ci", json={"app": "shop-code-bug", "branch": s}, headers=B))
    check(f"bridge deploy {s[:20]}", c.post(f"/api/bridge/deploy?app_name={quote(s, safe='')}", content=b"x", headers=B))
    check(f"bridge pair {s[:20]}", anon.post("/api/bridge/pair", json={"code": s}))
# uploads
for blob in (b"", b"not a zip", b"PK\x03\x04garbage", os.urandom(5000)):
    check(f"upload {blob[:10]!r}", c.post("/api/projects?filename=x.zip", content=blob, headers=O))
# no session / wrong origin / no bridge token
for path in ("/api/apps", "/api/actions", "/api/log", "/api/stream"):
    check(f"anon GET {path}", anon.get(path))
check("anon troubleshoot", anon.post("/api/troubleshoot", json={"app": "a-b", "text": "x"}, headers=O))
check("wrong origin", c.post("/api/troubleshoot", json={"app": "shop-code-bug", "text": "x"}, headers={"origin": "http://evil"}))
check("bridge no token", anon.post("/api/bridge/troubleshoot", json={"app": "shop-code-bug", "symptom": "x"}))
check("dev propose bad tool", c.post("/api/dev/propose", json={"tool": "delete_everything", "args": {}}, headers=O))
check("dev propose bad args", c.post("/api/dev/propose", json={"tool": "set_envs", "args": {"app": "shop-code-bug", "changes": {"BAD KEY": 1}}}, headers=O))

print(f"{results} requests, {len(bad)} problems")
for b in bad[:40]:
    print("  ", b)
