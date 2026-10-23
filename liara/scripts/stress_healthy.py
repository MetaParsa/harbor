"""Run: uv run --frozen python liara/scripts/stress_healthy.py

Healthy app × many phrasings: the answer must say healthy (H0), never invent a cause, and answer "slow?" with the
measured time. Uses the v1_healthy recording as a 4th demo app (test-only mapping). Free: no network, no LLM."""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")  # Persian output on Windows consoles

os.environ["LIARA_REPLAY"] = "demo"
os.environ["LIARA_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from liara.liara_api import replay  # noqa: E402

replay.DEMO_APPS["shop-healthy"] = "v1_healthy"
from fastapi.testclient import TestClient  # noqa: E402

import liara.app as m  # noqa: E402

O = {"origin": "http://testserver"}
c = TestClient(m.app)
c.post("/api/session", json={"token": "judge"}, headers=O)
PHRASES = ["صفحه‌ی پرداخت خطا می‌ده", "سایتم کند شده", "سایت بالا نمیاد", "یه بررسی کلی کن؛ همه‌چیز سالمه؟", "رم پر شده؟",
           "چرا سایت ری‌استارت می‌شه", "ارور میده", "site kond shode", "checkout returns 500", "the site is slow",
           "it keeps crashing", "help", "", "سلام", "ممنون", "هزینه‌ام چقدره؟", "دامنه‌ام رو وصل کن", "نسخه‌ی جدید رو منتشر کن"]
bad = 0
for text in PHRASES:
    b = c.post("/api/troubleshoot", json={"app": "shop-healthy", "text": text}, headers=O).json()
    top = (b.get("hypotheses") or [{}])[0]
    fixes = [f.get("label") or f.get("kind") for f in top.get("fixes", [])]
    flag = "" if top.get("id") == "H0" else "   <-- not H0"
    bad += bool(flag)
    print(f"«{text[:30]}» → {top.get('id')} {top.get('confidence')} | {b.get('headline')} | q: {b.get('question')} | fixes {fixes}{flag}")
print(f"\n{len(PHRASES) - bad}/{len(PHRASES)} said healthy")
