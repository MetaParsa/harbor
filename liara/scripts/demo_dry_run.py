"""Dry-run demo-shop on the real Liara account and record everything for L3 (fixtures/demo/<label>/).

Uses our own L1 client (dogfooding). Every response is saved redacted; /v1/me is never recorded (personal data).
Spends credit: an app (~25k toman/day) and builds, so run it on purpose.

  uv run --frozen python -m liara.scripts.demo_dry_run create
  uv run --frozen python -m liara.scripts.demo_dry_run deploy v1 --env PAYMENT_API_URL=https://pay.example.ir/api
  uv run --frozen python -m liara.scripts.demo_dry_run traffic 30 [rps]
  uv run --frozen python -m liara.scripts.demo_dry_run snapshot v1_healthy
  uv run --frozen python -m liara.scripts.demo_dry_run envs PAYMENT_API_URL=     (empty = remove)
  uv run --frozen python -m liara.scripts.demo_dry_run rollback v1
  uv run --frozen python -m liara.scripts.demo_dry_run watch 240 v3_watch       (memory every 20 s)
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

from liara.agent.redact import Redactor
from liara.liara_api import LiaraClient, package_dir, probe

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "demo-apps" / "demo-shop"
OUT = ROOT / "fixtures" / "demo"
STATE = Path(__file__).with_name("demo_state.json")
TOKEN_FILE = Path(os.environ.get("LIARA_TOKEN_FILE") or ROOT.parents[1] / "Liara" / "api.txt")   # outside the repo
L0_STATE = ROOT / "l0" / "state.json"


class Recorder(httpx.AsyncBaseTransport):
    """Pass-through transport that saves each JSON response (redacted) under OUT/<label>/."""

    def __init__(self, redactor: Redactor):
        self.inner = httpx.AsyncHTTPTransport(retries=0)
        self.redactor = redactor
        self.label = "misc"
        self.n = 0

    async def handle_async_request(self, request):
        resp = await self.inner.handle_async_request(request)
        if request.url.path == "/v1/me":
            return resp
        await resp.aread()
        self.n += 1
        try:
            body = json.loads(resp.content) if resp.content else None
        except ValueError:
            body = resp.content[:2000].decode("utf-8", "replace")
        slug = re.sub(r"[^a-z0-9]+", "_", request.url.path.lower()).strip("_")[:60]
        params = {k: v for k, v in request.url.params.items() if k != "teamID"}
        d = OUT / self.label
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.n:03d}_{request.method}_{slug}.json").write_text(json.dumps(
            {"request": f"{request.method} {request.url.path}", "params": params, "status": resp.status_code,
             "recorded_at": int(time.time()), "body": self.redactor.obj(body)}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        return httpx.Response(resp.status_code, headers=resp.headers, content=resp.content, request=request)


def state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def put(**kw) -> None:
    s = state()
    s.update(kw)
    STATE.write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")


def make_client() -> tuple[LiaraClient, Recorder]:
    token = os.environ.get("LIARA_TOKEN", "").strip() or TOKEN_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    team = json.loads(L0_STATE.read_text(encoding="utf-8"))["team_id"]
    rec = Recorder(Redactor([token]))
    return LiaraClient(token, team, transport=rec, timeout_s=60), rec


async def cmd_create(c, rec, args):
    plans = await c.plans()
    plan = plans.cheapest_iran()
    print("plan", plan.id, "price/month", plans.price(plan.id, "basic")[1])
    for name in (args.name, f"{args.name}-2", f"{args.name}-3"):
        rec.label = "create"
        try:
            pid = await c.create_app(name, plan.id, "node", "basic")
            put(app=name, project_id=pid, releases={})
            print("created", name)
            return
        except Exception as e:     # noqa: BLE001
            print("create", name, "→", e)
    sys.exit("could not create an app")


def parse_envs(items) -> dict[str, str | None]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k] = v or None
    return out


async def cmd_deploy(c, rec, args):
    app = state()["app"]
    data, info = package_dir(APPS / args.version, "node")
    rec.label = f"deploy_{args.version}"
    sid = await c.upload_source(app, data)
    envs = {k: v for k, v in parse_envs(args.env).items() if v is not None}
    body = c.release_body(sid, "node", 3000, envs=envs, message=f"demo-shop {args.version}")
    t0 = time.time()
    rid = await c.create_release(app, body)
    res = await c.wait_for_release(rid, on_line=lambda t, s: print("  |", t[:160]) if s != "STDOUT" or args.verbose else None)
    print(f"{args.version}: release {rid} {res.state} in {round(time.time() - t0)} s, failure={res.failure_line!r}")
    body_no_envs = {**body, "envs": {}}            # never store env values on disk
    s = state()
    s.setdefault("releases", {})[args.version] = {"id": rid, "body": body_no_envs, "state": res.state,
                                                   "seconds": round(time.time() - t0)}
    put(releases=s["releases"])
    after = await c.get_app(app)
    print("env keys after deploy:", [e.key for e in after.envs])


async def cmd_rollback(c, rec, args):
    app, rel = state()["app"], state()["releases"][args.version]
    rec.label = f"rollback_{args.version}"
    t0 = time.time()
    rid = await c.redeploy(app, rel["body"], message=f"rollback to {args.version}")
    res = await c.wait_for_release(rid)
    print(f"rollback to {args.version}: {rid} {res.state} in {round(time.time() - t0)} s")


async def cmd_envs(c, rec, args):
    app = state()["app"]
    rec.label = "envs"
    t0 = time.time()
    keys = await c.set_envs(app, parse_envs(args.env))
    print("env keys now:", keys, f"({round(time.time() - t0)} s)")


async def cmd_traffic(c, rec, args):
    url = f"https://{state()['app']}.liara.run"
    paths = ["/", "/checkout?id=1", "/api/products", "/checkout?id=2", "/resize?id=2"]
    stats: dict[str, list] = {}
    async with httpx.AsyncClient(timeout=20) as h:
        end, i = time.time() + args.seconds, 0
        while time.time() < end:
            p = paths[i % len(paths)]
            t0 = time.perf_counter()
            try:
                r = await h.get(url + p)
                code = r.status_code
            except httpx.HTTPError:
                code = None
            stats.setdefault(p.split("?")[0], []).append((code, round((time.perf_counter() - t0) * 1000)))
            i += 1
            await asyncio.sleep(1 / args.rps)
    for p, v in stats.items():
        codes = {}
        for code, _ in v:
            codes[code] = codes.get(code, 0) + 1
        print(f"{p:14} n={len(v):3} codes={codes} median_ms={sorted(ms for _, ms in v)[len(v) // 2]}")


async def cmd_snapshot(c, rec, args):
    app = state()["app"]
    rec.label = args.label
    a = await c.get_app(app)
    rels = await c.releases(app, 5)
    ev = await c.events(app, 3600)
    usage = await c.usage_now(app)
    plan = (await c.plans()).apps[a.plan_id]
    logs = await c.app_logs(app, since_s=1800)
    probes = {p: await probe(f"{a.url}{p}", n=3) for p in ("/", "/checkout?id=1")}
    errors = [ln.text for ln in logs if ln.stream == "stderr"][-6:]
    statuses = {}
    for ln in logs:
        m = re.match(r"^(GET|POST) (\S+) (\d{3}) ", ln.text)
        if m:
            statuses.setdefault(m.group(2), {}).setdefault(m.group(3), 0)
            statuses[m.group(2)][m.group(3)] += 1
    mem = usage["memory_bytes"]
    summary = {"label": args.label, "at": int(time.time()), "release": rels.current_id,
               "releases": [(r.tag, r.type, r.state) for r in rels.items],
               "events": [(e.status, e.tag, e.time) for e in ev][-12:],
               "memory_mb": round(mem / 1048576, 1) if mem else None,
               "memory_pct": round(100 * mem / (plan.ram_gb * 1024 ** 3), 1) if mem else None,
               "cpu_cores": usage["cpu_cores"], "probes": probes, "log_lines": len(logs),
               "statuses_from_logs": statuses, "stderr_tail": errors}
    (OUT / args.label).mkdir(parents=True, exist_ok=True)
    (OUT / args.label / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


async def cmd_watch(c, rec, args):
    """Memory / events every 20 s (for the leak curve and the OOM moment)."""
    app = state()["app"]
    end = time.time() + args.seconds
    rows = []
    while time.time() < end:
        rec.label = args.label
        u = await c.usage_now(app)
        ev = await c.events(app, 600)
        p = (await probe(f"https://{app}.liara.run/", n=1))[0]
        row = {"t": int(time.time()), "mb": round((u["memory_bytes"] or 0) / 1048576, 1),
               "container": (u["container"] or "")[-14:], "probe": p,
               "last_events": [(e.status, e.tag) for e in ev][-3:]}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
        await asyncio.sleep(20)
    (OUT / args.label).mkdir(parents=True, exist_ok=True)
    (OUT / args.label / "watch.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["create", "deploy", "rollback", "envs", "traffic", "snapshot", "watch"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("arg2", nargs="?")
    ap.add_argument("--env", action="append")
    ap.add_argument("--name", default="dastyar-demo-shop")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.cmd in ("deploy", "rollback"):
        a.version = a.arg
    if a.cmd == "envs":
        a.env = [a.arg] + (a.env or [])
    if a.cmd == "traffic":
        a.seconds, a.rps = int(a.arg or 30), float(a.arg2 or 2)
    if a.cmd == "snapshot":
        a.label = a.arg
    if a.cmd == "watch":
        a.seconds, a.label = int(a.arg or 240), a.arg2 or "watch"
    c, rec = make_client()
    async with c:
        await globals()[f"cmd_{a.cmd}"](c, rec, a)
    print(f"[{len(c.calls)} Liara API calls]")


if __name__ == "__main__":
    asyncio.run(main())
