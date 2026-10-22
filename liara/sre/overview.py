"""The app's home card: status, plan + price, CPU/RAM against the plan's limits, last release.

Read-only, one round of parallel calls. Covers rubric #4 "health and resource monitoring" outside incidents.
Anything Liara doesn't return is left as None and the page says «نامعلوم» — never a guessed number.
"""

import asyncio
import time

from liara.agent.policy import Session
from liara.liara_api import LiaraError

from .collect import release_ts


async def app_overview(s: Session, name: str) -> dict:
    app, rels, plans, usage = await asyncio.gather(s.client.get_app(name), s.client.releases(name, 1),
                                                   s.client.plans(), s.client.usage_now(name), return_exceptions=True)
    if isinstance(app, Exception):
        kind = app.kind if isinstance(app, LiaraError) else "internal"
        return {"status": "error", "error": kind, "message": getattr(app, "fa", "برنامه خوانده نشد.")}
    s.redactor.add(*[e.value for e in app.envs])            # values seen anywhere get masked everywhere
    out = {"name": app.name, "url": None if s.demo else app.url,   # recorded demo apps have no live site
           "platform": app.platform, "state": app.status, "on": app.scale > 0,
           "plan": app.plan_id, "bundle": app.bundle_id, "env_keys": [e.key for e in app.envs],
           "zero_downtime": app.zero_downtime,
           "zero_downtime_allowed": app.bundle_id != "basic",     # live 2026-10-22: basic → 428 "plan does not support"
           "cpu_pct": None, "ram_pct": None, "ram_mb": None, "plan_cpu": None, "plan_ram_gb": None,
           "price_month": None, "last_release": None}
    if not isinstance(plans, Exception) and app.plan_id in plans.apps:
        plan = plans.apps[app.plan_id]
        out.update(plan_cpu=plan.cpu, plan_ram_gb=plan.ram_gb, price_month=plans.price(app.plan_id, app.bundle_id)[1])
        if not isinstance(usage, Exception) and usage.get("container"):
            if usage.get("memory_bytes") is not None and plan.ram_gb:
                out["ram_mb"] = round(usage["memory_bytes"] / 1048576, 1)
                out["ram_pct"] = round(100 * usage["memory_bytes"] / (plan.ram_gb * 1024 ** 3), 1)
            if usage.get("cpu_cores") is not None and plan.cpu:
                out["cpu_pct"] = round(100 * usage["cpu_cores"] / plan.cpu, 1)
    if not isinstance(rels, Exception) and rels.items:
        r = rels.items[0]
        t = release_ts(r)
        out["last_release"] = {"tag": r.tag, "type": r.type, "state": r.state,
                               "ago_s": round(time.time() - t) if t else None}
    # Only free text needs masking; names, URLs and plan ids come straight from Liara's own fields and a secret that
    # happens to contain the app name must not blank the app's name and link (review 2026-10-22).
    return out


async def app_list(s: Session) -> list[dict]:
    apps = await s.client.list_apps()
    return [{"name": a.name, "platform": a.platform, "state": a.status, "on": a.scale > 0} for a in apps]
