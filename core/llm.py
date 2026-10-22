"""OpenAI-compatible LLM client with provider failover, a hard spend guard and a scripted FakeLLM.

Real calls are OFF unless `LLM_ENABLED=1` (user rule: no AI or credit use without permission). With it off, only
FakeLLM answers, so development, tests and CI never spend credit.
"""

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import httpx


class LLMError(RuntimeError):
    pass


class LLMDisabled(LLMError):
    """A real provider was asked for while LLM_ENABLED is not 1."""


class BudgetExceeded(LLMError):
    pass


@dataclass
class LLMResult:
    text: str
    tool_calls: list[dict]
    usage: dict            # prompt_tokens, completion_tokens
    provider: str
    model: str
    ms: int

    def json(self) -> dict:
        """Parse a JSON-mode answer; tolerates ```json fences some models add."""
        s = self.text.strip()
        if s.startswith("```"):
            s = s.strip("`").removeprefix("json").strip()
        return json.loads(s)


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    timeout_s: float = 30


def estimate_tokens(messages: list[dict]) -> int:
    """Rough prompt size (Persian ≈ 3 chars/token in JSON). Used to reserve budget before a call."""
    return len(json.dumps(messages, ensure_ascii=False)) // 3 + 1


@dataclass
class SpendGuard:
    """Token caps per session and per day. A call reserves prompt + max_tokens up front (atomically, so concurrent
    calls can't overshoot), then settles to the real usage; billed-but-failed replies still count."""
    daily_tokens: int = 200_000
    session_tokens: int = 20_000
    _day: date = field(default_factory=date.today)
    _used_today: int = 0
    _by_session: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reserve(self, session_id: str, tokens: int) -> None:
        with self._lock:
            self._roll()
            if self._used_today + tokens > self.daily_tokens:
                raise BudgetExceeded("daily token cap reached")
            if self._by_session.get(session_id, 0) + tokens > self.session_tokens:
                raise BudgetExceeded("session token cap reached")
            self._add(session_id, tokens)

    def settle(self, session_id: str, reserved: int, actual: int) -> None:
        with self._lock:
            self._add(session_id, actual - reserved)

    def used_today(self) -> int:
        return self._used_today

    def _add(self, session_id: str, tokens: int) -> None:
        self._used_today += tokens
        self._by_session[session_id] = self._by_session.get(session_id, 0) + tokens
        if len(self._by_session) > 10_000:   # bound memory; the daily cap still holds for everyone
            self._by_session = dict(list(self._by_session.items())[-5_000:])

    def _roll(self) -> None:
        if date.today() != self._day:
            self._day, self._used_today, self._by_session = date.today(), 0, {}


Script = Callable[[str, list[dict]], dict | str]   # (role, messages) -> JSON dict or plain text


class FakeLLM:
    """Scripted stand-in for development and tests. `script(role, messages)` returns the answer."""

    name = "fake"

    def __init__(self, script: Script):
        self.script = script
        self.calls: list[tuple[str, list[dict]]] = []

    def complete(self, role: str, messages: list[dict], **_) -> LLMResult:
        self.calls.append((role, messages))
        out = self.script(role, messages)
        if isinstance(out, dict) and "tool_calls" in out:
            return LLMResult("", out["tool_calls"], {"prompt_tokens": 0, "completion_tokens": 0}, "fake", role, 0)
        text = json.dumps(out, ensure_ascii=False) if isinstance(out, dict) else out
        return LLMResult(text, [], {"prompt_tokens": 0, "completion_tokens": 0}, "fake", role, 0)


class LLMClient:
    """Calls providers in order; on timeout / 429 / 5xx / bad JSON it tries the next one."""

    def __init__(self, providers: list[Provider], models: dict[str, str], guard: SpendGuard | None = None,
                 enabled: bool = False, fake: FakeLLM | None = None, transport: httpx.BaseTransport | None = None):
        self.providers, self.models, self.guard = providers, models, guard or SpendGuard()
        self.enabled, self.fake = enabled, fake
        self._http = httpx.Client(transport=transport)
        self.log: list[dict] = []   # last calls, for /admin

    def complete(self, role: str, messages: list[dict], *, session_id: str = "-", max_tokens: int = 400,
                 json_mode: bool = False, tools: list[dict] | None = None) -> LLMResult:
        if not self.enabled:
            if self.fake is None:
                raise LLMDisabled("LLM_ENABLED is off and no FakeLLM is configured")
            return self.fake.complete(role, messages)
        model = self.models[role]
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"], body["tool_choice"] = tools, "auto"
        per_attempt = estimate_tokens(messages) + max_tokens
        errors = []
        for p in self.providers:
            self.guard.reserve(session_id, per_attempt)   # each attempt may be billed, so each one reserves
            t0, used = time.perf_counter(), 0
            try:
                r = self._http.post(f"{p.base_url.rstrip('/')}/chat/completions", json=body, timeout=p.timeout_s,
                                    headers={"Authorization": f"Bearer {p.api_key}"})
                if r.status_code == 429 or r.status_code >= 500:
                    raise LLMError(f"HTTP {r.status_code}")
                r.raise_for_status()
                data = r.json()
                usage = data.get("usage") or {}
                used = sum(int(usage.get(k) or 0) for k in ("prompt_tokens", "completion_tokens")) or per_attempt
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                if not (msg.get("content") or msg.get("tool_calls")):
                    raise LLMError("empty reply")
                res = LLMResult(msg.get("content") or "", msg.get("tool_calls") or [], usage,
                                p.name, model, int((time.perf_counter() - t0) * 1000))
                if json_mode:
                    res.json()   # bad JSON counts as a provider failure (its tokens still count)
                self._note(role, res, used)
                return res
            except Exception as e:   # any malformed reply → next provider; never leak the key (it's only in headers)
                errors.append(f"{p.name}: {type(e).__name__}: {e}"[:200])
            finally:
                self.guard.settle(session_id, per_attempt, used)   # used=0 when nothing came back (not billed)
        raise LLMError("all providers failed: " + " | ".join(errors))

    def _note(self, role: str, res: LLMResult, tokens: int) -> None:
        self.log.append({"role": role, "provider": res.provider, "model": res.model, "tokens": tokens, "ms": res.ms})
        del self.log[:-200]


HAIKU = "anthropic/claude-haiku-5.5"   # every role runs on Claude Haiku (BoxAPI model id); MODEL_* env can still override


def from_env(fake: FakeLLM | None = None) -> LLMClient:
    """Providers and models from env. Keys come from env only; never logged."""
    providers = []
    for prefix in ("LLM_PRIMARY", "LLM_FALLBACK"):
        base, key = os.environ.get(f"{prefix}_BASE_URL"), os.environ.get(f"{prefix}_API_KEY")
        if base and key:
            providers.append(Provider(os.environ.get(f"{prefix}_NAME", prefix.lower()), base, key))
    models = {
        "router": os.environ.get("MODEL_ROUTER", HAIKU),
        "chat": os.environ.get("MODEL_CHAT", HAIKU),
        "call": os.environ.get("MODEL_CALL", HAIKU),
    }
    guard = SpendGuard(int(os.environ.get("LLM_DAILY_TOKENS", "200000")),
                       int(os.environ.get("LLM_SESSION_TOKENS", "20000")))
    return LLMClient(providers, models, guard, enabled=os.environ.get("LLM_ENABLED") == "1", fake=fake)
