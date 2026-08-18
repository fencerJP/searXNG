#!/usr/bin/env python3
"""searxng_engine_probe.py — probe SearXNG engines for connectivity + time-to-results,
with a deterministic per-source cooldown policy (no LLM in the throttling decisions).

Tests each engine in isolation via SearXNG's `/search?engines=<name>` API, runs N
engines in parallel, and records per-attempt rows in a small SQLite db so repeated
runs reveal flakiness (failure rate / latency over time).

Usage:
  python3 tools/searxng_engine_probe.py                      # full sweep, ~5 parallel
  python3 tools/searxng_engine_probe.py -v                   # verbose diagnostic logging
  python3 tools/searxng_engine_probe.py --limit 8
  python3 tools/searxng_engine_probe.py --categories general,news
  python3 tools/searxng_engine_probe.py --parallel 5 --run myrun
  python3 tools/searxng_engine_probe.py --list-only          # list enabled engines and exit
  python3 tools/searxng_engine_probe.py --status             # show current cooldowns
  python3 tools/searxng_engine_probe.py --status -v          # verbose cooldown history
  python3 tools/searxng_engine_probe.py --metrics            # show p50/p95 latency and failure rates
  python3 tools/searxng_engine_probe.py --metrics -v          # verbose error breakdown per engine
  python3 tools/searxng_engine_probe.py --clear-cooldowns    # reset all cooldowns
"""

import argparse
import datetime
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from searxng_policy import (
    WINDOW_HOURS,
    apply_cooldown,
    classify,
    compute_engine_metrics,
    get_db_connection,
    init_db,
    normalize_cooldown,
    record_probe_result,
)

DEFAULT_SEARXNG = "http://localhost:8082"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "searxng_engine.db")
DEFAULT_QUERY = "openclaw"


def get_enabled_engines(base_url, categories=None, verbose=False):
    headers = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
    if verbose:
        print(f"[DEBUG] Fetching engine configuration from {base_url}/config with headers: {headers}")
    r = requests.get(base_url + "/config", headers=headers, timeout=20)
    r.raise_for_status()
    engs = [e for e in r.json().get("engines", []) if e.get("enabled")]
    if categories:
        cats = set(c.strip() for c in categories.split(",") if c.strip())
        engs = [e for e in engs if set(e.get("categories") or []) & cats]
    if verbose:
        print(f"[DEBUG] Discovered {len(engs)} enabled engines matching category filter: {categories or 'ALL'}")
    return engs


def probe_one(engine, query, base_url, verbose=False):
    """Test a single engine. Returns a row dict (state classified, no cooldown logic)."""
    name = engine["name"]
    params = {"q": query, "engines": name, "format": "json", "safesearch": "0", "language": "en"}
    headers = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
    t0 = time.monotonic()
    http_status = None
    if verbose:
        print(f"[DEBUG] -> PROBE GET {base_url}/search?engines={name}&q={query}")
    try:
        r = requests.get(base_url + "/search", params=params, headers=headers, timeout=30)
        http_status = r.status_code
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if http_status != 200:
            state, rc, reason, detail = "error", 0, f"http_{http_status}", ""
        else:
            data = r.json()
            state, rc, reason, detail = classify(name, data)
    except requests.exceptions.Timeout:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        state, http_status, rc, reason, detail = "timeout", http_status, 0, "request_timeout", ""
    except Exception as e:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        state, http_status, rc, reason, detail = "our_error", http_status, 0, "exception", str(e)[:200]

    if verbose:
        print(
            f"[DEBUG] <- PROBE {name}: status={http_status}, state={state}, results={rc}, time={elapsed_ms}ms, reason={reason or 'none'}"
        )

    return {
        "engine": name,
        "categories": ",".join(engine.get("categories") or []),
        "query": query,
        "http_status": http_status,
        "state": state,
        "result_count": rc,
        "elapsed_ms": elapsed_ms,
        "reason": reason,
        "note": detail,
        "skipped": False,
        "cooldown_seconds": None,
    }


def print_status(con, verbose=False):
    now = time.time()
    rows = con.execute(
        """SELECT engine, last_type, fail_count, last_failure_at, cooldown_until
           FROM cooldowns WHERE cooldown_until IS NOT NULL OR fail_count>0
           ORDER BY cooldown_until DESC"""
    ).fetchall()
    if not rows:
        print("no cooldowns set")
        return
    if verbose:
        print(
            f"{'engine':28s} {'type':14s} {'fails':>5} {'cooldown_left':>14} {'last_failure':>20} {'cooldown_until':>20}"
        )
        for engine, lt, fc, lfa, cu in rows:
            left = "" if cu is None else f"{max(0,int(cu-now))}s"
            lfa_str = datetime.datetime.fromtimestamp(lfa).strftime("%Y-%m-%d %H:%M:%S") if lfa else "n/a"
            cu_str = datetime.datetime.fromtimestamp(cu).strftime("%Y-%m-%d %H:%M:%S") if cu else "n/a"
            print(f"{engine:28s} {str(lt):14s} {fc:>5} {left:>14} {lfa_str:>20} {cu_str:>20}")
    else:
        print(f"{'engine':28s} {'type':14s} {'fails':>5} {'cooldown_left':>14}")
        for engine, lt, fc, lfa, cu in rows:
            left = "" if cu is None else f"{max(0,int(cu-now))}s"
            print(f"{engine:28s} {str(lt):14s} {fc:>5} {left:>14}")


def print_metrics(con, hours=WINDOW_HOURS, verbose=False):
    metrics_list = compute_engine_metrics(con, hours=hours)
    if not metrics_list:
        print(f"No probe data in the last {hours} hours.")
        return
    print(f"\n================ ENGINE METRICS (Last {hours}h) ================")
    print(f"{'engine':28s} {'probes':>7} {'ok':>6} {'fails':>6} {'fail_rate':>10} {'p50(ms)':>8} {'p95(ms)':>8}")
    for m in metrics_list:
        p50_s = f"{m['p50_ms']}ms" if m["p50_ms"] is not None else "n/a"
        p95_s = f"{m['p95_ms']}ms" if m["p95_ms"] is not None else "n/a"
        fail_pct = f"{m['fail_rate']:.1f}%"
        print(f"{m['engine']:28s} {m['total']:>7} {m['ok_count']:>6} {m['fail_count']:>6} {fail_pct:>10} {p50_s:>8} {p95_s:>8}")

        if verbose and m["reasons"]:
            for r in m["reasons"]:
                print(f"    └── [fail breakdown] state={r['state']:12s} count={r['count']:>2} reason={r['reason'] or 'none'}")


def main():
    ap = argparse.ArgumentParser(
        description="Probe SearXNG engines for connectivity and latency with deterministic cooldown policy enforcement.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="Enable verbose diagnostic and debugging output")
    ap.add_argument("--searxng", default=DEFAULT_SEARXNG, help="Base URL of target SearXNG instance")
    ap.add_argument("--db", default=DEFAULT_DB, help="Path to SQLite results and cooldown database")
    ap.add_argument("--query", default=DEFAULT_QUERY, help="Search query string used for probing")
    ap.add_argument("--parallel", type=int, default=5, help="Number of concurrent worker threads for probing")
    ap.add_argument("--limit", type=int, default=None, help="Maximum number of engines to probe in this run")
    ap.add_argument("--categories", default=None, help="Comma-separated category filter (e.g. 'general,news')")
    ap.add_argument("--run", default=datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), help="Custom run identifier for database tracking")
    ap.add_argument("--list-only", action="store_true", help="List discovered enabled engines matching filter and exit without probing")
    ap.add_argument("--status", action="store_true", help="Show active cooldowns and failure counts, then exit")
    ap.add_argument("--metrics", action="store_true", help="Show latency percentiles (p50/p95) and failure rates over 24h, then exit")
    ap.add_argument("--clear-cooldowns", action="store_true", help="Clear all engine cooldown entries from the database and exit")
    ap.add_argument("--no-cooldown", dest="cooldown", action="store_false", help="Disable cooldown checks and updates during probing")
    ap.set_defaults(cooldown=True)
    args = ap.parse_args()

    con = get_db_connection(args.db)
    init_db(con, verbose=args.verbose)

    if args.clear_cooldowns:
        con.execute("DELETE FROM cooldowns")
        con.commit()
        print("cooldowns cleared")
        con.close()
        return
    if args.status:
        print_status(con, verbose=args.verbose)
        con.close()
        return
    if args.metrics:
        print_metrics(con, verbose=args.verbose)
        con.close()
        return

    engines = get_enabled_engines(args.searxng, args.categories, verbose=args.verbose)
    engines.sort(key=lambda e: e["name"])

    if args.list_only:
        print(f"Enabled engines ({len(engines)} total" + (f", filtered by categories: {args.categories}" if args.categories else "") + "):")
        for e in engines:
            cats = ",".join(e.get("categories") or [])
            print(f"  - {e['name']:28s} [{cats}]")
        con.close()
        return

    if args.limit:
        engines = engines[: args.limit]

    now = time.time()
    skipped = []
    if args.cooldown:
        active = []
        for e in engines:
            left = normalize_cooldown(con, e["name"], now)
            if left > 0:
                active.append((e["name"], left))
        if active:
            print(f"Cooldown active for {len(active)} engines — skipping:")
            for name, left in active:
                print(f"   - {name} (still {left}s)")
            skipset = {n for n, _ in active}
            skipped = [e for e in engines if e["name"] in skipset]
            engines = [e for e in engines if e["name"] not in skipset]

    print(f"Probing {len(engines)} engines (parallel={args.parallel}, query='{args.query}', run={args.run})"
          + (f", {len(skipped)} on cooldown" if skipped else ""))

    done = 0
    rows = []

    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = {ex.submit(probe_one, e, args.query, args.searxng, args.verbose): e["name"] for e in engines}
        for fut in as_completed(futs):
            name = futs[fut]
            done += 1
            try:
                row = fut.result()
            except Exception:  # noqa: BLE001
                row = {
                    "engine": name,
                    "state": "our_error",
                    "result_count": 0,
                    "elapsed_ms": 0,
                    "reason": "probe_exception",
                    "note": "probe raised",
                    "categories": "",
                    "query": "",
                    "http_status": None,
                    "skipped": False,
                    "cooldown_seconds": None,
                    "cooldown_type": None,
                }
            rows.append(row)
            print(
                f"  [{done}/{len(engines)}] {name:28s} {row['state']:14s} "
                f"{row['elapsed_ms']:>6}ms  results={row.get('result_count', 0):>3}  {row.get('reason') or ''}"
            )

    # Record + apply cooldowns (single-threaded here)
    for row in rows:
        record_probe_result(con, row, args.run)
        if args.cooldown:
            secs = apply_cooldown(con, row["engine"], row["state"], time.time(), verbose=args.verbose)
            if secs:
                con.execute(
                    "UPDATE engine_probe SET cooldown_seconds=?, cooldown_type=? WHERE run_id=? AND engine=?",
                    (secs, row["state"], args.run, row["engine"]),
                )
                con.commit()
                print(f"  ! set cooldown {row['state']} -> {secs}s on {row['engine']}")

    for e in skipped:
        row = {
            "engine": e["name"],
            "categories": "",
            "query": args.query,
            "http_status": None,
            "state": "skipped",
            "result_count": 0,
            "elapsed_ms": 0,
            "reason": "cooldown",
            "note": "not probed (active cooldown)",
            "skipped": 1,
            "cooldown_seconds": 0,
            "cooldown_type": None,
        }
        record_probe_result(con, row, args.run)

    _report(con, args.run)
    con.close()


def _report(con, run_id):
    print("\n================ SUMMARY  (run %s) ================" % run_id)
    for state, n in con.execute(
        "SELECT state, COUNT(*) FROM engine_probe WHERE run_id=? GROUP BY state", (run_id,)
    ):
        print(f"  {state:16s} {n}")
    vals = [
        v[0]
        for v in con.execute(
            "SELECT elapsed_ms FROM engine_probe WHERE run_id=? AND state='ok'", (run_id,)
        ).fetchall()
    ]
    if vals:
        s = sorted(vals)
        import statistics

        print(
            f"  avg/p50/max time-to-results (ok): {int(statistics.mean(s))}/{int(statistics.median(s))}/{max(s)} ms  (n={len(s)})"
        )
    else:
        print("  (no 'ok' engines this run)")
    n = con.execute("SELECT COUNT(*) FROM engine_probe WHERE run_id=?", (run_id,)).fetchone()[0]
    print("  queries stored:", n, "rows this run")


if __name__ == "__main__":
    sys.exit(main())
