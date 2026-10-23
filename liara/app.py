"""Liara deploy + SRE assistant — web backend: session, live event stream, approvals, SRE, first deploy, domains,
and the coding-agent bridge.

Run:  uv run --frozen python -m liara.dev_server [--live]      (see dev_server.py; main.py for hosting)
LIARA_REPLAY=demo   judge mode: three apps recorded on Liara, each with its recorded fix simulated (DemoReplay)
LIARA_REPLAY=auto   hosted: an empty key (or "demo") opens judge mode, a real key a live session on that account
LIARA_REPLAY=<name> one recorded scenario (tests), e.g. d_fixed; any token is accepted

Security model (README.md §2): the Liara token lives only in this process's memory, inside one session's client.
Approvals come only from this web session (cookie, SameSite=Strict, same-origin check); a coding agent or the
LLM can propose actions but has no route that approves them.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

import httpx

from fastapi import Cookie, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from liara.agent.actions import APP_NAME
from liara.agent.budget import DEFAULT_DIR, Budget
from liara.agent.policy import ActionLog, AgentPort, Policy, Session, new_session_id
from liara.agent.redact import Redactor
from liara.deploy import ci, flow, github
from liara.deploy.analyze import DEFAULT_PORT
from liara.deploy.domain import clean_domain, domain_status
from liara.liara_api import DemoReplay, LiaraClient, LiaraError, ReplayTransport
from liara.liara_api.replay import V1_RELEASE
from liara.sre import watcher
from liara.sre.capabilities import capability
from liara.sre.engine import adopt_code_fix, propose_fix, support_ticket, troubleshoot, verify_fix
from liara.sre.overview import app_list, app_overview
from liara.sre.patterns import contract_fa

WEB = Path(__file__).resolve().parent / "web"
DATA = Path(os.environ.get("LIARA_DATA_DIR") or DEFAULT_DIR)
SESSION_IDLE_S = 30 * 60
COOKIE_MAX_AGE_S = 12 * 3600       # the server-side idle sweep is what ends a session, not the cookie
PAIR_TTL_S = 600
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
UPLOADS_PER_10_MIN = 5
MAX_SOURCE_BYTES = int(os.environ.get("LIARA_MAX_SOURCE_MB", "128")) * 1024 * 1024
MAX_SESSIONS = int(os.environ.get("LIARA_MAX_SESSIONS", "300"))       # a public instance: memory is the limit
SESSIONS_PER_IP_10_MIN = 20
SESSION_MAX_AGE_S = 12 * 3600                                         # hard end, even with a page left open
PAIR_FAILS_PER_MIN_TOTAL = 300                                         # 8-char codes: brute force is out of reach
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#0b6e5f"/>'
           '<path d="M9 17l5 5 9-11" stroke="#fff" stroke-width="3.2" fill="none" stroke-linecap="round" '
           'stroke-linejoin="round"/></svg>')
MAX_JSON_BYTES = 64 * 1024                 # every JSON route; code uploads have their own streaming caps
UPLOAD_ROUTES = ("/api/projects", "/api/bridge/deploy")
MAX_STREAMS_PER_SESSION = 4
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/app.css": ("app.css", "text/css")}

app = FastAPI(title="liara-sre")
policy = Policy(Budget(DATA), ActionLog(DATA / "actions.jsonl"))
agent_port = AgentPort(policy)   # what the LLM and the MCP bridge get: propose + status, no approve/reject
sessions: dict[str, Session] = {}
pair_codes: dict[str, tuple[str, float]] = {}          # code -> (session id, expires at)
bridge_tokens: dict[str, str] = {}                     # bridge token -> session id
pair_failures: dict[str, list[float]] = {}             # client ip -> failed attempts in the last minute
session_opens: dict[str, list[float]] = {}             # client ip -> sessions opened in the last 10 min
heavy = asyncio.Semaphore(2)                           # uploads + GitHub fetches buffered at once, server-wide
uploads: dict[str, list[float]] = {}                   # session id -> upload times in the last 10 min


# ---------- plumbing: refusals, headers, sessions ----------

class Refused(Exception):
    """Raised anywhere in a handler or dependency; answered as {"error", "message"} with a Persian message."""

    def __init__(self, error: str, message: str, status: int):
        self.error, self.message, self.status = error, message, status


@app.exception_handler(Refused)
async def refused(_request: Request, e: Refused):
    return JSONResponse({"error": e.error, "message": e.message}, status_code=e.status)


FRAMEWORK_FA = {404: "این آدرس وجود نداره.", 405: "این آدرس این نوع درخواست رو قبول نمی‌کنه."}


@app.exception_handler(StarletteHTTPException)
async def framework_refused(_request: Request, e: StarletteHTTPException):
    """Routing errors (unknown path, wrong method) answer in the same {"error", "message"} shape, in Persian."""
    return JSONResponse({"error": f"http_{e.status_code}", "message": FRAMEWORK_FA.get(e.status_code, "درخواست قابل انجام نیست.")},
                        status_code=e.status_code)


@app.exception_handler(RequestValidationError)
async def malformed(_request: Request, e: RequestValidationError):
    fields = sorted({".".join(str(x) for x in err.get("loc", [])[1:]) or "body" for err in e.errors()})
    return JSONResponse({"error": "bad_request", "fields": fields,
                         "message": "درخواست ناقص یا نادرسته (" + "، ".join(fields) + ")."}, status_code=422)


NO_SESSION = ("no_session", "اول توکن لیارا رو وصل کن.", 401)
BAD_APP = ("bad_app", "اسم برنامه درست نیست (فقط حروف کوچک انگلیسی، عدد و خط تیره).", 400)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    path = request.url.path
    if request.method in ("POST", "PUT", "PATCH") and path not in UPLOAD_ROUTES:
        # JSON bodies are parsed whole before any check runs: refuse big (or unsized) ones up front
        try:
            size = int(request.headers.get("content-length") or -1)
        except ValueError:
            size = -1
        if size > MAX_JSON_BYTES or (size < 0 and request.headers.get("transfer-encoding")):
            return JSONResponse({"error": "too_big", "message": "درخواست بیش از حد بزرگه."}, status_code=413,
                                headers=SECURITY_HEADERS)
    resp = await call_next(request)
    resp.headers.update(SECURITY_HEADERS)
    if path in STATIC:
        resp.headers["Cache-Control"] = "no-cache"      # an update must reach an open browser without a hard reload
    elif path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"      # per-session data: no cache in front may ever keep it
        resp.headers["Vary"] = "Cookie"
    return resp


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def same_origin(request: Request) -> bool:
    """Every state-changing request must come from this site's own page. Missing Origin (curl, scripts) is refused.

    Expected origin is pinned by LIARA_PUBLIC_ORIGIN in production (e.g. https://dastyar.liara.run); locally it is
    the scheme + Host header. x-forwarded-host is not trusted.
    """
    origin = request.headers.get("origin")
    if not origin:
        return False
    expected = os.environ.get("LIARA_PUBLIC_ORIGIN") or \
        f"{'https' if is_https(request) else 'http'}://{request.headers.get('host', '')}"
    return origin.rstrip("/") == expected.rstrip("/")


def sweep(now: float) -> None:
    """Drop idle sessions (never one mid-build), and everything that hangs off them."""
    building = {j.session_id for j in flow.jobs.values() if j.state == "building"}
    for k in [k for k, s in sessions.items() if k not in building and
              ((now - s.last_seen > SESSION_IDLE_S and not s.busy) or now - s.created > SESSION_MAX_AGE_S)]:
        stale = sessions.pop(k)
        if stale.watch_task:
            stale.watch_task.cancel()
        flow.forget_session(k)                      # its jobs and the copies of the user's code go with it
        uploads.pop(k, None)
        with contextlib.suppress(RuntimeError):     # no loop (sync caller): the client is dropped with the session
            asyncio.get_running_loop().create_task(stale.client.aclose())
    for t in [t for t, sid in bridge_tokens.items() if sid not in sessions]:
        bridge_tokens.pop(t)                        # the bridge dies with its web session
    for c in [c for c, (sid, exp) in pair_codes.items() if sid not in sessions or exp < now]:
        pair_codes.pop(c)
    for ip in [ip for ip, ts in pair_failures.items() if not ts or now - ts[-1] > 60]:
        pair_failures.pop(ip)
    for ip in [ip for ip, ts in session_opens.items() if not ts or now - ts[-1] > 600]:
        session_opens.pop(ip)


def get_session(sid: str | None, *, touch: bool = True) -> Session | None:
    """touch=False for the coding-agent bridge: only the user's own page keeps a session (and its token) alive."""
    now = time.time()
    sweep(now)
    s = sessions.get(sid or "")
    if s and touch:
        s.last_seen = now
    return s


def web_session(lsid: str | None = Cookie(default=None)) -> Session:
    if not (s := get_session(lsid)):
        raise Refused(*NO_SESSION)
    return s


def page_write(request: Request, s: Session = Depends(web_session)) -> Session:
    """A state-changing request from the user's own page: session cookie + same origin."""
    if not same_origin(request):
        raise Refused("origin", "این کار فقط از صفحه‌ی خود دستیار ممکنه.", 403)
    return s


def bridge(request: Request) -> Session:
    """The coding agent's routes: a bridge token from pairing, no cookie, no Origin, never an approval."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    sid = bridge_tokens.get(token) if token else None     # 256-bit random tokens: a dict lookup is safe
    s = get_session(sid, touch=False) if sid else None
    if sid and not s:
        bridge_tokens.pop(token, None)                  # the web session ended: the bridge dies with it
    if not s:
        raise Refused("not_paired", "اول با کد صفحه‌ی دستیار جفت شو (ابزار pair).", 401)
    return s


def check_app(name: str) -> str:
    if not APP_NAME.match(name):
        raise Refused(*BAD_APP)
    return name


async def read_upload(request: Request, s: Session, limit: int, too_big: str) -> bytes:
    """The request body, never buffering more than `limit` (chunked bodies too), at most 5 accepted per 10 min.
    A refused body doesn't count against the rate."""
    now = time.time()
    recent = uploads[s.id] = [t for t in uploads.get(s.id, []) if now - t < 600]
    if len(recent) >= UPLOADS_PER_10_MIN:
        raise Refused("slow_down", "در ۱۰ دقیقه‌ی اخیر به سقف ارسال کد رسیدیم؛ کمی بعد دوباره امتحان کن.", 429)
    try:
        if int(request.headers.get("content-length") or 0) > limit:
            raise Refused("too_big", too_big, 413)
    except ValueError:
        raise Refused("bad_length", "طول بدنه‌ی درخواست نامعتبره.", 400) from None
    if heavy.locked():
        raise Refused("busy", "سرور الان چند پروژه‌ی دیگه رو هم‌زمان دریافت می‌کنه؛ یک دقیقه بعد دوباره امتحان کن.", 503)
    async with heavy:
        return await _read_body(request, s, limit, too_big, now, recent)


async def _read_body(request: Request, s: Session, limit: int, too_big: str, now: float, recent: list[float]) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise Refused("too_big", too_big, 413)
        chunks.append(chunk)
    recent.append(now)
    return b"".join(chunks)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sre_llm():
    """Real model only with LLM_ENABLED=1 (no AI credit without permission); else rules + templates."""
    if os.environ.get("LLM_ENABLED") != "1":
        return None
    from core.llm import from_env
    return from_env()


# ---------- session ----------

class SessionIn(BaseModel):
    token: str
    team_id: str | None = None


@app.post("/api/session")
async def open_session(body: SessionIn, request: Request):
    if not same_origin(request):
        raise Refused("origin", "این کار فقط از صفحه‌ی خود دستیار ممکنه.", 403)
    now = time.time()
    sweep(now)
    ip = request.client.host if request.client else "?"
    opened = session_opens[ip] = [t for t in session_opens.get(ip, []) if now - t < 600]
    if len(opened) >= SESSIONS_PER_IP_10_MIN or len(sessions) >= MAX_SESSIONS:
        raise Refused("slow_down", "الان نشست تازه باز نمی‌شه (تعداد زیاد)؛ چند دقیقه بعد دوباره امتحان کن.", 429)
    opened.append(now)
    replay = os.environ.get("LIARA_REPLAY")
    if replay == "auto":
        # hosted: an empty key (or the word "demo") opens judge mode; a real key opens a live session on that account
        replay = "demo" if body.token.strip().lower() in ("", "demo") else None
    demo = DemoReplay() if replay == "demo" else None
    client = LiaraClient(body.token.strip(), transport=demo or (ReplayTransport(replay) if replay else None))
    try:
        teams = await client.teams()
    except LiaraError as e:
        await client.aclose()
        return JSONResponse({"error": e.kind, "message": e.fa}, status_code=401 if e.kind == "auth" else 502)
    # Credit may live on a team (the hackathon's does): use the only team automatically, or the one the user picked.
    team = body.team_id or (teams[0]["id"] if len(teams) == 1 else None)
    client.team_id = team
    sid = new_session_id()
    # a demo "key" is any text (often empty or the word demo): masking it would blank ordinary words on the page
    sessions[sid] = Session(sid, client, Redactor([] if demo else [body.token.strip()]), demo=demo)
    if demo:
        # as in a live session that deployed v1 itself: its source is stored and it was checked healthy
        sessions[sid].ctx.release_bodies.update(demo.release_bodies())
        for app_name in demo.release_bodies():
            sessions[sid].ctx.known_good[app_name] = [V1_RELEASE]
        sessions[sid].ctx.env_history.update(demo.env_history())
    resp = JSONResponse({"ok": True, "team": next((t["name"] for t in teams if t["id"] == team), None),
                         "teams": [t["name"] for t in teams], "replay": bool(replay)})
    resp.set_cookie("lsid", sid, httponly=True, samesite="strict", secure=is_https(request), max_age=COOKIE_MAX_AGE_S)
    return resp


@app.delete("/api/session")
async def close_session(request: Request, lsid: str | None = Cookie(default=None)):
    if not same_origin(request):
        raise Refused("origin", "این کار فقط از صفحه‌ی خود دستیار ممکنه.", 403)
    if s := sessions.pop(lsid or "", None):
        if s.watch_task:
            s.watch_task.cancel()
        flow.forget_session(s.id)
        for t in [t for t, sid in bridge_tokens.items() if sid == s.id]:
            bridge_tokens.pop(t)                        # logging out also unpairs the coding agent
        await s.client.aclose()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("lsid")
    return resp


@app.get("/api/stream")
async def stream(request: Request, s: Session = Depends(web_session)):
    if len(s.subscribers) >= MAX_STREAMS_PER_SESSION:
        raise Refused("slow_down", "این نشست در چند زبانه‌ی دیگه هم باز است؛ یکی رو ببند.", 429)
    q: asyncio.Queue = asyncio.Queue()
    s.subscribers.append(q)
    watcher.ensure_running(s, lambda: sessions.get(s.id) is s)

    async def gen():
        try:
            pending = [a.public() for a in s.actions.values() if a.status == "pending" and not a.expired]
            yield sse("hello", s.redactor.obj({"pending": pending, "watch": watcher.status(s)}))
            while not await request.is_disconnected():
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=15)
                    yield sse(event, data)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                s.last_seen = time.time()          # an open page keeps its session alive
        finally:
            if q in s.subscribers:
                s.subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=STREAM_HEADERS)


# ---------- approvals (web session only) ----------

class ApproveIn(BaseModel):
    args_hash: str
    sure: bool = False


@app.get("/api/actions")
async def list_actions(s: Session = Depends(web_session)):
    return {"actions": s.redactor.obj([a.public() for a in s.actions.values()]),
            "bridge": any(sid == s.id for sid in bridge_tokens.values())}


@app.post("/api/actions/{action_id}/approve")
async def approve(action_id: str, body: ApproveIn, s: Session = Depends(page_write)):
    return await policy.approve(s, action_id, body.args_hash, sure=body.sure)


@app.post("/api/actions/{action_id}/reject")
async def reject(action_id: str, s: Session = Depends(page_write)):
    return policy.reject(s, action_id)


@app.get("/api/whoami")
async def whoami(lsid: str | None = Cookie(default=None)):
    """Is this browser already connected? (200 either way, so a fresh page doesn't log an error.)"""
    return {"connected": get_session(lsid) is not None}


@app.get("/favicon.ico")
@app.get("/favicon.svg")
async def favicon():
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/watch")
async def watch_status(s: Session = Depends(web_session)):
    """The watcher's last round: per app a one-line summary and open problems, plus recent alerts."""
    return watcher.status(s)


@app.get("/api/log")
async def action_log(s: Session = Depends(web_session)):
    return {"log": s.log[-200:], "api_calls": s.client.n_calls}


class ProposeIn(BaseModel):
    tool: str
    args: dict
    why: str = ""


def dev_only() -> None:
    if os.environ.get("LIARA_DEV") != "1":
        raise Refused("disabled", "این مسیر فقط در حالت توسعه فعاله.", 404)


@app.post("/api/dev/propose")
async def dev_propose(body: ProposeIn, _dev: None = Depends(dev_only), s: Session = Depends(page_write)):
    """Developer tool (LIARA_DEV=1 only): propose any action by hand to try a card. Still needs the user's approval."""
    return await agent_port.call(s, body.tool, body.args, caller="user", why=body.why)


# ---------- SRE: home card, diagnose → propose a fix → verify ----------

class TroubleshootIn(BaseModel):
    app: str
    text: str


class FixIn(BaseModel):
    hypothesis: str
    option: int
    values: dict[str, str] | None = None
    board_id: str | None = None          # which diagnosis the user clicked (an older board is refused)


@app.get("/api/apps")
async def api_apps(s: Session = Depends(web_session)):
    try:
        return {"apps": await app_list(s)}
    except LiaraError as e:
        return JSONResponse({"error": e.kind, "message": e.fa}, status_code=502)


@app.get("/api/apps/{name}/overview")
async def api_overview(name: str, s: Session = Depends(web_session)):
    return await app_overview(s, check_app(name))


@app.post("/api/apps/{name}/zero-downtime")
async def api_zero_downtime(name: str, s: Session = Depends(page_write)):
    return await agent_port.call(s, "zero_downtime", {"app": check_app(name)}, caller="user",
                                 why="با این تنظیم، انتشارهای بعدی بدون قطعی جایگزین نسخه‌ی قبلی می‌شن")


@app.get("/api/apps/{name}/ticket")
async def api_ticket(name: str, s: Session = Depends(web_session)):
    return support_ticket(s, check_app(name))


def demo_args(s: Session, app_name: str) -> dict:
    """Judge mode: site checks and "now" come from the recording, so the diagnosis is the one Liara really showed."""
    return {"prober": s.demo.probe, "now": s.demo.now(app_name)} if s.demo else {}


def session_llm(s: Session, app_name: str, text: str):
    """Judge mode: Claude's recorded answers for that app; otherwise the real model only if LLM_ENABLED=1."""
    return s.demo.llm(app_name, text) if s.demo else sre_llm()


@app.post("/api/troubleshoot")
async def api_troubleshoot(body: TroubleshootIn, s: Session = Depends(page_write)):
    return await troubleshoot(s, check_app(body.app), body.text[:500], llm=session_llm(s, body.app, body.text[:500]),
                              **demo_args(s, body.app))


@app.post("/api/incidents/{incident_id}/fix")
async def api_fix(incident_id: str, body: FixIn, s: Session = Depends(page_write)):
    return await propose_fix(policy, s, incident_id, body.hypothesis, body.option, body.values, body.board_id)


def demo_verify_args(s: Session, incident_id: str | None) -> dict:
    """Judge mode: verify a simulated fix against the app's real after-recording (see DemoReplay.verify_args)."""
    inc = s.incidents.get(incident_id or "")
    return s.demo.verify_args(inc.app) if s.demo and inc else {}


@app.post("/api/incidents/{incident_id}/verify")
async def api_verify(incident_id: str, s: Session = Depends(page_write)):
    res = await verify_fix(s, incident_id, **demo_verify_args(s, incident_id))
    if res.get("ok"):
        watcher.check_soon(s)
    return res


# ---------- first deploy: zip → analysis + plan → create (two taps) → build/heal loop → live ----------

class CreateIn(BaseModel):
    app: str


def own_job(s: Session, job_id: str) -> "flow.Job":
    job = flow.jobs.get(job_id)
    if not job or job.session_id != s.id:
        raise Refused("no_project", "این پروژه پیدا نشد (شاید دستیار دوباره راه‌اندازی شده).", 404)
    return job


@app.post("/api/projects")
async def api_new_project(request: Request, filename: str = "", s: Session = Depends(page_write)):
    """Body = the project as a .zip (raw bytes). Analysis only: nothing on Liara changes here."""
    data = await read_upload(request, s, flow.MAX_ZIP_BYTES, "حجم فایل zip از ۵۰ مگابایت بیشتره.")
    try:
        job = await flow.new_project(s, data, DATA, name_hint=filename[:80])
    except flow.UploadError as e:
        raise Refused("bad_upload", e.fa, 400) from None
    return job.public()


class GithubIn(BaseModel):
    url: str


@app.post("/api/projects/github")
async def api_github_project(body: GithubIn, s: Session = Depends(page_write)):
    """A public GitHub repo link instead of a zip: GitHub's own archive of it, then the same analysis (no Liara write)."""
    link = github.parse_link(body.url[:300])
    if not link:
        raise Refused("bad_link", "لینک مخزن گیت‌هاب درست نیست؛ مثل https://github.com/owner/repo بنویس.", 400)
    now = time.time()
    recent = uploads[s.id] = [t for t in uploads.get(s.id, []) if now - t < 600]
    if len(recent) >= UPLOADS_PER_10_MIN:
        raise Refused("slow_down", "در ۱۰ دقیقه‌ی اخیر به سقف ارسال کد رسیدیم؛ کمی بعد دوباره امتحان کن.", 429)
    if heavy.locked():
        raise Refused("busy", "سرور الان چند پروژه‌ی دیگه رو هم‌زمان دریافت می‌کنه؛ یک دقیقه بعد دوباره امتحان کن.", 503)
    recent.append(now)
    try:
        async with heavy:
            data = await github.fetch_zip(*link, limit=flow.MAX_ZIP_BYTES)
    except github.GithubError as e:
        raise Refused("github", e.fa, 400) from None
    except httpx.HTTPError:
        raise Refused("github", "به گیت‌هاب وصل نشدم؛ کمی بعد دوباره امتحان کن یا zip پروژه رو آپلود کن.", 502) from None
    try:
        job = await flow.new_project(s, data, DATA, name_hint=link[1][:80])
    except flow.UploadError as e:
        raise Refused("bad_upload", e.fa, 400) from None
    return job.public()


@app.get("/api/projects/{job_id}")
async def api_project(job_id: str, s: Session = Depends(web_session)):
    return s.redactor.obj(own_job(s, job_id).public())


@app.post("/api/projects/{job_id}/create")
async def api_create(job_id: str, body: CreateIn, s: Session = Depends(page_write)):
    return await flow.propose_create(policy, s, own_job(s, job_id), body.app.strip().lower())


@app.get("/api/projects/{job_id}/patch")
async def api_patch(job_id: str, s: Session = Depends(web_session)):
    job = own_job(s, job_id)
    return PlainTextResponse(await flow.patch_text(job), media_type="text/x-diff",
                             headers={"Content-Disposition": f'attachment; filename="dastyar-{job.app or job.id}.patch"'})


@app.get("/api/projects/{job_id}/liara.json")
async def api_liara_json(job_id: str, s: Session = Depends(web_session)):
    return JSONResponse(flow.liara_json(own_job(s, job_id)), headers={"Content-Disposition": 'attachment; filename="liara.json"'})


@app.get("/api/projects/{job_id}/workflow.yml")
async def api_workflow(job_id: str, s: Session = Depends(web_session)):
    return PlainTextResponse(flow.github_workflow(own_job(s, job_id), s.client.team_id), media_type="text/yaml",
                             headers={"Content-Disposition": 'attachment; filename="liara-deploy.yml"'})


# ---------- custom domain ----------

class DomainIn(BaseModel):
    app: str
    domain: str


def domain_args(body: DomainIn) -> tuple[str, str]:
    domain = clean_domain(body.domain)
    if not domain:
        raise Refused("bad_domain", "اسم دامنه درست نیست؛ مثل shop.example.ir بنویس (بدون https).", 400)
    return check_app(body.app), domain


@app.post("/api/domains")
async def api_add_domain(body: DomainIn, s: Session = Depends(page_write)):
    app_name, domain = domain_args(body)
    if s.demo:
        raise Refused("demo", "در نسخه‌ی نمایشی دامنه وصل نمی‌شه (برنامه‌ها ضبط‌شده‌اند)؛ با کلید API لیارای خودت امتحانش کن.", 403)
    st = await domain_status(s, app_name, domain)
    if st["state"] != "not_added":
        return {"status": "exists", "domain_status": st}
    return await agent_port.call(s, "add_domain", {"app": app_name, "domain": domain}, caller="user",
                                 why=f"دامنه‌ی {domain} رو خواستی به {app_name} وصل کنم")


@app.post("/api/domains/check")
async def api_check_domain(body: DomainIn, s: Session = Depends(page_write)):
    return await domain_status(s, *domain_args(body))


@app.post("/api/domains/ssl")
async def api_domain_ssl(body: DomainIn, s: Session = Depends(page_write)):
    app_name, domain = domain_args(body)
    st = await domain_status(s, app_name, domain)
    if st["state"] != "verified":
        msg = {"ssl_active": "SSL این دامنه فعاله.", "waiting_dns": "اول باید DNS دامنه درست بشه و لیارا تأییدش کنه؛ بعد SSL می‌گیرم.",
               "not_added": "این دامنه هنوز به برنامه وصل نشده."}.get(st["state"], st.get("message") or "وضعیت دامنه خوانده نشد.")
        return {"status": "not_ready", "domain_status": st, "message": msg}
    return await agent_port.call(s, "provision_ssl", {"domain": domain}, caller="user", why=f"HTTPS برای {domain}")


# ---------- coding-agent bridge (MCP): pair with a short code, then propose — never approve ----------
#
# The web page asks for a code (/api/pair, cookie + Origin). The local MCP server exchanges it once for a bridge
# token (/api/bridge/pair). Bridge routes accept only that token (no cookie, no Origin), reach the policy only
# through AgentPort (call + status), and therefore cannot approve anything: approvals stay on the page.

class PairIn(BaseModel):
    code: str


@app.post("/api/pair")
async def new_pair_code(s: Session = Depends(page_write)):
    for c in [c for c, (sid, _) in pair_codes.items() if sid == s.id]:
        pair_codes.pop(c)                               # one live code per session
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
    pair_codes[code] = (s.id, time.time() + PAIR_TTL_S)
    return {"code": code, "expires_in_s": PAIR_TTL_S,
            "message": "این کد رو به ایجنت کدنویسی‌ات بده: «با کد … به دستیار لیارا وصل شو». ۱۰ دقیقه اعتبار داره."}


@app.post("/api/bridge/pair")
async def bridge_pair(body: PairIn, request: Request):
    now = time.time()
    ip = request.client.host if request.client else "?"
    fails = pair_failures[ip] = [t for t in pair_failures.get(ip, []) if now - t < 60]
    total = sum(1 for ts in pair_failures.values() for t in ts if now - t < 60)
    if len(fails) >= 20 or total >= PAIR_FAILS_PER_MIN_TOTAL:   # per client, plus a global ceiling (IPs can be faked)
        raise Refused("slow_down", "تلاش‌های ناموفق زیاد بود؛ یک دقیقه صبر کن.", 429)
    entry = pair_codes.pop(body.code.strip().upper(), None)
    if not entry or entry[1] < now or entry[0] not in sessions:
        fails.append(now)
        raise Refused("bad_code", "کد درست نیست یا منقضی شده؛ از صفحه‌ی دستیار کد تازه بگیر.", 401)
    for t in [t for t, sid in bridge_tokens.items() if sid == entry[0]]:
        bridge_tokens.pop(t)                            # pairing again replaces the earlier agent's token
    token = secrets.token_urlsafe(32)
    bridge_tokens[token] = entry[0]
    s = sessions[entry[0]]
    s.emit("bridge_paired", {"text": "ایجنت کدنویسی وصل شد. تأیید کارها همچنان فقط در همین صفحه است."})
    return {"token": token, "team": s.client.team_id is not None}


class BridgeTroubleshootIn(BaseModel):
    app: str
    symptom: str


class BridgeVerifyIn(BaseModel):
    app: str | None = None
    incident_id: str | None = None


class BridgeTicketIn(BaseModel):
    app: str


class BridgeCiIn(BaseModel):
    app: str
    branch: str = "main"


@app.post("/api/bridge/troubleshoot")
async def bridge_troubleshoot(body: BridgeTroubleshootIn, s: Session = Depends(bridge)):
    return await troubleshoot(s, check_app(body.app), body.symptom[:500], llm=session_llm(s, body.app, body.symptom[:500]),
                              **demo_args(s, body.app))


@app.post("/api/bridge/deploy")
async def bridge_deploy(request: Request, app_name: str, port: int | None = None, message: str = "",
                        s: Session = Depends(bridge)):
    """Body = the project as tar.gz (packed locally by the MCP server). Uploading is free; the release is a T2 card."""
    check_app(app_name)
    if s.demo:
        raise Refused("demo", "در نسخه‌ی نمایشی نمی‌شه کد فرستاد؛ برای انتشار واقعی با کلید API خودت وصل شو.", 403)
    if port is not None and not 1 <= port <= 65535:
        raise Refused("bad_port", "پورت باید عددی بین ۱ و ۶۵۵۳۵ باشه.", 400)
    data = await read_upload(request, s, MAX_SOURCE_BYTES, "حجم کد از ۱۲۸ مگابایت بیشتره.")
    if not data.startswith(b"\x1f\x8b"):
        raise Refused("bad_source", "بدنه باید یک فایل tar.gz باشه.", 400)
    try:
        app_info = await s.client.get_app(app_name)
        source_id = await s.client.upload_source(app_name, data)
    except LiaraError as e:
        return JSONResponse({"error": e.kind, "message": e.fa}, status_code=400)
    args = {"app": app_name, "source_id": source_id, "platform": app_info.platform,
            "port": port or DEFAULT_PORT.get(app_info.platform, 80), "message": (message or "deploy from coding agent")[:120]}
    inc = s.open_incident(app_name)
    fix = adopt_code_fix(inc)                           # the agent is acting on the board's code-fix option
    if fix and fix.get("contract"):
        args["success"] = contract_fa(fix["contract"])
    why = (f"ایجنت کدنویسی کد رو برای «{inc.board.get('headline', '')}» درست کرده و نسخه‌ی تازه رو فرستاده"
           if fix else f"ایجنت کدنویسی نسخه‌ی تازه‌ی {app_name} رو فرستاده")
    res = await agent_port.call(s, "deploy", args, caller="mcp", why=why)
    if inc and inc.fix and inc.fix.get("tool") is None and res.get("status") in ("waiting_for_user", "done"):
        # the code fix the incident waits for; a T1 deploy (an app this session created) already ran, so note when
        inc.fix.update(tool="deploy", action_id=res.get("pending_action_id"),
                       started_at=time.time() if res.get("status") == "done" else None)
    return res


@app.get("/api/bridge/actions/{action_id}")
async def bridge_action_status(action_id: str, s: Session = Depends(bridge)):
    return s.redactor.obj(agent_port.status(s, action_id))


@app.post("/api/bridge/verify")
async def bridge_verify(body: BridgeVerifyIn, s: Session = Depends(bridge)):
    inc_id = body.incident_id
    if not inc_id and body.app:
        incs = [i for i in s.incidents.values() if i.app == body.app]
        inc_id = max(incs, key=lambda i: i.opened_at).id if incs else None
    if not inc_id:
        raise Refused("no_incident", "برای این برنامه هنوز مشکلی بررسی نشده؛ اول troubleshoot رو اجرا کن.", 404)
    res = await verify_fix(s, inc_id, **demo_verify_args(s, inc_id))
    if res.get("ok"):
        watcher.check_soon(s)
    return res


@app.post("/api/bridge/ci")
async def bridge_ci(body: BridgeCiIn, s: Session = Depends(bridge)):
    """The workflow file for continuous deployment; the coding agent writes it into the user's repo. Read-only here."""
    try:
        info = await s.client.get_app(check_app(body.app))
    except LiaraError as e:
        return JSONResponse({"error": e.kind, "message": e.fa}, status_code=400)
    return {"path": ci.WORKFLOW_PATH, "content": ci.github_workflow(info.name, DEFAULT_PORT.get(info.platform, 80),
                                                                     s.client.team_id, body.branch),
            "secret_name": ci.SECRET, "user_steps": ci.STEPS_FA[1:],
            "note": "Never put the Liara key in the file or ask the user to paste it to you; it only goes in the "
                    "GitHub secret. The user adds the secret themselves."}


@app.get("/api/bridge/capability/{name}")
async def bridge_capability(name: str, _s: Session = Depends(bridge)):
    return capability(name)


@app.post("/api/bridge/ticket")
async def bridge_ticket(body: BridgeTicketIn, s: Session = Depends(bridge)):
    return support_ticket(s, check_app(body.app))


# ---------- pages ----------

@app.get("/health")
async def health():
    mode = os.environ.get("LIARA_REPLAY")
    return {"ok": True, "killed": policy.budget.killed(), "demo": mode == "demo", "both": mode == "auto"}


def _versioned_index() -> str:
    """index.html with app.js / app.css addressed by their content hash: a CDN in front (seen live: ArvanCloud
    caches them despite no-cache) can then never serve an old script with a new page."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "app.css"):
        tag = hashlib.sha256((WEB / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f'"/{name}"', f'"/{name}?v={tag}"')
    return html


@app.get("/")
async def index_page():
    return HTMLResponse(_versioned_index())


@app.get("/app.js")
@app.get("/app.css")
async def static_file(request: Request):
    name, media = STATIC[request.url.path]
    return FileResponse(WEB / name, media_type=media)
