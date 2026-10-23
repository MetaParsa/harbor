"""The action policy: the only path from "the agent wants to do X" to a Liara write.

- `Policy.call()` is what the LLM orchestrator and the MCP bridge use (through `AgentPort`, which exposes only
  call() and status()). T0/T1 run at once; T2/T3 only produce a pending action with an approval card.
- `Policy.approve()` is called only by the web route that holds the user's session cookie. An approval is
  single-use, expires after 10 min, is bound to a hash of the exact args, needs two separate taps for
  resize / create app / turn off, and refuses to run unless the app is verifiably unchanged since the card was
  shown (release, plan, scale, env fingerprint) — a failed check re-proposes, it never runs blind.
- Budget and the incident's 3-change cap are reserved under a per-session lock *before* a write runs, so
  parallel approvals can't overrun them, and an attempt counts even if Liara errors halfway (it may have acted).
- Every proposal, approval, refusal and result goes to the append-only action log, redacted.
"""

import asyncio
import copy
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from liara.liara_api import LiaraClient, LiaraError

from .actions import TOOLS, Card, Ctx, Tool, effective_tier, validate
from .budget import Budget, BudgetRefused, Caps
from .incident import Incident
from .redact import Redactor

APPROVAL_TTL_S = 600
SECOND_TAP_MIN_S = 0.8      # the «مطمئنم» tap must be a separate, later request than the first tap


def args_hash(tool: str, args: dict) -> str:
    canon = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def fingerprint(client: LiaraClient, app: str, redactor: Redactor | None = None) -> dict | None:
    """What must not change between showing a card and running it. None = couldn't read it (callers fail closed)."""
    try:
        a, rels = await asyncio.gather(client.get_app(app), client.releases(app, 1))
    except LiaraError:
        return None
    if redactor:
        redactor.add(*[e.value for e in a.envs])     # existing values get masked everywhere from now on
    envs = {e.key: hashlib.sha256((e.value or "").encode()).hexdigest()[:12] for e in a.envs}
    return {"release": rels.current_id, "plan": a.plan_id, "scale": a.scale, "envs": envs}


@dataclass
class PendingAction:
    tool: str
    args: dict = field(repr=False)          # a private copy; may hold env values: never logged or sent as-is
    tier: int
    card: Card
    session_id: str
    caller: str                              # agent (our own flows) | llm | mcp | user
    why: str = ""
    incident_id: str | None = None
    fingerprint: dict | None = None
    id: str = field(default_factory=lambda: "act-" + secrets.token_urlsafe(9))
    hash: str = ""
    created: float = field(default_factory=time.time)
    status: str = "pending"                  # pending → executing → done | failed | rejected | expired | stale | refused
    result: dict | None = None
    approved_via: str | None = None
    armed_at: float | None = None            # first tap of a two-tap action
    executed_at: float | None = None         # when the write actually started (verify measures from here)
    demo: bool = False                       # judge mode: the write is simulated (the card says so)

    @property
    def expired(self) -> bool:
        return time.time() - self.created > APPROVAL_TTL_S

    def public(self) -> dict:
        """What the web page sees: card, tier, hash to echo back. No args (they may hold secrets)."""
        return {"id": self.id, "tool": self.tool, "tier": self.tier, "second_tap": TOOLS[self.tool].second_tap,
                "card": asdict(self.card), "why": self.why, "args_hash": self.hash, "status": self.status,
                "expires_in_s": max(0, round(APPROVAL_TTL_S - (time.time() - self.created))),
                "incident_id": self.incident_id, "caller": self.caller, "demo": self.demo}


class ActionLog:
    """Append-only, redacted JSONL on disk + per-session list for the UI."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, session: "Session", entry: dict) -> dict:
        safe = session.redactor.obj({"ts": round(time.time(), 3), "session": session.id[:8], **entry})
        session.log.append(safe)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(safe, ensure_ascii=False) + "\n")
        return safe


@dataclass
class Session:
    """One browser session: holds the Liara token in memory only (inside the client), never on disk."""
    id: str
    client: LiaraClient
    redactor: Redactor
    ctx: Ctx = None
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    actions: dict[str, PendingAction] = field(default_factory=dict)
    incidents: dict[str, Incident] = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sre_locks: dict[str, asyncio.Lock] = field(default_factory=dict)   # app -> one diagnose/verify at a time
    demo: object | None = None          # DemoReplay in judge mode: recorded site checks and recording time
    watch: dict = field(default_factory=dict)          # the watcher's last round per app (liara/sre/watcher.py)
    alerts: list[dict] = field(default_factory=list)   # recent watcher alerts, newest last
    watch_task: asyncio.Task | None = None

    def __post_init__(self):
        if self.ctx is None:
            self.ctx = Ctx(self.client, self.redactor)

    def emit(self, event: str, data: dict) -> None:
        """Push a redacted event to every open stream of this session."""
        payload = self.redactor.obj(data)
        for q in list(self.subscribers):
            q.put_nowait((event, payload))

    @property
    def busy(self) -> bool:
        return bool(self.subscribers) or any(a.status == "executing" for a in self.actions.values())

    def open_incident(self, app: str | None) -> Incident | None:
        return next((i for i in self.incidents.values() if i.app == app and not i.closed), None)


class Policy:
    def __init__(self, budget: Budget, log: ActionLog):
        self.budget = budget
        self.log = log
        # judge-mode writes are simulated: they get their own counters, so judges can't use up the live caps
        self.demo_budget = Budget(budget.dir / "demo", Caps(deploys_per_day=10**6, apps_per_day=0,
                                                            writes_per_session_hour=30, writes_per_hour_total=2000))

    def _budget(self, s: Session) -> Budget:
        return self.demo_budget if s.demo else self.budget

    # ---------- the agent's entry point ----------

    async def call(self, s: Session, tool_name: str, args: dict, *, caller: str, why: str = "",
                   incident_id: str | None = None) -> dict:
        tool = TOOLS.get(tool_name)
        if tool is None:
            return {"status": "error", "error": f"unknown tool {tool_name}"}
        if (bad := validate(tool, args)) is not None:
            return {"status": "error", "error": "bad_args", "message": bad}
        args = copy.deepcopy(args)                     # the caller keeps no handle on what gets approved and run
        if tool_name == "set_envs":
            s.redactor.add(*args["changes"].values())
        # the incident is resolved server-side from the app; a caller can't pick (or skip) the one that caps it
        incident = s.open_incident(args.get("app"))
        if incident_id and (not incident or incident.id != incident_id):
            return {"status": "error", "error": "bad_incident"}
        incident_id = incident.id if incident else None

        tier = effective_tier(tool, s.ctx, args)
        if tier <= 1:
            return await self._execute_now(s, tool, tier, args, caller)
        try:
            self._budget(s).check(s.id, write=True, deploy=tool.deploy, create_app=tool.create_app)
        except BudgetRefused as e:
            self.log.write(s, {"event": "refused", "tool": tool_name, "caller": caller, "reason": e.reason})
            return {"status": "refused", "reason": e.reason, "message": e.fa}
        try:
            card = await tool.card(s.ctx, args) if tool.card else Card(tool_name, "")
        except (LiaraError, KeyError):
            card = Card(f"اجرای «{tool_name}» روی {args.get(tool.app_arg or '', '')}",
                        "جزئیات هزینه الان از لیارا خوانده نشد.", cost="نامشخص")
        fp = await fingerprint(s.client, args[tool.app_arg], s.redactor) if tool.app_arg else None
        # why and card are shown to people and sent over SSE: store them redacted (the LLM may quote a secret)
        card = Card(**s.redactor.obj(asdict(card)))
        act = PendingAction(tool_name, args, tier, card, s.id, caller, s.redactor.text(why), incident_id, fp)
        act.hash = args_hash(tool_name, args)
        act.demo = s.demo is not None
        s.actions[act.id] = act
        self.log.write(s, {"event": "proposed", "action": act.id, "tool": tool_name, "tier": tier, "caller": caller,
                           "why": act.why, "incident": incident_id})
        s.emit("action_proposed", act.public())
        return {"status": "waiting_for_user", "pending_action_id": act.id, "tier": tier,
                "message": "این کار تأیید کاربر رو لازم داره؛ کارتش در صفحه‌ی دستیار نشون داده شد.",
                "card": asdict(card)}

    async def _execute_now(self, s: Session, tool: Tool, tier: int, args: dict, caller: str) -> dict:
        if tier >= 1:
            async with s.lock:                         # check + reserve atomically, before the write
                try:
                    self._budget(s).check(s.id, write=True, deploy=tool.deploy, create_app=tool.create_app)
                except BudgetRefused as e:
                    self.log.write(s, {"event": "refused", "tool": tool.name, "caller": caller, "reason": e.reason})
                    return {"status": "refused", "reason": e.reason, "message": e.fa}
                ran_out = tier != effective_tier(tool, s.ctx, args)    # the free builds ran out meanwhile
                if not ran_out:
                    self._budget(s).spend(s.id, deploy=tool.deploy, create_app=tool.create_app)
                    if tool.name == "deploy":
                        s.ctx.builds_on_created[args["app"]] = s.ctx.builds_on_created.get(args["app"], 0) + 1
            if ran_out:
                return await self.call(s, tool.name, args, caller=caller)   # now a T2 card, outside the lock
        try:
            result = await tool.run(s.ctx, args)
        except Exception as e:     # noqa: BLE001 — any failure is reported, never left hanging
            kind = e.kind if isinstance(e, LiaraError) else "internal"
            self.log.write(s, {"event": "failed", "tool": tool.name, "tier": tier, "caller": caller, "error": kind})
            return {"status": "failed", "error": kind, "message": getattr(e, "fa", "خطای داخلی؛ ثبت شد.")}
        if tier >= 1:
            self.log.write(s, {"event": "executed", "tool": tool.name, "tier": tier, "caller": caller,
                               "ok": result.get("ok")})
            s.emit("step", {"tool": tool.name, "tier": tier, "ok": result.get("ok")})
        return {"status": "done", "result": s.redactor.obj(result)}

    # ---------- only the web session may call these ----------

    async def approve(self, s: Session, action_id: str, echoed_hash: str, *, sure: bool = False) -> dict:
        act = s.actions.get(action_id)      # looked up in *this* session only
        if act is None:
            return {"status": "error", "error": "not_found", "message": "این کارت دیگه وجود نداره (مثلاً دستیار دوباره راه‌اندازی شده)."}
        if act.status != "pending":
            return {"status": "error", "error": f"already_{act.status}", "final": True,
                    "message": "این کارت قبلاً تموم شده و دیگه قابل اجرا نیست."}
        if act.expired:
            act.status = "expired"
            self.log.write(s, {"event": "expired", "action": act.id})
            return {"status": "expired", "message": "این درخواست تأیید منقضی شده؛ دوباره پیشنهادش می‌کنم."}
        if echoed_hash != act.hash:
            self.log.write(s, {"event": "hash_mismatch", "action": act.id})
            return {"status": "error", "error": "args_mismatch", "message": "این کارت با چیزی که نشون داده شده نمی‌خونه؛ اجرا نشد."}
        tool = TOOLS[act.tool]
        if tool.second_tap:
            now = time.time()
            if not sure or act.armed_at is None or now - act.armed_at < SECOND_TAP_MIN_S:
                act.armed_at = act.armed_at if (sure and act.armed_at) else now
                return {"status": "needs_second_tap",
                        "message": "این کار هزینه یا قطعی داره؛ برای اجرا «مطمئنم» رو بزن."}
        act.status = "executing"            # no await since the status check: a double tap can't run it twice
        try:
            return await self._run_approved(s, act, tool)
        except Exception:                   # noqa: BLE001 — never leave an action stuck in "executing"
            act.status, act.result = "failed", {"error": "internal"}
            self.log.write(s, {"event": "failed", "action": act.id, "error": "internal"})
            s.emit("action_result", {"id": act.id, "ok": False, "message": "خطای داخلی؛ ثبت شد."})
            return {"status": "failed", "error": "internal", "message": "خطای داخلی؛ ثبت شد."}

    async def _run_approved(self, s: Session, act: PendingAction, tool: Tool) -> dict:
        if tool.app_arg:
            now = await fingerprint(s.client, act.args[tool.app_arg], s.redactor)
            if now is None:                 # can't verify → don't run; the card stays approvable
                act.status = "pending"
                return {"status": "error", "error": "verify_failed",
                        "message": "الان نتونستم وضعیت برنامه رو از لیارا بخونم؛ چند لحظه بعد دوباره تأیید کن."}
            if act.fingerprint is None or now != act.fingerprint:
                act.status = "stale"
                self.log.write(s, {"event": "stale", "action": act.id})
                s.emit("action_stale", {"id": act.id})
                again = await self.call(s, act.tool, act.args, caller=act.caller, why=act.why)
                act.result = {"new_action": again.get("pending_action_id")}     # status() points callers at it
                return {"status": "stale", "message": "از وقتی این کارت رو دیدی، برنامه تغییر کرده؛ کارت تازه‌اش رو ببین.",
                        "new_action": again.get("pending_action_id")}
        incident = s.incidents.get(act.incident_id) if act.incident_id else None
        async with s.lock:                  # check + reserve atomically, right before the write
            if incident and (incident.closed or incident.mutations_left() <= 0):
                act.status = "refused"
                self.log.write(s, {"event": "refused", "action": act.id, "reason": "incident_mutation_budget"})
                return {"status": "refused", "reason": "incident_mutation_budget",
                        "message": "برای این مشکل سه تغییر انجام دادیم و حل نشد؛ گزارشش رو برای پشتیبانی آماده می‌کنم."}
            try:
                self._budget(s).check(s.id, write=True, deploy=tool.deploy, create_app=tool.create_app)
            except BudgetRefused as e:
                act.status = "refused"
                self.log.write(s, {"event": "refused", "action": act.id, "reason": e.reason})
                return {"status": "refused", "reason": e.reason, "message": e.fa}
            self._budget(s).spend(s.id, deploy=tool.deploy, create_app=tool.create_app)   # an attempt counts
            if incident:
                incident.record_action(act.id)
        run_args = copy.deepcopy(act.args)
        if args_hash(act.tool, run_args) != act.hash:      # no await between this check and the run below
            act.status = "failed"
            return {"status": "error", "error": "args_mismatch"}
        act.approved_via = "web"
        self.log.write(s, {"event": "approved", "action": act.id, "tool": act.tool, "tier": act.tier, "via": "web"})
        s.emit("action_running", {"id": act.id})
        act.executed_at = time.time()
        try:
            result = await tool.run(s.ctx, run_args)
        except LiaraError as e:
            act.status, act.result = "failed", {"error": e.kind}
            self.log.write(s, {"event": "failed", "action": act.id, "error": e.kind, "liara": e.message})
            s.emit("action_result", {"id": act.id, "ok": False, "message": e.fa})
            return {"status": "failed", "error": e.kind, "message": e.fa}
        act.status, act.result = "done", s.redactor.obj(result)
        self.log.write(s, {"event": "executed", "action": act.id, "tool": act.tool, "ok": result.get("ok")})
        s.emit("action_result", {"id": act.id, "ok": result.get("ok"), "result": act.result})
        return {"status": "done", "result": act.result}

    def reject(self, s: Session, action_id: str) -> dict:
        act = s.actions.get(action_id)
        if act is None or act.status != "pending":
            return {"status": "error", "error": "not_pending"}
        act.status = "rejected"
        self.log.write(s, {"event": "rejected", "action": act.id})
        s.emit("action_result", {"id": act.id, "ok": False, "rejected": True})
        return {"status": "rejected"}

    @staticmethod
    def status(s: Session, action_id: str) -> dict:
        """Read-only (used by the coding agent's wait_for_approval)."""
        act = s.actions.get(action_id)
        if act is None:
            return {"status": "not_found"}
        if act.status == "pending" and act.expired:
            act.status = "expired"
        return {"status": act.status, "result": act.result}


class AgentPort:
    """What the LLM orchestrator and the MCP bridge get: propose and read status. No approve, no reject."""
    __slots__ = ("_call", "_status")

    def __init__(self, policy: Policy):
        self._call, self._status = policy.call, policy.status

    async def call(self, s: Session, tool: str, args: dict, *, caller: str, why: str = "") -> dict:
        return await self._call(s, tool, args, caller=caller, why=why)

    def status(self, s: Session, action_id: str) -> dict:
        return self._status(s, action_id)


def new_session_id() -> str:
    return uuid.uuid4().hex + secrets.token_hex(8)
