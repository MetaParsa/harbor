"""Entry point of the hosted assistant: Liara's python platform runs `python3 main.py`
(docs.liara.ir/paas/python/quick-start), and so does the VPS service (deploy/vps/).

Hosted mode: an empty key opens judge mode (recorded apps, simulated fixes); a real Liara key opens a live session
on the visitor's own account. Settings come from env vars (deploy/vps/dastyar.env.example lists them).
"""

import os

import uvicorn

os.environ.setdefault("LIARA_REPLAY", "auto")
os.environ.setdefault("LIARA_DEV", "0")
os.environ.setdefault("LIARA_DATA_DIR", "/tmp/liara-assistant")      # a Liara app's root filesystem is read-only

if __name__ == "__main__":
    uvicorn.run("liara.app:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "80")),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
