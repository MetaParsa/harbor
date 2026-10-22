"""Typed views of the Liara responses we use. Shapes follow the live recordings in fixtures/, not the docs,
where they differ (events, logs, metrics)."""

import json
from dataclasses import dataclass, field

HOURS_PER_MONTH = 720  # Liara's own monthly = hourly × 720


@dataclass
class Plan:
    id: str
    cpu: float
    ram_gb: float
    price_hour: float
    available: bool
    region: str

    @classmethod
    def from_api(cls, pid: str, d: dict) -> "Plan":
        return cls(pid, float((d.get("CPU") or {}).get("amount") or 0), float((d.get("RAM") or {}).get("amount") or 0),
                   float(d.get("price") or 0), bool(d.get("available")), d.get("region") or "")


@dataclass
class Bundle:
    id: str                      # free | basic | standard | pro
    price_hour: float
    price_month: int             # displayPrice, toman
    available: bool
    max_deploys_per_day: int | None
    build_timeout_min: int | None
    logs_retention_days: float | None
    max_domains: int | None
    max_source_mb: int | None
    rollback: bool
    zero_downtime: bool

    @classmethod
    def from_api(cls, bid: str, d: dict) -> "Bundle":
        return cls(bid, float(d.get("price") or 0), int(d.get("displayPrice") or 0), bool(d.get("available")),
                   d.get("maxDeploymentCountInDay"), d.get("buildTimeout"), d.get("maxLogsRetention"),
                   d.get("maxDomainsCount"), d.get("maxSourceSize"), bool(d.get("rollback")), bool(d.get("zeroDowntime")))


@dataclass
class Plans:
    apps: dict[str, Plan]
    bundles: dict[str, dict[str, Bundle]]   # plan id -> bundle id -> Bundle

    @classmethod
    def from_me(cls, me: dict) -> "Plans":
        p = me.get("plans") or {}
        apps = {k: Plan.from_api(k, v) for k, v in (p.get("projects") or {}).items()}
        bundles = {pid: {bid: Bundle.from_api(bid, b) for bid, b in bs.items()}
                   for pid, bs in (p.get("projectBundlePlans") or {}).items()}
        return cls(apps, bundles)

    def iran_available(self) -> list[Plan]:
        return sorted((p for p in self.apps.values() if p.available and p.region == "iran"), key=lambda p: p.price_hour)

    def cheapest_iran(self) -> Plan:
        plans = self.iran_available()
        if not plans:
            raise ValueError("no available Iran app plan")
        return plans[0]

    def bundle(self, plan_id: str, bundle_id: str) -> Bundle | None:
        return (self.bundles.get(plan_id) or {}).get(bundle_id)

    def price(self, plan_id: str, bundle_id: str) -> tuple[float, int]:
        """(toman per hour, toman per month) for plan + bundle together — what the user actually pays."""
        hour = self.apps[plan_id].price_hour + (b.price_hour if (b := self.bundle(plan_id, bundle_id)) else 0)
        return round(hour, 2), round(hour * HOURS_PER_MONTH)


@dataclass
class EnvVar:
    key: str
    value: str | None = field(repr=False)   # plain text even when encrypted (seen live): never log. None = not returned
    encrypted: bool = False


@dataclass
class App:
    name: str
    id: str
    platform: str
    status: str                 # ACTIVE, FROZEN, …
    scale: int                  # 0 = off, 1 = on
    plan_id: str
    bundle_id: str
    hourly_price: float
    bundle_hourly_price: float
    is_deployed: bool
    envs: list[EnvVar]
    zero_downtime: bool | None = None     # new release starts next to the old one before traffic moves

    @classmethod
    def from_api(cls, d: dict) -> "App":
        p = d.get("project") or d
        return cls(p.get("project_id") or "", p.get("_id") or "", p.get("type") or "", p.get("status") or "",
                   int(p.get("scale") or 0), p.get("planID") or "", p.get("bundlePlanID") or "",
                   float(p.get("hourlyPrice") or 0), float(p.get("bundleHourlyPrice") or 0), bool(p.get("isDeployed")),
                   [EnvVar(e.get("key", ""), e.get("value"), bool(e.get("encrypted"))) for e in p.get("envs") or []],
                   p.get("zeroDowntime"))

    @property
    def url(self) -> str:
        return f"https://{self.name}.liara.run"

    @property
    def total_hourly_price(self) -> float:
        return self.hourly_price + self.bundle_hourly_price


@dataclass
class Release:
    id: str
    tag: str                    # v1, v2, …
    type: str                   # DEPLOY | ENV_UPDATE
    state: str                  # READY ≠ healthy (seen live): verify with probe + logs + events
    created_at: str
    finished_at: str | None
    source_available: bool
    message: str | None

    @classmethod
    def from_api(cls, d: dict) -> "Release":
        return cls(d.get("_id") or "", d.get("tag") or "", d.get("type") or "", d.get("state") or "",
                   d.get("createdAt") or "", d.get("finishedAt"), bool(d.get("sourceAvailable")), d.get("message"))


@dataclass
class Releases:
    total: int
    current_id: str | None
    items: list[Release]

    @classmethod
    def from_api(cls, d: dict) -> "Releases":
        return cls(int(d.get("total") or 0), d.get("currentRelease"), [Release.from_api(r) for r in d.get("releases") or []])


@dataclass
class BuildLine:
    text: str
    stream: str                 # STDOUT | STDERR | …; first non-STDOUT line of a failed build = failure line
    created_at: str
    id: str = ""                # Liara's line _id, to drop lines re-sent across `since` polls


@dataclass
class BuildLogs:
    state: str | None
    fail_reason: str | None
    queue: int
    lines: list[BuildLine]

    @classmethod
    def from_api(cls, d: dict) -> "BuildLogs":
        r = d.get("release") or {}
        out = d.get("buildOutput") or d.get("tail") or []   # "tail" = our trimmed fixture
        return cls(r.get("state"), r.get("failReason"), int(r.get("queue") or 0),
                   [BuildLine((x.get("line") or "").rstrip(), x.get("stream") or "", x.get("createdAt") or "",
                              x.get("_id") or "") for x in out])


TERMINAL_STATES = {"READY", "FAILED", "TIMEDOUT", "CANCELED", "UNHEALTHY"}
CLIENT_TIMEOUT = "CLIENT_TIMEOUT"   # our own deadline passed before Liara reported a terminal state


@dataclass
class BuildResult:
    release_id: str
    state: str | None
    fail_reason: str | None
    failure_line: str | None
    seconds: int
    lines: list[BuildLine]


@dataclass
class LogLine:
    ts_ns: int
    stream: str                 # stdout | stderr
    text: str
    release_id: str | None

    @property
    def ts(self) -> float:
        return self.ts_ns / 1e9


def parse_logs(d: dict) -> list[LogLine]:
    """v2 app logs: streams of [ns_ts, '{"type":…,"_entry":…}'] → flat list, oldest first."""
    out = []
    for stream in d.get("data") or []:
        rid = (stream.get("metaData") or {}).get("releaseId")
        for ts, raw in stream.get("values") or []:
            try:
                j = json.loads(raw)
                kind, text = j.get("type") or "", j.get("_entry") or ""
            except (ValueError, AttributeError):
                kind, text = "", str(raw)
            out.append(LogLine(int(ts), kind, text, rid))
    return sorted(out, key=lambda x: x.ts_ns)


@dataclass
class Event:
    container: str
    status: str                 # create | start | kill | stop | die | destroy | oom (live, 2026-10-22)
    time: int                   # unix seconds
    release_id: str | None
    tag: str | None


def parse_events(d: dict) -> list[Event]:
    """Live shape (seen live) — differs from the docs' Loki-style stream. Oldest first."""
    ev = [Event(x.get("id") or "", x.get("status") or x.get("action") or "", int(x.get("time") or 0),
                x.get("release"), x.get("tag")) for x in d.get("data") or [] if isinstance(x, dict)]
    return sorted(ev, key=lambda e: e.time)


def oom_count(events: list[Event], tag: str | None = None) -> int:
    """Out-of-memory kills (Liara emits status "oom" just before die → start, seen live on demo-shop v3)."""
    return sum(1 for e in events if e.status == "oom" and (tag is None or e.tag == tag))


def restart_count(events: list[Event], tag: str | None = None) -> int:
    """Crash-loop signal (applets endpoint is unusable, seen live). Docker restarts keep the container id, so restarts =
    starts − 1 per container; a normal replace (die/stop of the old container) counts 0."""
    starts: dict[str, int] = {}
    for e in events:
        if e.status == "start" and (tag is None or e.tag == tag):
            starts[e.container] = starts.get(e.container, 0) + 1
    return sum(n - 1 for n in starts.values())


@dataclass
class MetricSeries:
    container: str              # "<projectId>.1.<uuid>-DEPLOY"; a new one per restart
    points: list[tuple[int, float]]

    @property
    def last(self) -> float | None:
        return self.points[-1][1] if self.points else None


def parse_series(d: dict) -> list[MetricSeries]:
    return [MetricSeries(s.get("applet") or "", [(int(t), float(v)) for t, v in s.get("values") or []])
            for s in d.get("result") or []]


@dataclass
class MetricsSummary:
    memory: dict[str, tuple[int, float]]   # container -> (ts, bytes)
    cpu: dict[str, tuple[int, float]]      # container -> (ts, fraction of a core)
    disks: list[dict]

    @classmethod
    def from_api(cls, d: dict) -> "MetricsSummary":
        def latest(items):
            return {x.get("applet") or "": (int((x.get("value") or [0, 0])[0]), float((x.get("value") or [0, 0])[1]))
                    for x in items or []}
        return cls(latest(d.get("memoryUsage")), latest(d.get("cpuUsage")), list(d.get("disksUsage") or []))

    # Containers from previous releases are still listed right after a deploy (a live test saw 3 for one app, same ts),
    # so the caller must say which container is current: see LiaraClient.usage_now().
    def memory_of(self, container: str) -> float | None:
        return (self.memory.get(container) or (None, None))[1]

    def cpu_of(self, container: str) -> float | None:
        return (self.cpu.get(container) or (None, None))[1]


def newest_container(series: list[MetricSeries]) -> str | None:
    """The current container is the series that started last (each restart/deploy starts a new one)."""
    live = [s for s in series if s.points]
    return max(live, key=lambda s: s.points[0][0]).container if live else None


@dataclass
class ApiCall:
    """One request, for the action log and the 'API receipts' counter. Never holds bodies or tokens."""
    method: str
    path: str
    status: int | None
    ms: int
    kind: str | None = None     # error kind if it failed
