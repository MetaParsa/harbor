"""Evidence collection for the SRE engine (shaped by what Liara really returns).

All collectors are read-only (T0) and run in parallel with a timeout each. A collector that fails leaves an
"unavailable" entry instead of a guess, and the rules lower their confidence when an input they need is missing.

Live facts used here (liara/l0/RESULTS.md §4, §8):
- release READY says nothing about health; each app log stream carries the release id that produced it;
- Liara emits `oom` → `die` → `start` events on an out-of-memory kill; restarts keep the container id;
- memory comes in bytes per container series, one point per ~20 s; a fresh series' first point is ~2 MB (skip it);
- the summary still lists dead containers right after a deploy → the current container is the newest series;
- edge 502/503 with no app log lines = not routed yet / starting, not an app error.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from liara.liara_api import LiaraClient, LiaraError, probe
from liara.liara_api.models import App, Event, LogLine, MetricSeries, Plan, Release, newest_container
from liara.text import fa_num, ltr  # noqa: F401 — re-exported for the sre package

COLLECTOR_TIMEOUT_S = 8
PROBE_TIMEOUT_S = 15          # probes run one after another on one connection; a slow app is exactly what we must still measure
REQUEST_LINE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\S+) (\d{3}) ([\d.]+) ?ms\b")
NGINX_ACCESS = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD) (\d{3}) \S+ \"(\S+)\"")       # Liara's Flask/nginx line
ERROR_HEAD = re.compile(r"^(?:Uncaught )?((?:[A-Za-z_][\w.]*)?(?:Error|Exception|Exit|Interrupt))\b(?: \[[A-Z_]+\])?:? ?(.*)")
NODE_FRAME = re.compile(r"\(?(/app/[^\s():]+):(\d+):(\d+)\)?")
PY_FRAME = re.compile(r'File "(/app/[^"]+)", line (\d+)')


def parse_request(text: str) -> tuple[str, int, float | None] | None:
    """(path without query, status, server ms) from our Node log format or Liara's nginx access line."""
    if m := REQUEST_LINE.match(text):
        return m.group(2).split("?")[0], int(m.group(3)), float(m.group(4))
    if m := NGINX_ACCESS.match(text):
        return m.group(3).split("?")[0], int(m.group(2)), None
    return None


def _app_frame(path: str) -> bool:
    return "/node_modules/" not in path and "/site-packages/" not in path
INJECTION = re.compile(r"(?i)(ignore (all |the )?(previous|above)|you are (now )?an? |system prompt|approve (it|this|the)|"
                       r"call the tool|run the tool|delete_app|دستور(ات)? قبلی|تأیید کن)")

Prober = Callable[[str, int], Awaitable[list[dict]]]


def default_prober(url: str, n: int) -> Awaitable[list[dict]]:
    return probe(url, n=n)


@dataclass
class Evidence:
    id: str
    kind: str                   # probe | requests | error | release | events | memory | cpu | app | warning
    text: str                   # one Persian line a person can check
    quality: str = "measured"   # measured | inferred | unavailable
    source: str = ""            # the Liara endpoint (or "probe") it came from
    value: Any = None


@dataclass
class Facts:
    app_name: str
    now: float
    app: App | None = None
    plan: Plan | None = None
    plan_list: list[Plan] = field(default_factory=list)          # available Iran plans, cheapest first
    releases: list[Release] = field(default_factory=list)        # newest first
    events: list[Event] = field(default_factory=list)
    memory: list[MetricSeries] = field(default_factory=list)
    cpu: list[MetricSeries] = field(default_factory=list)
    logs: list[LogLine] = field(default_factory=list)
    probes: dict[str, list[dict]] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)          # collector -> why it's unavailable

    # ---- derived views ----
    @property
    def current_release(self) -> Release | None:
        return self.releases[0] if self.releases else None

    def requests(self) -> list[dict]:
        out = []
        for ln in self.logs:
            if r := parse_request(ln.text):
                out.append({"ts": ln.ts, "path": r[0], "status": r[1], "ms": r[2], "release": ln.release_id})
        return out

    def tracebacks(self) -> list[dict]:
        """Error head + the app frame that matters, from consecutive stderr lines.

        Node prints the head first and frames after it (take the first /app/ frame outside node_modules);
        Python prints frames first and the head last (take the last /app/ frame before the head).
        """
        out, cur, py_frames = [], None, []
        for ln in self.logs:
            if ln.stream != "stderr":
                continue
            text = ln.text.strip()
            if m := PY_FRAME.search(text):
                if _app_frame(m.group(1)):
                    py_frames.append((m.group(1), int(m.group(2))))
                continue
            if m := ERROR_HEAD.match(text):
                cur = {"ts": ln.ts, "error": m.group(1), "message": m.group(2)[:200], "release": ln.release_id,
                       "file": None, "line": None}
                if py_frames:
                    cur["file"], cur["line"] = py_frames[-1]
                py_frames = []
                out.append(cur)
            elif cur and cur["file"] is None and (m := NODE_FRAME.search(text)) and _app_frame(m.group(1)):
                cur["file"], cur["line"] = m.group(1), int(m.group(2))
        return out

    def suspicious_lines(self) -> list[str]:
        return [ln.text[:160] for ln in self.logs if INJECTION.search(ln.text)]

    def current_series(self, kind: str = "memory") -> MetricSeries | None:
        """Dead containers stay in the series for the whole window; the current one started last."""
        series = self.memory if kind == "memory" else self.cpu
        name = newest_container(series)
        return next((s for s in series if s.container == name), None)


def trim_startup(points: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Drop leading samples taken while the container was still starting (~2 MB live), i.e. far below the rest."""
    if len(points) < 3:
        return points
    rest = sorted(v for _, v in points[1:])
    median = rest[len(rest) // 2]
    i = 0
    while i < len(points) - 2 and points[i][1] < 0.5 * median:
        i += 1
    return points[i:]


def release_ts(r: Release) -> float | None:
    try:
        return datetime.fromisoformat(r.created_at.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


async def guard(name: str, coro, facts: Facts, timeout: float = COLLECTOR_TIMEOUT_S):
    try:
        return await asyncio.wait_for(coro, timeout)
    except (LiaraError, TimeoutError, OSError) as e:
        facts.missing[name] = getattr(e, "kind", None) or type(e).__name__
        return None


async def collect(client: LiaraClient, app: str, routes: list[str], *, prober: Prober | None = None,
                  now: float | None = None) -> Facts:
    """Probe the site, then read everything from Liara in parallel. `routes` are probed (always "/" plus the failing route if we know it)."""
    f = Facts(app, now or time.time())
    url = f"https://{app}.liara.run"
    prober = prober or default_prober
    routes = list(dict.fromkeys(["/"] + [r for r in routes if r and r.startswith("/")]))[:3]
    # Probes first, on their own: run alongside the API calls their timings came out ~3 s instead of ~0.1 s (live,
    # 2026-10-22), which made a fast app look slow. A response time is only worth showing if it's the app's.
    probes = await asyncio.gather(*[guard(f"probe {r}", prober(url + r, 5), f, PROBE_TIMEOUT_S) for r in routes])
    results = await asyncio.gather(
        guard("app", client.get_app(app), f),
        guard("releases", client.releases(app, 6), f),
        guard("events", client.events(app, 3600), f),
        guard("memory", client.metrics(app, "memory", 1800), f),
        guard("cpu", client.metrics(app, "cpu", 1800), f),
        guard("logs", client.app_logs(app, since_s=1800), f),
        guard("plans", client.plans(), f),
    )
    a, rels, ev, mem, cpu, logs, plans = results
    f.app, f.events, f.memory, f.cpu, f.logs = a, ev or [], mem or [], cpu or [], logs or []
    f.releases = rels.items if rels else []
    if a and plans:
        f.plan = plans.apps.get(a.plan_id)
        f.plan_list = plans.iran_available()
    f.probes = {r: p for r, p in zip(routes, probes) if p is not None}
    return f


# ---------- ledger: facts → numbered evidence a person (and the LLM) can cite ----------

def build_ledger(f: Facts) -> list[Evidence]:
    ev: list[Evidence] = []

    def add(kind, text, quality="measured", source="", value=None) -> str:
        e = Evidence(f"E{len(ev) + 1}", kind, text, quality, source, value)
        ev.append(e)
        return e.id

    for route, ps in f.probes.items():
        codes = [p.get("status") for p in ps]
        ms = sorted(p["ms"] for p in ps if p.get("ms") is not None)
        med = ms[len(ms) // 2] if ms else None
        add("probe", f"بررسی خودمون روی {ltr(route)}: {'، '.join('×' if c is None else fa_num(c) for c in codes)} (میانه {fa_num(med or 0)} میلی‌ثانیه)", source="probe",
            value={"route": route, "codes": codes, "median_ms": med})
    cur = f.current_release
    reqs = f.requests()
    if cur and any(r["release"] == cur.id for r in reqs):
        reqs = [r for r in reqs if r["release"] == cur.id]     # what the running release does, not history
    by_route: dict[str, dict[int, int]] = {}
    for r in reqs:
        by_route.setdefault(r["path"], {}).setdefault(r["status"], 0)
        by_route[r["path"]][r["status"]] += 1
    for path, codes in sorted(by_route.items(), key=lambda kv: -sum(v for k, v in kv[1].items() if k >= 500)):
        errors = sum(v for k, v in codes.items() if k >= 500)
        total = sum(codes.values())
        add("requests", f"در لاگ برنامه: {ltr(path)} — {fa_num(errors)} خطای ۵xx از {fa_num(total)} درخواست",
            source="GET /v2/projects/{app}/logs", value={"path": path, "errors": errors, "total": total})
    seen: dict[tuple, dict] = {}                 # the same crash repeated per request → one line with a count
    for tb in f.tracebacks():
        key = (tb["error"], tb["message"], tb["file"], tb["line"], tb["release"])
        seen.setdefault(key, {**tb, "count": 0})["count"] += 1
        seen[key]["ts"] = tb["ts"]
    for tb in sorted(seen.values(), key=lambda t: t["ts"])[-3:]:
        where = f" در {ltr(tb['file'] + ':' + str(tb['line']))}" if tb["file"] else ""
        rel = next((r.tag for r in f.releases if r.id == tb["release"]), None)
        times = f" — {fa_num(tb['count'])} بار" if tb["count"] > 1 else ""
        old = cur is not None and tb["release"] not in (None, cur.id)   # a crash from an earlier release is history
        add("error", ("تاریخچه — " if old else "") + ltr(f"{tb['error']}: {tb['message']}") + where
            + (f" (نسخه‌ی {rel})" if rel else "") + times,
            source="GET /v2/projects/{app}/logs", value=tb)
    for r in f.releases[:3]:
        t = release_ts(r)
        ago = f"{fa_num((f.now - t) / 60)} دقیقه پیش" if t else ""
        kind = "تغییر تنظیمات (ENV_UPDATE)" if r.type == "ENV_UPDATE" else "دیپلوی کد"
        add("release", f"نسخه‌ی {r.tag}: {kind}، {ago}", source="GET /v1/projects/{app}/releases",
            value={"id": r.id, "tag": r.tag, "type": r.type, "ts": t})
    if f.events:
        tag = cur.tag if cur else None
        mine = [e for e in f.events if tag is None or e.tag == tag]      # the running release only
        ooms = sum(1 for e in mine if e.status == "oom")
        starts: dict[str, int] = {}
        for e in mine:
            if e.status == "start":
                starts[e.container] = starts.get(e.container, 0) + 1
        restarts = sum(n - 1 for n in starts.values())
        older_ooms = sum(1 for e in f.events if e.status == "oom") - ooms
        add("events", f"رویدادهای نسخه‌ی فعلی{f' ({tag})' if tag else ''}: {fa_num(ooms)} بار کمبود حافظه (OOM)، "
            f"{fa_num(restarts)} ری‌استارت" + (f" — نسخه‌های قبلی: {fa_num(older_ooms)} OOM" if older_ooms else ""),
            source="GET /v1/projects/{app}/events", value={"ooms": ooms, "restarts": restarts, "older_ooms": older_ooms})
    s = f.current_series()
    if s and f.plan:
        pts = trim_startup(s.points)                                     # skip the ~2 MB startup sample(s)
        mb = s.last / 1048576
        pct = 100 * s.last / (f.plan.ram_gb * 1024 ** 3) if f.plan.ram_gb else None
        slope = None
        if len(pts) >= 3 and pts[-1][0] > pts[0][0]:
            slope = (pts[-1][1] - pts[0][1]) / 1048576 / ((pts[-1][0] - pts[0][0]) / 60)
        add("memory", f"حافظه: {fa_num(mb)} مگابایت از {fa_num(f.plan.ram_gb * 1024)}"
            + (f" ({fa_num(pct)}٪)" if pct is not None else "")
            + (f"، روند {'+' if slope >= 0 else ''}{fa_num(slope, 1)} مگابایت در دقیقه" if slope is not None else ""),
            source="GET /v1/projects/{app}/metrics/memory",
            value={"mb": mb, "pct": pct, "slope_mb_min": slope, "points": len(pts), "limit_mb": f.plan.ram_gb * 1024})
    elif "memory" not in f.missing:
        why = "پلن برنامه خوانده نشد" if s and not f.plan else "داده‌ی حافظه هنوز کافی نیست"
        add("memory", why, "unavailable", "GET /v1/projects/{app}/metrics/memory")
    cs = f.current_series("cpu")
    cpu_now = cs.last if cs else None
    if cpu_now is not None and f.plan:
        pct = 100 * cpu_now / f.plan.cpu if f.plan.cpu else None
        add("cpu", f"پردازنده: {fa_num(pct or 0)}٪ از سقف پلن", source="GET /v1/projects/{app}/metrics/cpu",
            value={"cores": cpu_now, "pct": pct})
    if f.app:
        add("app", f"وضعیت برنامه: {f.app.status}، {'روشن' if f.app.scale else 'خاموش'}، پلن {f.app.plan_id}",
            source="GET /v1/projects/{app}", value={"status": f.app.status, "scale": f.app.scale})
    for line in f.suspicious_lines()[:2]:
        add("warning", "⚠ متن شبیه دستور در لاگ — فقط به‌عنوان داده خوانده شد", "measured", "logs", {"line": line})
    for name, why in f.missing.items():
        add("unavailable", f"{name} از API لیارا خوانده نشد ({why})", "unavailable", name)
    return ev
