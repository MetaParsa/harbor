"""First deploy (analysis → create with two taps → build → heal from the logs → live) and the domain card.
REPLAY + fakes only, no credit."""

import asyncio
import importlib
import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from liara.agent.budget import Budget, Caps
from liara.agent.policy import ActionLog, Policy, Session
from liara.agent.redact import Redactor
from liara.deploy import flow
from liara.deploy.analyze import analyze, recommend_plan
from liara.deploy.domain import clean_domain, domain_status, record_name
from liara.deploy.heal import apply_fix, classify_failure
from liara.liara_api import LiaraClient, ReplayTransport
from liara.liara_api.models import BuildResult, LogLine

pytestmark = pytest.mark.anyio
APP_D = Path(__file__).resolve().parents[1] / "demo-apps" / "app-d"
ORIGIN = {"origin": "http://testserver"}
PKG = ["Traceback (most recent call last):", "  File \"/usr/local/bin/gunicorn\", line 3, in <module>",
       "ModuleNotFoundError: No module named 'pkg_resources'"]
DOTENV = ["[ERROR] Exception in worker process", "  File \"/app/main.py\", line 5, in <module>",
          "ModuleNotFoundError: No module named 'dotenv'"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def zip_of(folder: Path, prefix: str = "app-d/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in folder.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, prefix + p.relative_to(folder).as_posix())
    return buf.getvalue()


# ---------- analysis + catalogue ----------

async def test_app_d_analysis_finds_flask_in_main_and_recommends_the_small_plan():
    a = analyze(APP_D)
    assert (a.platform, a.port) == ("flask", 80)
    assert a.platform_config["appModule"] == "main:app"
    assert [f.rule for f in a.preflight] == ["flask-app-module"]
    assert a.env_vars == ["SHOP_NAME"] and a.database is None
    async with LiaraClient("t", transport=ReplayTransport("d_fixed")) as c:
        plan = recommend_plan(a, await c.plans())
    assert plan["plan_id"] and plan["ram_gb"] == 0.5 and plan["month"] > 0
    assert "۰٫۵" in plan["reason"] or "۰.۵" in plan["reason"]


def test_catalogue_heals_pkg_resources_then_dotenv_and_never_repeats(tmp_path):
    (tmp_path / "requirements.txt").write_text("Flask==3.0.3\n")
    d = classify_failure(PKG)
    assert d.rule == "py-pkg-resources"
    fix = apply_fix(tmp_path, d)
    assert fix and "+setuptools<80" in fix.diff
    assert apply_fix(tmp_path, classify_failure(PKG)) is None            # same fix again would just loop
    d2 = classify_failure(DOTENV)
    assert (d2.rule, d2.package) == ("py-module-missing", "python-dotenv")
    assert "+python-dotenv" in apply_fix(tmp_path, d2).diff
    assert (tmp_path / "requirements.txt").read_text().splitlines() == ["Flask==3.0.3", "setuptools<80", "python-dotenv"]
    unknown = classify_failure(["boom", "RuntimeError: database is locked"])
    assert unknown.rule is None and "database is locked" in unknown.line and apply_fix(tmp_path, unknown) is None


def test_zip_slip_and_links_are_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.py", "x")
    with pytest.raises(flow.UploadError):
        flow.unpack_zip(buf.getvalue(), tmp_path / "a")
    assert not (tmp_path / "evil.py").exists()
    with pytest.raises(flow.UploadError):
        flow.unpack_zip(b"not a zip", tmp_path / "b")
    root = flow.unpack_zip(zip_of(APP_D), tmp_path / "c")
    assert root.name == "app-d" and (root / "main.py").is_file()


# ---------- the whole loop on a fake Liara ----------

class FakeLiara:
    """Release n crashes with PKG (1), then DOTENV (2), then serves 200 (3) — app D's real first deploy."""

    def __init__(self, client: LiaraClient):
        self.c, self.n, self.sources = client, 0, []

    async def upload_source(self, app, data):
        self.sources.append(data)
        return f"src-{len(self.sources)}.tar.gz"

    async def create_release(self, app, body):
        self.n += 1
        self.bodies = getattr(self, "bodies", []) + [body]
        return f"rel-{self.n}"

    async def wait_for_release(self, rid, **_):
        return BuildResult(rid, "READY", None, None, 30, [])

    async def app_logs(self, app, *, release_id=None, **_):
        lines = {"rel-1": PKG, "rel-2": DOTENV}.get(release_id, ["> [GUNICORN] Starting...", "Listening at: http://0.0.0.0:8000"])
        return [LogLine(i, "stderr", t, release_id) for i, t in enumerate(lines)]


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LIARA_AGENT_KILL", raising=False)
    client = LiaraClient("tok-123456789", team_id="t1", transport=ReplayTransport("d_fixed"))
    fake = FakeLiara(client)
    for name in ("upload_source", "create_release", "wait_for_release", "app_logs"):
        monkeypatch.setattr(client, name, getattr(fake, name))
    s = Session("sess-1", client, Redactor(["tok-123456789"]))
    q: asyncio.Queue = asyncio.Queue()
    s.subscribers.append(q)
    pol = Policy(Budget(tmp_path, Caps(deploys_per_day=15, apps_per_day=3)), ActionLog(tmp_path / "actions.jsonl"))
    return pol, s, fake, q, tmp_path


async def test_first_deploy_heals_twice_and_goes_live_in_three_builds(loop_env):
    pol, s, fake, q, tmp = loop_env
    job = await flow.new_project(s, zip_of(APP_D), tmp)
    probes = []

    async def prober(url, n):
        probes.append(fake.n)
        return [{"status": 200 if fake.n >= 3 else 502, "ms": 40}]

    async def no_sleep(_):
        await asyncio.sleep(0)

    async def runner(pol_, s_, job_):
        await flow.run_after_approval(pol_, s_, job_, poll_s=0.01, prober=prober, sleep=no_sleep, steady_s=0)

    res = await flow.propose_create(pol, s, job, "app-d-test", runner=runner)
    assert res["status"] == "waiting_for_user" and fake.n == 0          # nothing built before the user's taps
    act = s.actions[res["pending_action_id"]]
    assert act.card.cost.startswith("حدود")
    assert (await pol.approve(s, act.id, act.hash))["status"] == "needs_second_tap"
    act.armed_at -= 1
    assert (await pol.approve(s, act.id, act.hash, sure=True))["status"] == "done"
    await asyncio.wait_for(job.task, 5)

    assert job.state == "live" and job.builds == 3 and job.url == "https://app-d-test.liara.run"
    assert [c.rule for c in job.changes] == ["flask-app-module", "py-pkg-resources", "py-module-missing"]
    assert all(b["platformConfig"]["appModule"] == "main:app" and b["envs"] == {} for b in fake.bodies)
    patch = await flow.patch_text(job)
    assert "+setuptools<80" in patch and "+python-dotenv" in patch and "main.py" not in patch
    assert flow.liara_json(job) == {"platform": "flask", "app": "app-d-test", "port": 80,
                                    "build": {"location": "iran", "cache": True},
                                    "flask": {"pythonVersion": "3.12", "appModule": "main:app"}}
    wf = flow.github_workflow(job, "team-1")
    assert '--app="app-d-test" --port=80 --team-id="team-1"' in wf and "secrets.LIARA_API_TOKEN" in wf
    assert "--detach" not in wf and "--no-app-logs" in wf and "tok-" not in wf
    kinds = [e["kind"] for e in job.events]
    assert kinds.count("heal") == 2 and kinds[-1] == "live"
    assert (APP_D / "requirements.txt").read_text().strip() == "Flask==3.0.3"     # the user's copy is never touched


async def test_unknown_crash_stops_and_hands_over_without_a_fourth_build(loop_env, monkeypatch):
    pol, s, fake, q, tmp = loop_env

    async def bad_logs(app, *, release_id=None, **_):
        return [LogLine(1, "stderr", "sqlite3.OperationalError: unable to open database file", release_id)]

    monkeypatch.setattr(s.client, "app_logs", bad_logs)
    job = await flow.new_project(s, zip_of(APP_D), tmp)
    job.app, s.ctx.created_apps = "app-d-test", {"app-d-test"}

    async def prober(url, n):
        return [{"status": 502, "ms": 40}]

    async def no_sleep(_):
        await asyncio.sleep(0)

    await flow.deploy_loop(pol, s, job, prober=prober, sleep=no_sleep, live_wait_s=0.05, poll_s=0)
    assert job.state == "stopped" and job.builds == 1
    assert "unable to open database" in job.events[-1]["line"]


async def test_create_card_is_refused_for_a_bad_name_and_a_second_create(loop_env):
    pol, s, fake, q, tmp = loop_env
    job = await flow.new_project(s, zip_of(APP_D), tmp)
    assert (await flow.propose_create(pol, s, job, "Bad_Name"))["error"] == "bad_app"
    job.created = True
    assert (await flow.propose_create(pol, s, job, "app-d-2"))["error"] == "busy"


# ---------- domain card ----------

def test_domain_names_and_record_names():
    assert clean_domain("https://Shop.Example.ir/path") == "shop.example.ir"
    assert clean_domain("not a domain") is None
    assert record_name("example.ir") == "@" and record_name("shop.example.ir") == "shop"
    assert record_name("example.co.ir") == "@" and record_name("www.example.co.ir") == "www"


async def test_domain_status_shows_liaras_records_and_the_irnic_guide():
    async with LiaraClient("t", transport=ReplayTransport("d_fixed")) as c:
        s = Session("s", c, Redactor())
        st = await domain_status(s, "l0-dyar-6b46", "example.ir")
        missing = await domain_status(s, "l0-dyar-6b46", "other.com")
    assert st["state"] == "waiting_dns"
    assert {r["type"]: r["value"] for r in st["records"]} == {"ALIAS یا CNAME": "l0-dyar-6b46.liara.run", "TXT": "liara-verify-abc"}
    assert "nic.ir" in " ".join(st["guide"]["steps"]) and st["guide"]["ours"] and st["limit"]["api"] == "none"
    assert missing["state"] == "not_added"


# ---------- routes ----------

@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("LIARA_REPLAY", "d_fixed")
    monkeypatch.setenv("LIARA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LIARA_AGENT_KILL", raising=False)
    import liara.app as app_mod
    app_mod = importlib.reload(app_mod)
    page = TestClient(app_mod.app)
    assert page.post("/api/session", json={"token": "replay-token-123"}, headers=ORIGIN).status_code == 200
    return app_mod, page


def test_upload_route_analyses_and_downloads_belong_to_the_session(web):
    app_mod, page = web
    r = page.post("/api/projects?filename=app-d.zip", content=zip_of(APP_D), headers=ORIGIN)
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["analysis"]["framework"] == "Flask" and job["suggested_app"].startswith("app-d-")
    assert page.post("/api/projects?filename=x.zip", content=zip_of(APP_D)).status_code == 403     # no Origin
    other = TestClient(app_mod.app)
    assert other.get(f"/api/projects/{job['id']}/patch").status_code == 401
    assert page.get(f"/api/projects/{job['id']}/liara.json").json()["flask"]["appModule"] == "main:app"


def test_domain_route_proposes_a_card_and_reports_an_existing_domain(web):
    _, page = web
    r = page.post("/api/domains", json={"app": "l0-dyar-6b46", "domain": "new-shop.com"}, headers=ORIGIN).json()
    assert r["status"] == "waiting_for_user" and "new-shop.com" in r["card"]["title"]
    r = page.post("/api/domains", json={"app": "l0-dyar-6b46", "domain": "example.ir"}, headers=ORIGIN).json()
    assert r["status"] == "exists" and r["domain_status"]["state"] == "waiting_dns"
    r = page.post("/api/domains/ssl", json={"app": "l0-dyar-6b46", "domain": "example.ir"}, headers=ORIGIN).json()
    assert r["status"] == "not_ready"
    assert page.post("/api/domains", json={"app": "l0-dyar-6b46", "domain": "bad domain"}, headers=ORIGIN).status_code == 400


# ---------- review fixes ----------

def test_utf16_requirements_unknown_and_local_modules_are_never_guessed(tmp_path):
    (tmp_path / "requirements.txt").write_bytes("Flask==3.0.3\r\nsetuptools>=70\r\n".encode("utf-16"))
    fix = apply_fix(tmp_path, classify_failure(PKG))                      # pins the existing setuptools line, no crash
    assert fix and (tmp_path / "requirements.txt").read_text(encoding="utf-8").splitlines() == ["Flask==3.0.3", "setuptools<80"]
    d = classify_failure(["ModuleNotFoundError: No module named 'leftpadx'"])
    assert d.package is None and apply_fix(tmp_path, d) is None and "requirements.txt" in d.advice
    (tmp_path / "utils").mkdir()
    d = classify_failure(["ModuleNotFoundError: No module named 'utils.helpers'"])
    assert d.module == "utils" and apply_fix(tmp_path, d) is None and "خود پروژه" in d.advice
    (tmp_path / "package.json").write_text('["not", "an", "object"]')
    from liara.deploy.heal import add_start_script
    assert add_start_script(tmp_path) is None


async def test_double_click_makes_one_create_card(loop_env):
    pol, s, fake, q, tmp = loop_env
    job = await flow.new_project(s, zip_of(APP_D), tmp)

    async def never(*_a, **_k):
        await asyncio.sleep(3600)

    r1, r2 = await asyncio.gather(flow.propose_create(pol, s, job, "app-d-one", runner=never),
                                  flow.propose_create(pol, s, job, "app-d-two", runner=never))
    assert sorted(r.get("status") for r in (r1, r2)) == ["error", "waiting_for_user"]
    assert len([a for a in s.actions.values() if a.tool == "create_app"]) == 1
    job.task.cancel()


async def test_an_unexpected_error_stops_the_job_instead_of_hanging(loop_env, monkeypatch):
    pol, s, fake, q, tmp = loop_env
    job = await flow.new_project(s, zip_of(APP_D), tmp)

    async def boom(*_a, **_k):
        raise RuntimeError("client closed")

    monkeypatch.setattr(s.client, "upload_source", boom)
    res = await flow.propose_create(pol, s, job, "app-d-test",
                                    runner=lambda p, s_, j: flow.run_after_approval(p, s_, j, poll_s=0.01))
    act = s.actions[res["pending_action_id"]]
    await pol.approve(s, act.id, act.hash)
    act.armed_at -= 1
    await pol.approve(s, act.id, act.hash, sure=True)
    await asyncio.wait_for(job.task, 5)
    assert job.state == "stopped" and "RuntimeError" in job.stop_reason


async def test_no_untested_fix_after_the_last_build_and_bad_zips_leave_nothing(loop_env):
    pol, s, fake, q, tmp = loop_env
    job = await flow.new_project(s, zip_of(APP_D), tmp)
    job.app, job.builds, s.ctx.created_apps = "app-d-test", 2, {"app-d-test"}
    s.ctx.builds_on_created["app-d-test"] = 2
    fake.n = 1                                                    # the next release crashes with DOTENV

    async def prober(url, n):
        return [{"status": 502, "ms": 40}]

    async def no_sleep(_):
        await asyncio.sleep(0)

    await flow.deploy_loop(pol, s, job, prober=prober, sleep=no_sleep, poll_s=0)
    assert job.state == "stopped" and job.builds == 3 and "python-dotenv" not in await flow.patch_text(job)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a", "x")
        z.writestr("a/b", "y")                                   # file and folder with the same name
    with pytest.raises(flow.UploadError):
        await flow.new_project(s, buf.getvalue(), tmp)
    assert len(list((tmp / "projects").iterdir())) == 1          # only the good project's folder is left


def test_zero_downtime_is_a_card_and_github_autodeploy_is_declared_console_only(web):
    _, page = web
    assert page.get("/api/apps/l0-dyar-6b46/overview").json()["zero_downtime"] is False
    r = page.post("/api/apps/l0-dyar-6b46/zero-downtime", headers=ORIGIN).json()
    assert r["status"] == "waiting_for_user" and "بدون قطعی" in r["card"]["title"]
    from liara.sre.capabilities import capability
    cap = capability("github_autodeploy")
    assert cap["api"] == "none" and any("LIARA" in s or "گیت‌هاب" in s for s in cap["steps"])


def test_basic_bundle_zero_downtime_refusal_is_explained():
    from liara.liara_api.errors import error_kind, FA
    kind = error_kind(428, {"message": "This plan does not support zero downtime"})   # live answer, 2026-10-22
    assert kind == "not_in_plan" and "ارتقا" in FA[kind]
