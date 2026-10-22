"""The SRE chapter: «سایتم کند شده» → evidence → ranked causes → one recommended fix → approval → verified.

troubleshoot()  opens (or continues) the app's incident, collects evidence in parallel, scores the patterns,
                lets the LLM reorder/explain (validated; templates if the model is off), and returns the board.
propose_fix()   turns the chosen fix option into a policy proposal (approval card) or a coding-agent brief
                or a plain "Liara's API can't do this, here's how" message.
verify_fix()    checks the fix against its success contract and closes or reopens the incident.

One diagnose/verify per app at a time (per-session lock). Everything returned is redacted with the session's
redactor (logs can echo secrets), and so is everything sent to the model.
"""

import asyncio
import time
import uuid

from liara.agent.incident import STATE_FA, TRANSITIONS, Incident
from liara.agent.policy import Policy, Session

from .capabilities import capability
from .collect import Prober, build_ledger, collect, fa_num, ltr
from .llm_jobs import explain, hypothesis_dict, understand
from .patterns import contract_fa, diagnose
from .verify import verify


def _lock(s: Session, app: str) -> asyncio.Lock:
    return s.sre_locks.setdefault(app, asyncio.Lock())


def _goto(inc: Incident, state: str) -> None:
    """Move if it's a real, allowed transition; a repeat or an impossible move is a no-op, never an exception."""
    if inc.state != state and state in TRANSITIONS[inc.state]:
        inc.move(state)


def _note_env_keys(s: Session, app: str, keys: set[str]) -> None:
    hist = s.ctx.env_history.setdefault(app, [])
    if not hist or hist[-1] != keys:
        hist.append(keys)
        del hist[:-5]


def _check_rollbacks(s: Session, diag) -> None:
    """We can only roll back to releases whose source + body we stored (no rollback endpoint, seen live)."""
    for h in diag.hypotheses:
        for i, fx in enumerate(h.fixes):
            if fx.get("tool") == "rollback":
                stored = s.ctx.release_bodies.get(fx["args"]["app"], {})
                if fx["args"]["release_id"] not in stored:
                    h.fixes[i] = {"kind": "manual", "capability": "rollback_console", "label": fx["label"],
                                  "contract": fx.get("contract")}


class _HypView:
    """A board hypothesis (dict) seen through the attributes _brief() reads."""

    def __init__(self, d: dict):
        self.title, self.files = d.get("title", ""), d.get("files") or []


def _brief(app: str, h, symptom_text: str) -> str:
    """A ready prompt for a coding agent without our MCP («بریف برای ایجنت کدنویسی»)."""
    where = "\n".join(f"- {f['path']} line {f['line']}: {f['why']}" for f in h.files) or "- (no stack trace; see evidence)"
    return (f"Production issue on Liara app `{app}` (user report: \"{symptom_text[:200]}\").\n"
            f"Diagnosis: {h.title}\nWhere:\n{where}\n"
            "Fix the code in this project (smallest safe change), run it locally if you can, then redeploy. "
            "Don't change unrelated files.")


async def troubleshoot(s: Session, app: str, text: str, *, llm=None, prober: Prober | None = None,
                       now: float | None = None) -> dict:
    async with _lock(s, app):
        return await _troubleshoot(s, app, text, llm=llm, prober=prober, now=now)


async def _troubleshoot(s: Session, app: str, text: str, *, llm, prober, now) -> dict:
    t0 = time.perf_counter()
    calls0 = len(s.client.calls)
    inc = s.open_incident(app)
    if inc is None:
        inc = Incident(app, s.redactor.text(text[:300]))
        s.incidents[inc.id] = inc
    _goto(inc, "diagnosing")
    s.emit("status", {"incident": inc.id, "text": "دارم شواهد رو از لیارا جمع می‌کنم…"})

    redact = s.redactor.text
    symptom, symptom_src = await understand(llm, text, s.id, redact)
    facts = await collect(s.client, app, [symptom.get("route") or ""], prober=prober, now=now)
    if facts.app:
        s.redactor.add(*[e.value for e in facts.app.envs])
        _note_env_keys(s, app, {e.key for e in facts.app.envs})
    ledger = build_ledger(facts)
    diag = diagnose(facts, ledger, symptom, s.ctx.env_history.get(app), s.ctx.known_good.get(app))
    _check_rollbacks(s, diag)
    diag, headline, explain_src = await explain(llm, diag, ledger, text, s.id, redact)

    top = diag.hypotheses[0]
    recommended = next((fx for fx in top.fixes if fx["kind"] in ("action", "coding_agent", "wait", "manual")), None)
    probe_now = next((e.value for e in ledger if e.kind == "probe" and e.value["route"] == (symptom.get("route") or "/")),
                     None) or next((e.value for e in ledger if e.kind == "probe"), {})
    mem = next((e.value for e in ledger if e.kind == "memory" and e.quality == "measured"), {}) or {}
    inc.before = {"route": probe_now.get("route"), "codes": probe_now.get("codes"),
                  "median_ms": probe_now.get("median_ms"), "mem_mb": mem.get("mb"), "slope_mb_min": mem.get("slope_mb_min"),
                  "errors_5xx": sum(e.value["errors"] for e in ledger if e.kind == "requests"),
                  "containers": sorted({e.container for e in facts.events if e.container}),
                  "release": facts.current_release.id if facts.current_release else None}
    inc.evidence_ids = [e.id for e in ledger]
    inc.hypotheses = s.redactor.obj([hypothesis_dict(h) for h in diag.hypotheses])
    inc.fix = None                                   # a new diagnosis replaces any earlier plan
    board = {
        "id": uuid.uuid4().hex[:12], "incident": inc.id, "app": app, "headline": headline,
        "symptom": symptom, "question": symptom.get("question"),
        "hypotheses": [{**hypothesis_dict(h), "confidence": round(h.confidence, 2),
                        "fixes": [{**fx, "contract_fa": contract_fa(fx["contract"]) if fx.get("contract") else None,
                                   **({"capability": capability(fx["capability"])} if fx["kind"] == "manual" else {})}
                                  for fx in h.fixes]} for h in diag.hypotheses],
        "recommended": {"hypothesis": top.id, "option": top.fixes.index(recommended)} if recommended else None,
        "ruled_out": diag.ruled_out, "unknowns": diag.unknowns,
        "evidence": [{"id": e.id, "kind": e.kind, "text": e.text, "quality": e.quality, "source": e.source} for e in ledger],
        "brief": _brief(app, top, text) if any(fx["kind"] == "coding_agent" for fx in top.fixes) else None,
        "footer": {"seconds": round(time.perf_counter() - t0, 1), "liara_calls": len(s.client.calls) - calls0,
                   "sources": len({e.source for e in ledger if e.quality == "measured"}),
                   "model": "llm" if "llm" in (symptom_src, explain_src) else "off"},
        "limits": [capability("http_metrics")["fa"]],
    }
    board = s.redactor.obj(board)
    inc.board = board
    _goto(inc, "waiting_user")
    s.emit("diagnosis", board)
    return board


def adopt_code_fix(inc: Incident | None) -> dict | None:
    """The coding agent is deploying for this incident without anyone pressing «درست کردن کد»: take the board's
    code-fix option (the recommended one first) as the incident's plan, so verify_fix knows what to check."""
    if not inc or inc.closed or not inc.board:
        return None
    if inc.fix:
        # only a code fix can be what a coding-agent deploy delivers; an env or resize plan stays separate
        return inc.fix if inc.fix.get("tool") in (None, "deploy") and _fix_kind(inc) == "coding_agent" else None
    rec = inc.board.get("recommended") or {}
    options = [(h["id"], i, fx) for h in inc.board.get("hypotheses", []) for i, fx in enumerate(h["fixes"])
               if fx["kind"] == "coding_agent"]
    options.sort(key=lambda o: (o[0] != rec.get("hypothesis"), o[1] != rec.get("option")))
    if not options:
        return None
    hid, i, fx = options[0]
    inc.fix = {"hypothesis": hid, "option": i, "contract": fx.get("contract"), "action_id": None, "tool": None,
               "started_at": None}
    _goto(inc, "waiting_coding_agent")
    return inc.fix


def _fix_kind(inc: Incident) -> str | None:
    fix, board = inc.fix or {}, inc.board or {}
    h = next((h for h in board.get("hypotheses", []) if h["id"] == fix.get("hypothesis")), None)
    return h["fixes"][fix["option"]]["kind"] if h and 0 <= fix.get("option", -1) < len(h["fixes"]) else None


async def propose_fix(policy: Policy, s: Session, incident_id: str, hypothesis: str, option: int,
                      values: dict[str, str] | None = None, board_id: str | None = None) -> dict:
    inc = s.incidents.get(incident_id)
    if not inc or inc.closed or not inc.board:
        return {"status": "error", "error": "no_open_incident",
                "message": "این مورد بسته شده یا دیگه وجود نداره؛ اگر هنوز مشکل هست، دوباره بگو چی شده."}
    if board_id and board_id != inc.board.get("id"):
        return {"status": "error", "error": "old_board",
                "message": "این تشخیص قدیمیه؛ از آخرین تشخیص (پایین‌تر) انتخاب کن."}
    if inc.state not in ("waiting_user", "waiting_coding_agent"):
        return {"status": "error", "error": "diagnose_first",
                "message": "اول دوباره بررسی می‌کنم؛ وضعیت از آخرین تشخیص عوض شده."}
    h = next((h for h in inc.board["hypotheses"] if h["id"] == hypothesis), None)
    if not h or not isinstance(option, int) or not (0 <= option < len(h["fixes"])):
        return {"status": "error", "error": "bad_option", "message": "این گزینه دیگه معتبر نیست."}
    fx = h["fixes"][option]
    if fx["kind"] == "coding_agent":
        inc.fix = {"hypothesis": hypothesis, "option": option, "contract": fx.get("contract"), "action_id": None,
                   "tool": None, "started_at": time.time()}
        _goto(inc, "waiting_coding_agent")
        brief = _brief(inc.app, _HypView(h), inc.symptom)            # the chosen cause, not necessarily the top one
        return {"status": "handoff", "brief": s.redactor.text(brief), "files": fx.get("files"),
                "message": "این کار تغییر کد لازم داره؛ بریف رو به ایجنت کدنویسی‌ات بده یا از اتصال MCP استفاده کن."}
    if fx["kind"] in ("manual", "handoff", "wait"):
        return {"status": fx["kind"], "capability": fx.get("capability"), "label": fx.get("label")}
    args = dict(fx["args"])
    if fx.get("contract_fa"):
        args["success"] = fx["contract_fa"]          # the card states the diagnosis's own success test
    if fx["tool"] == "set_envs":
        values = {k: v for k, v in (values or {}).items() if isinstance(k, str) and isinstance(v, str) and v}
        if not values:
            return {"status": "needs_values", "keys": fx.get("needs_values") or [],
                    "message": "مقدار متغیر رو وارد کن (فقط همین‌جا دیده می‌شه و جایی ذخیره نمی‌شه)."}
        args["changes"] = values
    res = await policy.call(s, fx["tool"], args, caller="user", why=inc.board.get("headline", ""), incident_id=inc.id)
    if res.get("status") in ("waiting_for_user", "done"):
        inc.fix = {"hypothesis": hypothesis, "option": option, "contract": fx.get("contract"),
                   "action_id": res.get("pending_action_id"), "tool": fx["tool"], "started_at": None}
    return res


def _executed_action(s: Session, inc: Incident):
    """The incident's action that actually ran — a stale card is re-proposed under a new id, so look by incident."""
    done = [a for a in s.actions.values() if a.incident_id == inc.id and a.status == "done"
            and a.tool == (inc.fix or {}).get("tool")]
    return max(done, key=lambda a: a.executed_at or 0) if done else None


async def verify_fix(s: Session, incident_id: str, *, prober: Prober | None = None, **kw) -> dict:
    inc = s.incidents.get(incident_id)
    if not inc:
        return {"status": "error", "error": "no_incident", "message": "این مورد پیدا نشد؛ دوباره بگو چی شده."}
    async with _lock(s, inc.app):
        if inc.closed:                               # double click / retry: the answer we already have
            return s.redactor.obj({**(inc.verify_result or {}), "report": report(inc), "status": inc.state})
        if not inc.fix or not inc.fix.get("contract"):
            return {"status": "error", "error": "nothing_to_verify",
                    "message": "برای این مورد هنوز راه‌حلی انتخاب نشده که بررسیش کنم."}
        started = inc.fix.get("started_at")
        if inc.fix.get("tool"):
            act = _executed_action(s, inc)
            if act:
                started = act.executed_at or act.created
            elif not started:                       # a T1 action (no card) records its own start time
                pending = s.actions.get(inc.fix.get("action_id") or "")
                return {"status": "error", "error": "fix_not_executed", "action_status": pending.status if pending else None,
                        "message": "راه‌حل انتخاب‌شده هنوز اجرا نشده؛ بعد از تأیید کارتش دوباره بررسی می‌کنم."}
        _goto(inc, "verifying")
        s.emit("status", {"incident": inc.id, "text": "دارم بررسی می‌کنم که درست شد یا نه…"})
        res = await verify(s.client, inc.app, inc.fix["contract"], inc.before, started or time.time(), prober=prober, **kw)
        inc.after, inc.verify_result = res["after"], res
        if res["ok"] and res.get("provisional"):
            pass                            # stays in "verifying" until the metric has enough post-fix samples
        elif res["ok"]:
            await mark_known_good(s, inc.app)
            _goto(inc, "resolved")
        elif inc.mutations_left() <= 0:
            _goto(inc, "handed_to_support")
        else:
            _goto(inc, "diagnosing")
            inc.fix = None                  # didn't work: diagnose again before proposing anything else
        res = s.redactor.obj({**res, "id": uuid.uuid4().hex[:12], "report": report(inc), "status": inc.state})
        s.emit("verify", {"incident": inc.id, **res})
        return res


async def mark_known_good(s: Session, app: str) -> None:
    """Remember the release that just passed a verify: the preferred rollback target from now on."""
    try:
        rid = (await s.client.releases(app, 1)).current_id
    except Exception:          # noqa: BLE001 — best effort
        return
    if rid:
        good = s.ctx.known_good.setdefault(app, [])
        if rid not in good:
            good.insert(0, rid)


def support_ticket(s: Session, app: str) -> dict:
    """A ready-to-send, redacted ticket for Liara support from the app's latest incident (handoff path)."""
    incs = [i for i in s.incidents.values() if i.app == app]
    if not incs:
        return {"status": "error", "error": "no_incident", "message": "اول با troubleshoot مشکل رو بررسی کن."}
    inc = max(incs, key=lambda i: i.opened_at)
    b = inc.board or {}
    tried = [{"tool": a.tool, "status": a.status} for a in s.actions.values() if a.incident_id == inc.id]
    lines = [f"برنامه‌ی لیارا: {app}", f"مشکلی که کاربر گفت: {inc.symptom}",
             f"شناسه‌ی بررسی: {inc.id} ({STATE_FA.get(inc.state, inc.state)})",
             f"تشخیص دستیار: {b.get('headline', '—')}", "شواهد:"]
    lines += [f"  {e['id']} [{e['kind']}] {e['text']}" for e in b.get("evidence", [])[:12]]
    lines += ["رد شده:"] + ([f"  - {r['text']}" for r in b.get("ruled_out", [])] or ["  - هیچ"])
    lines += ["کارهای امتحان‌شده:"] + ([f"  - {t['tool']}: {t['status']}" for t in tried] or ["  - هیچ"])
    if inc.verify_result:
        lines.append(f"آخرین بررسی نتیجه: {inc.verify_result.get('summary')}")
        lines += [f"  - {c.get('text')}" for c in inc.verify_result.get("checks", [])]
    return s.redactor.obj({"status": "ok", "incident": inc.id, "text": "\n".join(lines)})


def report(inc: Incident) -> list[str]:
    """Five lines: what happened · cause · action · result · prevention (+ time)."""
    top = (inc.hypotheses or [{}])[0]
    fix = inc.fix or {}
    hyp = next((h for h in inc.board.get("hypotheses", []) if h["id"] == fix.get("hypothesis")), None)
    opt = fix.get("option")
    label = (hyp["fixes"][opt].get("label") if hyp and isinstance(opt, int) and opt < len(hyp["fixes"]) else None) or "—"
    b, a = inc.before or {}, inc.after or {}
    result = (f"{ltr(b.get('route') or '/')}: {_codes(b.get('codes'))} ← {_codes(a.get('codes'))}" if a else "هنوز بررسی نشده")
    if a.get("slope_mb_min") is not None and b.get("slope_mb_min") is not None:
        result += f"؛ روند حافظه {fa_num(b['slope_mb_min'], 1)} → {fa_num(a['slope_mb_min'], 1)} مگ/دقیقه"
    prevent = {"P1": "قبل از انتشار، صفحه‌ای که خراب شد رو یک بار امتحان کن (یا یک تست ساده براش اضافه کن).",
               "P2": "تغییر متغیرهای محیطی رو با من انجام بده تا قبلش بررسی بشه.",
               "P3": "حافظه‌ی کش رو محدود کن (مثلاً حداکثر تعداد یا زمان انقضا).",
               "P5": "خطای راه‌اندازی رو در لاگ ببین و قبل از انتشار محلی اجرا کن."}.get(top.get("id"), "—")
    return [f"چی شد: {inc.symptom}", f"علت: {top.get('title', '—')}", f"اقدام: {label}", f"نتیجه: {result}",
            f"جلوگیری: {prevent}", f"زمان: {fa_num(inc.elapsed_s())} ثانیه، بدون تیکت پشتیبانی" if inc.state == "resolved"
            else f"وضعیت: {STATE_FA.get(inc.state, inc.state)}"]


def _codes(codes) -> str:
    return "، ".join("×" if c is None else fa_num(c) for c in codes or []) or "—"
