"""Event emitter (SSE + persisted log) and a bounded tool-calling loop.

Products mostly route turns with a step machine (code decides the tool); the tool loop is only for open
questions about results already shown (spec §0.2 R10).
"""

import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from core.llm import LLMClient, LLMError

Sink = Callable[[str, str, dict], None]   # (session_id, event, data) -> persist


_MD_MARKS = re.compile(r"\*\*|__|^#{1,6}\s*", re.M)
_MD_BULLET = re.compile(r"^\s*[-*]\s+", re.M)


def plain_text(text: str) -> str:
    """Chat bubbles show plain text: drop Markdown bold/heading marks a model may add, keep bullets as «•»."""
    return _MD_BULLET.sub("• ", _MD_MARKS.sub("", text)).strip()


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@dataclass
class Emitter:
    """Collects a turn's events in order; each is persisted (if a sink is given) and streamed as SSE."""
    session_id: str
    sink: Sink | None = None
    events: list[tuple[str, dict]] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)
    llm: object = None          # the turn's LLM client, for handlers that may need it (no globals across requests)
    paused: float = 0.0         # seconds spent on deliberate pacing (call lines); excluded from `ms`

    def emit(self, event: str, data: dict) -> str:
        data = {**data, "ms": int((time.perf_counter() - self.started - self.paused) * 1000)}
        self.events.append((event, data))
        if self.sink:
            self.sink(self.session_id, event, data)
        return sse(event, data)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                     # JSON schema
    fn: Callable[..., dict]

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def tool_loop(llm: LLMClient, messages: list[dict], tools: list[Tool], em: Emitter, *,
              max_steps: int = 6, max_tokens: int = 500) -> Iterator[str]:
    """Run model ↔ tools until the model answers in text or `max_steps` is hit. Yields SSE strings.

    Tool output goes back to the model as data; the final text is emitted as one `message` event.
    """
    by_name = {t.name: t for t in tools}
    msgs = list(messages)
    for step in range(max_steps):
        try:
            res = llm.complete("chat", msgs, session_id=em.session_id, max_tokens=max_tokens,
                               tools=[t.schema() for t in tools])
        except LLMError:   # providers down or budget spent: say so plainly, don't crash the turn
            yield em.emit("message", {"text": "الان نمی‌تونم به این سؤال جواب بدم؛ از کارت‌ها کمک بگیر یا کمی بعد بپرس."})
            return
        if not res.tool_calls:
            yield em.emit("message", {"text": plain_text(res.text) or "جوابی پیدا نکردم؛ سؤال رو یه جور دیگه بپرس."})
            return
        msgs.append({"role": "assistant", "content": res.text or None, "tool_calls": res.tool_calls})
        for call in res.tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            yield em.emit("step", {"tool": name, "n": step + 1})
            tool = by_name.get(name)
            try:
                args = json.loads(fn.get("arguments") or "{}")
                out = tool.fn(**args) if tool else {"error": f"unknown tool {name}"}
            except Exception as e:   # a bad tool call must not crash the turn; the model sees the error
                out = {"error": f"{type(e).__name__}: {e}"[:300]}
            msgs.append({"role": "tool", "tool_call_id": call.get("id", name), "content": json.dumps(out, ensure_ascii=False)})
    yield em.emit("message", {"text": "این سؤال بیشتر از توانم پیچیده شد؛ کوتاه‌تر بپرس."})
