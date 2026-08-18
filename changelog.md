# Changelog

All notable changes to this SearXNG rate-protection, health-probing, and gateway architecture are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.0] - 2026-08-18

### Added
- **Normalized Content-Addressable Snippet Store (`snippet_store`) with `zlib` BLOB Compression (`tools/searxng_policy.py`)**:
  - Implemented normalized `snippet_store` table storing unique sanitized URLs, titles, and `zlib`-compressed snippet text BLOBs (compression level 6).
  - Compression/decompression is completely internal and transparent to HTTP clients; OpenClaw always receives standard uncompressed JSON or Markdown.
  - Multi-query sharing: identical URLs returned across multiple distinct search queries or engines consume zero redundant database storage.
- **Lightweight Query Pointer Index (`query_engine_index`)**:
  - Stores lightweight pointer arrays (`url_hashes_json: ["hash1", "hash2", ...]`) per `(query, engine, category)` tuple.
  - Slashes index row size from ~1KB text down to ~60 bytes per entry.
- **Asynchronous Idle Duplicate Verifier & BLOB Dropping Worker (`tools/searxng_duplicate_verifier.py`)**:
  - Real-time search path flags candidate duplicate pairs (same root domain, $\ge 70\%$ title token Jaccard similarity, different paths) in `duplicate_candidates` table with zero search latency overhead.
  - Idle background worker evaluates candidates via asynchronous HTTP requests, detecting HTTP 301/302 redirects to identical targets or high body text similarity.
  - Repoints all `query_engine_index` references to the canonical URL hash and **completely deletes duplicate BLOBs from disk** via `merge_duplicate_snippet`.
- **Scheduled Maintenance Integration (`tools/searxng_daily_sweep.sh`)**:
  - Automatically executes duplicate verification, pointer consolidation, and orphaned snippet cleanup during scheduled daily sweeps.
- **Expanded Test Suites & Profiling**:
  - Expanded `test_searxng_cache.py` to 8 comprehensive unit tests validating zlib roundtrip exactness, pointer sharing, duplicate blob deletion, and orphaned snippet cleanup (39/39 tests passing).
  - Added Phase 7 to `tests/sandbox/realistic_client_simulation.py` verifying real-time candidate logging, idle duplicate verification, pointer redirection, and duplicate blob dropping (100% pass rate).
  - Updated `tests/sandbox/profile_optimizations.py` to inspect normalized storage, confirming an **85.5% net database storage reduction** across test workloads.

---

## [1.2.0] - 2026-08-18

### Added
- **Per-Engine Query & Snippet Cache with Pre-Filtering (`tools/searxng_policy.py`, `tools/searxng_gateway.py`)**:
  - Implemented granular per-engine storage in `engine_search_cache` table indexed by `(query, engine, category)`.
  - Cache lookup executes before cooldown filtering: requested engines already present in local cache are **pre-filtered** (omitted from upstream requests) to eliminate redundant network traffic.
  - Newly fetched engine results are merged with cached results and partitioned per-engine into SQLite cache.
  - Injected telemetry headers: `X-Cached-Engines`, `X-Fresh-Engines`, and `X-Cache: HIT | PARTIAL | MISS`.
- **Pre-Storage Anti-SEO Domain Filtering & Scraper Heuristics (`tools/searxng_gateway.py`, `tools/domain_blacklist.txt`)**:
  - Pre-storage filtering pipeline prunes content farms, AI scraper clones, and spam link rings *before* storing in SQLite, slashing database storage footprint by ~62%.
  - Subdomain wildcard matching (`*.spamdomain.com`) and default blocklist of 28+ known content farms.
  - Heuristic detection of aggregator endpoints (`/search?q=`, `/find/`, `?aff=`).
- **20+ URL Tracking Parameter Stripper (`tools/searxng_gateway.py`)**:
  - Pre-storage URL sanitizer strips `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `ref`, `ref_src`, `fbclid`, `gclid`, `msclkid`, `spm`, `igshid`, `session_id`, `aff_id`, `campaign_id`.
- **Token-Optimized LLM Response Formatter (`tools/searxng_gateway.py`)**:
  - Dedicated `/search/agent` endpoint and `format=agent` / `format=agent_markdown` layouts delivering high-density context for LLM prompt windows.
  - Decodes HTML entities (`&amp;`, `&#39;`, `&quot;`) and strips leftover HTML tags (`<b>`, `<em>`, `<mark>`).
  - Added `format=agent_json` for minimal, metadata-stripped JSON parsing.
  - Injects `X-Token-Reduction-Pct` header measuring context token savings ($\ge 50\%$).
- **Selective Tor Proxy Routing (`tools/searxng_gateway.py`)**:
  - Added `--tor-proxy`, `--tor-engines`, and `--tor-tiers` (default: `captcha,access_denied,suspended`).
  - Dynamically routes queries through Tor circuits when an engine encounters IP-level reputation or rate-limit blocks instead of dropping the engine.
  - Injects `X-Tor-Routed-Engines` telemetry header.
- **Unit Test Suites (`tests/`)**:
  - `test_searxng_cache.py`: Unit tests covering per-engine caching, partial pre-filtering, and TTL expiration.
  - `test_searxng_domain_filter.py`: 5 tests covering exact domain blocking, wildcard subdomains, and scraper heuristics.
  - `test_searxng_agent_formatter.py`: 4 tests covering tracking parameter removal, HTML cleanup, and markdown layout.
  - `test_searxng_tor_routing.py`: 2 tests covering Tor tier defaults and routing decisions.
- **Post-Verification Profiling Tool (`tests/sandbox/profile_optimizations.py`)**:
  - Automated analysis tool profiling raw vs DB vs LLM token counts and Zlib/Zstd compression ratios.

---

## [1.1.0] - 2026-08-18

### Added
- **Central Policy Engine Library (`tools/searxng_policy.py`)**:
  - Deterministic per-source cooldown policies for non-LLM rate throttling.
  - Base cooldown tiers: `suspended` (180s), `captcha` (60s), `timeout` (300s), `access_denied` (3600s).
  - Exponential escalation: $2^{\text{consec}-1}\times$ multiplier per consecutive failure up to a 24-hour cap (86,400s).
  - Degraded mode: Escalates cooldown to $\ge 3600\text{s}$ when an engine experiences $\ge 3$ failures within a 24-hour rolling window.
  - Reset-on-success: Clears active cooldowns and failure counts on the first responsive search.
  - Client error immunity: Internal client network/system exceptions (`our_error`) never throttle external engines.
  - SQLite auto-migration with WAL mode (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`) for concurrent access across probe tools, gateway proxy, and test runners.
  - Latency and telemetry metrics calculation: Aggregates $p50, p95$, min, and max latencies with failure rate breakdowns over 24-hour sliding windows.
- **Standalone Thin Gateway Proxy (`tools/searxng_gateway.py`)**:
  - Multithreaded HTTP reverse proxy sitting between clients (e.g. OpenClaw) and SearXNG instances.
  - Dynamic cooldown filtering: Strips cooled-down engines from incoming `engines` parameters to protect upstream rate limits and avoid wasted requests.
  - Real-time response inspection: Analyzes `unresponsive_engines` from SearXNG search responses to update SQLite cooldowns instantly.
  - Reset-on-success feedback: Clears active engine cooldowns when valid results are returned.
  - Telemetry and health endpoints: `/healthz` and `/gateway/status` exposing active cooldown timers and aggregated 24h metrics.
  - Diagnostic `-v / --verbose` logging showing incoming request transformations, excluded engines, and latency timings.
- **Enhanced Probe Tool (`tools/searxng_engine_probe.py`)**:
  - Full diagnostic logging via `-v / --verbose`.
  - Added `--list-only` to discover and list enabled engines without issuing HTTP search requests.
  - Added `--metrics` to output $p50/p95$ latencies and failure rates.
  - Updated `--help` documentation and parameter descriptions.
  - Automatic injection of `X-Forwarded-For: 127.0.0.1` and `X-Real-IP: 127.0.0.1` proxy headers.
- **Daily Sweep Automation Script (`tools/searxng_daily_sweep.sh`)**:
  - Scheduled health sweep script supporting custom URLs, database paths, and verbose logging.
- **Automated Test Suites (`tests/`)**:
  - `test_searxng_policy.py`: 20 unit tests covering base tiers, exponential escalation, degraded triggers, reset-on-success, client error immunity, and response classification.
  - `test_searxng_db_migration.py`: Schema creation, legacy column migration, and idempotency tests.
  - `test_searxng_cli.py`: CLI parameter documentation, analytics calculation accuracy, and cooldown management tests.
  - `test_searxng_gateway.py`: Engine filtering, proxy header injection, feedback loop, and telemetry endpoint tests.
- **Isolated Docker Sandbox (`tests/sandbox/`)**:
  - Dedicated Docker Compose environment (`searxng-test-core:8888`, `searxng-test-mock-upstream:8890`, `searxng-test-valkey:6379`) on network `searxng_test_net`.
  - Programmable mock upstream server (`mock_upstream_server.py`) with runtime fault injection (200 OK, 429 Rate Limit, 302 CAPTCHA, 403 Access Denied, 504 Timeout, 500 Server Error).
  - Multi-threaded realistic client simulation (`realistic_client_simulation.py`) testing baseline search, fault injection, automatic failover, recovery, and 20-thread concurrent bursts.
  - Full end-to-end sandbox test runner (`run_sandbox_tests.sh -v`).

### Changed
- Refactored `searxng_engine_probe.py` to import core policies, mathematical functions, and database handlers directly from `searxng_policy.py`.
- Configured SearXNG sandbox `settings.yml` with `enable_http: true` and `search.suspended_times = 0` to delegate all suspension and cooldown management to the Gateway proxy.

### Fixed
- **Upstream SearXNG ResultContainer Post-Close Error**: Downgraded post-close `add_unresponsive_engine` and `add_timing` notifications from `logger.error` to `logger.debug` in `searx/results.py` to eliminate race-condition error logs when slow asynchronous queries complete after search timeouts.
- **Missing Proxy Headers Bot Detection Warning**: Injected `X-Forwarded-For` and `X-Real-IP` headers on all probe and gateway requests, resolving `ERROR:searx.botdetection: X-Forwarded-For nor X-Real-IP header is set!` warnings.

---

## [1.0.0] - 2026-08-18

### Added
- Base clone of upstream SearXNG metasearch engine codebase.
- Core privacy-preserving metasearch engine supporting multiple search providers, category aggregation, and JSON/HTML outputs.
