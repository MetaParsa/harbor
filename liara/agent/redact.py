"""One redaction pass for everything that leaves the process: action log, SSE events, LLM prompts, tickets.

Two layers:
1. Known secrets: the session's Liara token and every env value we have seen are registered per session and
   replaced wherever they appear (Liara returns env values in plain text, even encrypted ones — seen live).
2. Patterns for secrets we were never told about: bearer tokens, key=value pairs with secret-ish names,
   passwords inside connection URLs, JWTs, private-key blocks.
Container ids, release ids and hashes are evidence, not secrets, so long hex strings are deliberately kept.
"""

import re
from collections.abc import Iterable

MASK = "«پنهان»"
SECRET_NAME = re.compile(r"(token|secret|passw(or)?d|pwd|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth)",
                         re.I)
PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + MASK),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*)"
                r"(\s*[=:]\s*)(['\"]?)[^\s'\"&,;]{4,}\3"), lambda m: f"{m.group(1)}{m.group(2)}{MASK}"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/\s@]+):([^@\s/]+)@"), lambda m: f"{m.group(1)}:{MASK}@"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
]
MIN_SECRET_LEN = 4   # shorter registered values (e.g. "1", "true") would mangle normal text


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets: set[str] = set()
        self.add(*secrets)

    def add(self, *values: str | None) -> None:
        for v in values:
            if isinstance(v, str) and len(v) >= MIN_SECRET_LEN:
                self._secrets.add(v)

    def text(self, s: str) -> str:
        for v in sorted(self._secrets, key=len, reverse=True):   # longest first: a secret may contain another
            if v in s:
                s = s.replace(v, MASK)
        for pattern, repl in PATTERNS:
            s = pattern.sub(repl, s)
        return s

    def obj(self, o, key: str = ""):
        """Deep copy with secrets masked. Values under secret-ish keys are masked whatever they look like."""
        if isinstance(o, dict):
            return {k: self.obj(v, k) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v, key) for v in o]
        if isinstance(o, str):
            if key and SECRET_NAME.search(key) and o:
                return MASK
            return self.text(o)
        return o
