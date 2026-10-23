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


IMAGE_TOKENS = 1600   # a ≤1024 px photo costs about this much; its base64 text says nothing about the real cost


def estimate_tokens(messages: list[dict]) -> int:
    """Rough prompt size (Persian ≈ 3 chars/token in JSON). Used to reserve budget before a call."""
    images, parts = 0, []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):   # multimodal: count images at a flat rate, measure the text
            images += sum(1 for c in content if c.get("type") == "image_url")
            content = [c for c in content if c.get("type") != "image_url"]
        parts.append({**m, "content": content})
    return len(json.dumps(parts, ensure_ascii=False)) // 3 + 1 + images * IMAGE_TOKENS


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


def _quick_thinking(model: str) -> dict:
    """No reasoning pass for these short steps: Sonnet 5.5 rejects «disabled» and takes «between_tools» instead
    (at effort high or below); Haiku 5.5 takes «disabled» (at high or below)."""
    return {"type": "between_tools"} if model.startswith("claude-sonnet-5-5") else {"type": "disabled"}


class LLMClient:
    """Calls providers in order; on timeout / 429 / 5xx / bad JSON it tries the next one."""

    def __init__(self, providers: list[Provider], models: dict[str, str], guard: SpendGuard | None = None,
                 enabled: bool = False, fake: FakeLLM | None = None, transport: httpx.BaseTransport | None = None):
        self.providers, self.models, self.guard = providers, models, guard or SpendGuard()
        self.enabled, self.fake = enabled, fake
        self._http = httpx.Client(transport=transport)
        self.log: list[dict] = []   # last calls, for /admin

    def complete(self, role: str, messages: list[dict], *, session_id: str = "-", max_tokens: int = 400,
                 json_mode: bool = False, tools: list[dict] | None = None, schema: dict | None = None) -> LLMResult:
        """`schema`: a JSON schema the reply must match (Claude's structured outputs); implies json_mode."""
        json_mode = json_mode or schema is not None
        if not self.enabled:
            if self.fake is None:
                raise LLMDisabled("LLM_ENABLED is off and no FakeLLM is configured")
            return self.fake.complete(role, messages)
        model = self.models[role]
        per_attempt = estimate_tokens(messages) + max_tokens
        errors, prose = [], ""
        for p in self.providers:
            self.guard.reserve(session_id, per_attempt)   # each attempt may be billed, so each one reserves
            t0, used = time.perf_counter(), 0
            try:
                call = self._anthropic if isinstance(p, AnthropicProvider) else self._openai
                text, tool_calls, usage, used = (call(p, model, messages, max_tokens, json_mode, tools, schema)
                                                 if isinstance(p, AnthropicProvider)
                                                 else call(p, model, messages, max_tokens, json_mode, tools))
                used = used or per_attempt
                if not (text or tool_calls):
                    raise LLMError("empty reply")
                res = LLMResult(text, tool_calls, usage, p.name, model, int((time.perf_counter() - t0) * 1000))
                if json_mode:
                    try:
                        res.json()   # bad JSON counts as a provider failure (its tokens still count)
                    except ValueError:
                        prose = text   # kept: a caller may want to know what the model said instead
                        raise
                self._note(role, res, used)
                return res
            except Exception as e:   # any malformed reply → next provider; never leak the key (it's only in headers)
                errors.append(f"{p.name}: {type(e).__name__}: {e}"[:200])
            finally:
                self.guard.settle(session_id, per_attempt, used)   # used=0 when nothing came back (not billed)
        err = LLMError("all providers failed: " + " | ".join(errors))
        err.text = prose   # the model's non-JSON reply, if that was the failure
        raise err

    def _openai(self, p: "Provider", model, messages, max_tokens, json_mode, tools):
        """An OpenAI-compatible gateway: (text, tool_calls, usage, billed tokens)."""
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"], body["tool_choice"] = tools, "auto"
        r = self._http.post(f"{p.base_url.rstrip('/')}/chat/completions", json=body, timeout=p.timeout_s,
                            headers={"Authorization": f"Bearer {p.api_key}"})
        if r.status_code == 429 or r.status_code >= 500:
            raise LLMError(f"HTTP {r.status_code}")
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        used = sum(int(usage.get(k) or 0) for k in ("prompt_tokens", "completion_tokens"))
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return msg.get("content") or "", msg.get("tool_calls") or [], usage, used

    def _anthropic(self, p: "AnthropicProvider", model, messages, max_tokens, json_mode, tools, schema=None):
        """Claude through the official SDK. The app speaks OpenAI-shaped messages; they are translated both ways.
        Thinking off (or the nearest the model allows) and low effort: every call is a short, latency-sensitive step."""
        system, msgs = to_anthropic_messages(messages)
        if json_mode:
            system = (system + "\n\n" if system else "") + "Reply with one JSON object only, no prose and no code fence."
        kw: dict = {"model": model, "max_tokens": max_tokens, "messages": msgs,
                    "thinking": _quick_thinking(model), "output_config": {"effort": "low"}}
        if model.startswith("claude-sonnet-5-5"):   # a refused request is re-run by Anthropic's default fallback model
            kw["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            kw["extra_body"] = {"fallbacks": "default"}
        if schema:   # structured outputs: the reply is JSON matching the schema, never prose
            kw["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        if system:
            kw["system"] = system
        if tools:
            kw["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                            "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                           for t in tools]
            kw["tool_choice"] = {"type": "auto"}
        resp = p.client().messages.create(**kw)
        used = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
        if resp.stop_reason == "refusal":
            raise LLMError("refusal")
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)}}
                 for b in resp.content if b.type == "tool_use"]
        return text, calls, {"prompt_tokens": resp.usage.input_tokens, "completion_tokens": resp.usage.output_tokens}, used

    def _note(self, role: str, res: LLMResult, tokens: int) -> None:
        self.log.append({"role": role, "provider": res.provider, "model": res.model, "tokens": tokens, "ms": res.ms})
        del self.log[:-200]


@dataclass
class AnthropicProvider:
    """Claude via the official `anthropic` SDK (api.anthropic.com)."""
    name: str
    api_key: str = field(repr=False)
    timeout_s: float = 30
    _client: object = field(default=None, repr=False)

    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout_s, max_retries=1)
        return self._client


def to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-shaped chat (system/user/assistant+tool_calls/tool, image_url data URLs) → (system, Claude messages)."""
    system, out = [], []
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if role == "system":
            system.append(content if isinstance(content, str) else "")
        elif role == "tool":
            out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                                                     "content": content or ""}]})
        elif role == "assistant" and m.get("tool_calls"):
            blocks = [{"type": "text", "text": content}] if content else []
            for c in m["tool_calls"]:
                fn = c.get("function", {})
                blocks.append({"type": "tool_use", "id": c.get("id", ""), "name": fn.get("name", ""),
                               "input": json.loads(fn.get("arguments") or "{}")})
            out.append({"role": "assistant", "content": blocks})
        elif isinstance(content, list):
            blocks = []
            for part in content:
                if part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    head, _, data = url.partition(",")
                    blocks.append({"type": "image", "source": {"type": "base64", "data": data,
                                   "media_type": head.removeprefix("data:").removesuffix(";base64")}})
                elif part.get("type") == "text":
                    blocks.append({"type": "text", "text": part.get("text", "")})
            out.append({"role": role, "content": blocks})
        elif content:   # Claude rejects empty text blocks
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":   # the conversation must open with the user (trimmed history may not)
        out.pop(0)
    return "\n\n".join(s for s in system if s), out


SONNET = "claude-sonnet-5-5"   # every role runs on Claude Sonnet 5.5; MODEL_* env can still override


def from_env(fake: FakeLLM | None = None) -> LLMClient:
    """Providers and models from env. Keys come from env only; never logged.
    With ANTHROPIC_API_KEY set, Claude is the only provider: no gateway fallback, so no other credit is ever spent."""
    providers: list = []
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        providers.append(AnthropicProvider("anthropic", key))
    else:
        for prefix in ("LLM_PRIMARY", "LLM_FALLBACK"):
            base, key = os.environ.get(f"{prefix}_BASE_URL"), os.environ.get(f"{prefix}_API_KEY")
            if base and key:
                providers.append(Provider(os.environ.get(f"{prefix}_NAME", prefix.lower()), base, key))
    models = {
        "router": os.environ.get("MODEL_ROUTER", SONNET),
        "chat": os.environ.get("MODEL_CHAT", SONNET),
        "call": os.environ.get("MODEL_CALL", SONNET),
        "vision": os.environ.get("MODEL_VISION", SONNET),
        "judge": os.environ.get("MODEL_JUDGE", SONNET),
        "ui": os.environ.get("MODEL_UI", SONNET),
    }
    guard = SpendGuard(int(os.environ.get("LLM_DAILY_TOKENS", "200000")),
                       int(os.environ.get("LLM_SESSION_TOKENS", "40000")))
    return LLMClient(providers, models, guard, enabled=os.environ.get("LLM_ENABLED") == "1", fake=fake)
