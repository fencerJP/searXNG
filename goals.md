# searXNG project — goals

Long-term project: a safe, monitored policy/cooldown layer between OpenClaw's
`web_search` and the local SearXNG instance.

> **Note on this checkout:** this folder (`workspace/projects/searXNG/`) is a
> **source clone** of [github.com/searxng/searxng](https://github.com/searxng/searxng)
> for reading the code and building/patching against. The **live** SearXNG that
> OpenClaw actually queries is the separate **Docker compose** install at
> `~/searxng/` (config `~/searxng/core-config/settings.yml`), on `localhost:8082`.
> Do not confuse the two.

## Goals

1. **Track site (engine) connectivity.**
   - Probe every enabled SearXNG engine in isolation (`/search?engines=<name>`),
     N in parallel, and record connectivity + time-to-results.
   - Store the history in a small SQLite db (`tools/searxng_engine.db`) so
     repeated runs reveal failure rates and latency p50/p95 per engine over time.

2. **Implement cooldowns / per-source rate protection.**
   - Deterministic, **non-LLM** throttling policy (the script decides, not the agent).
   - Failure types derived from SearXNG's own reason strings:
     `suspended`, `captcha`, `access_denied`, `timeout`.
   - Cooldown tiers: `suspended` 180s, `captcha` 60s, `timeout` 300s,
     `access_denied` 3600s; `degraded` 3600s activated at **3 consecutive
     failures**; all escalate x2 per consecutive failure, capped at 24h.
   - Reset rules: **reset-on-success** (first `ok`/`ok_no_results` clears) and a
     **24h sliding window** for counting failures. Never throttle a source on
     our-own client errors.
   - Open decision (in progress): enforce coarse via scheduled SearXNG
     engine enable/disable, or a thin gateway in front of SearXNG that applies
     the policy per live request. OpenClaw wires `tools.web.search.provider`
     → `searxng` → `baseUrl http://localhost:8082`.

3. **Daily tests.**
   - A scheduled daily sweep re-checks engine health, applies cooldown policy,
     detects recoveries, and (depending on the enforcement approach) updates
     which engines SearXNG enables.
   - Cooldown granularity must match the enforcement cadence — if we only sweep
     daily, second-scale cooldowns are meaningless.

## Status / open threads

- Engine config already edited on the live instance (2026-08-10): JSON format
  enabled, IPv6 disabled in-container, `outgoing.request_timeout` raised;
  `brave*`, `google cse*`, `startpage*` disabled (no API keys / bot-blocked).
- Deciding between **scheduled-config controller** vs **thin gateway** for
  enforcing the cooldown policy at request time.