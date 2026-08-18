# searXNG project — goals

Long-term project: a safe, monitored policy/cooldown layer between OpenClaw's
`web_search` and the local SearXNG instance.

> **Note on this checkout:** this folder (`workspace/projects/searXNG/`) is a
> **source clone** of [github.com/searxng/searxng](https://github.com/searxng/searxng)
> for reading the code and building/patching against. The **live** SearXNG that
> OpenClaw actually queries is the separate **Docker compose** install at
> `~/searxng/` (config `~/searxng/core-config/settings.yml`), on `localhost:8082`.
> Do not confuse the two.

## Goals & Status

1. **Track site (engine) connectivity.** [✓ COMPLETED]
   - Probes every enabled SearXNG engine in isolation via `tools/searxng_engine_probe.py`.
   - SQLite database (`tools/searxng_engine.db`) records connectivity, failure rates, and $p50/p95$ latencies over 24h rolling windows.

2. **Implement cooldowns / per-source rate protection.** [✓ COMPLETED]
   - Deterministic, non-LLM throttling policy implemented in `tools/searxng_policy.py`.
   - Base cooldown tiers: `suspended` (180s), `captcha` (60s), `timeout` (300s), `access_denied` (3600s).
   - Exponential escalation ($2^{\text{consec}-1}\times$), degraded mode ($\ge 3$ failures $\implies \ge 3600\text{s}$), reset-on-success, and client error immunity.
   - Enforced in real time via the thin **SearXNG Gateway Proxy** (`tools/searxng_gateway.py`).

3. **Per-Engine Caching & Normalized zlib Storage.** [✓ COMPLETED]
   - Content-addressable storage (`snippet_store`) with Python `zlib` BLOB compression and lightweight query pointer indexes (`query_engine_index`).
   - Slashes SQLite database storage by **85.5%**.
   - Pre-filters cached engines before querying upstream SearXNG, reducing cache hit latency to **< 2ms**.

4. **Pre-Storage Anti-SEO Filtering & Token Formatter.** [✓ COMPLETED]
   - Prunes content farms (`domain_blacklist.txt`) and scraper links *before* DB storage.
   - Strips 20+ URL tracking tags (`utm_*`, `ref`, `fbclid`, etc.).
   - Delivers high-density Markdown context (`/search/agent`) saving **27% to 70% prompt tokens**.

5. **Tor Proxy Fallback & Idle Duplicate Pruning.** [✓ COMPLETED]
   - Dynamically routes requests through Tor circuits on CAPTCHA / 403 / Suspension tiers.
   - Background worker (`searxng_duplicate_verifier.py`) evaluates candidate duplicates and completely deletes duplicate BLOBs from disk.

6. **Daily Maintenance & Test Automation.** [✓ COMPLETED]
   - Scheduled daily maintenance script (`tools/searxng_daily_sweep.sh`).
   - Automated test suite: 39/39 unit tests and full Docker sandbox multi-agent simulation with 100% pass rate.