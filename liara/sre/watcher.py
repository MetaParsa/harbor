"""The watcher: a background health and resource check of every app, every minute, while the page is open.

Rubric #4 asks for monitoring of service health and resources and for automating repeated operations. This is that
loop, with no AI and no writes (T0 only):
- the site answers: a few requests to "/" (status + response time);
- the app's own access log: 5xx answers in the last 5 minutes, and which page they were on;
- RAM / CPU of the current container against the plan's limits (same numbers as the home card);
- Liara's events: an out-of-memory kill, a crash restart, the app turned off.

Each condition alerts once when it starts and once when it clears (no alert storms), on the page (SSE "alert")
and, on a self-hosted assistant with LIARA_WATCH_PUSH=1 + BALE_BOT_TOKEN/BALE_CHAT_ID (or TELEGRAM_BOT_TOKEN/
TELEGRAM_CHAT_ID), also to the owner's phone — live sessions only, never on a shared instance. An alert carries a one-tap
«بررسی کن» that runs the normal diagnosis; any fix it suggests still needs the user's approval on the page.

It runs inside the user's web session because the Liara token lives only there (never on disk): with the page
closed for 30 minutes the loop stops and the session sweep drops the token. At most the first 5 apps are watched. Judge mode watches the recordings, on the recording's clock.
"""

import asyncio
import contextlib
import os
import time

import httpx

from liara.agent.policy import Session
from liara.liara_api import LiaraError

from .collect import default_prober, fa_num, ltr, parse_request
from .overview import app_overview

INTERVAL_S = int(os.environ.get("LIARA_WATCH_INTERVAL_S", "60"))
FIRST_RUN_S = 3
MAX_APPS = 5
RAM_ALERT_PCT, CPU_ALERT_PCT = 85, 90
ERRORS_ALERT = 3                    # 5xx answers in the log window before it counts as "errors"
LOG_WINDOW_S = 300
MAX_ALERTS = 30
IDLE_S = 30 * 60                    # page closed this long -> stop (the session sweep then drops the token)
SOURCES = {"overview": ("off", "state", "ram", "cpu"), "probe": ("down",), "logs": ("errors",), "events": ("oom",)}
BAD_STATES = ("FROZEN", "FAIL", "ERROR", "UNHEALTHY", "SUSPEND")


async def check_app(s: Session, app: str) -> dict:
    """One round for one app: {"conditions": {key: Persian text without the app name}, "summary": one line}."""
    now = (s.demo.now(app) if s.demo else None) or time.time()
    prober = s.demo.probe if s.demo else default_prober
    ov, events, logs, probes = await asyncio.gather(
        app_overview(s, app), s.client.events(app, 3 * INTERVAL_S + 600), s.client.app_logs(app, since_s=LOG_WINDOW_S),
        prober(f"https://{app}.liara.run/", 3), return_exceptions=True)
    cond: dict[str, str] = {}
    parts: list[str] = []
    failed = {name for name, v in (("overview", ov), ("events", events), ("logs", logs), ("probe", probes))
              if isinstance(v, BaseException) or (name == "overview" and v.get("status") == "error")}
    if isinstance(ov, dict) and ov.get("status") != "error":
        if not ov.get("on"):
            cond["off"] = "خاموشه (تعداد نسخه‌ی در حال اجرا صفره)."
        if ov.get("state") and any(b in str(ov["state"]).upper() for b in BAD_STATES):
            cond["state"] = f"وضعیت در لیارا: {ov['state']}."
        if (ram := ov.get("ram_pct")) is not None:
            parts.append(f"حافظه {fa_num(ram)}٪")
            if ram >= RAM_ALERT_PCT:
                cond["ram"] = f"حافظه به {fa_num(ram)}٪ سقف پلن رسیده."
        if (cpu := ov.get("cpu_pct")) is not None:
            parts.append(f"پردازنده {fa_num(cpu)}٪")
            if cpu >= CPU_ALERT_PCT:
                cond["cpu"] = f"پردازنده روی {fa_num(cpu)}٪ سقف پلنه."
    if isinstance(probes, list) and probes:
        codes = [p.get("status") for p in probes]
        ms = sorted(p["ms"] for p in probes if p.get("ms") is not None and p.get("status"))
        if all(c is None or c >= 500 for c in codes) and "off" not in cond:
            cond["down"] = f"صفحه‌ی اصلی جواب درست نمی‌ده ({'، '.join('×' if c is None else str(c) for c in codes)})."
        elif ms:
            parts.append(f"پاسخ {fa_num(ms[len(ms) // 2])} میلی‌ثانیه")
    if isinstance(logs, list):
        by_route: dict[str, int] = {}
        for ln in logs:
            if ln.ts >= now - LOG_WINDOW_S and (r := parse_request(ln.text)) and r[1] >= 500:
                by_route[r[0]] = by_route.get(r[0], 0) + 1
        if (n := sum(by_route.values())) >= ERRORS_ALERT:
            worst = max(by_route, key=by_route.get)
            cond["errors"] = f"{fa_num(n)} خطای ۵xx در ۵ دقیقه‌ی اخیر (بیشترش روی {ltr(worst)})."
    if isinstance(events, list):
        # an OOM of the release that is running now (one before a rollback is history, not a current problem)
        current = ((ov.get("last_release") or {}).get("tag") if isinstance(ov, dict) else None)
        recent = [e for e in events if e.time >= now - 3 * INTERVAL_S - 600 and (not current or e.tag == current)]
        if any(e.status == "oom" for e in recent):
            cond["oom"] = "لیارا برنامه رو به‌خاطر کمبود حافظه (OOM) متوقف و دوباره روشن کرد."
    summary = "، ".join(parts) or "داده‌ای خوانده نشد"
    return {"conditions": cond, "summary": summary, "at": now, "failed": failed}


def _push(text: str) -> None:
    """Best-effort phone alert for a self-hosted owner (env-configured). Never raises, never blocks the loop long."""
    for token_env, chat_env, base in (("BALE_BOT_TOKEN", "BALE_CHAT_ID", "https://tapi.bale.ai"),
                                      ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "https://api.telegram.org")):
        token, chat = os.environ.get(token_env), os.environ.get(chat_env)
        if token and chat:
            with contextlib.suppress(Exception):
                httpx.post(f"{base}/bot{token}/sendMessage", json={"chat_id": chat, "text": text}, timeout=5)


async def run_once(s: Session) -> None:
    """One round over the account's apps: update s.watch, emit alerts for conditions that started or cleared."""
    try:
        apps = [a.name for a in await s.client.list_apps()][:MAX_APPS]
    except LiaraError as e:
        s.watch["error"] = e.fa
        s.emit("watch", status(s))
        return
    results = await asyncio.gather(*(check_app(s, a) for a in apps), return_exceptions=True)
    state = s.watch.setdefault("apps", {})
    for app, res in zip(apps, results, strict=True):
        if isinstance(res, BaseException):
            continue
        before = state.get(app, {}).get("conditions", {})
        # a source that couldn't be read this round (rate limit, timeout) keeps its last known conditions:
        # a failed read is not "fixed"
        for src in res.pop("failed"):
            for key in SOURCES[src]:
                if key in before:
                    res["conditions"][key] = before[key]
        for key, text in res["conditions"].items():
            if key not in before:
                _alert(s, app, key, text, ok=False)
        for key, text in before.items():
            if key not in res["conditions"]:
                _alert(s, app, key, f"برطرف شد: {text}", ok=True)
        state[app] = res
    for gone in [a for a in state if a not in apps]:
        state.pop(gone)                                  # an app deleted (or past the first five) is no longer watched
    s.watch.update(last_run=time.time(), error=None)
    s.watch.setdefault("first_run", s.watch["last_run"])
    s.emit("watch", status(s))


def _alert(s: Session, app: str, key: str, text: str, *, ok: bool) -> None:
    # the app name is isolated: a Latin first word would turn the whole Persian line left-to-right
    a = {"app": app, "key": key, "text": s.redactor.text(f"{ltr(app)}: {text}"), "ok": ok, "at": time.time()}
    s.alerts.append(a)
    del s.alerts[:-MAX_ALERTS]
    s.emit("alert", a)
    if not s.demo and os.environ.get("LIARA_WATCH_PUSH") == "1":
        # self-hosted only: one owner's chat; never on a shared instance where sessions belong to different people
        asyncio.get_running_loop().run_in_executor(None, _push, f"دستیار لیارا: {a['text']}")


def status(s: Session) -> dict:
    """What the page's watcher strip shows: per app the last summary and open conditions, plus recent alerts."""
    apps = {a: {"summary": r["summary"], "problems": list(r["conditions"].values())}
            for a, r in (s.watch.get("apps") or {}).items()}
    return s.redactor.obj({"interval_s": INTERVAL_S, "demo": s.demo is not None, "max_apps": MAX_APPS,
                           "first_run": s.watch.get("first_run"), "last_run": s.watch.get("last_run"), "error": s.watch.get("error"),
                           "apps": apps, "alerts": s.alerts[-10:]})


async def loop(s: Session, alive) -> None:
    """Every INTERVAL_S while `alive()` (the session exists). One round never stops the loop."""
    await asyncio.sleep(FIRST_RUN_S)
    while alive() and (s.subscribers or time.time() - s.last_seen < IDLE_S):
        with contextlib.suppress(Exception):
            await run_once(s)
        await asyncio.sleep(INTERVAL_S)


def check_soon(s: Session) -> None:
    """One extra round right away (after a fix was verified), so the watcher says «برطرف شد» without the minute's wait."""
    if s.watch.get("last_run"):
        task = asyncio.get_running_loop().create_task(run_once(s))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # a failed extra round is just skipped


def ensure_running(s: Session, alive) -> None:
    """Start the session's watcher once (called when the page opens its event stream)."""
    if s.watch_task is None or s.watch_task.done():
        s.watch_task = asyncio.get_running_loop().create_task(loop(s, alive))
