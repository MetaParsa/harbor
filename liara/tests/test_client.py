"""L1 Liara client against the live L0 recordings (2026-10-22) in REPLAY mode. No network, no credit."""

import json

import httpx
import pytest

from liara.liara_api import LiaraClient, LiaraError, ReplayTransport
from liara.liara_api.errors import error_kind
from liara.liara_api.models import restart_count

pytestmark = pytest.mark.anyio
TOKEN = "tok-SECRET-123"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def client(scenario="d_fixed", **kw) -> tuple[LiaraClient, ReplayTransport]:
    t = ReplayTransport(scenario, **kw)
    return LiaraClient(TOKEN, team_id="team-1", transport=t), t


async def test_plans_prices_and_bundle_limits():
    c, _ = client()
    plans = await c.plans()
    cheap = plans.cheapest_iran()
    assert cheap.id == "small-g2" and cheap.ram_gb == 0.5 and cheap.price_hour == 659.72
    basic = plans.bundle("small-g2", "basic")
    assert basic.max_deploys_per_day == 20 and basic.build_timeout_min == 5 and basic.rollback is True
    hour, month = plans.price("small-g2", "basic")
    assert hour == 1041.66 and month == round(1041.66 * 720)
    await c.plans()
    assert [x.path for x in c.calls] == ["/v1/me"]          # cached


async def test_team_id_sent_on_every_call_and_token_never_recorded():
    c, t = client()
    await c.plans()
    await c.get_app("l0-dyar-6b46")
    assert all(r.url.params.get("teamID") == "team-1" for r in t.requests)
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in t.requests)
    assert TOKEN not in repr(c) and TOKEN not in json.dumps([vars(x) for x in c.calls])
    assert [x.path for x in c.calls] == ["/v1/me", "/v1/projects/l0-dyar-6b46"]


async def test_balance_is_advisory_zero():
    c, _ = client()
    assert await c.balance() == 0  # live: 0 although ~2M gift credit was usable


async def test_app_and_releases():
    c, _ = client()
    app = await c.get_app("l0-dyar-6b46")
    assert (app.platform, app.status, app.scale, app.plan_id, app.bundle_id) == ("flask", "ACTIVE", 1, "small-g2", "basic")
    assert app.total_hourly_price == pytest.approx(1041.66) and app.url == "https://l0-dyar-6b46.liara.run"
    rels = await c.releases("l0-dyar-6b46")
    assert rels.total == 3 and rels.current_id == rels.items[0].id
    assert all(r.state == "READY" and r.source_available for r in rels.items)


async def test_crash_loop_visible_in_logs_and_events_not_release_state():
    c, _ = client("d_raw2")
    rels = await c.releases("l0-dyar-6b46")
    assert rels.items[0].state == "READY"            # Liara says READY…
    logs = await c.app_logs("l0-dyar-6b46")
    assert any("No module named 'pkg_resources'" in ln.text for ln in logs)   # …logs say it crashes
    assert all(ln.stream in ("stdout", "stderr") for ln in logs)
    assert logs == sorted(logs, key=lambda x: x.ts_ns)


async def test_events_restart_counter():
    c, _ = client("d_raw2")
    ev = await c.events("l0-dyar-6b46")
    assert ev and ev == sorted(ev, key=lambda e: e.time)
    assert {e.status for e in ev} <= {"create", "start", "kill", "stop", "die", "destroy", "oom"}
    crashed = restart_count(ev)
    assert crashed >= 4                                    # live crash loop: same container restarted again and again
    c, _ = client("d_fixed")
    ev = await c.events("l0-dyar-6b46")
    assert restart_count(ev, "v3") == 0                    # healthy release: one start, replacing v2 is not a restart


def test_demo_shop_oom_recording():
    """demo-shop v3 on live Liara (2026-10-22): the leak ends in oom → die → start on the same container."""
    from pathlib import Path

    from liara.liara_api.models import oom_count, parse_events

    d = Path(__file__).resolve().parents[1] / "fixtures" / "demo" / "v3_oom"
    body = json.loads(next(d.glob("*_events.json")).read_text(encoding="utf-8"))["body"]
    ev = parse_events(body)
    assert oom_count(ev, "v5") >= 1 and restart_count(ev, "v5") >= 1
    statuses = [e.status for e in ev if e.tag == "v5"]
    i = statuses.index("oom")
    assert statuses[i + 1:i + 3] == ["die", "start"]


async def test_usage_now_picks_the_current_container():
    # The live summary listed 3 containers at the same timestamp (two from older releases, 4.1 and 4.2 MB).
    # The current v3 container is the series that started last: 1,552,384 bytes.
    c, _ = client()
    u = await c.usage_now("l0-dyar-6b46")
    assert u["memory_bytes"] == 1552384 and u["container"].endswith("-DEPLOY")
    cpu = await c.metrics("l0-dyar-6b46", "cpu")
    assert 0 <= cpu[0].last < 1                            # fraction of a core
    with pytest.raises(ValueError):
        await c.metrics("l0-dyar-6b46", "network-receive")


async def test_metrics_relative_since_maps_to_precondition():
    # Live: since=1800 (a look-back) → 428 "maximum allowed value for since is 7 days". Our client sends absolute.
    t = ReplayTransport()
    async with httpx.AsyncClient(base_url="https://api.liara.ir", transport=t) as raw:
        r = await raw.get("/v1/projects/x/metrics/memory", params={"since": 1800})
    assert r.status_code == 428
    assert error_kind(r.status_code, r.json()) == "precondition"


async def test_rollback_endpoint_missing_and_redeploy_works():
    c, _ = client()
    with pytest.raises(LiaraError) as e:
        await c._req("POST", "/v2/projects/l0-dyar-6b46/releases/abc/rollback", retry=False)
    assert e.value.kind == "not_found" and e.value.fa
    body = c.release_body("src.tar.gz", "flask", 80, platform_config={"appModule": "main:app"})
    assert body["envs"] == {} and body["build"]["location"] == "iran"
    assert await c.redeploy("l0-dyar-6b46", body) == "6ad9b1234d2d3476a02ceb9f"


async def test_deploy_flow_in_replay():
    c, _ = client()
    sid = await c.upload_source("l0-dyar-6b46", b"x")
    assert sid.endswith(".tar.gz")
    rid = await c.create_release("l0-dyar-6b46", c.release_body(sid, "flask", 80))
    lines = []
    res = await c.wait_for_release(rid, on_line=lambda text, stream: lines.append(text), poll_s=0)
    assert res.state == "READY" and res.failure_line is None and lines


async def test_set_envs_read_modify_write():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"project": {"project_id": "a", "envs": [
                {"key": "KEEP", "value": "1", "encrypted": False},
                {"key": "SECRET", "value": "s3", "encrypted": True},
                {"key": "DROP", "value": "x", "encrypted": False}]}})
        sent.update(json.loads(request.content))
        return httpx.Response(201, json={"message": "success"})

    c = LiaraClient(TOKEN, transport=httpx.MockTransport(handler))
    keys = await c.set_envs("a", {"NEW": "v", "DROP": None}, encrypted={"NEW"})
    assert keys == ["KEEP", "SECRET", "NEW"]
    assert sent["variables"] == [{"key": "KEEP", "value": "1"}, {"key": "SECRET", "value": "s3", "encrypted": True},
                                 {"key": "NEW", "value": "v", "encrypted": True}]
    assert "s3" not in repr(await c.get_app("a"))       # values never in reprs


async def test_set_envs_keeps_existing_encryption_when_value_changes():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"project": {"envs": [{"key": "SECRET", "value": "old", "encrypted": True},
                                                                  {"key": "EMPTY_OK", "value": "", "encrypted": True}]}})
        sent.update(json.loads(request.content))
        return httpx.Response(201, json={"message": "success"})

    c = LiaraClient(TOKEN, transport=httpx.MockTransport(handler))
    await c.set_envs("a", {"SECRET": "new"})
    assert {"key": "SECRET", "value": "new", "encrypted": True} in sent["variables"]
    assert {"key": "EMPTY_OK", "value": "", "encrypted": True} in sent["variables"]   # a real empty value is fine


async def test_set_envs_refuses_if_a_value_is_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"project": {"envs": [{"key": "S", "encrypted": True}]}})

    c = LiaraClient(TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(LiaraError) as e:
        await c.set_envs("a", {"X": "1"})
    assert e.value.kind == "precondition"


def scripted_build(pages, queue_status=200):
    """MockTransport: queue endpoint answers queue_status; build-logs returns `pages` in order (last repeats)."""
    seen = {"polls": [], "i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/releases/"):
            return httpx.Response(queue_status, json={"release": {"queue": 0}} if queue_status < 400 else {"error": "x"})
        seen["polls"].append(request.url.params.get("since"))
        page = pages[min(seen["i"], len(pages) - 1)]
        seen["i"] += 1
        return httpx.Response(200, json=page)
    return httpx.MockTransport(handler), seen


def line(i, text, stream="STDOUT"):
    return {"_id": f"id{i}", "line": text, "stream": stream, "createdAt": f"2026-10-22T06:41:4{i}.000Z"}


async def test_wait_for_release_multi_poll_dedupes_and_ignores_stderr_on_success():
    pages = [
        {"release": {"state": "BUILDING"}, "buildOutput": [line(1, "step 1")]},
        {"release": {"state": "BUILDING"}, "buildOutput": [line(1, "step 1"), line(2, "pip warning", "STDERR")]},
        {"release": {"state": "READY"}, "buildOutput": [line(3, "done")]},
    ]
    transport, seen = scripted_build(pages, queue_status=404)   # queue blip must not abort the wait
    c = LiaraClient(TOKEN, transport=transport)
    got = []
    res = await c.wait_for_release("r1", on_line=lambda t, s: got.append(t), poll_s=0)
    assert got == ["step 1", "pip warning", "done"]
    assert res.state == "READY" and res.failure_line is None
    assert seen["polls"][1] == "2026-10-22T06:41:41.000Z"       # ISO `since` is passed through


async def test_wait_for_release_failure_line_and_client_timeout():
    pages = [{"release": {"state": "FAILED", "failReason": "build"},
              "buildOutput": [line(1, "ok"), line(2, "ERROR: boom", "STDERR")]}]
    c = LiaraClient(TOKEN, transport=scripted_build(pages)[0])
    res = await c.wait_for_release("r1", poll_s=0)
    assert res.state == "FAILED" and res.failure_line == "ERROR: boom"
    pages = [{"release": {"state": "BUILDING"}, "buildOutput": []}]
    c = LiaraClient(TOKEN, transport=scripted_build(pages)[0])
    res = await c.wait_for_release("r1", poll_s=0, timeout_s=0.05)
    assert res.state == "CLIENT_TIMEOUT"


async def test_replay_handles_iso_since_and_scenario_fallback():
    c, _ = client("d_raw2")
    logs = await c.build_logs("6ad9b1234d2d3476a02ceb9f", since="2026-10-22T06:46:24.093Z")
    assert logs.state == "READY"
    assert (await c.upload_source("l0-dyar-6b46", b"x")).endswith(".tar.gz")   # d_raw2 has no upload → d_fixed


@pytest.mark.parametrize("status,body,kind", [
    (401, {"error": "Unauthorized"}, "auth"),
    (402, {"message": "not enough balance"}, "no_funds"),
    (400, {"message": "frozen_project"}, "frozen"),
    (403, {"message": "free_plan_platform"}, "free_plan"),
    (428, {"message": "max_deployment_count_in_day"}, "quota"),
    (404, {"error": "Not Found"}, "not_found"),
    (409, {"error": "Conflict"}, "name_taken"),
    (413, "Request Entity Too Large", "too_big"),
    (503, {"error": "Service Unavailable"}, "server"),
])
async def test_error_mapping(status, body, kind):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

    c = LiaraClient(TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(LiaraError) as e:
        await c.get_app("a")
    assert e.value.kind == kind and e.value.fa and TOKEN not in str(e.value)
    assert len(calls) == (2 if status == 503 else 1)       # GETs retry once on 502/503/504 only


async def test_odd_error_bodies_and_env_errors_hide_bodies():
    def handler(request):
        if "update-envs" in request.url.path:
            return httpx.Response(400, json={"echo": {"variables": [{"key": "K", "value": "secret-v"}]}})
        return httpx.Response(400, json=["weird", "list"])

    c = LiaraClient(TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(LiaraError) as e:
        await c.get_app("a")
    assert e.value.kind == "bad_request"
    with pytest.raises(LiaraError) as e:
        await c._req("POST", "/v1/projects/update-envs", json={}, retry=False)
    assert "secret-v" not in str(e.value)


async def test_writes_never_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={"error": "x"})

    c = LiaraClient(TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(LiaraError):
        await c.restart("a")
    assert len(calls) == 1
