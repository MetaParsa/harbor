"""Local dev server.

uv run --frozen python -m liara.dev_server          judge mode: three recorded demo apps (missing env var, code bug,
                                                    memory leak); no account, no network, no credit; any key works
                                                    →  http://127.0.0.1:8771 (another port: PORT=8800)
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
    os.environ.setdefault("LIARA_DEV", "0")

if os.environ.get("LLM_ENABLED") == "1" and not os.environ.get("ANTHROPIC_API_KEY") and sys.platform == "win32":
    # Windows: a user-level variable set after this shell started isn't inherited; read it (never printed or saved)
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            os.environ["ANTHROPIC_API_KEY"] = winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")[0]
    except OSError:
        pass

if __name__ == "__main__":
    uvicorn.run("liara.app:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8771")))
