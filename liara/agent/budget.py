"""Hard caps on what the agent may spend on Liara, plus a kill switch. LLM tokens are capped by core.llm.SpendGuard.

User rule: no credit use without permission. The user approved ~20–26 builds over Thu–Fri (2026-10-22); these caps
make sure a bug or a loop can't go past that, whatever the LLM or a judge does.
- deploys per day across all sessions (Liara's own cap on the basic bundle is 20/day; keep headroom for us)
- write actions (T1–T3) per session per hour
- app creations per day (each new app costs money every hour it exists)
- kill switch: env LIARA_AGENT_KILL=1 or a file named KILL in the data dir → every write is refused

Counts are persisted (JSON in the data dir) so a backend restart doesn't reset the day.
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "liara"


class BudgetRefused(RuntimeError):
    def __init__(self, reason: str, fa: str):
        super().__init__(reason)
        self.reason = reason
        self.fa = fa


@dataclass
class Caps:
    deploys_per_day: int = int(os.environ.get("LIARA_DEPLOYS_PER_DAY", "15"))
    apps_per_day: int = int(os.environ.get("LIARA_APPS_PER_DAY", "3"))
    writes_per_session_hour: int = int(os.environ.get("LIARA_WRITES_PER_HOUR", "12"))
    writes_per_hour_total: int = int(os.environ.get("LIARA_WRITES_PER_HOUR_TOTAL", "40"))   # reconnecting can't reset it


class Budget:
    def __init__(self, data_dir: Path | None = None, caps: Caps | None = None):
        self.dir = Path(data_dir or os.environ.get("LIARA_DATA_DIR") or DEFAULT_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.caps = caps or Caps()
        self._file = self.dir / "budget.json"
        self._lock = threading.Lock()
        self._writes: dict[str, list[float]] = {}     # session -> timestamps (in memory; an hour window)

    # ---- kill switch ----
    def killed(self) -> bool:
        return os.environ.get("LIARA_AGENT_KILL") == "1" or (self.dir / "KILL").exists()

    # ---- persisted daily counters ----
    def _load(self) -> dict:
        try:
            d = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        if d.get("day") != date.today().isoformat():
            d = {"day": date.today().isoformat(), "deploys": 0, "apps": 0}
        return d

    def _save(self, d: dict) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(self._file)

    def today(self) -> dict:
        with self._lock:
            return self._load()

    def check(self, session_id: str, *, write: bool, deploy: bool = False, create_app: bool = False) -> None:
        """Raise BudgetRefused if this action would pass a cap. Call before proposing and again before executing."""
        if not write:
            return
        if self.killed():
            raise BudgetRefused("kill_switch", "اجرای هر تغییری روی لیارا فعلاً خاموش شده (کلید توقف اضطراری). فقط خواندن ممکنه.")
        now = time.time()
        recent = [t for t in self._writes.get(session_id, []) if now - t < 3600]
        total = sum(1 for ts in self._writes.values() for t in ts if now - t < 3600)
        if len(recent) >= self.caps.writes_per_session_hour or total >= self.caps.writes_per_hour_total:
            raise BudgetRefused("session_writes", "در این یک ساعت به سقف تغییرها رسیدیم؛ کمی بعد ادامه می‌دیم.")
        with self._lock:
            d = self._load()
        if deploy and d["deploys"] >= self.caps.deploys_per_day:
            raise BudgetRefused("deploys_per_day", f"سقف {self.caps.deploys_per_day} دیپلوی امروزِ این دستیار پر شده؛ فردا ادامه می‌دیم.")
        if create_app and d["apps"] >= self.caps.apps_per_day:
            raise BudgetRefused("apps_per_day", "امروز به سقف ساخت برنامه‌ی جدید رسیدیم.")

    def spend(self, session_id: str, *, deploy: bool = False, create_app: bool = False) -> None:
        """Record a write that actually ran."""
        self._writes.setdefault(session_id, []).append(time.time())
        if deploy or create_app:
            with self._lock:
                d = self._load()
                d["deploys"] += int(deploy)
                d["apps"] += int(create_app)
                self._save(d)
