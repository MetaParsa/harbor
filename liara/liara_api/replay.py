"""REPLAY mode: answer LiaraClient calls from recorded fixtures instead of the network.

Used by tests and as a disclosed demo fallback («نمایش ضبط‌شده»). Recorded fixtures come from live runs
(liara/fixtures/api/, redacted); calls L0 never made are answered from liara/fixtures/synthetic/ and every
response from there carries the header `x-replay-source: synthetic` so nobody mistakes it for a real answer.

A scenario picks which recording to use for per-app calls, e.g. "d_raw2" (crash-looping app) or "d_fixed" (healthy).
"""

import json
import re
from pathlib import Path

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# (method, path regex, fixture name; "{s}" = scenario suffix, "{kind}" = metric kind)
ROUTES = [
    ("GET", r"/v1/me", "api/me_plans"),
    ("GET", r"/v1/billing", "api/billing"),
    ("GET", r"/v1/me/free-credit", "api/free_credit"),
    ("GET", r"/v2/teams", "synthetic/teams"),
    ("GET", r"/v1/networks", "api/networks"),
    ("POST", r"/v1/networks", "api/create_network"),
    ("GET", r"/v1/projects", "synthetic/projects_list"),
    ("POST", r"/v1/projects", "api/create_project"),
    ("POST", r"/v1/projects/update-envs", "api/update_envs"),
    ("GET", r"/v1/projects/[^/]+", "api/project_{s}"),
    ("DELETE", r"/v1/projects/[^/]+", "api/delete_project"),
    ("GET", r"/v1/projects/[^/]+/events", "api/events_{s}"),
    ("GET", r"/v1/projects/[^/]+/metrics/summary", "api/metrics_summary_{s}"),
    ("GET", r"/v1/projects/[^/]+/metrics/(?P<kind>cpu|memory)", "api/metrics_{kind}_{since}_{s}"),
    ("GET", r"/v1/projects/[^/]+/releases", "api/releases_{s}"),
    ("POST", r"/v1/projects/[^/]+/actions/(restart|scale)", "synthetic/ok"),
    ("POST", r"/v1/projects/[^/]+/resize", "synthetic/ok"),
    ("POST", r"/v1/projects/[^/]+/zero-downtime/(enable|disable)", "synthetic/ok"),
    ("GET", r"/v1/releases/[^/]+", "synthetic/release_status"),
    ("GET", r"/v2/releases/[^/]+/build-logs", "api/build_logs_{s}"),
    ("POST", r"/v2/projects/[^/]+/sources", "api/upload_{s}"),
    ("POST", r"/v2/projects/[^/]+/releases", "api/release_{s}"),
    ("POST", r"/v[12]/projects/[^/]+/releases/[^/]+/rollback", "api/rollback_try"),
    ("GET", r"/v2/projects/[^/]+/logs", "api/app_logs_{s}"),
    ("GET", r"/v1/domains", "synthetic/domains_list"),
    ("POST", r"/v1/domains", "synthetic/domain_add"),
    ("GET", r"/v1/domains/[^/]+/check", "synthetic/domain_check"),
    ("POST", r"/v1/domains/provision-ssl-certs", "synthetic/ok"),
]


DEMO_APPS = {"shop-missing-env": "env_missing", "shop-code-bug": "v2_regression", "shop-memory-leak": "v3_oom"}
RECORDED_APP = "dastyar-demo-shop"


class DemoReplay(httpx.AsyncBaseTransport):
    """Judge mode: three apps, each answered from one real recording of the demo shop on Liara (fixtures/demo/), with
    no account and no network. Reads only — every write is refused with a clear message (nothing is pretended).

    shop-missing-env → a deploy dropped PAYMENT_API_URL (500 on /checkout) · shop-code-bug → a code release throws a
    TypeError at server.js:37 · shop-memory-leak → memory climbs until Liara kills the container (OOM).
    """

    def __init__(self):
        self.labels = {app: LabelReplay(label) for app, label in DEMO_APPS.items()}
        self.requests: list[httpx.Request] = []

    def now(self, app: str) -> float | None:
        """The moment the recording was taken: "the last 30 minutes" must mean the recording's, not today's."""
        t = self.labels.get(app)
        return t.summary().get("at") if t else None

    async def probe(self, url: str, n: int = 5) -> list[dict]:
        """The site checks made while recording (they can't be re-run: the faults were fixed afterwards)."""
        host, _, path = url.removeprefix("https://").partition("/")
        t = self.labels.get(host.removesuffix(".liara.run"))
        recorded = (t.summary().get("probes") or {}) if t else {}
        route = "/" + path.split("?")[0]
        hits = next((v for k, v in recorded.items() if k.split("?")[0] == route), None)
        return (hits or [{"status": None, "ms": 0, "error": "not_recorded"}])[:n]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        if method != "GET":
            return httpx.Response(403, json={"error": "replay_read_only", "message": "demo mode is read-only"})
        if path == "/v2/teams":
            return httpx.Response(200, json={"teams": []}, headers={"x-replay-source": "synthetic"})
        if path == "/v1/me":
            return ReplayTransport._answer("api/me_plans")
        if path == "/v1/projects":
            projects = []
            for app, t in self.labels.items():
                rec = t.by_req.get(f"GET /v1/projects/{RECORDED_APP}")
                if rec:
                    projects.append({**rec[0]["body"]["project"], "project_id": app})
            return httpx.Response(200, json={"projects": projects}, headers={"x-replay-source": "recorded"})
        m = re.fullmatch(r"(/v[12]/projects/)([^/]+)(/.*)?", path)
        if m and m.group(2) in self.labels:
            t = self.labels[m.group(2)]
            recs = t.by_req.get(f"GET {m.group(1)}{RECORDED_APP}{m.group(3) or ''}")
            if recs:
                body = recs[-1]["body"]
                if isinstance(body, dict) and isinstance(body.get("project"), dict):
                    body = {**body, "project": {**body["project"], "project_id": m.group(2)}}
                return httpx.Response(recs[-1]["status"], json=body, headers={"x-replay-source": "recorded"})
        return httpx.Response(404, json={"error": f"demo: nothing recorded for {method} {path}"})


class LabelReplay(httpx.AsyncBaseTransport):
    """Replay one recorded scenario folder (fixtures/demo/<label>/, written by scripts/demo_dry_run.py).

    Files are `NNN_<METHOD>_<path slug>.json`; repeated calls to the same path are served in recording order
    (the last one repeats). /v1/me was never recorded (personal data), so plans come from a live recording.
    """

    def __init__(self, label: str, root: Path | None = None, latest: set[str] = frozenset()):
        self.dir = (root or FIXTURES / "demo") / label
        self.latest = latest            # request keys served from their LAST recording (e.g. metrics after a change)
        self.by_req: dict[str, list[dict]] = {}
        for f in sorted(self.dir.glob("[0-9][0-9][0-9]_*.json")):
            rec = json.loads(f.read_text(encoding="utf-8"))
            self.by_req.setdefault(rec["request"], []).append(rec)
        self.seen: dict[str, int] = {}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        if key == "GET /v1/me":
            return ReplayTransport._answer("api/me_plans")
        recs = self.by_req.get(key)
        if not recs:
            return httpx.Response(404, json={"error": f"replay: {self.dir.name} has no {key}"})
        i = self.seen.get(key, 0)
        self.seen[key] = i + 1
        rec = recs[-1] if key in self.latest else recs[min(i, len(recs) - 1)]
        body = rec.get("body")
        if isinstance(body, str):
            return httpx.Response(rec["status"], text=body, headers={"x-replay-source": "recorded"})
        return httpx.Response(rec["status"], json=body, headers={"x-replay-source": "recorded"})

    def summary(self) -> dict:
        f = self.dir / "summary.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, scenario: str = "d_fixed", overrides: dict[str, str] | None = None):
        self.scenario = scenario
        self.overrides = overrides or {}       # fixture name -> other fixture name (e.g. events_d_fixed -> events_d_raw2)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        for m, pattern, name in ROUTES:
            match = re.fullmatch(pattern, path)
            if m != method or not match:
                continue
            since = request.url.params.get("since") or ""
            fields = {"s": self.scenario, "kind": match.groupdict().get("kind") or "",
                      "since": "rel" if since.isdigit() and int(since) < 1_000_000_000 else "abs"}
            chosen = self.overrides.get(name.format(**fields), name.format(**fields))
            if not (FIXTURES / f"{chosen}.json").exists() and "{s}" in name:
                # scenarios without a recording for this call (e.g. d_raw2 had no upload) fall back to d_fixed
                chosen = self.overrides.get(name.format(**{**fields, "s": "d_fixed"}), name.format(**{**fields, "s": "d_fixed"}))
            return self._answer(chosen)
        return httpx.Response(404, json={"error": f"replay: no fixture for {method} {path}"})

    @staticmethod
    def _answer(name: str) -> httpx.Response:
        f = FIXTURES / f"{name}.json"
        if not f.exists():
            return httpx.Response(404, json={"error": f"replay: missing fixture {name}"})
        rec = json.loads(f.read_text(encoding="utf-8"))
        body = rec.get("body")
        source = "synthetic" if name.startswith("synthetic/") else "recorded"
        if isinstance(body, str):
            # rollback_try stores {path: {status, body}}; plain-text bodies (e.g. DELETE) pass through
            return httpx.Response(rec.get("status", 200), text=body, headers={"x-replay-source": source})
        if body is None and name == "api/rollback_try":
            first = next(iter(rec.values()))
            return httpx.Response(first["status"], text=first["body"], headers={"x-replay-source": source})
        return httpx.Response(rec.get("status", 200), json=body, headers={"x-replay-source": source})
