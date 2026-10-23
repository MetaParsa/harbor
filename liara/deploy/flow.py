"""First deploy: zip → analysis + plan → create the app (T3, the user's two taps) → build → watch it come up → heal.

The loop (README.md §4):
  1. the upload is unpacked into data/projects/<job>/orig and copied to work/; config-only preflight fixes go in;
  2. create_app is proposed through the policy — nothing is spent until the user taps «تأیید» and «مطمئنم»;
  3. up to 3 builds (covered by that approval, T1): pack work/ → upload → release → wait for the build;
  4. READY is not "live" (seen live): we probe https://<app>.liara.run and read the release's own app logs until the site
     answers 200 twice, 15 s apart, with no crash in between — or a known failure shows up in the logs;
  5. a known failure → one catalogue fix on work/ (shown as a diff) → next build; an unknown one → stop and hand over.
The user gets the live URL, every change we made, one patch for their repo and a liara.json.
"""

import asyncio
import io
import re
import secrets
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from liara.agent.actions import APP_NAME, TOOLS, effective_tier
from liara.agent.policy import Policy, Session
from liara.liara_api import LiaraError
from liara.liara_api.client import probe
from liara.liara_api.package import PackageError, package_dir
from liara.text import fa_num

from . import ci
from .analyze import Analysis, Fix, analyze, recommend_plan
from .heal import add_start_script, apply_fix, classify_failure, project_diff

MAX_BUILDS = 3
LIVE_WAIT_S = 180            # READY → first 200 took 30–60 s in live tests (the edge answers 502/503 meanwhile); crash loops show in ~20 s
STEADY_S = 15                # a 200 must still be a 200 this much later, with no crash logged in between
POLL_S = 6
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_FILES = 5000
JOBS_PER_SESSION = 3
JOBS_TOTAL = 30
DONE_STATES = {"done", "failed", "rejected", "expired", "refused"}

jobs: dict[str, "Job"] = {}


class UploadError(Exception):
    def __init__(self, fa: str):
        self.fa = fa


@dataclass
class Job:
    id: str
    session_id: str
    dir: Path
    analysis: Analysis
    plan: dict
    suggested_app: str
    app: str | None = None
    state: str = "analyzed"      # analyzed → waiting_approval → building → live | stopped
    action_id: str | None = None
    changes: list[Fix] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    url: str | None = None
    builds: int = 0
    created: bool = False
    stop_reason: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    created_at: float = field(default_factory=time.time)

    @property
    def orig(self) -> Path:
        return self.dir / "orig"

    @property
    def work(self) -> Path:
        return self.dir / "work"

    def public(self) -> dict:
        return {"id": self.id, "state": self.state, "app": self.app, "suggested_app": self.suggested_app,
                "analysis": self.analysis.public(), "plan": self.plan, "action_id": self.action_id,
                "changes": [asdict(c) for c in self.changes], "events": self.events[-60:], "url": self.url,
                "builds": self.builds, "max_builds": MAX_BUILDS, "stop_reason": self.stop_reason, "created": self.created}


def _emit(s: Session, job: Job, kind: str, text: str, **extra) -> None:
    ev = {"job": job.id, "app": job.app if job.created else None, "kind": kind, "text": text,
          "t": round(time.time(), 1), **extra}
    job.events.append(s.redactor.obj(ev))
    s.emit("deploy", ev)


# ---------- 1. upload + analysis ----------

def unpack_zip(data: bytes, dest: Path) -> Path:
    """Unpack a user's zip safely (no paths outside dest, no links, size and file caps). Returns the project root."""
    if len(data) > MAX_ZIP_BYTES:
        raise UploadError("حجم فایل zip از ۵۰ مگابایت بیشتره.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UploadError("این فایل zip سالم نیست.") from None
    infos = [i for i in z.infolist() if not i.is_dir()]
    if not infos:
        raise UploadError("فایل zip خالیه.")
    if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise UploadError("پروژه خیلی بزرگه (بیش از ۵۰۰۰ فایل یا ۲۰۰ مگابایت بعد از باز کردن). node_modules رو بیرون بذار.")
    dest.mkdir(parents=True)
    base = dest.resolve()
    for i in infos:
        if (i.external_attr >> 16) & 0o170000 == 0o120000:      # symlink entry: skip, never follow
            continue
        name = i.filename.replace("\\", "/")
        target = (base / name).resolve()
        if name.startswith("/") or ".." in Path(name).parts or not target.is_relative_to(base) or target == base:
            raise UploadError("مسیر یکی از فایل‌های zip مشکوکه؛ بازش نکردم.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(i) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)
    top = [p for p in dest.iterdir() if p.name not in ("__MACOSX",)]
    return top[0] if len(top) == 1 and top[0].is_dir() else dest     # zip of a folder → that folder is the project


def suggest_app_name(base: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-")[:40] or "app"
    name = f"{slug}-{secrets.token_hex(2)}"
    return name if APP_NAME.match(name) else f"app-{secrets.token_hex(3)}"


def _prepare(data: bytes, d: Path, name_hint: str | None) -> tuple[Analysis, str]:
    """The blocking part of an upload (unpack, copy, analyse); runs in a worker thread."""
    root = unpack_zip(data, d / "upload")
    shutil.copytree(root, d / "orig")
    shutil.copytree(root, d / "work")
    shutil.rmtree(d / "upload", ignore_errors=True)
    a = analyze(d / "work")
    for i, fx in enumerate(a.preflight):               # file preflight fixes are applied to the working copy now
        if fx.rule == "node-start" and not fx.applied:
            a.preflight[i] = add_start_script(d / "work") or fx
    base = root.name if root.name != "upload" else re.sub(r"\.zip$", "", name_hint or "app", flags=re.I)
    return a, base


def _forget(job: "Job") -> None:
    jobs.pop(job.id, None)
    if job.task and not job.task.done():
        job.task.cancel()
    shutil.rmtree(job.dir, ignore_errors=True)


def _make_room(session_id: str) -> None:
    """At most JOBS_PER_SESSION per session and JOBS_TOTAL overall: the oldest idle jobs (and their files) go first."""
    idle = lambda j: not (j.task and not j.task.done())      # noqa: E731
    mine = [j for j in jobs.values() if j.session_id == session_id]
    for j in sorted(mine, key=lambda j: j.created_at)[:max(0, len(mine) - JOBS_PER_SESSION + 1)]:
        if idle(j):
            _forget(j)
    everyone = sorted(jobs.values(), key=lambda j: j.created_at)
    for j in everyone[:max(0, len(everyone) - JOBS_TOTAL + 1)]:
        if idle(j):
            _forget(j)
    if len([j for j in jobs.values() if j.session_id == session_id]) >= JOBS_PER_SESSION or len(jobs) >= JOBS_TOTAL:
        raise UploadError("الان چند پروژه‌ی دیگه در جریانه؛ کمی بعد دوباره امتحان کن.")


def forget_session(session_id: str) -> None:
    """The web session ended (logout or idle sweep): stop its jobs and delete their copies of the user's code."""
    for j in [j for j in jobs.values() if j.session_id == session_id]:
        _forget(j)


async def new_project(s: Session, data: bytes, data_dir: Path, *, name_hint: str | None = None) -> Job:
    _make_room(s.id)
    jid = "prj-" + secrets.token_hex(6)
    d = data_dir / "projects" / jid
    try:
        a, base = await asyncio.to_thread(_prepare, data, d, name_hint)
    except UploadError:
        shutil.rmtree(d, ignore_errors=True)
        raise
    except Exception:                                   # noqa: BLE001 — any odd zip is the user's problem, not a 500
        shutil.rmtree(d, ignore_errors=True)
        raise UploadError("این فایل zip رو نتونستم باز کنم (خراب، رمزدار یا ساختار عجیب).") from None
    try:
        plan = recommend_plan(a, await s.client.plans())
    except LiaraError:
        plan = {"plan_id": None, "reason": "فهرست پلن‌ها الان از لیارا خوانده نشد."}
    job = Job(jid, s.id, d, a, plan, suggest_app_name(base))
    job.changes = [f for f in a.preflight if f.applied]
    jobs[jid] = job
    return job


# ---------- 2. create the app (the user approves on the page) ----------

async def propose_create(policy: Policy, s: Session, job: Job, app: str, *, runner=None) -> dict:
    if job.state not in ("analyzed", "stopped") or job.created:
        return {"status": "error", "error": "busy", "message": "برای این پروژه قبلاً برنامه ساخته شده یا کارش در جریانه."}
    if not APP_NAME.match(app):
        return {"status": "error", "error": "bad_app",
                "message": "اسم برنامه فقط حروف کوچک انگلیسی، عدد و خط تیره (۳ تا ۶۳ حرف)."}
    if not job.plan.get("plan_id") or job.analysis.platform in ("unknown",):
        return {"status": "error", "error": "not_ready", "message": "نوع پروژه یا پلن مشخص نشد؛ نمی‌تونم برنامه بسازم."}
    before, job.state = job.state, "proposing"          # set before any await: a double click can't make two cards
    try:
        res = await policy.call(s, "create_app", {"app": app, "plan_id": job.plan["plan_id"], "platform": job.analysis.platform,
                                                  "bundle_id": job.plan.get("bundle_id", "basic")},
                                caller="agent", why=f"برای آنلاین کردن «{job.analysis.framework}» یک برنامه‌ی تازه لازمه")
    except Exception:
        job.state = before
        raise
    if res.get("status") != "waiting_for_user":
        job.state = before
        return res
    job.app, job.action_id, job.state, job.stop_reason = app, res["pending_action_id"], "waiting_approval", None
    _emit(s, job, "waiting", "کارت ساخت برنامه رو تأیید کن؛ بعدش بیلد و بررسی خودکاره.")
    job.task = asyncio.create_task((runner or run_after_approval)(policy, s, job))
    return res


async def run_after_approval(policy: Policy, s: Session, job: Job, *, poll_s: float = 1.0, **kw) -> None:
    """Wait for the create-app card's outcome (approval happens only on the page), then run the build loop.
    Never dies silently: any unexpected error stops the job with a message the page shows."""
    try:
        await _after_approval(policy, s, job, poll_s, **kw)
    except asyncio.CancelledError:
        job.state, job.stop_reason = "stopped", "کار متوقف شد (اتصال صفحه بسته شد)."
        raise
    except Exception as e:                              # noqa: BLE001
        policy.log.write(s, {"event": "deploy_job_error", "job": job.id, "error": type(e).__name__})
        _stop(s, job, f"یک خطای داخلی پیش اومد ({type(e).__name__})؛ کار متوقف شد و ثبت شد.")


async def _after_approval(policy: Policy, s: Session, job: Job, poll_s: float, **kw) -> None:
    aid = job.action_id
    deadline = time.time() + 15 * 60
    while time.time() < deadline:
        st = policy.status(s, aid)
        if st["status"] == "stale" and (st.get("result") or {}).get("new_action"):
            aid = job.action_id = st["result"]["new_action"]       # re-proposed card: follow it
        elif st["status"] in DONE_STATES or st["status"] == "not_found":
            break
        await asyncio.sleep(poll_s)
    else:
        st = {"status": "expired"}
    if st["status"] != "done" or not (st.get("result") or {}).get("ok"):
        job.state, job.stop_reason = "stopped", {"rejected": "ساخت برنامه رو رد کردی؛ چیزی ساخته نشد.",
                                                 "expired": "کارت ساخت برنامه منقضی شد؛ چیزی ساخته نشد."}.get(
            st["status"], "برنامه ساخته نشد (لیارا خطا داد)؛ پیامش روی کارت هست.")
        _emit(s, job, "stopped", job.stop_reason)
        return
    job.created = True
    _emit(s, job, "created", f"برنامه‌ی {job.app} روی لیارا ساخته شد.")
    await deploy_loop(policy, s, job, **kw)


# ---------- 3–5. build, watch, heal ----------

async def deploy_loop(policy: Policy, s: Session, job: Job, *, prober=probe, sleep=asyncio.sleep,
                      live_wait_s: float = LIVE_WAIT_S, steady_s: float = STEADY_S, poll_s: float = POLL_S) -> None:
    job.state = "building"
    a, app = job.analysis, job.app
    while job.builds < MAX_BUILDS:
        args = {"app": app, "source_id": "pending", "platform": a.platform, "port": a.port,
                "platform_config": a.platform_config, "message": f"first deploy #{job.builds + 1} by dastyar"}
        if effective_tier(TOOLS["deploy"], s.ctx, args) != 1:
            # the create approval covers 3 builds on this app; another deploy (coding agent, a fix) used them up
            return _stop(s, job, "سه بیلدی که با ساخت برنامه تأیید کرده بودی تموم شد (بخشی‌اش جای دیگه استفاده شد)؛ "
                                 "برای بیلد بعدی باید دوباره تأیید بگیرم.")
        job.builds += 1
        n = job.builds
        _emit(s, job, "build", f"بیلد {fa_num(n)} از {fa_num(MAX_BUILDS)}: بسته‌بندی و ارسال کد…", attempt=n)
        try:
            data, info = await asyncio.to_thread(package_dir, job.work, a.platform)
            args["source_id"] = await s.client.upload_source(app, data)
        except PackageError as e:
            return _stop(s, job, {"too_big": "حجم کد بعد از بسته‌بندی از ۱۲۸ مگابایت بیشتره؛ پوشه‌های بزرگ رو بیرون بذار.",
                                  "empty": "بعد از کنار گذاشتن فایل‌های نادیده‌گرفتنی، چیزی برای ارسال نموند."}.get(
                e.kind, "بسته‌بندی کد نشد."))
        except LiaraError as e:
            return _stop(s, job, f"ارسال کد به لیارا نشد: {e.fa}")
        res = await policy.call(s, "deploy", args, caller="agent")
        if res.get("status") != "done":
            return _stop(s, job, res.get("message") or "بیلد انجام نشد.")
        r = res["result"]
        rid = r.get("release_id")
        if r.get("state") != "READY":
            _emit(s, job, "build_failed", f"بیلد ناموفق بود ({r.get('state')}).", line=r.get("failure_line"))
            try:
                lines = [ln.text for ln in (await s.client.build_logs(rid)).lines]
            except LiaraError:
                lines = [r.get("failure_line") or ""]
        else:
            _emit(s, job, "ready", f"بیلد در {fa_num(r.get('seconds') or 0)} ثانیه تموم شد. «آماده» یعنی هنوز معلوم نیست سایت بالا اومده؛ "
                                   "حالا سایت و لاگ‌هاش رو نگاه می‌کنم…")
            outcome, lines = await _watch(s, job, rid, prober, sleep, live_wait_s, steady_s, poll_s)
            if outcome == "live":
                return await _live(s, job)
        d = classify_failure(lines)
        if d.line:
            _emit(s, job, "crash", d.explain, line=d.line, rule=d.rule)
        if job.builds >= MAX_BUILDS:
            break                                       # a fix we can't build any more would sit untested in the patch
        fix = apply_fix(job.work, d) if d.rule else None
        if not fix:
            why = d.explain if d.rule is None or d.advice else "درمان شناخته‌شده‌ی این خطا رو قبلاً امتحان کردم و جواب نداد."
            return _stop(s, job, why + (f" {d.advice}" if d.advice else ""), line=d.line)
        job.changes.append(fix)
        _emit(s, job, "heal", fix.title, diff=fix.diff, file=fix.file, rule=fix.rule)
    _stop(s, job, f"{fa_num(MAX_BUILDS)} بیلد انجام شد و سایت هنوز بالا نیومده؛ اینجا رو به خودت یا پشتیبانی می‌سپارم.")

async def _watch(s, job, rid, prober, sleep, live_wait_s, steady_s, poll_s) -> tuple[str, list[str]]:
    """'live' once the site answered 200, and again steady_s later with no crash in the release's logs."""
    url = f"https://{job.app}.liara.run/"
    t0, first_ok, lines, said_waiting = time.monotonic(), None, [], False
    while time.monotonic() - t0 < live_wait_s:
        try:
            lines = [ln.text for ln in await s.client.app_logs(job.app, since_s=1800, release_id=rid)]
        except LiaraError:
            pass
        if classify_failure(lines).rule:
            return "crash", lines
        status = (await prober(url, 1))[0].get("status")
        if status == 200:
            if first_ok is None:
                first_ok = time.monotonic()
                _emit(s, job, "probe", "سایت جواب ۲۰۰ داد؛ ۱۵ ثانیه‌ی دیگه دوباره نگاه می‌کنم که پایدار باشه.")
            elif time.monotonic() - first_ok >= steady_s:
                return "live", lines
        else:
            first_ok = None
            if not said_waiting:
                said_waiting = True
                _emit(s, job, "probe", f"سایت هنوز جواب نمی‌ده ({status or 'بدون پاسخ'})؛ لیارا بعد از بیلد تا یک دقیقه طول می‌کشه تا ترافیک رو بفرسته.")
        await sleep(poll_s)
    return "timeout", lines


async def _live(s: Session, job: Job) -> None:
    job.state, job.url = "live", f"https://{job.app}.liara.run"
    _emit(s, job, "live", f"آنلاین شد: {job.url}", url=job.url, changes=[c.title for c in job.changes])
    from liara.sre.engine import mark_known_good          # the SRE chapter's rollback baseline
    try:
        await mark_known_good(s, job.app)
    except Exception:                                   # noqa: BLE001 — best effort; the app is live either way
        pass


def _stop(s: Session, job: Job, why: str, line: str | None = None) -> None:
    job.state, job.stop_reason = "stopped", why
    _emit(s, job, "stopped", why, line=line)


# ---------- downloads ----------

async def patch_text(job: Job) -> str:
    return await asyncio.to_thread(project_diff, job.orig, job.work)


def github_workflow(job: Job, team_id: str | None) -> str:
    return ci.github_workflow(job.app or job.suggested_app, job.analysis.port, team_id)


def liara_json(job: Job) -> dict:
    a = job.analysis
    out = {"platform": a.platform, "app": job.app or job.suggested_app, "port": a.port,
           "build": {"location": "iran", "cache": True}}
    if a.platform_config:
        out[a.platform] = a.platform_config    # becomes the release's platformConfig, as the Liara CLI sends it
    return out                                 # never "envs": in liara.json it replaces every variable on the app
