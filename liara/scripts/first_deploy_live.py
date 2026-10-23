"""Live run of the first-deploy chapter against the local assistant (spends real builds; run it on purpose).

Reads the Liara token from LIARA_TOKEN, else the file named by LIARA_TOKEN_FILE (default ../Liara/api.txt, outside the
repo; never printed), opens a session on the local backend, uploads a
project zip, asks for the create card and approves exactly that card (two taps), then follows the job until it is
live or stopped. Usage:  uv run --frozen python -m liara.scripts.first_deploy_live liara/demo-apps/app-d.zip dastyar-app-d
"""

import os
import sys
import time
from pathlib import Path

import httpx

BASE = "http://localhost:8771"
HDR = {"origin": BASE}
TOKEN_FILE = Path(os.environ.get("LIARA_TOKEN_FILE") or Path(__file__).resolve().parents[3] / "Liara" / "api.txt")


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def main(zip_path: str, app: str) -> int:
    with httpx.Client(base_url=BASE, timeout=120) as c:
        r = c.post("/api/session", json={"token": os.environ.get("LIARA_TOKEN", "").strip() or TOKEN_FILE.read_text(encoding="utf-8").strip()}, headers=HDR)
        say("session", r.status_code, {k: v for k, v in r.json().items() if k in ("team", "teams", "replay", "message")})
        if r.status_code != 200:
            return 1
        job = c.post(f"/api/projects?filename={Path(zip_path).name}", content=Path(zip_path).read_bytes(),
                     headers={**HDR, "content-type": "application/zip"}).json()
        if "id" not in job:
            say("upload failed", job)
            return 1
        say("analysis:", job["analysis"]["summary"], "| plan:", job["plan"].get("plan_id"), job["plan"].get("month"),
            "| preflight:", [f["rule"] for f in job["analysis"]["preflight"]])
        res = c.post(f"/api/projects/{job['id']}/create", json={"app": app}, headers=HDR).json()
        if res.get("status") != "waiting_for_user":
            say("create refused", res)
            return 1
        aid = res["pending_action_id"]
        act = next(a for a in c.get("/api/actions").json()["actions"] if a["id"] == aid)
        assert act["tool"] == "create_app" and app in act["card"]["title"], act
        say("card:", act["card"]["title"], "|", act["card"]["cost"])
        say("tap 1:", c.post(f"/api/actions/{aid}/approve", json={"args_hash": act["args_hash"]}, headers=HDR).json().get("status"))
        time.sleep(1.2)
        say("tap 2:", c.post(f"/api/actions/{aid}/approve", json={"args_hash": act["args_hash"], "sure": True},
                             headers=HDR).json().get("status"))
        seen, t0 = 0, time.time()
        while time.time() - t0 < 20 * 60:
            j = c.get(f"/api/projects/{job['id']}").json()
            for ev in j["events"][seen:]:
                say(f"[{ev['kind']}]", ev["text"], ("| " + ev["line"]) if ev.get("line") else "")
                if ev.get("diff"):
                    print(ev["diff"], flush=True)
            seen = len(j["events"])
            if j["state"] in ("live", "stopped"):
                say("FINAL", j["state"], j.get("url") or j.get("stop_reason"), "| builds", j["builds"])
                print(c.get(f"/api/projects/{job['id']}/patch").text, flush=True)
                return 0 if j["state"] == "live" else 2
            time.sleep(3)
        say("timeout watching")
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
