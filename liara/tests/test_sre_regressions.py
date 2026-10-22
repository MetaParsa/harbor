"""Regression tests for the SRE engine's review findings. Crafted facts + stubs; no network, no credit."""

import asyncio
import json

import pytest

from core.llm import FakeLLM, LLMClient
from liara.agent.incident import Incident
from liara.agent.policy import Session
from liara.agent.redact import MASK, Redactor
from liara.liara_api import LabelReplay, LiaraClient
from liara.liara_api.models import App, Event, LogLine, MetricSeries, Plan, Release
from liara.sre.collect import Facts, build_ledger, parse_request
from liara.sre.engine import troubleshoot, verify_fix
from liara.sre.llm_jobs import rule_symptom, validate_explain, validate_symptom
from liara.sre.patterns import diagnose

pytestmark = pytest.mark.anyio
APP = "dastyar-demo-shop"
NOW = 1_792_666_000


@pytest.fixture
def anyio_backend():
    return "asyncio"


def rel(tag, rid, minutes_ago, kind="DEPLOY"):
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(NOW - minutes_ago * 60, timezone.utc).isoformat().replace("+00:00", "Z")
    return Release(rid, tag, kind, "READY", ts, None, True, None)


def log(release, text, stream="stdout", secs_ago=60):
    return LogLine(int((NOW - secs_ago) * 1e9), stream, text, release)


def healthy_after_rollback_facts() -> Facts:
    f = Facts(APP, NOW)
    f.app = App(APP, "p", "node", "ACTIVE", 1, "small-g2", "basic", 659.72, 381.94, True, [])
    f.plan = Plan("small-g2", 0.5, 0.5, 659.72, True, "iran")
    f.releases = [rel("v6", "r6", 5), rel("v5", "r5", 12), rel("v4", "r4", 20)]
    f.events = ([Event("old", s, NOW - 600 + i, "r5", "v5") for i, s in
                 enumerate(["start", "oom", "die", "start", "oom", "die", "start", "oom", "die", "start"])]
                + [Event("new", "start", NOW - 290, "r6", "v6")])
    f.logs = [log("r4", "GET /checkout 500 1.0ms", secs_ago=900), log("r6", "GET / 200 1.0ms"),
              log("r6", "GET /checkout?id=2 200 1.2ms")]
    f.memory = [MetricSeries("old", [(NOW - 600 + 30 * i, 100e6 * i) for i in range(1, 8)]),
                MetricSeries("new", [(NOW - 280 + 30 * i, 24e6) for i in range(8)])]
    f.cpu = [MetricSeries("old", [(NOW - 600, 0.49)]), MetricSeries("new", [(NOW - 280, 0.02), (NOW - 250, 0.02)])]
    f.probes = {"/": [{"status": 200, "ms": 120}] * 3}
    return f


def test_history_from_older_releases_is_not_counted_as_current():
    f = healthy_after_rollback_facts()
    ledger = build_ledger(f)
    d = diagnose(f, ledger, {"kind": "slow", "route": None})
    assert d.hypotheses[0].id == "H0", [h.id for h in d.hypotheses]     # not P5 crash loop, not P4, not P10
    assert not any(h.id in ("P3", "P4", "P5") for h in d.hypotheses)
    ev = next(e for e in ledger if e.kind == "events")
    assert ev.value["restarts"] == 0 and ev.value["older_ooms"] == 3
    assert next(e for e in ledger if e.kind == "cpu").value["cores"] == 0.02   # newest container, not the dead one


def test_log_parsing_edge_cases():
    assert parse_request("GET /checkout?id=1 500 12ms") == ("/checkout", 500, 12.0)
    assert parse_request('GET 502 185.1.1.1 "/x?y=1" "-" "curl"') == ("/x", 502, None)
    f = Facts(APP, NOW)
    f.logs = [log("r", 'Traceback (most recent call last):', "stderr"),
              log("r", '  File "/app/a.py", line 10, in f', "stderr"),
              log("r", '  File "/app/b.py", line 20, in g', "stderr"),
              log("r", "ValueError: bad", "stderr"),
              log("r", "Error: connect ECONNREFUSED 127.0.0.1:5432", "stderr"),
              log("r", "    at /app/node_modules/pg/lib/client.js:132:14", "stderr"),
              log("r", "    at query (/app/db.js:7:3)", "stderr")]
    tbs = f.tracebacks()
    assert [(t["error"], t["file"], t["line"]) for t in tbs] == [("ValueError", "/app/b.py", 20), ("Error", "/app/db.js", 7)]
    assert rule_symptom("https://x.liara.run/checkout خطا می‌ده")["route"] == "/checkout"
    assert validate_symptom({"kind": "error", "route": "//evil.example/x"})["route"] is None


@pytest.mark.parametrize("bad", [
    {"order": 5, "headline": "یک جمله‌ی به اندازه‌ی کافی بلند"},
    {"order": ["P1"], "headline": "یک جمله‌ی به اندازه‌ی کافی بلند", "added": {"title": "x" * 10, "supports": [{}], "confidence": 1}},
    {"order": ["P1"], "headline": "یک جمله‌ی به اندازه‌ی کافی بلند", "added": {"title": "xxxxxxxx", "supports": ["E1", "E2"], "confidence": "high"}},
])
def test_malformed_model_json_never_crashes(bad):
    from liara.sre.patterns import Diagnosis, Hypothesis
    from liara.sre.collect import Evidence
    diag = Diagnosis([Hypothesis("P1", "t", 0.8, [])], [], [])
    ledger = [Evidence("E1", "probe", "x"), Evidence("E2", "probe", "y")]
    out = validate_explain(bad, diag, ledger)
    assert out is None or (out["order"] == ["P1"] and (out["added"] is None or out["added"]["confidence"] <= 0.6))
    assert validate_symptom({"kind": ["error"]}) is None


def harness(label):
    t = LabelReplay(label)
    summ = t.summary()

    async def prober(url, n):
        route = url.split(".liara.run", 1)[1]
        for key, val in summ.get("probes", {}).items():
            if key.split("?")[0] == route.split("?")[0]:
                return val
        return [{"status": 200, "ms": 300}] * 3
    return t, prober, summ.get("at")


async def test_concurrent_troubleshoot_and_double_verify_never_raise():
    t, prober, now = harness("v2_regression")
    s = Session("s", LiaraClient("tok", transport=t), Redactor())
    a, b = await asyncio.gather(troubleshoot(s, APP, "پرداخت خطا می‌ده", prober=prober, now=now),
                                troubleshoot(s, APP, "پرداخت خطا می‌ده", prober=prober, now=now))
    assert a["incident"] == b["incident"]
    inc = s.incidents[a["incident"]]
    inc.move("verifying")
    inc.move("resolved")
    inc.verify_result = {"ok": True, "checks": []}
    res = await verify_fix(s, inc.id)                       # a retry on a closed incident returns the stored answer
    assert res["status"] == "resolved" and res["ok"]


async def test_prompt_and_report_are_redacted():
    t, prober, now = harness("v2_regression")
    key = f"GET /v2/projects/{APP}/logs"
    t.by_req[key][0]["body"]["data"][0]["values"].append(
        [str(int((now - 5) * 1e9)), json.dumps({"type": "stderr", "_entry": "Error: connect to postgres://app:S3cretPW@db failed"})])
    s = Session("s", LiaraClient("tok", transport=t), Redactor(["tok-SECRET-xyz"]))
    seen = []

    def script(role, msgs):
        seen.append(msgs[1]["content"])
        return {"kind": "error", "route": "/checkout"} if role == "router" else "not json"

    llm = LLMClient([], {"router": "m", "chat": "m"}, enabled=False, fake=FakeLLM(script))
    board = await troubleshoot(s, APP, "پرداخت خطا می‌ده tok-SECRET-xyz", llm=llm, prober=prober, now=now)
    blob = " ".join(seen) + json.dumps(board, ensure_ascii=False)
    assert "S3cretPW" not in blob and "tok-SECRET-xyz" not in blob and MASK in blob


class StubClient:
    """Enough of LiaraClient for verify(): scripted events / releases / logs."""

    def __init__(self, events, logs, current="r-new"):
        self._events, self._logs, self._current, self.calls = events, logs, current, []

    async def events(self, app, since):
        return self._events

    async def releases(self, app, count):
        from liara.liara_api.models import Releases
        return Releases(1, self._current, [])

    async def app_logs(self, app, since_s=0, **_):
        return self._logs

    async def metrics(self, app, kind, since):
        return []


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


async def test_verify_ignores_old_container_restart_and_old_release_lines():
    from liara.sre.verify import verify
    executed = NOW - 100
    events = [Event("old", "start", NOW - 50, "r-old", "v5")]            # OOM restart of the OLD container
    logs = [LogLine(int((NOW - 30) * 1e9), "stdout", "GET /checkout 500 1ms", "r-old"),     # old container's last words
            LogLine(int((NOW - 20) * 1e9), "stdout", "GET /checkout 200 1ms", "r-new")]
    client = StubClient(events, logs)

    async def ok(url, n):
        return [{"status": 200, "ms": 100}] * n

    clock = Clock(NOW)
    res = await verify(client, APP, {"route": "/checkout", "probes": 3, "max_status": 399, "new_container": True},
                       {"containers": ["old"]}, executed, prober=ok, sleep=clock.sleep, clock=clock,
                       container_wait_s=30, settle_s=0)
    checks = {c["name"]: c["ok"] for c in res["checks"]}
    assert checks["new_container"] is False                               # same container id → not the new one
    client._events = events + [Event("new", "start", NOW - 40, "r-new", "v6")]
    res = await verify(client, APP, {"route": "/checkout", "probes": 3, "max_status": 399, "new_container": True},
                       {"containers": ["old"]}, executed, prober=ok, sleep=clock.sleep, clock=clock, settle_s=0)
    assert {c["name"]: c["ok"] for c in res["checks"]} == {"new_container": True, "probe": True, "logs": True}


async def test_verify_follows_a_reproposed_stale_card():
    t, prober, now = harness("v2_regression")
    s = Session("s", LiaraClient("tok", transport=t), Redactor())
    inc = Incident(APP, "x")
    inc.move("diagnosing")
    inc.move("waiting_user")
    inc.fix = {"hypothesis": "P2", "option": 0, "tool": "set_envs", "action_id": "act-old", "started_at": None,
               "contract": {"route": "/", "probes": 2, "max_status": 399, "new_container": False}}
    s.incidents[inc.id] = inc
    from liara.agent.actions import Card
    from liara.agent.policy import PendingAction
    old = PendingAction("set_envs", {}, 2, Card("t", "r"), "s", "user", incident_id=inc.id, id="act-old")
    old.status = "stale"
    new = PendingAction("set_envs", {}, 2, Card("t", "r"), "s", "user", incident_id=inc.id, id="act-new")
    new.status, new.executed_at = "done", now - 300
    s.actions = {old.id: old, new.id: new}
    clock = Clock(now)

    async def ok(url, n):
        return [{"status": 200, "ms": 100}] * n

    res = await verify_fix(s, inc.id, prober=ok, sleep=clock.sleep, clock=clock, settle_s=0)
    assert res.get("error") != "fix_not_executed" and "checks" in res
