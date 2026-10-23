"""Rule-scored hypotheses over the evidence ledger. Deterministic: works with the LLM off.

Each hypothesis cites evidence ids, carries a confidence (capped at 0.6 when an input it needs is missing),
and comes with fix options. A fix option is either an action for the policy (tool + args + tier), a hand-off
to the user's coding agent (files + brief), a wait, or a manual step Liara's API can't do (capability).
Every action option has a success contract that verify.py checks after it runs.
"""

import re
from dataclasses import dataclass, field

from .collect import Evidence, Facts, fa_num, ltr, release_ts

ENV_HINT = re.compile(r"(?i)(invalid url|keyerror|is not defined|undefined|environ|getenv|process\.env|not set|missing|"
                      r"no such|connection refused|ECONNREFUSED|ENOTFOUND)")
# Errors that point at configuration whatever the release type (not "undefined": that's usually a code bug).
# Live 2026-10-22: a v3 deploy whose release body carried `envs` replaced the env list and dropped
# PAYMENT_API_URL; the next code release showed `TypeError: Invalid URL` — a config fault on a DEPLOY release.
CONFIG_HINT = re.compile(r"(?i)(invalid url|ERR_INVALID_URL|keyerror|environ|getenv|process\.env|not set|"
                         r"connection refused|ECONNREFUSED|ENOTFOUND|getaddrinfo|ImproperlyConfigured)")
MISSING_INPUT_CAP = 0.6


@dataclass
class Hypothesis:
    id: str                      # P1 regression · P2 config · P3 memory-leak / P3u undersized · P4 cpu · P5 crash loop
    title: str                   # one Persian sentence                               P6 slow dependency · P7 starting · P8 frozen
    confidence: float
    supports: list[str]
    fixes: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)    # for the coding agent: {path, line, why}


@dataclass
class Diagnosis:
    hypotheses: list[Hypothesis]
    ruled_out: list[dict]        # {text, evidence}
    unknowns: list[str]          # evidence ids of things we couldn't see


def _ids(ledger: list[Evidence], kind: str, pred=lambda e: True) -> list[str]:
    return [e.id for e in ledger if e.kind == kind and pred(e)]


def _val(ledger, kind):
    return next((e.value for e in ledger if e.kind == kind and e.quality == "measured"), None)


def contract(route: str = "/", *, new_container: bool = False, memory_flat: bool = False) -> dict:
    return {"route": route, "probes": 5, "max_status": 399, "log_window_s": 120, "new_container": new_container,
            "memory_flat": memory_flat}


def contract_fa(c: dict) -> str:
    s = f"{fa_num(c['probes'])} از {fa_num(c['probes'])} درخواست به {ltr(c['route'])} جواب زیر ۴۰۰ بده و تا ۲ دقیقه خطای تازه در لاگ نباشه"
    if c.get("memory_flat"):
        s += "، و حافظه دیگه بالا نره"
    return s + "."


def _rollback_target(f: Facts, known_good: list[str]):
    """Latest earlier release we verified healthy; else the previous code release, flagged as unverified.
    (The release right before a bad one is often itself bad — demo-shop v3's predecessor had the coupon bug.)"""
    cur = f.current_release
    earlier = [r for r in f.releases if cur and r.id != cur.id and (release_ts(r) or 0) <= (release_ts(cur) or 0)]
    good = next((r for r in earlier if r.id in known_good), None)
    if good:
        return good, True
    return next((r for r in earlier if r.type == "DEPLOY"), None), False


def ground_route(symptom: dict, f: Facts, text: str = "") -> dict:
    """The page the user (or the model reading them) named must exist: if the app's log never served it and our
    check of it got only 404s, while another page is failing, that failing page is the one meant. Seen live:
    «صفحه‌ی پرداخت» read as /payment while the shop's checkout is /checkout. The guess is kept for the board."""
    named = (symptom.get("route") or "").split("?")[0]
    if not named or named in text:              # a page the user typed is what they mean
        return symptom
    reqs = [r for r in f.requests() if r["ts"] < f.now - 2]   # f.now is taken before our own checks run
    cur = f.current_release
    if named in {r["path"] for r in reqs}:
        return symptom
    probed = next((p for r, p in f.probes.items() if r.split("?")[0] == named), None)
    if probed is None or not all(x.get("status") == 404 for x in probed):
        return symptom
    failing: dict[str, int] = {}
    for r in reqs:
        if r["status"] >= 500 and (not cur or r["release"] == cur.id):
            failing[r["path"]] = failing.get(r["path"], 0) + 1
    if not failing:
        return symptom
    return {**symptom, "route": max(failing, key=failing.get), "route_guess": symptom["route"]}


def diagnose(f: Facts, ledger: list[Evidence], symptom: dict, env_history: list[set[str]] | None = None,
             known_good: list[str] | None = None) -> Diagnosis:
    hyps: list[Hypothesis] = []
    ruled: list[dict] = []
    cur = f.current_release
    reqs = f.requests()
    tbs = f.tracebacks()
    err_reqs_cur = [r for r in reqs if r["status"] >= 500 and cur and r["release"] == cur.id]
    tbs_cur = [t for t in tbs if cur and t["release"] == cur.id]
    probe_5xx = {rt: [c for c in v["codes"] if c and c >= 500]
                 for rt, v in ((e.value["route"], e.value) for e in ledger if e.kind == "probe")}
    failing_routes = sorted({r["path"] for r in err_reqs_cur} | {rt for rt, c in probe_5xx.items() if c})
    route = symptom.get("route") or (failing_routes[0] if failing_routes else "/")
    logs_missing = "logs" in f.missing
    cap = (lambda c: min(c, MISSING_INPUT_CAP)) if logs_missing else (lambda c: c)
    ev_err = _ids(ledger, "error", lambda e: cur and e.value.get("release") == cur.id)
    ev_req = _ids(ledger, "requests", lambda e: e.value["errors"] > 0)
    ev_probe = _ids(ledger, "probe", lambda e: any(c and c >= 500 for c in e.value["codes"]))
    ev_rel = _ids(ledger, "release")[:1]
    files = list({(t["file"], t["line"]): {"path": t["file"].replace("/app/", "", 1), "line": t["line"],
                                           "why": f"{t['error']}: {t['message']}"}
                  for t in tbs_cur if t["file"]}.values())[:2]
    app = f.app_name
    prev, verified_good = _rollback_target(f, known_good or [])
    rollback = ({"kind": "action", "tool": "rollback", "tier": 2, "args": {"app": app, "release_id": prev.id, "tag": prev.tag},
                 "label": f"برگشت به نسخه‌ی {prev.tag}" + (" (آخرین نسخه‌ی سالم)" if verified_good else " (سلامتش رو قبلاً بررسی نکردم)"),
                 "tag": prev.tag, "verified_good": verified_good,
                 "contract": contract(route, new_container=True)} if prev else None)
    has_errors = bool(err_reqs_cur or tbs_cur or any(probe_5xx.values()))

    # P8 frozen / turned off — nothing else matters until this is fixed
    if f.app and (f.app.status.upper() == "FROZEN" or f.app.scale == 0):
        hyps.append(Hypothesis("P8", "برنامه متوقف یا خاموشه (معمولاً به‌خاطر تموم شدن اعتبار).", 0.95,
                               _ids(ledger, "app"),
                               [{"kind": "manual", "capability": "top_up", "label": "شارژ حساب و روشن کردن از کنسول"}]))

    # P7 still starting: edge 502/503, no app log lines in the last 2 min, release < 3 min old
    recent_lines = [ln for ln in f.logs if ln.ts > f.now - 120]
    edge_only = any(c in (502, 503) for v in probe_5xx.values() for c in v) and not recent_lines
    if edge_only and cur and (release_ts(cur) or 0) > f.now - 180:
        hyps.append(Hypothesis("P7", "نسخه‌ی تازه هنوز در حال بالا آمدنه؛ خطای ۵۰۲/۵۰۳ از دروازه‌ی لیاراست، نه از برنامه.",
                               0.7, ev_probe + ev_rel, [{"kind": "wait", "seconds": 45, "label": "۴۵ ثانیه صبر و بررسی دوباره"}]))

    # P2 configuration: errors from an ENV_UPDATE release, or config-shaped errors on any release
    recent_tbs = tbs_cur or tbs[-3:]                 # the current release's traces may be outside the 100-line window
    config_shaped = any(CONFIG_HINT.search(f"{t['error']} {t['message']}") for t in recent_tbs)
    # a variable that disappeared since we last saw the app counts when the current release is the settings change
    # (an old, deliberate removal must not explain a later code bug)
    removed = sorted(env_history[-2] - env_history[-1]) if (env_history and len(env_history) >= 2 and cur
                                                            and cur.type == "ENV_UPDATE") else []
    if cur and has_errors and not edge_only and (cur.type == "ENV_UPDATE" or config_shaped or removed):
        if cur.type == "ENV_UPDATE":
            c = 0.75 + (0.1 if any(ENV_HINT.search(f"{t['error']} {t['message']}") for t in tbs_cur) else 0)
            title = "خطا از وقتی شروع شد که تنظیمات (متغیرهای محیطی) عوض شد"
        else:
            c = 0.7 + (0.1 if removed else 0)
            title = "خطا شبیه یک تنظیم جاافتاده یا اشتباهه (مثلاً یک آدرس یا متغیر محیطی)"
        title += f"؛ متغیر {'، '.join(removed)} حذف شده." if removed else "."
        # values only the user has: the page asks for them (masked) and fills `changes` before proposing
        fix = {"kind": "action", "tool": "set_envs", "tier": 2, "args": {"app": app},
               "needs_values": removed, "label": "برگردوندن متغیر جاافتاده" if removed else "تنظیم متغیر جاافتاده",
               "contract": contract(route, new_container=True)}     # an env change starts a new container (live)
        hyps.append(Hypothesis("P2", title, cap(min(c + (0.05 if removed else 0), 0.95)), ev_err + ev_req + ev_probe + ev_rel,
                               [fix], files))

    # P1 regression: errors come only from a code release
    if cur and cur.type == "DEPLOY" and has_errors and not edge_only:
        older_ok = any(r["status"] < 400 and r["release"] != cur.id and r["path"] in failing_routes for r in reqs)
        c = 0.7 + (0.1 if files else 0) + (0.05 if older_ok else 0) - (0.15 if config_shaped else 0)
        title = f"خطا با نسخه‌ی {cur.tag} (دیپلوی کد) شروع شده"
        title += f"؛ مشکل در {files[0]['path']} خط {fa_num(files[0]['line'])}." if files else "."
        fixes = [{"kind": "coding_agent", "files": files, "label": "درست کردن کد با ایجنت کدنویسی",
                  "contract": contract(route, new_container=True)}]
        if rollback:
            fixes.append(rollback)
        hyps.append(Hypothesis("P1", title, cap(c), ev_err + ev_req + ev_probe + ev_rel, fixes, files))

    # P3 memory: leak (rising + OOM) vs undersized (high, flat)
    mem, events = _val(ledger, "memory"), _val(ledger, "events") or {}
    # only OOMs of the release that is running now: an old release's OOM in the last hour is history, not a cause
    ooms = sum(1 for e in f.events if e.status == "oom" and cur and e.tag == cur.tag)
    if not mem:
        if ooms:     # memory series or plan unreadable, but Liara's own OOM events are enough to suspect memory
            hyps.append(Hypothesis("P3", "برنامه به‌خاطر کمبود حافظه ری‌استارت شده (داده‌ی کامل حافظه در دسترس نبود).",
                                   MISSING_INPUT_CAP, _ids(ledger, "events"),
                                   ([{**rollback, "contract": contract("/", new_container=True, memory_flat=True)}]
                                    if rollback else [])
                                   + [{"kind": "coding_agent", "files": files, "label": "پیدا کردن مصرف زیاد حافظه در کد",
                                       "contract": contract("/", new_container=True, memory_flat=True)}]))
    else:
        pct, slope = mem.get("pct") or 0, mem.get("slope_mb_min")
        rising = slope is not None and slope > 0.5
        if (ooms or pct >= 80) and (rising or ooms):
            c = 0.75 + (0.1 if ooms and rising else 0) + (0.05 if ooms >= 2 else 0)
            minutes = None
            nxt = next((p for p in f.plan_list if f.plan and p.ram_gb > f.plan.ram_gb), None)
            if nxt and rising:
                minutes = max(1, round((nxt.ram_gb * 1024 - mem["mb"]) / slope))
            title = "حافظه مدام بالا می‌ره و برنامه به‌خاطر کمبود حافظه ری‌استارت می‌شه (نشت حافظه)."
            fixes = []
            if rollback:
                fixes.append({**rollback, "contract": contract("/", new_container=True, memory_flat=True)})
            fixes.append({"kind": "coding_agent", "files": files, "label": "پیدا کردن نشت حافظه در کد",
                          "contract": contract("/", new_container=True, memory_flat=True)})
            if nxt:
                fixes.append({"kind": "action", "tool": "resize", "tier": 3, "args": {"app": app, "plan_id": nxt.id},
                              "label": f"پلن بزرگ‌تر ({nxt.id})" + (f" — فقط حدود {fa_num(minutes)} دقیقه وقت می‌خره" if minutes else ""),
                              "contract": contract("/", new_container=True, memory_flat=True)})
            hyps.append(Hypothesis("P3", title, min(c, 0.95), _ids(ledger, "memory") + _ids(ledger, "events"), fixes))
        elif pct >= 85 and not rising and not ooms:
            nxt = next((p for p in f.plan_list if f.plan and p.ram_gb > f.plan.ram_gb), None)
            fixes = [{"kind": "action", "tool": "resize", "tier": 3, "args": {"app": app, "plan_id": nxt.id},
                      "label": f"پلن بزرگ‌تر ({nxt.id})",
                      "contract": contract("/", new_container=True, memory_flat=True)}] if nxt else []
            hyps.append(Hypothesis("P3u", "حافظه نزدیک سقف پلنه ولی بالا نمی‌ره؛ پلن برای این برنامه کوچیکه.", 0.55,
                                   _ids(ledger, "memory"), fixes))
        else:
            ruled.append({"text": f"حافظه {fa_num(pct)}٪ و بدون کمبود حافظه → علت نیست", "evidence": _ids(ledger, "memory")})

    # P4 CPU saturation
    cpu = _val(ledger, "cpu")
    if cpu and cpu.get("pct") is not None:
        if cpu["pct"] >= 90:
            nxt = next((p for p in f.plan_list if f.plan and p.cpu > f.plan.cpu), None)
            fixes = [{"kind": "action", "tool": "resize", "tier": 3, "args": {"app": app, "plan_id": nxt.id},
                      "label": f"پلن با پردازنده‌ی بیشتر ({nxt.id})",
                      "contract": {**contract(route, new_container=True), "cpu_max_cores": round(0.8 * nxt.cpu, 2)}}] \
                if nxt else []
            hyps.append(Hypothesis("P4", "پردازنده به سقف پلن رسیده.", 0.7, _ids(ledger, "cpu"), fixes))
        else:
            ruled.append({"text": f"پردازنده {fa_num(cpu['pct'])}٪ از سقف → علت نیست", "evidence": _ids(ledger, "cpu")})

    # P5 crash loop without OOM
    restarts = events.get("restarts", 0)
    if restarts >= 3 and not ooms:
        fixes = [{"kind": "coding_agent", "files": files, "label": "درست کردن خطای راه‌اندازی",
                  "contract": contract(route, new_container=True)}] + ([rollback] if rollback else [])
        hyps.append(Hypothesis("P5", f"برنامه مدام کرش می‌کنه و دوباره بالا میاد ({fa_num(restarts)} بار).",
                               cap(0.75), _ids(ledger, "events") + ev_err, fixes, files))
    elif events and not restarts:
        ruled.append({"text": "ری‌استارت یا کرشی نبوده", "evidence": _ids(ledger, "events")})

    # P6 slow dependency / slow code: slow, but CPU and RAM have room → a bigger plan won't help
    if symptom.get("kind") == "slow" and not any(h.id in ("P3", "P3u", "P4") for h in hyps):
        meds = [e.value["median_ms"] for e in ledger if e.kind == "probe" and e.value.get("median_ms")]
        mem_ok = not mem or (mem.get("pct") or 0) < 70
        cpu_ok = not cpu or (cpu.get("pct") or 0) < 70
        if meds and max(meds) > 1500 and mem_ok and cpu_ok:
            hyps.append(Hypothesis("P6", "کنده ولی پردازنده و حافظه آزادن؛ مشکل از پلن نیست و ارتقا کمکی نمی‌کنه "
                                         "(احتمالاً یک سرویس بیرونی یا یک بخش کند در کد).", 0.55,
                                   _ids(ledger, "probe") + _ids(ledger, "cpu") + _ids(ledger, "memory"),
                                   [{"kind": "coding_agent", "files": files, "label": "پیدا کردن بخش کند در کد",
                                     "contract": contract(route)}]))

    if cur and (rt := release_ts(cur)) and f.now - rt > 7200 and not any(h.id in ("P1", "P2") for h in hyps):
        ruled.append({"text": "در ۲ ساعت اخیر نسخه یا تنظیمات تازه‌ای منتشر نشده", "evidence": ev_rel})
    all_probes_ok = bool(probe_5xx) and not any(probe_5xx.values()) and \
        all(c is not None for e in ledger if e.kind == "probe" for c in e.value["codes"])
    if not hyps and all_probes_ok and not err_reqs_cur and not tbs_cur and not ooms and not restarts:
        ask = "کدوم صفحه یا چه کاری مشکل داره؟" if not symptom.get("route") else None
        meds = [e.value["median_ms"] for e in ledger if e.kind == "probe" and e.value.get("median_ms")]
        took = (f"{fa_num(max(meds) / 1000, 1)} ثانیه" if meds and max(meds) >= 1000 else f"{fa_num(max(meds or [0]))} میلی‌ثانیه")
        speed = (f" صفحه‌ها حدود {took} جواب می‌دن؛ الان کند نیست."
                 if meds and symptom.get("kind") == "slow" else "")      # answer the question that was asked
        named = next((e for e in ledger if e.kind == "probe" and symptom.get("route")
                      and e.value["route"].split("?")[0] == symptom["route"].split("?")[0]), None)
        page = (f" صفحه‌ای که گفتی ({ltr(named.value['route'].split('?')[0])}) رو {fa_num(len(named.value['codes']))} بار "
                "بررسی کردم و هر بار درست جواب داد." if named else "")
        # the message didn't describe a problem at all: say what this box is for and where the rest lives
        other = symptom.get("kind") == "other" and not symptom.get("route")
        tail = (" اگه مشکل خاصی دیدی بگو کدوم صفحه؛ دامنه، پروژه‌ی تازه و هزینه هم دکمه یا کارت خودشون رو دارن." if other
                else " بگو کدوم صفحه یا چه کاری مشکل داره تا همون رو بررسی کنم." if ask else "")
        hyps.append(Hypothesis("H0", "الان سایت سالم جواب می‌ده و در لاگ خطایی نیست." + page + speed + tail,
                               0.7, _ids(ledger, "probe") + _ids(ledger, "events"),
                               [{"kind": "wait", "seconds": 0, "label": "", "question": ask}]))
    if not hyps:
        hyps.append(Hypothesis("P10", "با شواهدی که از لیارا خوندم علت روشنی پیدا نشد؛ گزارشش رو برای پشتیبانی آماده می‌کنم.",
                               0.3, [], [{"kind": "handoff", "label": "گزارش برای پشتیبانی لیارا"}]))
    hyps.sort(key=lambda h: -h.confidence)
    return Diagnosis(hyps[:3], ruled, [e.id for e in ledger if e.quality == "unavailable"])
