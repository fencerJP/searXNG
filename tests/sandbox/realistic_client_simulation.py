#!/usr/bin/env python3
"""realistic_client_simulation.py — realistic OpenClaw search agent client simulation against the enhanced gateway."""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
from searxng_policy import apply_cooldown, get_db_connection

GATEWAY_URL = "http://127.0.0.1:8880"
MOCK_CONTROL_URL = "http://127.0.0.1:8890/mock/control"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_engine.db")


def set_mock_behavior(engine: str, behavior: str, verbose: bool = False):
    """Dynamically configure mock upstream engine behavior."""
    payload = {"engine": engine, "behavior": behavior}
    r = requests.post(MOCK_CONTROL_URL, json=payload, timeout=5)
    r.raise_for_status()
    if verbose:
        print(f"[TEST_CLIENT] Set mock upstream engine '{engine}' -> behavior='{behavior}'")


def reset_all_mocks(verbose: bool = False):
    r = requests.post(MOCK_CONTROL_URL, json={"reset_all": True}, timeout=5)
    r.raise_for_status()
    if verbose:
        print("[TEST_CLIENT] Reset all mock upstream engines to 'ok'")


def execute_search(query: str, engines: str = "", categories: str = "", format: str = "json", fresh: bool = False, verbose: bool = False):
    """Execute search query through gateway proxy."""
    params = {"q": query, "format": format}
    if engines:
        params["engines"] = engines
    if categories:
        params["categories"] = categories
    if fresh:
        params["fresh"] = "1"

    t0 = time.monotonic()
    r = requests.get(f"{GATEWAY_URL}/search", params=params, timeout=10)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if verbose:
        print(
            f"[TEST_CLIENT] Search query='{query}', engines='{engines}', format='{format}' -> status={r.status_code}, "
            f"time={elapsed_ms}ms, cache={r.headers.get('X-Cache', 'n/a')}, "
            f"cached_eng={r.headers.get('X-Cached-Engines', 'none')}, fresh_eng={r.headers.get('X-Fresh-Engines', 'none')}"
        )
    return r, elapsed_ms


def run_simulation(verbose: bool = False):
    print("================================================================================")
    print("      REALISTIC CLIENT WORKLOAD & COOLDOWN INTEGRATION SIMULATION")
    print("================================================================================")

    # 1. Healthcheck gateway & mock upstream
    print("\n[Phase 1] Verifying test environment connectivity...")
    r_gw = requests.get(f"{GATEWAY_URL}/healthz", timeout=5)
    assert r_gw.status_code == 200, f"Gateway healthcheck failed: {r_gw.text}"
    reset_all_mocks(verbose)
    print("  ✓ Gateway and Mock Upstream are healthy.")

    # 2. Per-Engine Cache & Pre-Filtering
    print("\n[Phase 2] Per-Engine Caching & Pre-Filtering...")
    # Step 2a: Query mock google and mock bing -> Fresh (X-Cache: MISS)
    r1, t1 = execute_search("paxos consensus", engines="mock google,mock bing", verbose=verbose)
    assert r1.status_code == 200
    assert r1.headers.get("X-Cache") == "MISS", f"Expected X-Cache: MISS, got {r1.headers.get('X-Cache')}"
    assert "mock google" in r1.headers.get("X-Fresh-Engines", "")
    assert "mock bing" in r1.headers.get("X-Fresh-Engines", "")
    print(f"  ✓ Initial multi-engine query completed and stored in per-engine cache (took {t1}ms).")

    # Step 2b: Query mock google + mock duckduckgo -> Pre-filters mock google! (X-Cache: PARTIAL)
    r2, t2 = execute_search("paxos consensus", engines="mock google,mock duckduckgo", verbose=verbose)
    assert r2.status_code == 200
    assert r2.headers.get("X-Cache") == "PARTIAL", f"Expected X-Cache: PARTIAL, got {r2.headers.get('X-Cache')}"
    assert "mock google" in r2.headers.get("X-Cached-Engines", ""), "mock google should be pre-filtered from cache"
    assert "mock duckduckgo" in r2.headers.get("X-Fresh-Engines", ""), "mock duckduckgo should be queried fresh"
    print(f"  ✓ Partial cache hit verified: mock google pre-filtered, mock duckduckgo fetched fresh (took {t2}ms).")

    # Step 2c: Query mock google alone -> 100% Cache HIT (< 5ms)
    r3, t3 = execute_search("paxos consensus", engines="mock google", verbose=verbose)
    assert r3.status_code == 200
    assert r3.headers.get("X-Cache") == "HIT", f"Expected X-Cache: HIT, got {r3.headers.get('X-Cache')}"
    assert t3 <= 25, f"Cache HIT should be ultra-fast (<25ms), took {t3}ms"
    print(f"  ✓ 100% Cache HIT verified for mock google (took {t3}ms).")

    # 3. Pre-Storage Anti-SEO Domain Filtering & Tracker Stripping
    print("\n[Phase 3] Anti-SEO Filtering & URL Tracking Stripper...")
    set_mock_behavior("mock duckduckgo", "with_spam", verbose)
    r_spam, _ = execute_search("python concurrency patterns", engines="mock duckduckgo", fresh=True, verbose=verbose)
    assert r_spam.status_code == 200
    spam_data = r_spam.json()
    results = spam_data.get("results", [])
    
    # Assert spam domain geeksforgeeks.org was pruned
    urls = [res["url"] for res in results]
    assert not any("geeksforgeeks.org" in u for u in urls), f"geeksforgeeks.org should have been pruned: {urls}"
    assert not any("scraper-clone.com" in u for u in urls), f"scraper-clone.com should have been pruned: {urls}"
    # Assert legitimate result remains
    assert any("docs.example.org" in u for u in urls), f"Legitimate doc should be preserved: {urls}"
    # Assert tracking params were stripped from URL
    for u in urls:
        assert "utm_source" not in u, f"Tracking parameter utm_source should be stripped: {u}"
    print(f"  ✓ Anti-SEO filter pruned spam domains and stripped tracking tags (clean results: {len(results)}).")

    # 4. Token-Optimized LLM Response Formatting
    print("\n[Phase 4] Token-Optimized LLM Response Formatter...")
    # Query /search/agent format
    r_agent = requests.get(f"{GATEWAY_URL}/search/agent", params={"q": "distributed raft consensus", "engines": "mock brave"}, timeout=10)
    assert r_agent.status_code == 200
    md_text = r_agent.text
    assert "# Search Results: distributed raft consensus" in md_text
    assert "### [1]" in md_text
    assert "- **URL**:" in md_text
    assert "- **Snippet**:" in md_text
    reduction_hdr = r_agent.headers.get("X-Token-Reduction-Pct", "0%")
    print(f"  ✓ Formatted LLM markdown output verified (Token reduction: {reduction_hdr}).")

    # 5. Fault Injection & Dynamic Tor Tier Fallback Routing
    print("\n[Phase 5] Fault Injection & Dynamic Tor Tier Routing...")
    # Reset mocks then inject captcha on mock google
    reset_all_mocks(verbose)
    set_mock_behavior("mock google", "captcha", verbose)
    r_fail, _ = execute_search("fault injection test", engines="mock google", fresh=True, verbose=verbose)
    assert r_fail.status_code == 200
    
    # Verify mock google is in captcha cooldown
    r_stat = requests.get(f"{GATEWAY_URL}/gateway/status", timeout=5).json()
    active_cds = r_stat.get("active_cooldowns", {})
    assert "mock google" in active_cds, f"mock google should be in active cooldowns: {active_cds}"
    print(f"  ✓ Captured CAPTCHA fault on 'mock google': active cooldown={active_cds['mock google']['remaining_seconds']}s")

    # When gateway is configured with Tor tiers, subsequent queries for mock google route via Tor
    r_retry, _ = execute_search("retry query during captcha", engines="mock google", fresh=True, verbose=verbose)
    assert r_retry.status_code == 200
    tor_hdr = r_retry.headers.get("X-Tor-Routed-Engines", "")
    assert "mock google" in tor_hdr, f"Expected mock google in X-Tor-Routed-Engines, got '{tor_hdr}'"
    print(f"  ✓ Verified Tor fallback routing for 'mock google' (X-Tor-Routed-Engines: '{tor_hdr}').")

    # 6. Multi-threaded Agent Burst Workload
    print("\n[Phase 6] Multi-Agent Burst Simulation (20 concurrent queries)...")
    queries = [
        ("compiler design", "mock duckduckgo"),
        ("operating system kernels", "mock duckduckgo"),
        ("vector databases", "mock duckduckgo"),
        ("distributed consensus raft", "mock duckduckgo"),
        ("paxos algorithms", "mock duckduckgo"),
        ("memory safety rust", "mock duckduckgo"),
        ("python gil removal", "mock duckduckgo"),
        ("large language model serving", "mock brave"),
        ("flash attention 3", "mock brave"),
        ("quantization gguf", "mock duckduckgo"),
        ("kv cache optimization", "mock duckduckgo"),
        ("speculative decoding", "mock duckduckgo"),
        ("fast inference rocm", "mock duckduckgo"),
        ("linux io_uring tutorial", "mock duckduckgo"),
        ("ebpf networking filters", "mock duckduckgo"),
        ("sqlite wal mode performance", "mock duckduckgo"),
        ("redis vs valkey benchmarks", "mock duckduckgo"),
        ("docker bridge networking", "mock duckduckgo"),
        ("http3 quic protocols", "mock brave"),
        ("openclaw agent architecture", "mock duckduckgo"),
    ]

    t_start = time.monotonic()
    latencies = []
    success_count = 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        futs = {
            executor.submit(execute_search, q, eng, "", "json", False, verbose): (q, eng)
            for q, eng in queries
        }
        for fut in as_completed(futs):
            q, eng = futs[fut]
            try:
                res, lat = fut.result()
                if res.status_code == 200:
                    success_count += 1
                    latencies.append(lat)
            except Exception as e:
                print(f"  [ERROR] Burst query failed for '{q}': {e}")

    total_time = time.monotonic() - t_start
    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.5)]
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
    print(f"  ✓ Processed {success_count}/{len(queries)} queries in {total_time:.2f}s.")
    print(f"  ✓ Latency percentiles: min={latencies[0]}ms, p50={p50}ms, p95={p95}ms, max={latencies[-1]}ms.")
    assert success_count == len(queries), f"Expected 100% success rate, got {success_count}/{len(queries)}"

    # 7. Idle Background Duplicate Verification & Blob Pruning
    print("\n[Phase 7] Idle Duplicate Verification & BLOB Pruning...")
    con = get_db_connection(DB_PATH)
    from searxng_policy import log_duplicate_candidate, store_engine_cached_results
    snip_c = {"title": "Asyncio Docs Guide", "url": "https://docs.python.org/3/library/asyncio.html", "content": "Asyncio content", "engine": "mock duckduckgo"}
    snip_d = {"title": "Asyncio Docs Index", "url": "https://docs.python.org/3/library/asyncio/index.html", "content": "Asyncio content", "engine": "mock brave"}
    store_engine_cached_results(con, "asyncio core test", "mock duckduckgo", "", [snip_c])
    store_engine_cached_results(con, "asyncio core test", "mock brave", "", [snip_d])
    log_duplicate_candidate(con, snip_c["url"], snip_d["url"], "docs.python.org", 0.95)
    con.close()

    from searxng_duplicate_verifier import run_verifier
    verifier_stats = run_verifier(DB_PATH, limit=100, mock_mode=True, verbose=verbose)
    print(f"  ✓ Idle duplicate verifier run completed: {verifier_stats}")
    assert verifier_stats["confirmed_duplicates"] >= 1, f"Expected confirmed duplicate: {verifier_stats}"
    assert verifier_stats["blobs_dropped"] >= 1, f"Expected blob dropped: {verifier_stats}"

    # Verify status reflects accurate counts
    r_stat_final = requests.get(f"{GATEWAY_URL}/gateway/status", timeout=5).json()
    print(f"  ✓ Gateway final status: cached_queries={r_stat_final.get('cached_queries_count')}, unique_snippets={r_stat_final.get('unique_snippets_count')}")

    print("\n================================================================================")
    print("      ✓ ALL REALISTIC CLIENT SIMULATION TESTS COMPLETED SUCCESSFULLY")
    print("================================================================================")


def main():
    ap = argparse.ArgumentParser(description="Realistic Client Search Simulation")
    ap.add_argument("-v", "--verbose", action="store_true", help="Enable verbose tracing")
    args = ap.parse_args()

    try:
        run_simulation(verbose=args.verbose)
    except Exception as e:
        print(f"\n[FATAL] Simulation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
