# SearXNG Gateway & Rate Protection Project

**Identity:** A specialized fork of [SearXNG](https://github.com/searxng/searxng) enhanced with an intelligent reverse-proxy gateway, deterministic rate-protection policies, normalized `zlib` caching, anti-SEO domain pruning, and token-optimized LLM context formatting for [OpenClaw](https://github.com/openclaw/openclaw) agents.

---

## 🎯 Purpose & Capabilities

This fork extends upstream SearXNG with a robust gateway layer sitting between LLM search clients and SearXNG metasearch instances:

1. **Deterministic Rate Protection & Cooldown Policy (`tools/searxng_policy.py`)**:
   - Automated non-LLM throttling based on upstream engine responses: `suspended` (180s), `captcha` (60s), `timeout` (300s), `access_denied` (3600s).
   - Exponential failure escalation ($2^{\text{consec}-1}\times$), degraded mode triggers ($\ge 3$ failures $\implies \ge 3600\text{s}$), reset-on-success, and client error immunity.

2. **Per-Engine Cache & Upstream Pre-Filtering (`tools/searxng_gateway.py`)**:
   - Caches search snippets on a granular `(query, engine, category)` basis.
   - Pre-filters cached engines before issuing upstream requests, slashing cache-hit latency to **< 2ms** and protecting engine quotas.

3. **Normalized Content-Addressable Storage with `zlib` Compression**:
   - Two-table normalized relational layout: `snippet_store` (unique URLs + `zlib`-compressed text BLOBs) and `query_engine_index` (lightweight SHA-256 pointer arrays).
   - Consumes zero redundant space across multi-engine searches, delivering an **85.5% net database storage reduction**.

4. **Pre-Storage Anti-SEO Filtering & 20+ URL Tracker Stripping**:
   - Prunes content farms (`domain_blacklist.txt` with wildcard subdomains), scraper mirrors, and stripping of 20+ URL tracking tags (`utm_*`, `ref`, `fbclid`, `gclid`, etc.) *before* database insertion.

5. **Token-Optimized LLM Response Formatter (`/search/agent`)**:
   - Produces high-density Markdown and compact JSON (`format=agent_json`) with HTML entity decoding and markup cleanup, saving **27% to 70% prompt tokens**.

6. **Selective Tor Proxy Routing**:
   - Dynamically promotes engines to Tor SOCKS5 circuits when encountering CAPTCHA, 403 Access Denied, or Suspension tiers.

7. **Asynchronous Idle Duplicate Verification & BLOB Dropping (`tools/searxng_duplicate_verifier.py`)**:
   - Zero-latency candidate duplicate logging during search queries.
   - Background sweep verifies HTTP redirects and DOM similarity, repoints query pointers, and completely deletes duplicate BLOBs from disk.

8. **Upstream Fixes**:
   - Resolved asynchronous ResultContainer race-condition error logging in `searx/results.py`.

---

## 🛠 Core Stack

- **Base Engine:** Python 3.12+ / SearXNG Core
- **Gateway & Policy Layer:** Python HTTP/REST Reverse Proxy (`tools/searxng_gateway.py`)
- **Database Engine:** SQLite (WAL mode) with normalized tables and `zlib` BLOB compression (`tools/searxng_engine.db`)
- **Testing & Verification:** Python `unittest` suite (39 tests) and isolated Docker Compose sandbox