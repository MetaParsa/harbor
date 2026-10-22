# demo-shop — the SRE demo app (not deployed itself; each version folder is)

Three real releases of one tiny Persian shop (Node, zero dependencies, port 3000, logs `METHOD path status ms`
for every request). Faults ship as real releases, never as hidden switches — except the env one, which is how
such outages really happen.

| Version | What changed | Fault it carries | What the agent should find |
|---|---|---|---|
| v1 | healthy shop | `/checkout` throws `TypeError [ERR_INVALID_URL]` if `PAYMENT_API_URL` is unset | P2 config: error starts right after an `ENV_UPDATE` release, `/` is fine, `/checkout` 500 → set the env (T2) |
| v2 | coupon codes | no coupon → `TypeError: Cannot read properties of undefined (reading 'percent')` at `server.js` checkout → `/checkout` 500 for most shoppers | P1 regression: errors start within minutes of the v2 release; stack trace points to the line → hand to the coding agent (fix: `coupons[code]?.percent ?? 0`) or roll back |
| v3 | coupon fix + `/resize` image previews | the preview cache never evicts: each `/resize` keeps `LEAK_MB` (default 6) → RAM climbs, responses slow, then OOM restart (sawtooth) | P3 memory leak: RAM slope since last start, die/start events, CPU normal → roll back (T2) rather than resize (T3, only delays it) |

Env knobs (v3): `LEAK_MB` (MB kept per `/resize` request), `SELF_LOAD_RPS` (the server calls `/resize` on itself, so
metrics move while filming without a laptop running load). External load: `bash liara/scripts/load.sh <url> [rps] [seconds]`.

Deploy (platform `node`, port 3000): each `vN/` folder is a complete project.
