"""REPLAY mode: answer LiaraClient calls from recorded fixtures instead of the network.

Used by tests and as a disclosed demo fallback («نمایش ضبط‌شده»). Recorded fixtures come from live runs
(liara/fixtures/api/, redacted); calls L0 never made are answered from liara/fixtures/synthetic/ and every
response from there carries the header `x-replay-source: synthetic` so nobody mistakes it for a real answer.

A scenario picks which recording to use for per-app calls, e.g. "d_raw2" (crash-looping app) or "d_fixed" (healthy).

DemoReplay is judge mode: three apps answered from real recordings of the demo shop on Liara, on the recording's
clock, with each app's one recorded fix simulated (labelled) and its after-state served from a real recording;
RecordedLLM replays Claude's real answers for those apps.
"""

import asyncio
import json
import re
from collections import deque
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
V1_RELEASE = "6ad9e2c163b87278cbb18303"      # demo-shop v1 on Liara, recorded healthy (fixtures/demo/v1_healthy)
ROLLBACK_RELEASE = "6ad9e5bc41fc53102532c7cd"  # the real rollback to v1's code (fixtures/demo/rollback_v1)

# Judge mode lets the judge approve each app's recommended fix. Nothing is sent anywhere: the write is answered with
# Liara's real answer to the same call (recorded), and from then on the app is served from a real recording of
# the same shop on Liara after that kind of fix. `at` = when the change took effect in that recording.
DEMO_AFTER = {
    "shop-missing-env": {"fix": "set_envs", "label": "v1_healthy", "at": 1792664303,
                         "fa": "همین فروشگاه روی لیارا وقتی PAYMENT_API_URL تنظیم بود (ضبط واقعی، ۳۰ مهر)"},
    "shop-code-bug": {"fix": "rollback", "label": "v1_healthy", "at": 1792664303,
                      "fa": "همین فروشگاه روی لیارا با کد سالم v1 (ضبط واقعی، ۳۰ مهر)"},
    "shop-memory-leak": {"fix": "rollback", "label": "after_rollback", "at": 1792665072,
                         "fa": "همین فروشگاه روی لیارا چند دقیقه بعد از برگشت واقعی به v1 (ضبط واقعی، ۳۰ مهر)"},
}
WRITE_ANSWERS = {"set_envs": "envs", "rollback": "rollback_v1"}    # the recordings holding Liara's real answers
DEMO_REFUSED = {"error": "replay_read_only", "message": "demo mode: only each app's recorded fix can run"}
# The complaint each app's model answers were recorded with (scripts/record_llm.py → fixtures/demo/llm/<app>.json)
DEMO_RECORDED_TEXT = {"shop-missing-env": "صفحه‌ی پرداخت خطا میده", "shop-code-bug": "صفحه‌ی پرداخت خطا میده",
                      "shop-memory-leak": "سایت هی قطع میشه"}


class RecordedLLM:
    """Judge mode's model: Claude's real answers, recorded once for this app's broken state (no spend per visit).

    The ranking/explain answer depends only on the evidence, which is the same recording, so it is replayed for any
    wording. The complaint-understanding answer is replayed only for the exact recorded wording; any other wording
    gets no answer here, so the rules read it (and the board says which one did).
    """

    recorded = True

    def __init__(self, rec: dict, text: str):
        self.rec, self.text = rec, text
        self.models = {"chat": (rec["answers"].get("chat") or {}).get("model", "claude")}

    def complete(self, role: str, _messages, **_kw):
        from core.llm import LLMError, LLMResult
        a = self.rec["answers"].get(role)
        same = re.sub(r"[\s‌]", "", self.text) == re.sub(r"[\s‌]", "", self.rec["text"])   # «می‌ده» = «میده»
        if not a or (role == "router" and not same):
            raise LLMError("not recorded")
        return LLMResult(a["text"], [], a.get("usage") or {}, "recorded", a.get("model", ""), 0)


class DemoReplay(httpx.AsyncBaseTransport):
    """Judge mode: three apps, each answered from one real recording of the demo shop on Liara (fixtures/demo/), with
    no account and no network.

    shop-missing-env → a deploy dropped PAYMENT_API_URL (500 on /checkout) · shop-code-bug → a code release throws a
    TypeError at server.js:37 · shop-memory-leak → memory climbs until Liara kills the container (OOM).

    Writes: only the one fix per app that was really done and recorded (DEMO_AFTER) "runs" — simulated, said so on
    the card and the verify result — and the app then switches to its recorded after-state. Any other write is
    refused. Each session gets its own DemoReplay, so a new session starts from the broken state again.
    """

    def __init__(self):
        self.labels = {app: LabelReplay(label) for app, label in DEMO_APPS.items()}
        self.after: dict[str, LabelReplay] = {}          # app -> its after-state, once the judge approved its fix
        latest = {f"GET /v1/releases/{ROLLBACK_RELEASE}", f"GET /v2/releases/{ROLLBACK_RELEASE}/build-logs"}
        self.answers = {fix: LabelReplay(label, latest=latest) for fix, label in WRITE_ANSWERS.items()}
        self.requests: deque[httpx.Request] = deque(maxlen=500)

    def fixed(self, app: str) -> str | None:
        """Where the after-state comes from (Persian), once the judge's approved fix has been simulated."""
        return DEMO_AFTER[app]["fa"] if app in self.after else None

    @staticmethod
    def release_bodies() -> dict[str, dict[str, dict]]:
        """What a rollback needs: v1's source + body, stored when v1 was deployed (as in a live session)."""
        return {app: {V1_RELEASE: {"recorded": True}} for app, a in DEMO_AFTER.items() if a["fix"] == "rollback"}

    def llm(self, app: str, text: str) -> RecordedLLM | None:
        """Claude's recorded answers for this app, while it is still in its recorded broken state."""
        f = FIXTURES / "demo" / "llm" / f"{app}.json"
        if app in self.after or not f.exists():
            return None
        return RecordedLLM(json.loads(f.read_text(encoding="utf-8")), text)

    def env_history(self) -> dict[str, list[set[str]]]:
        """The env keys the session saw before the fault (as a live session that watched the app would have):
        the recording of v1 was taken before the variable was dropped."""
        rec = LabelReplay("v1_healthy").by_req[f"GET /v1/projects/{RECORDED_APP}"][0]["body"]["project"]
        return {"shop-missing-env": [{e["key"] for e in rec.get("envs") or []}]}

    def _current(self, app: str) -> "LabelReplay | None":
        return self.after.get(app) or self.labels.get(app)

    def now(self, app: str) -> float | None:
        """The moment the recording was taken: "the last 30 minutes" must mean the recording's, not today's."""
        if not (t := self._current(app)):
            return None
        if at := t.summary().get("at"):
            return at
        watch = t.watch()
        return watch[-1]["t"] + 5 if watch else None

    def verify_args(self, app: str) -> dict:
        """verify() over the after-recording: its clock, the moment the change took effect, a short pause instead
        of the live settle window, and no new container to wait for (nothing was deployed — the card says so)."""
        now = self.now(app) or 0

        async def short_sleep(seconds: float) -> None:
            await asyncio.sleep(min(seconds, 1.5))

        base = {"prober": self.probe, "clock": lambda: now, "sleep": short_sleep, "settle_s": 0, "container_wait_s": 0}
        if app not in self.after:       # nothing was changed (e.g. the code-fix option): verify the recorded broken state
            return {**base, "started_at": now - 120, "recorded": "همین برنامه هنوز بدون تعمیر (ضبط واقعی)؛ "
                    "در نسخه‌ی نمایشی ایجنت کدنویسی کدی نفرستاده"}
        return {**base, "started_at": DEMO_AFTER[app]["at"], "recorded": DEMO_AFTER[app]["fa"]}

    async def probe(self, url: str, n: int = 5) -> list[dict]:
        """The site checks made while recording (they can't be re-run: the faults were fixed afterwards)."""
        host, _, path = url.removeprefix("https://").partition("/")
        app = host.removesuffix(".liara.run")
        t = self._current(app)
        recorded = (t.summary().get("probes") or {}) if t else {}
        if t and not recorded and (watch := t.watch()):
            # a watch recording checks "/" every ~20 s; only the checks after the change count
            at = DEMO_AFTER.get(app, {}).get("at", 0)
            recorded = {"/": [w["probe"] for w in watch if w["t"] >= at + 20]}
        route = "/" + path.split("?")[0]
        hits = next((v for k, v in recorded.items() if k.split("?")[0] == route), None)
        return (hits or [{"status": None, "ms": 0, "error": "not_recorded"}])[:n]

    def _write(self, request: httpx.Request) -> httpx.Response:
        """The recorded fix of a demo app: Liara's real recorded answer, then the app switches to its after-state."""
        path, app, fix = request.url.path, None, None
        if path == "/v1/projects/update-envs":
            try:
                body = json.loads(request.content or b"{}")
                given = {v.get("key") for v in body.get("variables") or [] if isinstance(v, dict) and v.get("value")}
                # only the recorded fix counts: the variable the fault dropped, with a value
                if given >= {k for keys in self.env_history().get(body.get("project"), []) for k in keys}:
                    app, fix = body.get("project"), "set_envs"
            except (ValueError, AttributeError, TypeError):
                pass
        elif m := re.fullmatch(r"/v2/projects/([^/]+)/releases", path):
            app, fix = m.group(1), "rollback"
        a = DEMO_AFTER.get(app) if isinstance(app, str) else None
        if not a or a["fix"] != fix or app in self.after:
            return httpx.Response(403, json=DEMO_REFUSED)
        rec = self.answers[fix].by_req[f"{request.method} {path.replace(app, RECORDED_APP)}"][0]
        self.after[app] = LabelReplay(a["label"], latest={f"GET /v1/projects/{RECORDED_APP}/metrics/memory",
                                                          f"GET /v1/projects/{RECORDED_APP}/metrics/summary",
                                                          f"GET /v1/projects/{RECORDED_APP}/events"})
        return httpx.Response(rec["status"], json=rec["body"], headers={"x-replay-source": "simulated"})

    def _with_rollback(self, body: dict) -> dict:
        """The release list after the rollback: the recorded list plus the recorded rollback release (v6) on top."""
        rel = self.answers["rollback"].by_req[f"GET /v1/releases/{ROLLBACK_RELEASE}"][0]["body"]["release"]
        rel = {**rel, "state": "READY", "tag": "v6", "message": "rollback to v1"}   # READY at the end of the recorded build
        return {**body, "total": body.get("total", 0) + 1, "currentRelease": ROLLBACK_RELEASE,
                "releases": [rel, *body.get("releases", [])]}

    async def _app_call(self, app: str, key: str) -> httpx.Response:
        """A per-app read: from the after-state once fixed (logs only if recorded there), else the broken state."""
        after = self.after.get(app)
        t = after if after and key in after.by_req else self.labels[app]
        if after and t is not after and key.endswith("/logs"):
            return httpx.Response(404, json={"error": "demo: no app logs were recorded after this fix"})
        recs = t.by_req.get(key)
        if not recs:
            return httpx.Response(404, json={"error": f"demo: nothing recorded for {key}"})
        if after and t is not after and key.endswith("/releases") and DEMO_AFTER[app]["fix"] == "rollback":
            return httpx.Response(200, json=self._with_rollback(recs[-1]["body"]), headers={"x-replay-source": "simulated"})
        if t is after:
            i = t.seen.get(key, 0)
            t.seen[key] = i + 1
            rec = recs[-1] if key in t.latest else recs[min(i, len(recs) - 1)]
        else:
            rec = recs[-1]
        body = rec["body"]
        if isinstance(body, dict) and isinstance(body.get("project"), dict):
            body = {**body, "project": {**body["project"], "project_id": app}}
        return httpx.Response(rec["status"], json=body, headers={"x-replay-source": "recorded"})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        if method != "GET":
            return self._write(request)
        if path.startswith((f"/v1/releases/{ROLLBACK_RELEASE}", f"/v2/releases/{ROLLBACK_RELEASE}/")):
            return await self.answers["rollback"].handle_async_request(request)   # the rollback's build, as recorded
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
            return await self._app_call(m.group(2), f"GET {m.group(1)}{RECORDED_APP}{m.group(3) or ''}")
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
        self.requests: deque[httpx.Request] = deque(maxlen=500)

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

    def watch(self) -> list[dict]:
        f = self.dir / "watch.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, scenario: str = "d_fixed", overrides: dict[str, str] | None = None):
        self.scenario = scenario
        self.overrides = overrides or {}       # fixture name -> other fixture name (e.g. events_d_fixed -> events_d_raw2)
        self.requests: deque[httpx.Request] = deque(maxlen=500)

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
