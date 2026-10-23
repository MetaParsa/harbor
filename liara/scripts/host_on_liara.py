"""Host the assistant itself on Liara, with its own Liara client (user-approved 2026-10-23: one app + ~3 builds).

Creates the app (small plan, python platform) on the token's team if it doesn't exist, packs this repo the way the
assistant packs any project (.gitignore rules, no secrets), releases it with the hosted settings (empty key = judge
mode, a real key = live on the visitor's account), waits for the build and checks /health twice. The Liara token is
read from Hackathon/Liara/api.txt and never printed.

  uv run --frozen python -m liara.scripts.host_on_liara [app-name]
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from liara.liara_api import LiaraClient, LiaraError, package_dir, probe

ROOT = Path(__file__).resolve().parents[2]
TOKEN_FILE = ROOT.parent / "Liara" / "api.txt"
L0_STATE = ROOT / "liara" / "l0" / "state.json"
PLAN = "small-g2"


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


async def main(name: str) -> int:
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    team = json.loads(L0_STATE.read_text(encoding="utf-8"))["team_id"]
    c = LiaraClient(token, team_id=team)
    try:
        try:
            app = await c.get_app(name)
            say("app exists:", app.name, app.plan_id)
        except LiaraError as e:
            if e.kind != "not_found":
                raise
            say("creating", name, PLAN)
            await c.create_app(name, PLAN, "python")
        tar, info = package_dir(ROOT, "python")
        say("packed", info)
        source = await c.upload_source(name, tar)
        envs = {"LIARA_REPLAY": "auto", "LIARA_DEV": "0", "LIARA_PUBLIC_ORIGIN": f"https://{name}.liara.run",
                "LIARA_DATA_DIR": "/tmp/liara-assistant", "LLM_ENABLED": "0", "LIARA_WATCH_PUSH": "0",
                "LIARA_MAX_SESSIONS": "200", "LIARA_MAX_SOURCE_MB": "64", "LIARA_JOBS_TOTAL": "6",
                "LIARA_DEPLOYS_PER_DAY": "60", "LIARA_APPS_PER_DAY": "10", "LIARA_WRITES_PER_HOUR_TOTAL": "200"}
        body = c.release_body(source, "python", 80, platform_config={"version": "3.12"}, envs=envs,
                              message="dastyar: the assistant hosting itself")
        rid = await c.create_release(name, body)
        say("release", rid)
        res = await c.wait_for_release(rid, on_line=lambda s, ln: print("   ", ln.rstrip()[:160]))
        say("build:", res.state, f"{res.seconds}s" if res.seconds else "", res.failure_line or "")
        if res.state != "READY":
            return 1
        for i in range(12):
            await asyncio.sleep(10)
            hits = await probe(f"https://{name}.liara.run/health", n=2)
            say("health", [h.get("status") for h in hits])
            if all(h.get("status") == 200 for h in hits):
                say("live: https://%s.liara.run" % name)
                return 0
        logs = await c.app_logs(name, since_s=300)
        for ln in logs[-25:]:
            print("   log:", ln.text[:200])
        return 1
    finally:
        await c.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "dastyar")))
