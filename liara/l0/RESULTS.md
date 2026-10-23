# L0 results (live the platform⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ API, 2026-10-22)

Fixtures (redacted): `liara/fixtures/api/`. Script: `liara/l0/liara_l0.py` (`state.json` holds the team id and stored release bodies — no secrets).
Throwaway app `l0-dyar-6b46` (small-g2 + basic, Flask) on team "hackathon": 4 builds + 1 env update, then deleted. Cost ≈ 1 app-hour.

## 1. Account and credit

| Check | Result |
|---|---|
| Auth | Bearer token works; **all calls need `?teamID=<id>`** — the credit and apps live on the team "hackathon" (`GET /v2/teams`; `/v1/teams` is 404). |
| Balance | `/v1/billing` → `balance: 0` for both the user and the team, yet app creation succeeded and the console shows ~2M toman credit. **The API does not expose this (gift/hackathon) credit.** → never tell a user "you have no balance" from `/v1/billing` alone; the real test is the create call (402 = no funds). |
| Plans | `small-g2` 0.5 CPU / 0.5 GB **659.72 toman/h**; `medium-g2` 1/1 GB 1,145.83/h; `standard-base-g2` 1 CPU/2 GB 2,048.61/h. |
| Bundles (small-g2) | basic 381.94/h (275,000/month), standard 659.72/h (475,000), pro 1,145.83/h (825,000), free unavailable. |
| basic limits (live, from `/v1/me`) | `maxDeploymentCountInDay` 20 · `buildTimeout` 5 min · `maxLogsRetention` 7 d · `maxReleaseRetention` 10 · `maxDomainsCount` 2 · `maxSourceSize` 128 MB · `rollback: true` · `zeroDowntime: false` · `freeSSL: true`. |
| Running cost | small-g2 + basic ≈ 1,041.66 toman/h ≈ 25,000/day ≈ 750,000/month. |

## 2. Deploy over HTTP

- Create: `POST /v1/projects {name, planID, platform, bundlePlanID:"basic", network}` → 200 `{projectID}`. A team needs a network first (`POST /v1/networks`), created automatically.
- Upload `POST /v2/projects/{app}/sources` (multipart) → `{sourceID: "<uuid>.tar.gz"}`; release `POST /v2/projects/{app}/releases` → 201 `{releaseID}`; Flask config goes in `platformConfig: {appModule, pythonVersion}`.
- Build logs: `GET /v2/releases/{id}/build-logs?since=` works as documented. A cached Flask build takes **~30 s**; the first one ~2 min (base image pull + pip).
- **Release state ≠ app health.** All three broken releases ended `READY` while the site returned 502/503. Health must come from our probe + app logs + events.

## 3. Real failures seen (gold for the demo)

| Release | What happened | Where it shows |
|---|---|---|
| v1 | **the platform's⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ own Flask platform crash-loops today:** it installs `gunicorn==20.1.0`, pip pulls the newest `setuptools` (84.2.1), which no longer has `pkg_resources` → `ModuleNotFoundError: No module named 'pkg_resources'`, `multirun: one or more of the provided commands ended abnormally`, site 503/502. Affects any fresh Flask deploy that doesn't pin setuptools. | app logs only |
| v2 | Fix: `setuptools<80` in requirements.txt (installed before gunicorn, so gunicorn keeps it) → next error `No module named 'dotenv'` inside gunicorn's worker boot → 502. | app logs only (build log clean, release READY) |
| v3 | Add `python-dotenv` → **200**, Persian page. | probe |

→ New catalogue rule (high confidence, real): signature `No module named 'pkg_resources'` + `gunicorn` in traceback → add `setuptools<80` to requirements.txt. Our Flask demo should keep this — it's a live, current the platform⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ bug our agent fixes and the platform's⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ console doesn't explain.

## 4. Runtime signals

| Endpoint | Live behaviour |
|---|---|
| `GET /v1/projects/{name}/applets` | **Always 500** (crash-looping and healthy). Unusable → drop from collectors. |
| `GET /v1/projects/{name}/events?start&end` | Works, but the shape differs from the docs: `{data:[{id, status, action, time (unix s, string), release, tag}]}` with statuses `create, start, kill, stop, die, destroy`. Crash loops = repeated `die`/`start` on one tag → **restart counter is derivable**. Empty while a container crash-loops before its first event. |
| `GET /v1/projects/{name}/metrics/summary` | `memoryUsage:[{value:[ts, bytes], applet}]` — **bytes**, one entry per container (`<projectId>.1.<uuid>-DEPLOY`); `cpuUsage` can be empty right after start. |
| `metrics/{memory,cpu}?since=` | **`since` is an absolute unix timestamp.** A look-back value (`since=1800`) → 428 "The maximum allowed value for since is 7 days." Memory in bytes, CPU as a fraction of a core. Each restart starts a new series (new applet id) → slope per series. |
| `GET /v2/projects/{app}/logs` | Lines are JSON strings `{"type":"stdout|stderr","_entry":"…"}`. Max **100 lines per call**; `releaseId=` filters to one release; `direction=backward&last_lines=true` returns the newest 100 first. Nginx access lines appear (`GET 502 <ip> "/" …`) → status codes parseable from logs on Flask. |
| `GET /v1/projects/{name}/releases?page&count` | Works: `tag`, `type` (`DEPLOY`, `ENV_UPDATE`), `state`, `sourceAvailable: true`, `createdAt`, `finishedAt`. |

## 5. Env vars

- `POST /v1/projects/update-envs` → 201 `{message:"success"}`. Accepts `{"key","value","encrypted":true}`.
- **Values come back in plain text, even when `encrypted: true`** → read-modify-write is safe (no masking), but secrets are readable through the API → redaction everywhere is mandatory.
- **An env update creates a new release** (`type: ENV_UPDATE`, no build): new container starts ~2 s after the call, the old one is killed ~15 s later → near-zero downtime. Whether it counts against the 20 deploys/day cap is unknown.

## 6. Rollback

- `POST /v2/projects/{app}/releases/{id}/rollback` and the `/v1` variant → **404**. No rollback endpoint despite `rollback: true` in the bundle (console-only feature).
- **Our rollback works:** a new release re-using the stored `sourceID` + stored release body → READY in **29 s**, site 200.
- A release body with `envs: {}` **does not wipe** existing env vars (they survived).
- `sourceAvailable: true` on all releases; how long sources are kept is still unknown (max 10 releases kept on basic).

## 7. Not tested yet (later steps)

- Whether the 20/day cap is per app or per account, and whether ENV_UPDATE counts.
- Metrics under load / OOM event text (with demo-shop v3).
- Domains (needs a domain), GitHub clone from a hosted box, MCP on Windows, console token deep link.
- Console baseline: the user times a manual console deploy for the video.

## 8. demo-shop dry run (2026-10-22, app `dastyar-demo-shop`, small-g2 + basic, Node)

Script: `liara/scripts/demo_dry_run.py` (our L1 client + a recording transport). Recordings: `liara/fixtures/demo/<label>/`
(redacted; `/v1/me` never recorded). Builds used: 4 (v1, v2, v3, rollback) + 3 env updates. App left running on v1.

| Step | the platform⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ release | What we saw |
|---|---|---|
| deploy v1 with `envs: {PAYMENT_API_URL}` in the release body | v1 DEPLOY, READY in 20 s | The app had no env vars yet, so this only looked like "added". **Correction (L3, live): a non-empty release-body `envs` REPLACES the whole list** — the v3 deploy with `SELF_LOAD_RPS`+`LEAK_MB` silently dropped `PAYMENT_API_URL`. Only `envs: {}` leaves vars alone; use `update-envs` (read-modify-write) for changes. For ~30 s after READY the site answered **503/502 from the platform's⁤⁤⁤⁤⁤⁤⁤⁤⁤⁤⁣ edge** with no app log lines at all. |
| remove `PAYMENT_API_URL` (judge-mode fault) | v2 ENV_UPDATE | `/checkout` 500 on every request, `/` and `/api/products` 200. Stack trace in app logs at `/app/server.js:30` → container code lives under `/app/`. |
| restore env, deploy v2 (coupon bug) | v3 ENV_UPDATE, v4 DEPLOY READY in 28 s | `/checkout` 500 with `TypeError: Cannot read properties of undefined (reading 'percent')` at `/app/server.js:37:34`; rest of the shop 200. |
| deploy v3 + `SELF_LOAD_RPS=0.5`, `LEAK_MB=4` | v5 DEPLOY READY in 25 s | RAM **2 → 467 MB in ~3.5 min (~2.4 MB/s)**, probe time up to 1.8 s, then event **`oom`** → `die` → `start` on the **same container** and RAM back to 41 MB (sawtooth). |
| rollback to v1 (redeploy stored body) | v6 DEPLOY READY in 29 s | The old leaking container kept serving and climbing for **~40 s after READY**, then was killed; new container flat at ~24 MB. |

**Consequences for L3 / verify**
- Event statuses seen live: `create, start, kill, stop, die, destroy, oom`. `oom_count()` added; P3 memory evidence = `oom` event + slope of the current container's memory series.
- Edge 503/502 without app log lines = "not routed yet / container starting", not an app error. App 500 = app log line with status 500 + a stack trace.
- Verify must wait for the **new container** (newest memory series / a `start` event of the new tag) and then a settle window; READY alone is not enough (old container serves ~40 s more; edge 503s ~30 s on a first deploy).
- Baseline latency from the laptop is 0.7–1.2 s even when healthy → TTFB is compared against a measured baseline, never an absolute threshold.
- Metrics have one point per ~20 s at best; a fresh container reports ~2 MB for the first point → ignore the first sample of a new series.
- Container path → local path: strip `/app/` (seen live for Node).

## 9. First deploy of app D through the assistant (2026-10-22 19:24–19:28 Tehran)

Run with `liara/scripts/first_deploy_live.py liara/demo-apps/app-d.zip dastyar-app-d` against the local backend (`--live`).

| t | event |
|---|---|
| 19:24:38 | analysis: Flask, `SHOP_NAME`, small-g2 749,995 toman/month, preflight `appModule: main:app` |
| 19:24:39 | create card approved (two taps) → 19:24:42 app created |
| 19:24:57 | build 1 READY (17 s); edge gave no answer, then the release logs showed `No module named 'pkg_resources'` at 19:25:33 → `setuptools<80` |
| 19:26:25 | build 2 READY (49 s); 502s; `ModuleNotFoundError: No module named 'dotenv'` at 19:27:01 → `python-dotenv` |
| 19:27:34 | build 3 READY (32 s); 502 until 19:28:04, first 200; second 200 at 19:28:25 → live |

Findings: crash loops show in the release's app logs ~30–40 s after READY; the edge answers 502 (not 503) while the
container crash-loops; first 200 came ~30 s after READY. The app is left running (~25k toman/day).
