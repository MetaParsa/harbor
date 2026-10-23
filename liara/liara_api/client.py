"""Async HTTP client for Liara's API — only the calls our agent uses, with behaviour verified against the live API.

Live facts baked in (see liara/l0/RESULTS.md):
- Every call carries ?teamID= when the account works through a team (the hackathon credit lives on one).
- /v1/billing can show 0 while gift credit is usable → `balance()` is advisory; a 402 on create is the real signal.
- Release READY does not mean the app works; callers verify with a probe + logs + events.
- /applets always answers 500 → not wrapped. No rollback endpoint (404) → `redeploy()` re-uses a stored source.
- update-envs replaces the whole list and creates an ENV_UPDATE release; `set_envs()` does read-modify-write.

The token is held in memory only and never appears in reprs, logs, call records or errors.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

import httpx

from .errors import LiaraError, from_response
from .models import (
    CLIENT_TIMEOUT, TERMINAL_STATES, ApiCall, App, BuildLogs, BuildResult, Event, LogLine, MetricSeries,
    MetricsSummary, Plans, Releases, newest_container, parse_events, parse_logs, parse_series,
)

API = "https://api.liara.ir"
METRIC_KINDS = ("cpu", "memory")   # network-receive/-transmit exist in the spec but aren't live-tested or used


def series_last(series: list[MetricSeries], container: str) -> float | None:
    return next((s.last for s in series if s.container == container), None)


class LiaraClient:
    def __init__(self, token: str, team_id: str | None = None, *, base_url: str = API,
                 transport: httpx.AsyncBaseTransport | None = None,
                 on_call: Callable[[ApiCall], None] | None = None, timeout_s: float = 30):
        self._http = httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {token}"},
                                       timeout=timeout_s, transport=transport)
        self.team_id = team_id
        self.on_call = on_call
        self.calls: deque[ApiCall] = deque(maxlen=500)     # recent calls (the watcher would grow a list forever)
        self.n_calls = 0
        self._plans: tuple[float, Plans] | None = None

    def __repr__(self) -> str:
        return f"LiaraClient(team={'yes' if self.team_id else 'no'}, calls={self.n_calls})"

    async def __aenter__(self) -> "LiaraClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- transport ----------

    async def _req(self, method: str, path: str, *, params: dict | None = None, retry: bool | None = None,
                   timeout: float | None = None, **kw):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.team_id:
            params["teamID"] = self.team_id
        retry = (method == "GET") if retry is None else retry
        for attempt in range(2 if retry else 1):
            t0 = time.perf_counter()
            try:
                r = await self._http.request(method, path, params=params,
                                             timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT, **kw)
            except httpx.HTTPError as e:
                self._record(method, path, None, t0, "network")
                if attempt == 0 and retry:
                    await asyncio.sleep(1)
                    continue
                raise LiaraError("network", None, type(e).__name__, path) from None
            try:
                body = r.json() if r.content else {}
            except ValueError:
                body = r.text[:2000]
            if r.status_code >= 400:
                err = from_response(r.status_code, body, path)
                self._record(method, path, r.status_code, t0, err.kind)
                if attempt == 0 and retry and r.status_code in (502, 503, 504):
                    await asyncio.sleep(1)
                    continue
                raise err
            self._record(method, path, r.status_code, t0)
            return body
        raise AssertionError("unreachable")

    def _record(self, method, path, status, t0, kind=None) -> None:
        call = ApiCall(method, path, status, round((time.perf_counter() - t0) * 1000), kind)
        self.calls.append(call)
        self.n_calls += 1
        if self.on_call:
            self.on_call(call)

    # ---------- account ----------

    async def plans(self, max_age_s: float = 600) -> Plans:
        """Plan and bundle catalogue (prices, daily deploy cap, log retention…). Personal data in /v1/me is dropped here.

        Cached for 10 min: /v1/me took ~5 s live, and prices don't change mid-session.
        """
        if self._plans and time.monotonic() - self._plans[0] < max_age_s:
            return self._plans[1]
        plans = Plans.from_me(await self._req("GET", "/v1/me"))
        self._plans = (time.monotonic(), plans)
        return plans

    async def balance(self) -> int | None:
        """Advisory only: gift/hackathon credit is not included (a live account showed 0 with ~2M toman usable)."""
        b = await self._req("GET", "/v1/billing")
        v = (b.get("user") or {}).get("balance") if isinstance(b, dict) else None
        return int(v) if v is not None else None

    async def teams(self) -> list[dict]:
        t = await self._req("GET", "/v2/teams")
        return [{"id": x.get("_id"), "name": x.get("name")} for x in (t.get("teams") or [])]

    async def ensure_network(self) -> str:
        nets = (await self._req("GET", "/v1/networks")).get("networks") or []
        if nets:
            return nets[0]["_id"]
        made = await self._req("POST", "/v1/networks", json={"networkName": "default"}, retry=False)
        return (made.get("network") or made).get("_id")

    # ---------- apps ----------

    async def list_apps(self) -> list[App]:
        return [App.from_api(p) for p in (await self._req("GET", "/v1/projects")).get("projects") or []]

    async def get_app(self, name: str) -> App:
        return App.from_api(await self._req("GET", f"/v1/projects/{name}"))

    async def create_app(self, name: str, plan_id: str, platform: str, bundle_id: str = "basic") -> str:
        """Costs money from the moment it exists (T3 in our policy). Returns the project id."""
        network = await self.ensure_network()
        body = {"name": name, "planID": plan_id, "platform": platform, "bundlePlanID": bundle_id, "network": network}
        return (await self._req("POST", "/v1/projects", json=body, retry=False)).get("projectID") or ""

    async def restart(self, name: str) -> None:
        await self._req("POST", f"/v1/projects/{name}/actions/restart", retry=False)

    async def turn_on(self, name: str) -> None:
        await self._req("POST", f"/v1/projects/{name}/actions/scale", json={"scale": 1}, retry=False)

    async def turn_off(self, name: str) -> None:
        await self._req("POST", f"/v1/projects/{name}/actions/scale", json={"scale": 0}, retry=False)

    async def resize(self, name: str, plan_id: str) -> None:
        await self._req("POST", f"/v1/projects/{name}/resize", json={"planID": plan_id}, retry=False)

    async def set_zero_downtime(self, app_id: str, on: bool) -> None:
        """Takes the project's _id, not its name (OpenAPI spec)."""
        await self._req("POST", f"/v1/projects/{app_id}/zero-downtime/{'enable' if on else 'disable'}", retry=False)

    # ---------- env vars ----------

    async def set_envs(self, name: str, changes: dict[str, str | None], encrypted: set[str] = frozenset()) -> list[str]:
        """Read-modify-write: Liara's update-envs replaces the whole list (and restarts via an ENV_UPDATE release).

        `changes` maps key -> new value, or None to remove. Returns the resulting key names (never values).
        Safe because Liara returns current values unmasked (seen live); if that ever changes, refuse rather than overwrite.
        """
        app = await self.get_app(name)
        current = {e.key: e for e in app.envs}
        if any(e.value is None for e in app.envs if e.key not in changes):
            raise LiaraError("precondition", None, "an env var came back without its value; refusing to overwrite it",
                             "/v1/projects/update-envs")
        merged = []
        for key, e in current.items():
            if key in changes:
                continue
            merged.append({"key": key, "value": e.value, **({"encrypted": True} if e.encrypted else {})})
        for key, value in changes.items():
            if value is not None:
                # an existing encrypted var stays encrypted when its value changes
                enc = key in encrypted or (key in current and current[key].encrypted)
                merged.append({"key": key, "value": value, **({"encrypted": True} if enc else {})})
        await self._req("POST", "/v1/projects/update-envs", json={"project": name, "variables": merged}, retry=False)
        return [v["key"] for v in merged]

    # ---------- deploy ----------

    async def upload_source(self, name: str, tar_gz: bytes) -> str:
        r = await self._req("POST", f"/v2/projects/{name}/sources", retry=False, timeout=300,
                            files={"file": ("source.tar.gz", tar_gz, "application/gzip")})
        return r["sourceID"]

    @staticmethod
    def release_body(source_id: str, platform: str, port: int, *, platform_config: dict | None = None,
                     envs: dict[str, str] | None = None, message: str = "", health_check: dict | None = None) -> dict:
        """Release body as the Liara CLI builds it.

        envs={} leaves the app's env vars alone (seen live). A NON-empty `envs` REPLACES the whole list (seen live
        2026-10-22: deploying with two new vars silently dropped PAYMENT_API_URL). Use set_envs() for env changes.
        """
        body = {"sourceID": source_id, "port": port, "type": platform, "message": message, "args": [], "cron": [],
                "envs": envs or {}, "disks": [], "build": {"cache": True, "location": "iran"},
                "platformConfig": platform_config or {}}
        if health_check:
            body["healthCheck"] = health_check
        return body

    async def create_release(self, name: str, body: dict) -> str:
        return (await self._req("POST", f"/v2/projects/{name}/releases", json=body, retry=False))["releaseID"]

    async def redeploy(self, name: str, stored_body: dict, message: str = "rollback") -> str:
        """Our rollback: a new release from a source + body we stored earlier (no rollback endpoint exists; 29 s in a live test)."""
        return await self.create_release(name, {**stored_body, "message": message})

    async def build_logs(self, release_id: str, since: str | None = None) -> BuildLogs:
        return BuildLogs.from_api(await self._req("GET", f"/v2/releases/{release_id}/build-logs", params={"since": since}))

    async def release_queue(self, release_id: str) -> int:
        r = await self._req("GET", f"/v1/releases/{release_id}")
        return int((r.get("release") or {}).get("queue") or 0)

    async def wait_for_release(self, release_id: str, *, on_line: Callable[[str, str], Awaitable[None] | None] | None = None,
                               timeout_s: float = 25 * 60, poll_s: float = 1.5) -> BuildResult:
        """Wait out the queue, stream build lines, stop at a terminal state. READY still needs verifying.

        state is CLIENT_TIMEOUT if our deadline passes first. failure_line is set only for a non-READY build
        (pip warnings on STDERR in a good build are not failures).
        """
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            try:
                if await self.release_queue(release_id) <= 0:
                    break
            except LiaraError:
                break           # a blip here shouldn't abort the wait; build-logs below is the real source
            await asyncio.sleep(3)
        since, lines, seen, logs, state = None, [], set(), BuildLogs(None, None, 0, []), None
        while time.monotonic() - t0 < timeout_s:
            logs = await self.build_logs(release_id, since)
            for ln in logs.lines:
                key = ln.id or (ln.created_at, ln.text)
                if key in seen:
                    continue    # `since` may be inclusive: don't repeat the boundary line
                seen.add(key)
                lines.append(ln)
                if on_line:
                    res = on_line(ln.text, ln.stream)
                    if asyncio.iscoroutine(res):
                        await res
                since = ln.created_at or since
            if logs.state in TERMINAL_STATES:
                state = logs.state
                break
            await asyncio.sleep(poll_s)
        state = state or CLIENT_TIMEOUT
        failure = None if state == "READY" else next((ln.text for ln in lines if ln.stream != "STDOUT"), None)
        return BuildResult(release_id, state, logs.fail_reason, failure, round(time.monotonic() - t0), lines)

    async def releases(self, name: str, count: int = 10) -> Releases:
        return Releases.from_api(await self._req("GET", f"/v1/projects/{name}/releases", params={"page": 1, "count": count}))

    # ---------- runtime signals ----------

    async def app_logs(self, name: str, *, since_s: int = 1800, release_id: str | None = None,
                       newest: bool = True) -> list[LogLine]:
        """Up to 100 lines per call (seen live). newest=True asks for the latest lines first; result is always oldest-first."""
        params = {"start": int(time.time()) - since_s, "releaseId": release_id,
                  "direction": "backward" if newest else "forward", "last_lines": "true" if newest else "false"}
        return parse_logs(await self._req("GET", f"/v2/projects/{name}/logs", params=params))

    async def events(self, name: str, since_s: int = 3600) -> list[Event]:
        now = int(time.time())
        return parse_events(await self._req("GET", f"/v1/projects/{name}/events", params={"start": now - since_s, "end": now}))

    async def metrics_summary(self, name: str) -> MetricsSummary:
        return MetricsSummary.from_api(await self._req("GET", f"/v1/projects/{name}/metrics/summary"))

    async def usage_now(self, name: str) -> dict:
        """Current container's memory (bytes) and CPU (cores). The summary also lists dead containers, so the
        current one is taken from the memory series that started last."""
        summary, series = await asyncio.gather(self.metrics_summary(name), self.metrics(name, "memory", 900))
        container = newest_container(series)
        if container is None:
            return {"container": None, "memory_bytes": None, "cpu_cores": None}
        return {"container": container, "memory_bytes": summary.memory_of(container) or series_last(series, container),
                "cpu_cores": summary.cpu_of(container)}

    async def metrics(self, name: str, kind: str, since_s: int = 1800) -> list[MetricSeries]:
        """`since` must be an absolute unix timestamp ≤ 7 days ago (a look-back value gets 428, seen live)."""
        if kind not in METRIC_KINDS:
            raise ValueError(f"unknown metric {kind}")
        since_s = min(since_s, 7 * 86400 - 60)
        return parse_series(await self._req("GET", f"/v1/projects/{name}/metrics/{kind}",
                                            params={"since": int(time.time()) - since_s}))

    # ---------- domains (shapes from Liara's OpenAPI spec; not yet tried against a real domain) ----------

    async def add_domain(self, name: str, domain: str) -> dict:
        return await self._req("POST", "/v1/domains", json={"name": domain, "project": name, "type": "PROJECT"}, retry=False)

    async def list_domains(self, name: str) -> list[dict]:
        return (await self._req("GET", "/v1/domains", params={"project": name})).get("domains") or []

    async def check_domain(self, domain_id: str) -> dict:
        return await self._req("GET", f"/v1/domains/{domain_id}/check")

    async def provision_ssl(self, domain: str) -> dict:
        return await self._req("POST", "/v1/domains/provision-ssl-certs", json={"domain": domain}, retry=False)


async def _get_same_site(c: httpx.AsyncClient, url: str, method: str = "GET") -> httpx.Response:
    """Follow at most 3 redirects, and only to https on the same host or another *.liara.run app. A redirect anywhere
    else (an internal address, a cloud metadata IP…) is not followed: its 3xx is the answer. The app being probed
    belongs to whoever holds the token, so it must never steer our server's requests."""
    host = httpx.URL(url).host
    for _ in range(4):
        r = await c.request(method, url)
        if not r.is_redirect:
            return r
        nxt = r.url.join(r.headers.get("location", ""))
        if nxt.scheme != "https" or not (nxt.host == host or nxt.host.endswith(".liara.run")):
            return r
        url = str(nxt)
    return r


async def probe(url: str, n: int = 5, timeout_s: float = 8, marker: str | None = None, budget_s: float = 15) -> list[dict]:
    """Our own HTTP check (Liara has no status-code or latency metric). One dict per request.

    One warm connection, requests one after another: parallel requests each paid their own DNS + TLS handshake and
    made a 0.1 s page look like 0.5–1.5 s (live, 2026-10-22). Stops early once `budget_s` is spent on a slow app.
    """
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as c:
        try:
            await _get_same_site(c, url, "HEAD")   # warm-up: DNS + TLS handshake aren't the app's response time
        except httpx.HTTPError:
            pass                            # the real requests below report the failure
        start = time.perf_counter()
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                r = await _get_same_site(c, url)
                out.append({"status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000),
                            "marker": marker is None or marker in r.text})
            except httpx.HTTPError as e:
                out.append({"status": None, "ms": round((time.perf_counter() - t0) * 1000), "error": type(e).__name__})
            if time.perf_counter() - start > budget_s:
                break
    return out
