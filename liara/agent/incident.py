"""Incident record: one per reported problem, the spine the SRE chapter hangs on.

State machine: open → diagnosing → waiting_user | waiting_coding_agent → verifying → resolved | handed_to_support.
It holds evidence ids, hypotheses, the actions taken (with who approved them), before/after numbers and times,
and enforces the mutation budget: at most 3 approved T2/T3 actions per incident, then it hands off.
"""

import time
import uuid
from dataclasses import dataclass, field

TRANSITIONS = {
    "open": {"diagnosing", "handed_to_support"},
    "diagnosing": {"waiting_user", "waiting_coding_agent", "verifying", "handed_to_support", "resolved"},
    "waiting_user": {"verifying", "diagnosing", "waiting_coding_agent", "handed_to_support"},
    "waiting_coding_agent": {"verifying", "diagnosing", "handed_to_support"},
    "verifying": {"resolved", "diagnosing", "handed_to_support"},
    "resolved": set(),
    "handed_to_support": set(),
}
MAX_MUTATIONS = 3
STATE_FA = {"open": "باز", "diagnosing": "در حال بررسی", "waiting_user": "منتظر تو", "waiting_coding_agent": "منتظر ایجنت کدنویسی",
            "verifying": "در حال بررسی نتیجه", "resolved": "حل شد", "handed_to_support": "سپرده به پشتیبانی"}


class IncidentError(RuntimeError):
    pass


@dataclass
class Incident:
    app: str
    symptom: str
    id: str = field(default_factory=lambda: "inc-" + uuid.uuid4().hex[:8])
    state: str = "open"
    opened_at: float = field(default_factory=time.time)
    history: list[tuple[float, str]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    hypotheses: list[dict] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)        # action ids that were executed
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    board: dict = field(default_factory=dict)                 # last diagnosis shown to the user
    fix: dict | None = None                                     # chosen fix: {hypothesis, option, action_id, contract}
    verify_result: dict | None = None

    def move(self, new: str) -> None:
        if new not in TRANSITIONS[self.state]:
            raise IncidentError(f"{self.state} → {new} is not allowed")
        self.history.append((time.time(), new))
        self.state = new

    @property
    def closed(self) -> bool:
        return self.state in ("resolved", "handed_to_support")

    def mutations_left(self) -> int:
        return MAX_MUTATIONS - len(self.actions)

    def record_action(self, action_id: str) -> None:
        if self.mutations_left() <= 0:
            raise IncidentError("mutation budget used up")
        self.actions.append(action_id)

    def elapsed_s(self) -> int:
        end = self.history[-1][0] if self.closed and self.history else time.time()
        return round(end - self.opened_at)
