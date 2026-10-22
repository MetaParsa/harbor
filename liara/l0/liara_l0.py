"""L0 spike: check what Liara's real API does before we build on it.

Every call is saved, redacted, to liara/fixtures/api/<name>.json so later steps
(L1 client, REPLAY mode, tests) can use real responses.

Token: LIARA_TOKEN env var, else the first line of Hackathon/Liara/api.txt.
Never printed, never written to disk by this script.

Commands (anything that costs credit needs --yes):
  read                          account, plans, balance, apps (read-only)
  create  --app N --yes         create a throwaway app (cheapest Iran plan)
  deploy  --app N --dir D --yes package + upload + release, stream build logs
  redeploy --app N --release R --yes   new release from a stored source (rollback test)
  inspect --app N               status, containers, events, metrics, logs, HTTP probe
  envtest --app N --yes         env round-trip incl. an encrypted var
  delete  --app N --yes         delete an app this script created
"""

import argparse
import io
import json
import os
import re
import sys
import tarfile
import time
from pathlib import Path

import httpx

API = "https://api.liara.ir"
HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures" / "api"
STATE = HERE / "state.json"
TOKEN_FILE = HERE.parents[2] / "Liara" / "api.txt"

SECRET_KEY = re.compile(r"(token|secret|password|passwd|apikey|api_key|accesskey|access_key|credential|private)", re.I)
ALWAYS_EXCLUDE = {".git", ".idea", ".vscode", ".next", "node_modules", "__pycache__", "venv", ".venv", ".env", "liara.json", ".DS_Store"}


def load_token() -> str:
    tok = os.environ.get("LIARA_TOKEN", "").strip()
    if not tok and TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not tok:
        sys.exit(f"No Liara token. Put it in {TOKEN_FILE} (one line) or set LIARA_TOKEN.")
    return tok


TOKEN = ""


def redact(obj, path=""):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if SECRET_KEY.search(k) and not isinstance(v, (dict, list, bool)) and v not in (None, ""):
                out[k] = "<redacted>"
            else:
                out[k] = redact(v, f"{path}.{k}")
        return out
    if isinstance(obj, list):
        return [redact(v, path) for v in obj]
    if isinstance(obj, str) and TOKEN and TOKEN in obj:
        return obj.replace(TOKEN, "<token>")
    return obj


def save(name: str, record: dict) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


def call(c: httpx.Client, method: str, path: str, name: str | None = None, shape=None, **kw):
    """One API call. Returns (status, body). Saves a redacted fixture when name is given.

    shape: optional function applied to the body before saving (allow-lists).
    """
    params = dict(kw.pop("params", {}) or {})
    team = os.environ.get("LIARA_TEAM_ID", "")
    if team:
        params["teamID"] = team
    t0 = time.perf_counter()
    r = c.request(method, path, params=params, **kw)
    ms = round((time.perf_counter() - t0) * 1000)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:4000]
    if name:
        kept = shape(body) if (shape and r.status_code < 400) else body
        save(name, {"request": f"{method} {path}", "params": {k: v for k, v in params.items() if k != "teamID"},
                    "status": r.status_code, "ms": ms, "body": redact(kept)})
    return r.status_code, body


def state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def put_state(**kw) -> None:
    s = state()
    s.update(kw)
    STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")


def need_yes(args, what: str) -> None:
    if not args.yes:
        sys.exit(f"Refusing: {what} costs credit or changes the account. Re-run with --yes.")


def plans_only(me):
    # /v1/me also holds personal data and storage secrets: keep only plan data.
    return {"plans": (me.get("plans") or {}) if isinstance(me, dict) else {}}


def cheapest_plan(me) -> tuple[str, dict]:
    projects = ((me.get("plans") or {}).get("projects") or {})
    ok = {k: v for k, v in projects.items() if v.get("available") and v.get("region") == "iran"}
    if not ok:
        sys.exit("No available Iran app plan found in /v1/me.")
    pid = min(ok, key=lambda k: ok[k].get("price", 1e18))
    return pid, ok[pid]


# ---------- commands ----------

def cmd_read(c, args):
    st, me = call(c, "GET", "/v1/me", "me_plans", shape=plans_only)
    print("GET /v1/me:", st)
    if st < 400:
        projects = (me.get("plans") or {}).get("projects") or {}
        iran = sorted(((k, v) for k, v in projects.items() if v.get("available") and v.get("region") == "iran"),
                      key=lambda kv: kv[1].get("price", 0))
        print(f"  {len(projects)} app plans, {len(iran)} available in Iran; cheapest 5:")
        for k, v in iran[:5]:
            print(f"   {k}: CPU {v.get('CPU')}, RAM {v.get('RAM')}, price/h {v.get('price')}")
        bundles = (me.get("plans") or {}).get("projectBundlePlans") or {}
        if iran and iran[0][0] in bundles:
            print("  bundles for cheapest plan:")
            for b, v in bundles[iran[0][0]].items():
                print(f"   {b}: {json.dumps(redact(v), ensure_ascii=False)[:300]}")
    st, bill = call(c, "GET", "/v1/billing", "billing",
                    shape=lambda b: {"user": {k: (b.get("user") or {}).get(k) for k in ("balance", "accountType", "minCreditAmount")}})
    print("GET /v1/billing:", st, (bill.get("user") or {}).get("balance") if isinstance(bill, dict) else bill)
    st, fc = call(c, "GET", "/v1/me/free-credit", "free_credit")
    print("GET /v1/me/free-credit:", st, fc)
    st, nets = call(c, "GET", "/v1/networks", "networks")
    print("GET /v1/networks:", st, [n.get("name") for n in (nets.get("networks") or [])] if isinstance(nets, dict) else nets)
    st, apps = call(c, "GET", "/v1/projects", "projects_list",
                    shape=lambda b: {"projects": [{k: p.get(k) for k in ("project_id", "type", "status", "scale", "planID", "bundlePlanID", "hourlyPrice", "isDeployed")}
                                                  for p in (b.get("projects") or [])]})
    if isinstance(apps, dict):
        print("GET /v1/projects:", st)
        for p in apps.get("projects") or []:
            print(f"   {p.get('project_id')}: {p.get('type')} status={p.get('status')} scale={p.get('scale')} plan={p.get('planID')}/{p.get('bundlePlanID')} {p.get('hourlyPrice')}/h")
    else:
        print("GET /v1/projects:", st, apps)


def cmd_create(c, args):
    need_yes(args, "creating an app")
    st, me = call(c, "GET", "/v1/me")
    pid, plan = cheapest_plan(me)
    st, nets = call(c, "GET", "/v1/networks")
    networks = nets.get("networks") or []
    if networks:
        net = networks[0]["_id"]
    else:
        st, made = call(c, "POST", "/v1/networks", "create_network", json={"networkName": "default"})
        net = (made.get("network") or made).get("_id")
    body = {"name": args.app, "planID": pid, "platform": args.platform, "bundlePlanID": args.bundle, "network": net}
    print(f"Creating {args.app}: plan {pid} ({plan.get('price')}/h), bundle {args.bundle}, platform {args.platform}")
    st, res = call(c, "POST", "/v1/projects", "create_project", json=body)
    print("POST /v1/projects:", st, res)
    if st < 300:
        put_state(**{f"app:{args.app}": {"created_by_l0": True, "plan": pid}})


def package(src: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if any(part in ALWAYS_EXCLUDE for part in rel.parts) or p.suffix == ".pyc":
                continue
            tar.add(p, arcname=str(rel).replace("\\", "/"), recursive=False)
    return buf.getvalue()


def stream_release(c, app: str, rid: str, label: str) -> dict:
    """Wait for the queue, stream build logs, return the timing + outcome summary."""
    t0 = time.time()
    first_failure, since, last_state = None, None, None
    while True:
        st, rel = call(c, "GET", f"/v1/releases/{rid}")
        if st >= 400 or not ((rel.get("release") or {}).get("queue") or 0) > 0:
            break
        time.sleep(3)
    lines_seen = 0
    while True:
        params = {"since": since} if since else {}
        st, bl = call(c, "GET", f"/v2/releases/{rid}/build-logs", params=params)
        if st >= 400:
            print("  build-logs error", st, bl)
            break
        out = bl.get("buildOutput") or []
        for ln in out:
            text = (ln.get("line") or "").rstrip()
            if ln.get("stream") != "STDOUT" and first_failure is None:
                first_failure = {"stream": ln.get("stream"), "line": text}
            print("  |", text[:200])
            since = ln.get("createdAt") or since
        lines_seen += len(out)
        rinfo = bl.get("release") or {}
        last_state = rinfo.get("state")
        if last_state in ("READY", "FAILED", "TIMEDOUT", "CANCELED", "UNHEALTHY"):
            save(f"build_logs_{label}", {"request": f"GET /v2/releases/{rid}/build-logs", "status": st,
                                         "body": redact({"release": rinfo, "tail": out[-40:]})})
            break
        if time.time() - t0 > 25 * 60:
            print("  giving up after 25 min")
            break
        time.sleep(1.5)
    summary = {"release": rid, "state": last_state, "seconds": round(time.time() - t0),
               "first_non_stdout": first_failure, "log_lines": lines_seen,
               "failReason": (bl.get("release") or {}).get("failReason") if isinstance(bl, dict) else None}
    print("Result:", json.dumps(summary, ensure_ascii=False))
    return summary


def release_body(args, source_id: str) -> dict:
    cfg = json.loads(args.config) if args.config else {}
    return {"sourceID": source_id, "port": args.port, "type": args.platform, "message": args.message,
            "args": [], "cron": [], "envs": {}, "disks": [],
            "build": {"cache": True, "location": "iran"}, "platformConfig": cfg}


def cmd_deploy(c, args):
    need_yes(args, "deploying (uses build quota)")
    src = Path(args.dir).resolve()
    data = package(src)
    print(f"Packaged {src.name}: {len(data)} bytes")
    st, up = call(c, "POST", f"/v2/projects/{args.app}/sources", f"upload_{args.label}",
                  files={"file": ("source.tar.gz", data, "application/gzip")})
    print("upload:", st, up)
    if st >= 300:
        return
    sid = up["sourceID"]
    body = release_body(args, sid)
    t0 = time.time()
    st, rel = call(c, "POST", f"/v2/projects/{args.app}/releases", f"release_{args.label}", json=body)
    print("release:", st, rel)
    if st >= 300:
        return
    rid = rel.get("releaseID")
    summary = stream_release(c, args.app, rid, args.label)
    summary["total_seconds_incl_upload"] = round(time.time() - t0)
    s = state()
    releases = s.get(f"releases:{args.app}", [])
    releases.append({"label": args.label, "sourceID": sid, "body": body, **summary})
    put_state(**{f"releases:{args.app}": releases})


def cmd_watch(c, args):
    """Re-attach to a release (default: the app's newest) without starting a new build."""
    st, rels = call(c, "GET", f"/v1/projects/{args.app}/releases", params={"page": 1, "count": 1})
    rid = args.release or ((rels.get("releases") or [{}])[0].get("_id") if isinstance(rels, dict) else None)
    if not rid:
        sys.exit(f"No release found: {st} {rels}")
    summary = stream_release(c, args.app, rid, args.label)
    s = state()
    releases = s.get(f"releases:{args.app}", [])
    releases.append({"label": args.label, "sourceID": None, "body": None, **summary})
    put_state(**{f"releases:{args.app}": releases})


def cmd_redeploy(c, args):
    need_yes(args, "redeploying (uses build quota)")
    releases = state().get(f"releases:{args.app}", [])
    old = next((r for r in releases if r["label"] == args.release), None)
    if not old:
        sys.exit(f"No stored release labelled {args.release}")
    body = dict(old["body"], message=f"L0 rollback to {args.release}")
    t0 = time.time()
    st, rel = call(c, "POST", f"/v2/projects/{args.app}/releases", f"release_rollback_{args.release}", json=body)
    print("release:", st, rel)
    if st >= 300:
        return
    summary = stream_release(c, args.app, rel.get("releaseID"), f"rollback_{args.release}")
    summary["total_seconds"] = round(time.time() - t0)
    releases.append({"label": f"rollback_{args.release}", "sourceID": old["sourceID"], "body": body, **summary})
    put_state(**{f"releases:{args.app}": releases})


def env_view(envs):
    return [{"key": e.get("key"), "encrypted": e.get("encrypted"), "value_len": len(e.get("value") or "")} for e in envs or []]


def cmd_inspect(c, args):
    a, now = args.app, int(time.time())
    st, p = call(c, "GET", f"/v1/projects/{a}", f"project_{args.label}",
                 shape=lambda b: {"project": {**{k: v for k, v in (b.get("project") or {}).items() if k != "envs"},
                                              "envs": env_view((b.get("project") or {}).get("envs"))}})
    proj = p.get("project") or {} if isinstance(p, dict) else {}
    print("project:", st, {k: proj.get(k) for k in ("status", "scale", "planID", "bundlePlanID", "hourlyPrice", "isDeployed", "type")})
    st, ap = call(c, "GET", f"/v1/projects/{a}/applets", f"applets_{args.label}")
    print("applets:", st, json.dumps(redact(ap), ensure_ascii=False)[:600])
    st, ev = call(c, "GET", f"/v1/projects/{a}/events", f"events_{args.label}", params={"start": now - 3600, "end": now})
    print("events:", st, json.dumps(redact(ev), ensure_ascii=False)[:800])
    st, ms = call(c, "GET", f"/v1/projects/{a}/metrics/summary", f"metrics_summary_{args.label}")
    print("metrics/summary:", st, json.dumps(ms, ensure_ascii=False)[:600])
    for kind in ("memory", "cpu"):
        for since, tag in ((now - 1800, "abs"), (1800, "rel")):
            st, m = call(c, "GET", f"/v1/projects/{a}/metrics/{kind}", f"metrics_{kind}_{tag}_{args.label}", params={"since": since})
            res = (m.get("result") or []) if isinstance(m, dict) else []
            pts = sum(len(s.get("values") or []) for s in res)
            first = res[0]["values"][:2] if res and res[0].get("values") else None
            last = res[0]["values"][-1:] if res and res[0].get("values") else None
            print(f"metrics/{kind} since={tag}: {st} series={len(res)} points={pts} first={first} last={last}")
    st, rels = call(c, "GET", f"/v1/projects/{a}/releases", f"releases_{args.label}", params={"page": 1, "count": 20})
    if isinstance(rels, dict):
        print("releases:", st, "total", rels.get("total"), "current", rels.get("currentRelease"))
        for r in (rels.get("releases") or [])[:6]:
            print(f"   {r.get('tag')} {r.get('state')} sourceAvailable={r.get('sourceAvailable')} {r.get('createdAt')}")
    st, logs = call(c, "GET", f"/v2/projects/{a}/logs", f"app_logs_{args.label}",
                    params={"start": now - 1800, "direction": "forward", "last_lines": "false"})
    n = 0
    for stream in (logs.get("data") or []) if isinstance(logs, dict) else []:
        for ts, line in stream.get("values") or []:
            n += 1
            if n <= 40:
                print("  log |", line[:200])
    print("app logs:", st, n, "lines")
    url = f"https://{a}.liara.run/"
    try:
        t0 = time.perf_counter()
        r = httpx.get(url, timeout=20, follow_redirects=True)
        print(f"probe {url}: {r.status_code} in {round((time.perf_counter()-t0)*1000)} ms, body {r.text[:120]!r}")
    except httpx.HTTPError as e:
        print(f"probe {url}: error {type(e).__name__}")


def cmd_envtest(c, args):
    need_yes(args, "changing env vars (restarts the app)")
    a = args.app
    st, p = call(c, "GET", f"/v1/projects/{a}")
    before = (p.get("project") or {}).get("envs") or []
    st, rels = call(c, "GET", f"/v1/projects/{a}/releases", params={"page": 1, "count": 5})
    rel_before = rels.get("total") if isinstance(rels, dict) else None
    secret_value = "l0-secret-" + os.urandom(4).hex()
    variables = [{"key": e["key"], "value": e["value"]} for e in before if e.get("key") not in ("L0_PLAIN", "L0_SECRET")]
    variables += [{"key": "L0_PLAIN", "value": "hello"}, {"key": "L0_SECRET", "value": secret_value, "encrypted": True}]
    t0 = time.time()
    st, res = call(c, "POST", "/v1/projects/update-envs", "update_envs", json={"project": a, "variables": variables})
    print("update-envs:", st, res)
    for _ in range(20):
        time.sleep(3)
        st, ap = call(c, "GET", f"/v1/projects/{a}/applets")
        states = [(x.get("state"), x.get("reason")) for x in (ap.get("applets") or [])] if isinstance(ap, dict) else ap
        print(f"  t+{round(time.time()-t0)}s applets {states}")
        if states and all(s[0] in ("running", "Running", "RUNNING") for s in states) and time.time() - t0 > 10:
            break
    st, p = call(c, "GET", f"/v1/projects/{a}")
    after = (p.get("project") or {}).get("envs") or []
    st, rels = call(c, "GET", f"/v1/projects/{a}/releases", params={"page": 1, "count": 5})
    rel_after = rels.get("total") if isinstance(rels, dict) else None
    sec = next((e for e in after if e.get("key") == "L0_SECRET"), {})
    plain = next((e for e in after if e.get("key") == "L0_PLAIN"), {})
    result = {
        "secret_encrypted_flag": sec.get("encrypted"),
        "secret_value_comes_back_equal": sec.get("value") == secret_value,
        "secret_value_len_returned": len(sec.get("value") or ""),
        "plain_value_comes_back_equal": plain.get("value") == "hello",
        "releases_before": rel_before, "releases_after": rel_after,
        "seconds_until_running": round(time.time() - t0),
        "env_keys_after": [e.get("key") for e in after],
    }
    save("envtest_result", result)
    print("ENV TEST:", json.dumps(result, ensure_ascii=False))


def cmd_delete(c, args):
    need_yes(args, "deleting an app")
    if not state().get(f"app:{args.app}", {}).get("created_by_l0"):
        sys.exit(f"{args.app} was not created by this script; refusing to delete it.")
    st, res = call(c, "DELETE", f"/v1/projects/{args.app}", "delete_project")
    print("DELETE:", st, res)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["read", "create", "deploy", "watch", "redeploy", "inspect", "envtest", "delete"])
    ap.add_argument("--app")
    ap.add_argument("--dir")
    ap.add_argument("--platform", default="flask")
    ap.add_argument("--bundle", default="basic")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--config", help='platformConfig JSON, e.g. {"appModule":"main:app"}')
    ap.add_argument("--message", default="L0 probe")
    ap.add_argument("--label", default="x")
    ap.add_argument("--release", help="label of a stored release (redeploy)")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console + Persian log lines
    if not os.environ.get("LIARA_TEAM_ID") and state().get("team_id"):
        os.environ["LIARA_TEAM_ID"] = state()["team_id"]
    if args.cmd != "read" and not args.app:
        sys.exit("--app is required")
    global TOKEN
    TOKEN = load_token()
    with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120) as c:
        globals()[f"cmd_{args.cmd}"](c, args)


if __name__ == "__main__":
    main()
