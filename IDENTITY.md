# searXNG Policy Layer Project

**One-line:** Safe, monitored policy/cooldown layer between OpenClaw's web_search and local SearXNG.

## What it does
- Tracks search engine (site) connectivity in parallel probes with latency metrics stored in SQLite
- Implements deterministic cooldowns per source based on failure types: `suspended` (180s), `captcha` (60s), `timeout` (300s), `access_denied`/escalating tiered rates up to 24h cap
- Provides daily health sweeps with recovery detection and optional SearXNG config updates

## Core Stack
- **Backend:** Python web_search integration layer
- **Database:** SQLite (`tools/searxng_engine.db`) for engine connectivity history
- **Target:** Local SearXNG instance at `localhost:8082` (separate from source clone)

## Status (as of 2026-08-14)
**In Progress - Phase 5b decision pending.** Engine config updated on live instance with JSON format, IPv6 disabled. Deciding between scheduled-config controller vs thin gateway for enforcing cooldown policy at request time.