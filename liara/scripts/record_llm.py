"""Record Claude's real answers for the three judge-mode apps (real model spend, about $0.20).

Runs the normal diagnosis over each demo recording with the real model on, and saves what the model answered for
its two jobs (understand the complaint, rank + explain the evidence) to fixtures/demo/llm/<app>.json, with the model
id, token counts and time. Judge mode replays these answers (labelled «پاسخ ضبط‌شده‌ی Claude») so every visitor sees
the model's part without any spend. The key comes from ANTHROPIC_API_KEY (Windows: the user-level variable); it is
never printed or saved.

  uv run --frozen python -m liara.scripts.record_llm
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Persian output on Windows consoles

from core.llm import from_env
from liara.agent.redact import Redactor
from liara.agent.policy import Session
from liara.liara_api import DemoReplay, LiaraClient
from liara.liara_api.replay import DEMO_RECORDED_TEXT, FIXTURES
from liara.sre.engine import troubleshoot

OUT = FIXTURES / "demo" / "llm"
MAX_TOKENS_TOTAL = 40_000          # ≈ $0.15 at Sonnet prices: the run stops before going past it


def load_key() -> None:
    if os.environ.get("ANTHROPIC_API_KEY") or sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        os.environ["ANTHROPIC_API_KEY"] = winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")[0]


class Recording:
    """Wraps the real client: passes each call through and keeps role → (answer text, model, usage)."""

    def __init__(self, llm):
        self.llm, self.calls, self.tokens = llm, [], 0

    def complete(self, role, messages, **kw):
        if self.tokens > MAX_TOKENS_TOTAL:
            raise RuntimeError("token cap reached")
        res = self.llm.complete(role, messages, **kw)
        self.tokens += sum(res.usage.values())
        self.calls.append({"role": role, "text": res.text, "model": res.model, "usage": res.usage, "ms": res.ms})
        return res


async def main() -> int:
    load_key()
    os.environ["LLM_ENABLED"] = "1"
    os.environ.setdefault("LLM_SESSION_TOKENS", str(MAX_TOKENS_TOTAL))
    rec = Recording(from_env())
    OUT.mkdir(parents=True, exist_ok=True)
    for app, text in DEMO_RECORDED_TEXT.items():
        demo = DemoReplay()
        s = Session(f"rec-{app}", LiaraClient("demo", transport=demo), Redactor(), demo=demo)
        s.ctx.env_history.update(demo.env_history())
        rec.calls.clear()
        board = await troubleshoot(s, app, text, llm=rec, prober=demo.probe, now=demo.now(app))
        answers = {c["role"]: c for c in rec.calls}
        (OUT / f"{app}.json").write_text(json.dumps({
            "app": app, "text": text, "recorded_at": int(time.time()), "answers": answers,
            "board_headline": board["headline"], "ai": board["ai"]}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(app, "→", board["ai"]["symptom_source"], board["ai"]["explain_source"], "|",
              [c["usage"] for c in rec.calls], flush=True)
    print("tokens:", rec.tokens)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
