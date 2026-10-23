"""Check a fix worked, against the success contract written on the card before approval.

No LLM. Steps, shaped by the live dry run (liara/l0/RESULTS.md §8):
1. if the fix replaces the container (deploy, rollback, resize), wait for a `start` event after the action —
   the old container keeps serving ~40 s after READY, so READY is not enough;
2. a settle window, then probe the route; edge 502/503 (not routed yet) get a short grace and a retry;
3. app logs since the action: no new 5xx on that route and no new traceback;
4. memory (when the contract asks): the new container's series must not keep climbing. With too few points
   the check is "provisional" and the board says so.
"""

import asyncio
import time

from liara.liara_api import LiaraClient, LiaraError
from liara.liara_api.models import newest_container

from .collect import ERROR_HEAD, Prober, default_prober, fa_num, parse_request, trim_startup



async def verify(client: LiaraClient, app: str, contract: dict, before: dict, started_at: float, *,
                 prober: Prober | None = None, sleep=asyncio.sleep, clock=time.time,
                 container_wait_s: float = 150, settle_s: float = 20, recorded: str | None = None) -> dict:
    """recorded = judge mode: the fix was simulated and `client` serves a real after-recording (described by this
    Persian text); there is no new container to wait for, and the result says where the numbers come from."""
    prober = prober or default_prober
    checks = []
    route = contract.get("route") or "/"

    if contract.get("new_container") and recorded:
        checks.append({"name": "new_container", "ok": None,
                       "text": "نمایشی: چیزی روی لیارا ساخته نشد؛ اعداد زیر از ضبط واقعی‌اند"})
    elif contract.get("new_container"):
        # a container that didn't exist before the fix — an OOM restart of the old one keeps its id (live)
        old = set(before.get("containers") or [])
        deadline, seen = clock() + container_wait_s, False
        while clock() < deadline:
            try:
                ev = await client.events(app, 900)
                if any(e.status == "start" and e.time >= started_at - 2 and e.container not in old for e in ev):
                    seen = True
                    break
            except LiaraError:
                pass
            await sleep(5)
        checks.append({"name": "new_container", "ok": seen,
                       "text": "نسخه‌ی تازه بالا اومد" if seen else "نسخه‌ی تازه در زمان مقرر بالا نیومد"})
    await sleep(settle_s)

    results = []
    for _ in range(3):                       # edge grace: 502/503 right after a switch are the router, not the app
        results = await prober(f"https://{app}.liara.run{route}", contract.get("probes", 5))
        if not all(r.get("status") in (502, 503) for r in results):
            break
        await sleep(15)
    codes = [r.get("status") for r in results]
    ok_probe = bool(codes) and all(c is not None and c <= contract.get("max_status", 399) for c in codes)
    ms = sorted(r["ms"] for r in results if r.get("ms") is not None)
    after = {"route": route, "codes": codes, "median_ms": ms[len(ms) // 2] if ms else None}
    checks.append({"name": "probe", "ok": ok_probe,
                   "text": f"{route}: {'، '.join(str(c) for c in codes)}"})

    new_5xx, new_tb = 0, 0
    try:
        current = None
        try:
            current = (await client.releases(app, 1)).current_id
        except LiaraError:
            pass
        logs = await client.app_logs(app, since_s=int(clock() - started_at) + 30)
        for ln in logs:
            if ln.ts < started_at + settle_s:
                continue
            if current and ln.release_id and ln.release_id != current:
                continue                     # the old container's last lines are not the fix's result
            if (r := parse_request(ln.text)) and r[1] >= 500 and r[0] == route.split("?")[0]:
                new_5xx += 1
            if ln.stream == "stderr" and ERROR_HEAD.match(ln.text.strip()):
                new_tb += 1
        checks.append({"name": "logs", "ok": new_5xx == 0 and new_tb == 0,
                       "text": f"بعد از تغییر: {fa_num(new_5xx)} خطای ۵xx و {fa_num(new_tb)} خطای برنامه در لاگ"})
    except LiaraError:
        checks.append({"name": "logs", "ok": None, "text": "نمایشی: لاگ بعد از این تغییر ضبط نشده" if recorded
                       else "لاگ از لیارا خوانده نشد"})
    after["errors_5xx"] = new_5xx

    if contract.get("memory_flat"):
        try:
            series = await client.metrics(app, "memory", 900)
            name = newest_container(series)
            s = next((x for x in series if x.container == name), None)
            pts = trim_startup([p for p in (s.points if s else []) if p[0] >= started_at])
            if len(pts) >= 3:
                slope = (pts[-1][1] - pts[0][1]) / 1048576 / max((pts[-1][0] - pts[0][0]) / 60, 0.1)
                flat = slope < max(0.5, 0.2 * (before.get("slope_mb_min") or 0))
                after.update(mem_mb=pts[-1][1] / 1048576, slope_mb_min=slope)
                checks.append({"name": "memory", "ok": flat, "text": f"حافظه‌ی نسخه‌ی تازه: روند "
                               f"{'+' if slope >= 0 else ''}{fa_num(slope, 1)} مگابایت در دقیقه"})
            else:
                checks.append({"name": "memory", "ok": None, "provisional": True,
                               "text": "هنوز نمونه‌ی کافی از حافظه‌ی نسخه‌ی تازه نیست؛ چند دقیقه بعد دوباره نگاه می‌کنم"})
        except LiaraError:
            checks.append({"name": "memory", "ok": None, "text": "داده‌ی حافظه خوانده نشد"})

    if contract.get("cpu_max_cores"):
        try:
            series = await client.metrics(app, "cpu", 900)
            name = newest_container(series)
            s = next((x for x in series if x.container == name), None)
            pts = [p for p in (s.points if s else []) if p[0] >= started_at]
            if len(pts) >= 2:
                cores = sum(v for _, v in pts[-3:]) / len(pts[-3:])
                checks.append({"name": "cpu", "ok": cores <= contract["cpu_max_cores"],
                               "text": f"پردازنده‌ی نسخه‌ی تازه: {fa_num(cores, 2)} هسته"})
            else:
                checks.append({"name": "cpu", "ok": None, "provisional": True,
                               "text": "هنوز نمونه‌ی کافی از پردازنده‌ی نسخه‌ی تازه نیست"})
        except LiaraError:
            checks.append({"name": "cpu", "ok": None, "text": "داده‌ی پردازنده خوانده نشد"})

    decided = [c["ok"] for c in checks if c["ok"] is not None]
    ok = bool(decided) and all(decided)
    provisional = any(c.get("provisional") for c in checks)
    return {"ok": ok, "provisional": provisional, "checks": checks, "before": before, "after": after,
            **({"recorded": recorded} if recorded else {}),
            "summary": ("درست شد ✓" if ok and not provisional else "فعلاً درست به نظر می‌رسه" if ok else "هنوز درست نشده")}
