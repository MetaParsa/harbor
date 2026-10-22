"""L2 safety tests (brainstorm §8): approvals enforced in code, single-use, args-bound, redacted, budgeted.
All on REPLAY fixtures — no network, no credit."""

import asyncio
import json
import time

import pytest

from liara.agent import policy as policy_mod
from liara.agent.actions import TOOLS
from liara.agent.budget import Budget, Caps
from liara.agent.incident import Incident
from liara.agent.policy import ActionLog, Policy, Session, args_hash
from liara.agent.redact import MASK, Redactor
from liara.liara_api import LiaraClient, ReplayTransport

pytestmark = pytest.mark.anyio
APP = "l0-dyar-6b46"
TOKEN = "tok-SEEDED-abcdef123456"
SECRET = "sk_live_SEEDED_9f8e7d6c"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("LIARA_AGENT_KILL", raising=False)
    transport = ReplayTransport("d_fixed")
    client = LiaraClient(TOKEN, team_id="t1", transport=transport)
    s = Session("sess-1", client, Redactor([TOKEN]))
    q: asyncio.Queue = asyncio.Queue()
    s.subscribers.append(q)
    pol = Policy(Budget(tmp_path, Caps(deploys_per_day=2, apps_per_day=1, writes_per_session_hour=50)),
                 ActionLog(tmp_path / "actions.jsonl"))
    return pol, s, transport, q, tmp_path


def writes(transport):
    return [r for r in transport.requests if r.method != "GET"]


async def two_taps(pol, s, act):
    assert (await pol.approve(s, act.id, act.hash))["status"] == "needs_second_tap"
    act.armed_at -= 1
    return await pol.approve(s, act.id, act.hash, sure=True)


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_llm_cannot_execute_t2(env):
    pol, s, transport, q, _ = env
    res = await pol.call(s, "restart", {"app": APP}, caller="llm", why="RAM پر شده")
    assert res["status"] == "waiting_for_user" and res["tier"] == 2
    assert writes(transport) == []                           # nothing sent to Liara
    events = drain(q)
    assert events[0][0] == "action_proposed" and events[0][1]["card"]["title"]
    # The only executing path is approve(), which only the web route calls; no tool for the agent approves anything.
    assert not any("approve" in name or "confirm" in name for name in TOOLS)


async def test_t0_runs_and_t3_needs_second_tap(env):
    pol, s, transport, _, _ = env
    assert (await pol.call(s, "read_releases", {"app": APP}, caller="llm"))["status"] == "done"
    res = await pol.call(s, "resize", {"app": APP, "plan_id": "medium-g2"}, caller="llm")
    act = s.actions[res["pending_action_id"]]
    assert "۷۴۹٬۹۹۵ تومان ← ۱٬۲۹۹٬۹۹۶ تومان" in act.card.cost         # monthly price before → after, Persian digits
    # one request with sure=true is not two taps (a script or browser agent could send it)
    assert (await pol.approve(s, act.id, act.hash, sure=True))["status"] == "needs_second_tap"
    first = await pol.approve(s, act.id, act.hash)
    assert first["status"] == "needs_second_tap" and act.status == "pending" and writes(transport) == []
    assert (await pol.approve(s, act.id, act.hash, sure=True))["status"] == "needs_second_tap"   # too fast
    act.armed_at -= 1                                                                          # a human pause
    done = await pol.approve(s, act.id, act.hash, sure=True)
    assert done["status"] == "done"
    assert [r.url.path for r in writes(transport)] == [f"/v1/projects/{APP}/resize"]


async def test_action_id_single_use_and_expiry(env, monkeypatch):
    pol, s, transport, _, _ = env
    act = s.actions[(await pol.call(s, "restart", {"app": APP}, caller="mcp"))["pending_action_id"]]
    assert (await pol.approve(s, act.id, act.hash))["status"] == "done"
    assert (await pol.approve(s, act.id, act.hash))["error"] == "already_done"
    assert len(writes(transport)) == 1
    act2 = s.actions[(await pol.call(s, "restart", {"app": APP}, caller="mcp"))["pending_action_id"]]
    monkeypatch.setattr(policy_mod.time, "time", lambda: act2.created + policy_mod.APPROVAL_TTL_S + 1)
    assert (await pol.approve(s, act2.id, act2.hash))["status"] == "expired"
    assert len(writes(transport)) == 1


async def test_args_hash_binding(env):
    pol, s, transport, _, _ = env
    act = s.actions[(await pol.call(s, "restart", {"app": APP}, caller="llm"))["pending_action_id"]]
    other = args_hash("resize", {"app": APP, "plan_id": "pro"})
    assert (await pol.approve(s, act.id, other))["error"] == "args_mismatch"
    act.args["app"] = "someone-elses-app"                   # tampering after the card was shown
    assert (await pol.approve(s, act.id, act.hash))["error"] == "args_mismatch"
    assert writes(transport) == []


async def test_other_session_cannot_approve(env):
    pol, s, transport, _, _ = env
    act = s.actions[(await pol.call(s, "restart", {"app": APP}, caller="mcp"))["pending_action_id"]]
    intruder = Session("sess-2", LiaraClient("x", transport=transport), Redactor())
    assert (await pol.approve(intruder, act.id, act.hash))["error"] == "not_found"
    assert writes(transport) == []


async def test_stale_card_is_reproposed_not_run(env):
    pol, s, transport, q, _ = env
    act = s.actions[(await pol.call(s, "restart", {"app": APP}, caller="llm"))["pending_action_id"]]
    act.fingerprint = {**act.fingerprint, "release": "an-older-release"}   # app changed since the card was shown
    res = await pol.approve(s, act.id, act.hash)
    assert res["status"] == "stale" and res["new_action"] in s.actions and act.status == "stale"
    assert writes(transport) == []


async def test_redaction_everywhere(env):
    pol, s, transport, q, tmp = env
    res = await pol.call(s, "set_envs", {"app": APP, "changes": {"PAYMENT_API_KEY": SECRET}}, caller="llm",
                         why=f"the key {SECRET} was missing; token {TOKEN}")
    act = s.actions[res["pending_action_id"]]
    assert (await pol.approve(s, act.id, act.hash))["status"] == "done"
    sent = json.loads(writes(transport)[0].content)
    assert sent["variables"][-1] == {"key": "PAYMENT_API_KEY", "value": SECRET}   # Liara gets the real value…
    blob = (tmp / "actions.jsonl").read_text(encoding="utf-8") + json.dumps(drain(q), ensure_ascii=False) \
        + json.dumps(act.public(), ensure_ascii=False) + json.dumps(s.log, ensure_ascii=False) + repr(act)
    assert SECRET not in blob and TOKEN not in blob and MASK in blob              # …nobody else ever sees it
    assert "PAYMENT_API_KEY" in act.card.title                                    # names are fine


async def test_kill_switch_and_daily_deploy_cap(env, monkeypatch):
    pol, s, transport, _, tmp = env
    (tmp / "KILL").write_text("1")
    res = await pol.call(s, "restart", {"app": APP}, caller="llm")
    assert res["status"] == "refused" and res["reason"] == "kill_switch" and "توقف" in res["message"]
    (tmp / "KILL").unlink()
    body = {"app": APP, "source_id": "s.tar.gz", "platform": "flask", "port": 80}
    for _ in range(2):
        a = s.actions[(await pol.call(s, "deploy", body, caller="llm"))["pending_action_id"]]
        assert (await pol.approve(s, a.id, a.hash))["status"] == "done"
    third = await pol.call(s, "deploy", body, caller="llm")
    assert third["status"] == "refused" and third["reason"] == "deploys_per_day"
    assert Budget(tmp).today()["deploys"] == 2                                    # persisted across restarts


async def test_created_app_gets_three_free_builds(env):
    pol, s, transport, _, _ = env
    a = s.actions[(await pol.call(s, "create_app", {"app": "new-app", "plan_id": "small-g2", "platform": "flask"},
                                  caller="llm"))["pending_action_id"]]
    assert a.tier == 3 and "ماه" in a.card.cost and "۳ بیلد" in a.card.risk
    assert (await two_taps(pol, s, a))["status"] == "done"
    assert "new-app" in s.ctx.created_apps
    body = {"app": "new-app", "source_id": "s.tar.gz", "platform": "flask", "port": 80}
    pol.budget.caps.deploys_per_day = 10
    for _ in range(3):
        assert (await pol.call(s, "deploy", body, caller="llm"))["status"] == "done"     # T1: no card
    assert (await pol.call(s, "deploy", body, caller="llm"))["status"] == "waiting_for_user"


async def test_incident_mutation_budget(env):
    pol, s, transport, _, _ = env
    inc = Incident(APP, "سایتم کند شده")
    s.incidents[inc.id] = inc
    for _ in range(3):
        a = s.actions[(await pol.call(s, "restart", {"app": APP}, caller="llm", incident_id=inc.id))["pending_action_id"]]
        assert (await pol.approve(s, a.id, a.hash))["status"] == "done"
    a = s.actions[(await pol.call(s, "restart", {"app": APP}, caller="llm", incident_id=inc.id))["pending_action_id"]]
    res = await pol.approve(s, a.id, a.hash)
    assert res["status"] == "refused" and res["reason"] == "incident_mutation_budget" and "پشتیبانی" in res["message"]


async def test_plans_response_holds_no_personal_data(env):
    _, s, _, _, _ = env
    plans = await s.client.plans()
    assert set(vars(plans)) == {"apps", "bundles"}


async def test_parallel_approvals_cannot_overrun_incident_or_deploy_caps(env):
    pol, s, transport, _, _ = env
    inc = Incident(APP, "سایتم خطا می‌ده")
    s.incidents[inc.id] = inc
    acts = [s.actions[(await pol.call(s, "restart", {"app": APP}, caller="llm"))["pending_action_id"]] for _ in range(5)]
    assert all(a.incident_id == inc.id for a in acts)                 # resolved server-side, not by the caller
    res = await asyncio.gather(*[pol.approve(s, a.id, a.hash) for a in acts])
    assert [r["status"] for r in res].count("done") == 3 and len(writes(transport)) == 3
    assert all(a.status in ("done", "refused") for a in acts)          # nothing stuck in "executing"
    inc.move("handed_to_support")
    body = {"app": APP, "source_id": "s.tar.gz", "platform": "flask", "port": 80}
    deps = [s.actions[(await pol.call(s, "deploy", body, caller="llm"))["pending_action_id"]] for _ in range(3)]
    res = await asyncio.gather(*[pol.approve(s, a.id, a.hash) for a in deps])
    assert [r["status"] for r in res].count("done") == 2                # cap is 2/day in this fixture
    assert pol.budget.today()["deploys"] == 2


async def test_fingerprint_failure_fails_closed(env, monkeypatch):
    pol, s, transport, _, _ = env
    real = policy_mod.fingerprint

    async def broken(*a, **k):
        return None

    monkeypatch.setattr(policy_mod, "fingerprint", broken)            # Liara blip at proposal time
    act = s.actions[(await pol.call(s, "restart", {"app": APP}, caller="llm"))["pending_action_id"]]
    res = await pol.approve(s, act.id, act.hash)                      # …and still at approve time
    assert res["error"] == "verify_failed" and act.status == "pending" and writes(transport) == []
    monkeypatch.setattr(policy_mod, "fingerprint", real)              # Liara back: unverified card is re-proposed
    res = await pol.approve(s, act.id, act.hash)
    assert res["status"] == "stale" and writes(transport) == []


async def test_bad_incident_id_and_args_validation(env):
    pol, s, transport, _, _ = env
    s.incidents["inc-real"] = Incident(APP, "x", id="inc-real")
    assert (await pol.call(s, "restart", {"app": APP}, caller="llm", incident_id="inc-made-up"))["error"] == "bad_incident"
    for tool, args in [("restart", {"app": "x/../../v1/billing"}), ("restart", {}),
                       ("set_envs", {"app": APP, "changes": ["A", "B"]}),
                       ("set_envs", {"app": APP, "changes": {"A": 5}}),
                       ("set_envs", {"app": APP, "changes": {"bad key": "v"}}),
                       ("resize", {"app": APP, "plan_id": "../x"}),
                       ("deploy", {"app": APP, "source_id": "s", "platform": "rm -rf", "port": 80}),
                       ("add_domain", {"app": APP, "domain": "evil/../x"})]:
        res = await pol.call(s, tool, args, caller="llm")
        assert res["status"] == "error" and res["error"] == "bad_args", (tool, args)
    assert writes(transport) == [] and s.actions == {}


async def test_caller_cannot_change_args_after_proposal(env):
    pol, s, transport, _, _ = env
    args = {"app": APP, "changes": {"MODE": "safe"}}
    act = s.actions[(await pol.call(s, "set_envs", args, caller="llm"))["pending_action_id"]]
    args["changes"]["MODE"] = "evil"                                   # the caller still holds its dict
    assert (await pol.approve(s, act.id, act.hash))["status"] == "done"
    sent = json.loads(writes(transport)[0].content)
    assert {"key": "MODE", "value": "safe"} in sent["variables"]


async def test_unexpected_error_marks_failed_not_stuck(env, monkeypatch):
    pol, s, transport, _, _ = env
    act = s.actions[(await pol.call(s, "restart", {"app": APP}, caller="llm"))["pending_action_id"]]

    async def boom(ctx, a):
        raise ValueError("bug")

    monkeypatch.setattr(TOOLS["restart"], "run", boom)
    res = await pol.approve(s, act.id, act.hash)
    assert res["status"] == "failed" and act.status == "failed"


async def test_existing_env_values_are_redacted(tmp_path):
    import httpx

    def handler(request):
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json={"total": 1, "currentRelease": "r1", "releases": []})
        return httpx.Response(200, json={"project": {"project_id": APP, "envs": [
            {"key": "DB_URL", "value": "already-there-secret-77", "encrypted": False}]}})

    s = Session("s", LiaraClient("t", transport=httpx.MockTransport(handler)), Redactor())
    pol = Policy(Budget(tmp_path), ActionLog(tmp_path / "a.jsonl"))
    await pol.call(s, "read_app", {"app": APP}, caller="llm")
    assert "already-there-secret-77" not in s.redactor.text("crash: could not connect with already-there-secret-77")


def test_agent_port_has_no_approve():
    from liara.agent.policy import AgentPort
    port = AgentPort(Policy.__new__(Policy))
    assert not hasattr(port, "approve") and not hasattr(port, "reject")


def test_redactor_patterns():
    r = Redactor(["hunter2-secret"])
    s = r.text("DATABASE_URL=postgres://app:p4ssw0rd@db:5432/x Authorization: Bearer abcdefghijkl "
               "OPENAI_API_KEY=sk-123456789 pw hunter2-secret container ebdee53429c32f37098db3c844a8ec6aaf11c87fc6744")
    for leaked in ("p4ssw0rd", "abcdefghijkl", "sk-123456789", "hunter2-secret"):
        assert leaked not in s
    assert "ebdee53429c32f37098db3c844a8ec6a" in s                 # container ids are evidence, not secrets
    assert r.obj({"password": "x1", "nested": [{"api_key": "y2"}], "ok": "fine"}) == \
        {"password": MASK, "nested": [{"api_key": MASK}], "ok": "fine"}


def test_incident_transitions():
    inc = Incident("a", "x")
    inc.move("diagnosing")
    inc.move("waiting_coding_agent")
    inc.move("verifying")
    inc.move("resolved")
    assert inc.closed
    with pytest.raises(Exception):
        inc.move("diagnosing")
