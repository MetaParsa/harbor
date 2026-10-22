"""Local dev server.

uv run --frozen python -m liara.dev_server          judge mode: three recorded demo apps (missing env var, code bug,
                                                    memory leak); no account, no network, no credit; any key works
                                                    →  http://127.0.0.1:8771
uv run --frozen python -m liara.dev_server --live   the real Liara API with the token you paste in the page
                                                    (reads cost nothing; writes still need your approval card)
"""

import os
import sys

import uvicorn

if "--live" in sys.argv:
    os.environ["LIARA_REPLAY"] = ""
    os.environ["LIARA_DEV"] = "0"
else:
    os.environ.setdefault("LIARA_REPLAY", "demo")
    os.environ.setdefault("LIARA_DEV", "1")

if __name__ == "__main__":
    uvicorn.run("liara.app:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8771")))
