"""Every Liara action the agent can take, with its tier, its approval card and its executor.

Tiers (LIARA.md §2; enforced by policy.Policy, never by a prompt):
  T0 read: auto · T1 safe write: auto, shown · T2: one tap · T3: two taps («مطمئنم») with cost and downtime.
There is deliberately no tool that deletes a user's app, touches billing or changes the bundle.

A card is what the user sees before approving: one plain sentence, then risk / cost / downtime / undo and
«چطور می‌فهمم درست شد؟» (the success contract), all in Persian. Values of env vars never appear on a card.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from liara.liara_api import LiaraClient

from liara.text import fa_num, toman

from .redact import Redactor


@dataclass
class Card:
    title: str                  # one plain sentence: what will happen
    risk: str
    cost: str = "رایگان"
    downtime: str = "بدون قطعی"
    undo: str = "این یکی برگشت خودکار نداره."
    success: str = ""           # how we'll judge it worked (verified after the action)


@dataclass
class Ctx:
    """What an executor may touch: the session's client and its memory of what it created and deployed."""
    client: LiaraClient
    redactor: Redactor = field(default_factory=Redactor)
    created_apps: set[str] = field(default_factory=set)
    builds_on_created: dict[str, int] = field(default_factory=dict)
    release_bodies: dict[str, dict[str, dict]] = field(default_factory=dict)   # app -> release tag/id -> body
    env_history: dict[str, list[set[str]]] = field(default_factory=dict)       # app -> env key sets seen, oldest first
    known_good: dict[str, list[str]] = field(default_factory=dict)             # app -> release ids that passed a verify


@dataclass
class Tool:
    name: str
    tier: int
    run: Callable[[Ctx, dict], Awaitable[dict]]
    card: Callable[[Ctx, dict], Awaitable[Card]] | None = None
    deploy: bool = False        # counts against the daily deploy cap
    create_app: bool = False
    second_tap: bool = False    # T3 actions that need «مطمئنم»: resize, create app, turn off
    app_arg: str | None = "app"


# ---------- executors ----------

async def _restart(ctx, a):
    await ctx.client.restart(a["app"])
    return {"ok": True}


async def _set_envs(ctx, a):
    await register_env_values(ctx, a["app"])
    keys = await ctx.client.set_envs(a["app"], a["changes"], set(a.get("encrypted") or ()))
    return {"ok": True, "keys": keys}


async def register_env_values(ctx, app: str) -> None:
    """Values already on the account must be masked too, not only the ones we set (they can echo in logs)."""
    a = await ctx.client.get_app(app)
    ctx.redactor.add(*[e.value for e in a.envs])


async def _deploy(ctx, a):
    # builds_on_created is counted by the policy before this runs (so parallel calls can't overrun the 3)
    c = ctx.client
    body = c.release_body(a["source_id"], a["platform"], a["port"], platform_config=a.get("platform_config"),
                          message=a.get("message") or "deploy by dastyar")
    rid = await c.create_release(a["app"], body)
    ctx.release_bodies.setdefault(a["app"], {})[rid] = body
    res = await c.wait_for_release(rid)
    return {"ok": res.state == "READY", "release_id": rid, "state": res.state, "failure_line": res.failure_line,
            "seconds": res.seconds}


async def _rollback(ctx, a):
    body = (ctx.release_bodies.get(a["app"]) or {}).get(a["release_id"])
    if not body:
        return {"ok": False, "error": "no_stored_body"}
    rid = await ctx.client.redeploy(a["app"], body, message=f"rollback to {a['release_id']}")
    res = await ctx.client.wait_for_release(rid)
    return {"ok": res.state == "READY", "release_id": rid, "state": res.state, "seconds": res.seconds}


async def _turn_on(ctx, a):
    await ctx.client.turn_on(a["app"])
    return {"ok": True}


async def _turn_off(ctx, a):
    await ctx.client.turn_off(a["app"])
    return {"ok": True}


async def _resize(ctx, a):
    await ctx.client.resize(a["app"], a["plan_id"])
    return {"ok": True}


async def _create_app(ctx, a):
    pid = await ctx.client.create_app(a["app"], a["plan_id"], a["platform"], a.get("bundle_id", "basic"))
    ctx.created_apps.add(a["app"])
    return {"ok": True, "project_id": pid}


async def _zero_downtime(ctx, a):
    app = await ctx.client.get_app(a["app"])
    await ctx.client.set_zero_downtime(app.id, True)
    return {"ok": True}


async def _add_domain(ctx, a):
    return {"ok": True, "domain": await ctx.client.add_domain(a["app"], a["domain"])}


async def _provision_ssl(ctx, a):
    return {"ok": True, "result": await ctx.client.provision_ssl(a["domain"])}


async def _read_app(ctx, a):
    app = await ctx.client.get_app(a["app"])
    ctx.redactor.add(*[e.value for e in app.envs])
    return {"ok": True, "app": {"name": app.name, "status": app.status, "scale": app.scale, "plan": app.plan_id,
                                "env_keys": [e.key for e in app.envs]}}


async def _read_releases(ctx, a):
    rels = await ctx.client.releases(a["app"], a.get("count", 5))
    return {"ok": True, "current": rels.current_id,
            "releases": [{"id": r.id, "tag": r.tag, "type": r.type, "state": r.state, "at": r.created_at} for r in rels.items]}


# ---------- cards ----------

async def _card_restart(ctx, a):
    return Card(f"برنامه‌ی {a['app']} رو ری‌استارت می‌کنم.", "اگر مشکل از کد باشه، بعد از ری‌استارت برمی‌گرده.",
                downtime="حدود ۲۰ ثانیه", undo="لازم نیست؛ فقط یک ری‌استارته.",
                success="سایت در ۲ دقیقه بعد جواب ۲۰۰ بده و خطای تازه در لاگ نباشه.")


async def _card_set_envs(ctx, a):
    names = "، ".join(a["changes"]) or "—"
    return Card(f"متغیرهای محیطی {names} رو روی {a['app']} تنظیم می‌کنم.",
                "برنامه با تنظیمات تازه دوباره بالا میاد؛ اگر مقدار اشتباه باشه، همون خطا می‌مونه.",
                downtime="تقریباً صفر (نسخه‌ی تازه کنار نسخه‌ی قبلی بالا میاد)",
                undo="متغیرهایی که الان عوض می‌کنم رو می‌تونم به مقدار قبلی برگردونم.",
                success=a.get("success") or "سایت جواب ۲۰۰ بده و خطای مربوط به این متغیر در لاگ تکرار نشه.")


async def _deploy_cost(ctx, a) -> str:
    try:
        app = await ctx.client.get_app(a["app"])
        cap = (await ctx.client.plans()).bundle(app.plan_id, app.bundle_id)
        return (f"یکی از {fa_num(cap.max_deploys_per_day)} دیپلوی روزانه"
                if cap and cap.max_deploys_per_day else "یک دیپلوی")
    except Exception:
        return "یک دیپلوی از سهمیه‌ی روزانه"


async def _card_deploy(ctx, a):
    return Card(f"نسخه‌ی تازه رو روی {a['app']} منتشر می‌کنم.",
                "اگر بیلد خراب بشه، نسخه‌ی فعلی سر جاش می‌مونه؛ اگر بالا بیاد ولی خطا بده، برمی‌گردونمش.",
                cost=await _deploy_cost(ctx, a), downtime="معمولاً بدون قطعی؛ بیلد حدود ۳۰ ثانیه",
                undo="برگشت به نسخه‌ی قبلی با یک بیلد تازه (حدود ۳۰ ثانیه).",
                success=a.get("success") or "سایت جواب ۲۰۰ بده و ۱۵ ثانیه لاگ بدون کرش باشه.")


async def _card_rollback(ctx, a):
    return Card(f"{a['app']} رو به نسخه‌ی {a.get('tag') or a['release_id']} برمی‌گردونم.",
                "تغییرهای نسخه‌ی فعلی از روی سایت برداشته می‌شه.",
                cost=await _deploy_cost(ctx, a), downtime="یک بیلد جدید، حدود ۳۰ ثانیه",
                undo="می‌شه دوباره نسخه‌ی فعلی رو منتشر کرد.",
                success=a.get("success") or "سایت جواب ۲۰۰ بده و نشانه‌ی مشکل (خطا یا مصرف حافظه) برگرده به حالت قبل.")


async def _card_turn_on(ctx, a):
    return Card(f"برنامه‌ی {a['app']} رو روشن می‌کنم.", "از این لحظه هزینه‌ی ساعتی حساب می‌شه.",
                cost="هزینه‌ی ساعتی پلن فعلی", undo="خاموش کردن دوباره (با تأیید).", success="سایت جواب ۲۰۰ بده.")


async def _card_turn_off(ctx, a):
    return Card(f"برنامه‌ی {a['app']} رو خاموش می‌کنم.", "تا روشنش نکنی، سایت از دسترس خارجه.",
                cost="هزینه‌ی ساعتی متوقف می‌شه", downtime="تا وقتی دوباره روشن بشه", undo="روشن کردن دوباره (با تأیید).",
                success="لیارا وضعیت برنامه رو خاموش نشون بده.")


async def _card_resize(ctx, a):
    plans = await ctx.client.plans()
    app = await ctx.client.get_app(a["app"])
    old_h, old_m = plans.price(app.plan_id, app.bundle_id)
    new_h, new_m = plans.price(a["plan_id"], app.bundle_id)
    new = plans.apps[a["plan_id"]]
    return Card(f"پلن {a['app']} رو به {new.id} ({fa_num(new.cpu, 2)} هسته، {fa_num(new.ram_gb, 2)} گیگ رم) تغییر می‌دم.",
                "هزینه‌ی ماهانه عوض می‌شه؛ اگر مشکل از کد باشه (مثل نشت حافظه)، فقط عقبش می‌اندازه.",
                cost=f"{toman(old_m)} ← {toman(new_m)} در ماه ({'+' if new_m >= old_m else ''}{toman(new_m - old_m)})",
                downtime="یک ری‌استارت کوتاه", undo="برگشت به پلن قبلی با یک تأیید دیگه.",
                success=a.get("success") or "مصرف حافظه زیر ۸۰٪ سقف تازه بمونه و سایت کند نباشه.")


async def _card_create_app(ctx, a):
    plans = await ctx.client.plans()
    p = plans.apps[a["plan_id"]]
    _, month = plans.price(a["plan_id"], a.get("bundle_id", "basic"))
    return Card(f"برنامه‌ی {a['app']} رو با پلن {p.id} ({fa_num(p.cpu, 2)} هسته، {fa_num(p.ram_gb, 2)} گیگ رم) روی حساب لیارات می‌سازم.",
                "از لحظه‌ی ساخت، هزینه‌ی ساعتی از اعتبارت کم می‌شه. تا ۳ بیلد روی همین برنامه بدون پرسیدن دوباره انجام می‌دم.",
                cost=f"حدود {toman(month)} در ماه (ساعتی حساب می‌شه)", downtime="ندارد (برنامه‌ی تازه است)",
                undo="حذف برنامه فقط از کنسول لیارا و توسط خودت.",
                success=f"{a['app']}.liara.run جواب ۲۰۰ بده و ۱۵ ثانیه بعد هم بی‌کرش جواب بده.")


async def _card_zero_downtime(ctx, a):
    return Card(f"«انتشار بدون قطعی» رو برای {a['app']} روشن می‌کنم: از این به بعد نسخه‌ی تازه کنار نسخه‌ی قبلی بالا میاد "
                "و ترافیک بعد از آماده شدنش جابه‌جا می‌شه.",
                "برای چند ثانیه دو نسخه همزمان اجرا می‌شن؛ اگر بسته‌ی برنامه اجازه نده، لیارا رد می‌کنه و همین رو می‌گم.",
                undo="خاموش کردنش از کنسول لیارا (تنظیمات برنامه).",
                success="لیارا این تنظیم رو برای برنامه روشن نشون بده.")


async def _card_add_domain(ctx, a):
    return Card(f"دامنه‌ی {a['domain']} رو به {a['app']} وصل می‌کنم.", "تا رکوردهای DNS رو در پنل دامنه تنظیم نکنی، کار نمی‌کنه.",
                undo="حذف دامنه از کنسول لیارا.", success="لیارا دامنه رو تأییدشده نشون بده.")


async def _card_ssl(ctx, a):
    return Card(f"گواهی SSL رایگان برای {a['domain']} می‌گیرم.", "اگر DNS هنوز درست نشده باشه، ناموفق می‌شه.",
                success=f"https://{a['domain']} بدون خطای گواهی باز بشه.", undo="لازم نیست.")


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("read_app", 0, _read_app),
    Tool("read_releases", 0, _read_releases),
    Tool("restart", 2, _restart, _card_restart),
    Tool("set_envs", 2, _set_envs, _card_set_envs),
    Tool("deploy", 2, _deploy, _card_deploy, deploy=True),
    Tool("rollback", 2, _rollback, _card_rollback, deploy=True),
    Tool("turn_on", 2, _turn_on, _card_turn_on),
    Tool("zero_downtime", 2, _zero_downtime, _card_zero_downtime),
    Tool("add_domain", 2, _add_domain, _card_add_domain),
    Tool("provision_ssl", 2, _provision_ssl, _card_ssl, app_arg=None),
    Tool("turn_off", 3, _turn_off, _card_turn_off, second_tap=True),
    Tool("resize", 3, _resize, _card_resize, second_tap=True),
    # app_arg=None: the app doesn't exist yet, so there is nothing to fingerprint (it would fail closed forever)
    Tool("create_app", 3, _create_app, _card_create_app, create_app=True, second_tap=True, app_arg=None),
]}

FREE_BUILDS_ON_CREATED = 3   # the create-app approval covers up to 3 builds on that app

# ---------- argument validation (the LLM writes these args; they go into URL paths and onto cards) ----------

# \Z, not $: "$" also matches before a trailing newline, which then reaches Liara's URL paths
APP_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")          # Liara's own app-name rule
DOMAIN = re.compile(r"^(?=.{4,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
SIMPLE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
PLATFORMS = {"node", "next", "react", "vue", "angular", "static", "flask", "django", "python", "php", "laravel",
             "go", "dotnet", "docker"}


def validate(tool: "Tool", args) -> str | None:
    """None if the args are well-formed for this tool, else a short reason. Checked before any card or call."""
    if not isinstance(args, dict):
        return "args must be an object"
    need = {"read_app": ["app"], "read_releases": ["app"], "restart": ["app"], "turn_on": ["app"],
            "turn_off": ["app"], "zero_downtime": ["app"], "set_envs": ["app", "changes"], "deploy": ["app", "source_id", "platform", "port"],
            "rollback": ["app", "release_id"], "resize": ["app", "plan_id"],
            "create_app": ["app", "plan_id", "platform"], "add_domain": ["app", "domain"],
            "provision_ssl": ["domain"]}.get(tool.name, [])
    missing = [k for k in need if k not in args]
    if missing:
        return f"missing {', '.join(missing)}"
    if "app" in args and not (isinstance(args["app"], str) and APP_NAME.match(args["app"])):
        return "bad app name"
    if "domain" in args and not (isinstance(args["domain"], str) and DOMAIN.match(args["domain"])):
        return "bad domain"
    for k in ("plan_id", "release_id", "source_id", "bundle_id"):
        if k in args and not (isinstance(args[k], str) and SIMPLE_ID.match(args[k])):
            return f"bad {k}"
    if "platform" in args and args["platform"] not in PLATFORMS:
        return "unknown platform"
    if "port" in args and not (isinstance(args["port"], int) and 1 <= args["port"] <= 65535):
        return "bad port"
    if tool.name == "set_envs":
        ch = args["changes"]
        if not (isinstance(ch, dict) and ch and all(isinstance(k, str) and ENV_KEY.match(k) for k in ch)
                and all(v is None or isinstance(v, str) for v in ch.values())):
            return "changes must map env names to string values (or null to remove)"
        if not all(isinstance(k, str) for k in args.get("encrypted") or []):
            return "bad encrypted list"
    return None


def effective_tier(tool: Tool, ctx: Ctx, args: dict) -> int:
    """T1 for deploys to an app this session created (up to 3 builds, covered by the create-app approval)."""
    app = args.get("app")
    if tool.name == "deploy" and app in ctx.created_apps and ctx.builds_on_created.get(app, 0) < FREE_BUILDS_ON_CREATED:
        return 1
    return tool.tier
