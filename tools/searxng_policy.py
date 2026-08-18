#!/usr/bin/env python3
"""searxng_policy.py — deterministic, non-LLM per-source rate protection & cooldown policy engine.

Shared library for engine health tracking, classification, SQLite auto-migration,
and exponential backoff rate throttling across probe tools and runtime gateways.
"""

import datetime
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

# Cooldown base (seconds) by failure type
COOLDOWN_BASE = {
    "suspended": 180,      # Align with SearXNG's ~3 min built-in suspension
    "captcha": 60,         # 1 min
    "timeout": 300,        # 5 min — slow-but-alive gets a gentle window
    "access_denied": 3600, # 1h
    "degraded": 3600,      # 1h — activated once >=3 failures in the 24h window
}
COOLDOWN_CAP = 86400       # 24h hard ceiling
WINDOW_HOURS = 24
DEGRADE_TRIGGER = 3


def get_db_connection(db_path: str, timeout: float = 10.0) -> sqlite3.Connection:
    """Create and configure a SQLite connection with WAL mode enabled for concurrency."""
    con = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db(con: sqlite3.Connection, verbose: bool = False) -> None:
    """Initialize database and perform non-destructive schema migration if needed."""
    if verbose:
        print("[DEBUG] Initializing SQLite database schema...")
    con.execute(
        """CREATE TABLE IF NOT EXISTS engine_probe(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT, ts TEXT, engine TEXT, categories TEXT, query TEXT,
            http_status INTEGER, state TEXT, result_count INTEGER,
            elapsed_ms INTEGER, reason TEXT, note TEXT,
            skipped INTEGER DEFAULT 0, cooldown_seconds INTEGER,
            cooldown_type TEXT
        )"""
    )
    cols = {row[1] for row in con.execute("PRAGMA table_info(engine_probe)").fetchall()}
    needed = [
        ("state", "TEXT"),
        ("skipped", "INTEGER DEFAULT 0"),
        ("cooldown_seconds", "INTEGER"),
        ("cooldown_type", "TEXT"),
    ]
    for col_name, col_type in needed:
        if col_name not in cols:
            if verbose:
                print(f"[DEBUG] Migrating schema: Adding missing column '{col_name} {col_type}' to engine_probe table.")
            con.execute(f"ALTER TABLE engine_probe ADD COLUMN {col_name} {col_type}")

    # Migrate legacy data if connectivity column was present
    if "connectivity" in cols:
        con.execute("UPDATE engine_probe SET state = connectivity WHERE state IS NULL AND connectivity IS NOT NULL")
    con.commit()

    con.execute("CREATE INDEX IF NOT EXISTS idx_probe_engine ON engine_probe(engine)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_probe_ts ON engine_probe(ts)")
    con.execute(
        """CREATE TABLE IF NOT EXISTS cooldowns(
            engine TEXT PRIMARY KEY, last_type TEXT, fail_count INTEGER,
            last_failure_at REAL, cooldown_until REAL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS snippet_store(
            url_hash TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            content_blob BLOB NOT NULL,
            content_hash TEXT,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_snippet_url ON snippet_store(url)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_snippet_last_seen ON snippet_store(last_seen_at)")

    con.execute(
        """CREATE TABLE IF NOT EXISTS query_engine_index(
            cache_key TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            engine TEXT NOT NULL,
            category TEXT NOT NULL,
            url_hashes_json TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            created_at REAL NOT NULL,
            ttl_seconds INTEGER NOT NULL
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_qe_lookup ON query_engine_index(query, engine, category)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_qe_created ON query_engine_index(created_at)")

    con.execute(
        """CREATE TABLE IF NOT EXISTS duplicate_candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash_a TEXT NOT NULL,
            url_hash_b TEXT NOT NULL,
            domain TEXT NOT NULL,
            similarity_score REAL NOT NULL,
            status TEXT DEFAULT 'PENDING',
            checked_at REAL
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_dup_status ON duplicate_candidates(status)")

    # Legacy cache tables for backwards compatibility
    con.execute(
        """CREATE TABLE IF NOT EXISTS search_cache(
            cache_key TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            engines TEXT NOT NULL,
            categories TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            ttl_seconds INTEGER NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS engine_search_cache(
            cache_key TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            engine TEXT NOT NULL,
            category TEXT NOT NULL,
            results_json TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            created_at REAL NOT NULL,
            ttl_seconds INTEGER NOT NULL
        )"""
    )
    con.commit()


def make_url_hash(url: str) -> str:
    """Generate deterministic SHA-256 hash for normalized URL."""
    import hashlib
    norm_u = url.strip().rstrip("/").lower()
    return hashlib.sha256(norm_u.encode("utf-8")).hexdigest()


def compress_blob(text: str) -> bytes:
    """Compress UTF-8 text into zlib binary BLOB (compression level 6)."""
    import zlib
    return zlib.compress(text.encode("utf-8"), level=6)


def decompress_blob(blob: bytes) -> str:
    """Decompress zlib binary BLOB back into UTF-8 string."""
    import zlib
    try:
        return zlib.decompress(blob).decode("utf-8")
    except Exception:
        # Fallback if uncompressed
        if isinstance(blob, bytes):
            return blob.decode("utf-8", errors="ignore")
        return str(blob)


def make_cache_key(query: str, engines: str = "", categories: str = "") -> str:
    """Generate deterministic SHA-256 cache key based strictly on normalized query, engines, and categories."""
    import hashlib
    norm_q = " ".join(query.strip().lower().split())
    engine_list = sorted([e.strip().lower() for e in engines.split(",") if e.strip()])
    norm_eng = ",".join(engine_list)
    norm_cat = categories.strip().lower()
    raw = f"{norm_q}|{norm_eng}|{norm_cat}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_engine_cache_key(query: str, engine: str, category: str = "") -> str:
    """Generate deterministic SHA-256 cache key for a specific single engine result set."""
    import hashlib
    norm_q = " ".join(query.strip().lower().split())
    norm_eng = engine.strip().lower()
    norm_cat = category.strip().lower()
    raw = f"{norm_q}|{norm_eng}|{norm_cat}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached_response(
    con: sqlite3.Connection,
    query: str,
    engines: str = "",
    categories: str = "",
    now: Optional[float] = None,
) -> Optional[str]:
    """Retrieve cached response JSON if exact match on query and engines, and within TTL."""
    if now is None:
        now = time.time()
    cache_key = make_cache_key(query, engines, categories)
    row = con.execute(
        "SELECT response_json, created_at, ttl_seconds FROM search_cache WHERE cache_key=?",
        (cache_key,),
    ).fetchone()
    if not row:
        return None
    resp_json, created_at, ttl_seconds = row
    if (created_at + ttl_seconds) < now:
        con.execute("DELETE FROM search_cache WHERE cache_key=?", (cache_key,))
        con.commit()
        return None
    return resp_json


def store_cached_response(
    con: sqlite3.Connection,
    query: str,
    engines: str,
    categories: str,
    response_json: str,
    ttl_seconds: int = 86400,
    now: Optional[float] = None,
) -> None:
    """Store search response JSON in local search_cache with exact query + engine key."""
    if now is None:
        now = time.time()
    cache_key = make_cache_key(query, engines, categories)
    con.execute(
        """INSERT INTO search_cache(cache_key, query, engines, categories, response_json, created_at, ttl_seconds)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(cache_key) DO UPDATE SET
               response_json=excluded.response_json,
               created_at=excluded.created_at,
               ttl_seconds=excluded.ttl_seconds""",
        (cache_key, query.strip().lower(), engines.strip().lower(), categories.strip().lower(), response_json, now, ttl_seconds),
    )
    con.commit()


def get_engine_cached_results(
    con: sqlite3.Connection,
    query: str,
    engine: str,
    category: str = "",
    now: Optional[float] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Retrieve cached results list from normalized pointer index + zlib snippet store."""
    import json
    if now is None:
        now = time.time()
    cache_key = make_engine_cache_key(query, engine, category)

    # 1. Check normalized query_engine_index
    row = con.execute(
        "SELECT url_hashes_json, created_at, ttl_seconds FROM query_engine_index WHERE cache_key=?",
        (cache_key,),
    ).fetchone()

    if row:
        url_hashes_json_str, created_at, ttl_seconds = row
        if (created_at + ttl_seconds) < now:
            con.execute("DELETE FROM query_engine_index WHERE cache_key=?", (cache_key,))
            con.commit()
            return None

        try:
            url_hashes = json.loads(url_hashes_json_str)
        except Exception:
            return None

        if not url_hashes:
            return []

        # Batch-fetch matching snippets from snippet_store
        placeholders = ",".join("?" for _ in url_hashes)
        snippet_rows = con.execute(
            f"SELECT url_hash, url, title, content_blob FROM snippet_store WHERE url_hash IN ({placeholders})",
            url_hashes,
        ).fetchall()

        by_hash = {}
        for u_hash, u_url, u_title, u_blob in snippet_rows:
            try:
                content_text = decompress_blob(u_blob)
            except Exception:
                content_text = ""
            by_hash[u_hash] = {
                "title": u_title,
                "url": u_url,
                "content": content_text,
                "engine": engine,
            }

        # Preserve original ranking order
        results = [by_hash[h] for h in url_hashes if h in by_hash]
        return results

    # 2. Backwards-compatible check on legacy engine_search_cache
    legacy_row = con.execute(
        "SELECT results_json, created_at, ttl_seconds FROM engine_search_cache WHERE cache_key=?",
        (cache_key,),
    ).fetchone()
    if legacy_row:
        results_json_str, created_at, ttl_seconds = legacy_row
        if (created_at + ttl_seconds) < now:
            con.execute("DELETE FROM engine_search_cache WHERE cache_key=?", (cache_key,))
            con.commit()
            return None
        try:
            return json.loads(results_json_str)
        except Exception:
            return None

    return None


def store_engine_cached_results(
    con: sqlite3.Connection,
    query: str,
    engine: str,
    category: str,
    results: List[Dict[str, Any]],
    ttl_seconds: int = 86400,
    now: Optional[float] = None,
) -> None:
    """Store cleaned results list using normalized snippet_store (zlib BLOBs) and query_engine_index."""
    import hashlib
    import json
    if now is None:
        now = time.time()

    url_hashes = []

    for item in results:
        raw_url = item.get("url", "").strip()
        if not raw_url:
            continue
        u_hash = make_url_hash(raw_url)
        title = item.get("title", "").strip()
        content = item.get("content", "").strip()
        c_blob = compress_blob(content)
        c_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Upsert unique snippet into snippet_store
        con.execute(
            """INSERT INTO snippet_store(url_hash, url, title, content_blob, content_hash, first_seen_at, last_seen_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(url_hash) DO UPDATE SET
                   title=excluded.title,
                   content_blob=excluded.content_blob,
                   content_hash=excluded.content_hash,
                   last_seen_at=excluded.last_seen_at""",
            (u_hash, raw_url, title, c_blob, c_hash, now, now),
        )
        url_hashes.append(u_hash)

    # Upsert pointer array into query_engine_index
    cache_key = make_engine_cache_key(query, engine, category)
    url_hashes_json_str = json.dumps(url_hashes)
    con.execute(
        """INSERT INTO query_engine_index(cache_key, query, engine, category, url_hashes_json, result_count, created_at, ttl_seconds)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(cache_key) DO UPDATE SET
               url_hashes_json=excluded.url_hashes_json,
               result_count=excluded.result_count,
               created_at=excluded.created_at,
               ttl_seconds=excluded.ttl_seconds""",
        (cache_key, query.strip().lower(), engine.strip().lower(), category.strip().lower(), url_hashes_json_str, len(url_hashes), now, ttl_seconds),
    )
    con.commit()


def log_duplicate_candidate(
    con: sqlite3.Connection,
    url_a: str,
    url_b: str,
    domain: str,
    similarity_score: float,
    now: Optional[float] = None,
) -> None:
    """Record potential duplicate pair in duplicate_candidates table for idle verification."""
    if now is None:
        now = time.time()
    h_a = make_url_hash(url_a)
    h_b = make_url_hash(url_b)
    if h_a == h_b:
        return
    # Order deterministically to prevent reverse pair duplicates
    if h_a > h_b:
        h_a, h_b = h_b, h_a
        url_a, url_b = url_b, url_a

    con.execute(
        """INSERT OR IGNORE INTO duplicate_candidates(url_hash_a, url_hash_b, domain, similarity_score, status, checked_at)
           VALUES(?,?,?,?, 'PENDING', NULL)""",
        (h_a, h_b, domain.lower(), similarity_score),
    )
    con.commit()


def merge_duplicate_snippet(con: sqlite3.Connection, canonical_hash: str, duplicate_hash: str) -> int:
    """Repoint all query_engine_index references from duplicate_hash to canonical_hash and delete the duplicate blob from snippet_store."""
    import json
    if canonical_hash == duplicate_hash:
        return 0

    # 1. Update all query_engine_index rows that reference duplicate_hash
    rows = con.execute("SELECT cache_key, url_hashes_json FROM query_engine_index WHERE url_hashes_json LIKE ?", (f"%{duplicate_hash}%",)).fetchall()
    updated_queries = 0

    for c_key, hashes_json in rows:
        try:
            hashes = json.loads(hashes_json)
            new_hashes = []
            changed = False
            for h in hashes:
                if h == duplicate_hash:
                    if canonical_hash not in new_hashes:
                        new_hashes.append(canonical_hash)
                    changed = True
                else:
                    new_hashes.append(h)
            if changed:
                con.execute(
                    "UPDATE query_engine_index SET url_hashes_json=?, result_count=? WHERE cache_key=?",
                    (json.dumps(new_hashes), len(new_hashes), c_key),
                )
                updated_queries += 1
        except Exception:
            pass

    # 2. Completely drop duplicate blob from snippet_store!
    con.execute("DELETE FROM snippet_store WHERE url_hash=?", (duplicate_hash,))
    # Mark candidate row as CONFIRMED_DUP
    con.execute(
        "UPDATE duplicate_candidates SET status='CONFIRMED_DUP', checked_at=? WHERE (url_hash_a=? AND url_hash_b=?) OR (url_hash_a=? AND url_hash_b=?)",
        (time.time(), canonical_hash, duplicate_hash, duplicate_hash, canonical_hash),
    )
    con.commit()
    return updated_queries


def purge_expired_cache(con: sqlite3.Connection, now: Optional[float] = None) -> int:
    """Delete expired entries from query_engine_index and prune orphaned snippet_store rows."""
    if now is None:
        now = time.time()

    # 1. Delete expired query_engine_index
    c_idx = con.execute(
        "DELETE FROM query_engine_index WHERE (created_at + ttl_seconds) < ?",
        (now,),
    ).rowcount

    # 2. Prune unreferenced snippet_store rows older than 7 days
    # (Extract all currently active url_hashes)
    all_active_json = con.execute("SELECT url_hashes_json FROM query_engine_index").fetchall()
    import json
    active_hashes = set()
    for row in all_active_json:
        try:
            for h in json.loads(row[0]):
                active_hashes.add(h)
        except Exception:
            pass

    c_snippets = 0
    all_stored = con.execute("SELECT url_hash, last_seen_at FROM snippet_store").fetchall()
    seven_days_ago = now - (7 * 86400)
    for u_hash, last_seen in all_stored:
        if u_hash not in active_hashes and last_seen < seven_days_ago:
            con.execute("DELETE FROM snippet_store WHERE url_hash=?", (u_hash,))
            c_snippets += 1

    # 3. Clean legacy tables if present
    c_leg1 = con.execute("DELETE FROM search_cache WHERE (created_at + ttl_seconds) < ?", (now,)).rowcount
    c_leg2 = con.execute("DELETE FROM engine_search_cache WHERE (created_at + ttl_seconds) < ?", (now,)).rowcount

    con.commit()
    return c_idx + c_snippets + c_leg1 + c_leg2


def classify(engine_name: str, data: Dict[str, Any]) -> Tuple[str, int, Optional[str], str]:
    """Derive (state, result_count, reason, detail) from a single-engine JSON search response.

    State uses the agreed failure types, keyed off SearXNG's reason string.
    """
    unresp = data.get("unresponsive_engines") or []
    detail = ", ".join(f"{n}:{reason}" for n, reason in unresp) if unresp else ""
    hit = next((reason for n, reason in unresp if n == engine_name), None)
    results = len(data.get("results") or [])
    if hit is not None:
        r = str(hit).lower()
        if any(k in r for k in ("too many", "rate", "suspended")):
            return "suspended", results, hit, detail
        if "captcha" in r:
            return "captcha", results, hit, detail
        if "timeout" in r:
            return "timeout", results, hit, detail
        if any(k in r for k in ("access denied", "denied", "http error", "4", "5")):
            return "access_denied", results, hit, detail
        return "timeout", results, hit, detail
    if results > 0:
        return "ok", results, None, detail
    return "ok_no_results", results, None, detail


def calculate_cooldown(state: str, consecutive: int, recent_24h_failures: int) -> int:
    """Pure mathematical calculation of cooldown seconds according to policy."""
    if state in ("ok", "ok_no_results", "our_error"):
        return 0

    base = COOLDOWN_BASE.get(state, 300)
    consec = max(1, consecutive)
    cooldown = int(base * (2 ** (consec - 1)))
    if recent_24h_failures >= DEGRADE_TRIGGER:
        cooldown = max(cooldown, COOLDOWN_BASE["degraded"])
    return min(cooldown, COOLDOWN_CAP)


def normalize_cooldown(con: sqlite3.Connection, engine: str, now: Optional[float] = None) -> int:
    """Return remaining cooldown seconds for an engine, or 0 if none/no row."""
    if now is None:
        now = time.time()
    row = con.execute(
        "SELECT cooldown_until FROM cooldowns WHERE engine=?", (engine,)
    ).fetchone()
    if not row or row[0] is None:
        return 0
    return max(0, int(row[0] - now))


def get_active_cooldowns(con: sqlite3.Connection, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """Return a dictionary of all currently active engine cooldowns."""
    if now is None:
        now = time.time()
    rows = con.execute(
        """SELECT engine, last_type, fail_count, last_failure_at, cooldown_until
           FROM cooldowns
           WHERE cooldown_until IS NOT NULL AND cooldown_until > ?""",
        (now,),
    ).fetchall()
    active = {}
    for engine, last_type, fail_count, last_failure_at, cooldown_until in rows:
        active[engine] = {
            "last_type": last_type,
            "fail_count": fail_count,
            "last_failure_at": last_failure_at,
            "cooldown_until": cooldown_until,
            "remaining_seconds": max(0, int(cooldown_until - now)),
        }
    return active


def apply_cooldown(
    con: sqlite3.Connection,
    engine: str,
    state: str,
    now: Optional[float] = None,
    verbose: bool = False,
) -> int:
    """Update cooldowns for an engine after a probe or query outcome. Returns seconds applied."""
    if now is None:
        now = time.time()

    # Reset-on-success: first ok or ok_no_results clears active cooldown and failure count
    if state in ("ok", "ok_no_results"):
        if verbose:
            print(f"[DEBUG] Engine '{engine}' state='{state}' -> Resetting failure count & clearing cooldowns.")
        con.execute("DELETE FROM cooldowns WHERE engine=?", (engine,))
        con.commit()
        return 0

    # Client-side exception: never penalize an external source
    if state == "our_error":
        if verbose:
            print(f"[DEBUG] Engine '{engine}' state='our_error' -> Client-side exception; ignoring (no cooldown applied).")
        return 0

    # Count recent failures in 24h sliding window
    since = (datetime.datetime.fromtimestamp(now) - datetime.timedelta(hours=WINDOW_HOURS)).isoformat(timespec="seconds")
    recent = con.execute(
        "SELECT COUNT(*) FROM engine_probe WHERE engine=? AND ts>=? AND state NOT IN ('ok','ok_no_results')",
        (engine, since),
    ).fetchone()[0]

    row = con.execute(
        "SELECT last_type, fail_count, last_failure_at, cooldown_until FROM cooldowns WHERE engine=?",
        (engine,),
    ).fetchone()
    if row and row[0] == state:
        consec = (row[1] or 0) + 1
    else:
        consec = 1

    cooldown = calculate_cooldown(state, consec, recent)

    if verbose:
        print(
            f"[DEBUG] Cooldown calculation for '{engine}': state={state}, consecutive={consec}, 24h_fails={recent}, "
            f"final_cooldown={cooldown}s (expires: {datetime.datetime.fromtimestamp(now + cooldown).isoformat()})"
        )

    con.execute(
        """INSERT INTO cooldowns(engine, last_type, fail_count, last_failure_at, cooldown_until)
           VALUES(?,?,?,?,?)
           ON CONFLICT(engine) DO UPDATE SET
             last_type=excluded.last_type, fail_count=excluded.fail_count,
             last_failure_at=excluded.last_failure_at, cooldown_until=excluded.cooldown_until""",
        (engine, state, consec, now, now + cooldown),
    )
    con.commit()
    return cooldown


def record_probe_result(con: sqlite3.Connection, row: Dict[str, Any], run_id: str) -> None:
    """Insert a probe attempt record into engine_probe."""
    row["run_id"] = run_id
    if "ts" not in row or not row["ts"]:
        row["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
    con.execute(
        """INSERT INTO engine_probe(
            run_id, ts, engine, categories, query, http_status, state, result_count,
            elapsed_ms, reason, note, skipped, cooldown_seconds, cooldown_type)
           VALUES(:run_id,:ts,:engine,:categories,:query,:http_status,:state,:result_count,
                  :elapsed_ms,:reason,:note,:skipped,:cooldown_seconds,:cooldown_type)""",
        {
            k: row.get(k)
            for k in (
                "run_id",
                "ts",
                "engine",
                "categories",
                "query",
                "http_status",
                "state",
                "result_count",
                "elapsed_ms",
                "reason",
                "note",
                "skipped",
                "cooldown_seconds",
                "cooldown_type",
            )
        },
    )
    con.commit()


def compute_engine_metrics(con: sqlite3.Connection, hours: int = WINDOW_HOURS) -> List[Dict[str, Any]]:
    """Compute aggregated metrics (p50, p95, min, max, fail_rate) per engine over lookback window."""
    since = (datetime.datetime.now() - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = con.execute(
        """SELECT engine, count(*),
                  sum(case when state in ('ok', 'ok_no_results') then 1 else 0 end),
                  sum(case when state not in ('ok', 'ok_no_results', 'skipped') then 1 else 0 end)
           FROM engine_probe
           WHERE ts >= ?
           GROUP BY engine
           ORDER BY engine ASC""",
        (since,),
    ).fetchall()

    metrics_list = []
    for engine, total, ok_cnt, fail_cnt in rows:
        ok_cnt = ok_cnt or 0
        fail_cnt = fail_cnt or 0
        latencies = [
            r[0]
            for r in con.execute(
                "SELECT elapsed_ms FROM engine_probe WHERE engine=? AND ts>=? AND state='ok' AND elapsed_ms IS NOT NULL",
                (engine, since),
            ).fetchall()
        ]
        p50 = None
        p95 = None
        min_lat = None
        max_lat = None
        if latencies:
            latencies.sort()
            min_lat = latencies[0]
            max_lat = latencies[-1]
            p50 = latencies[int(len(latencies) * 0.50)]
            p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]

        fail_rate = (fail_cnt / total * 100.0) if total > 0 else 0.0

        # Detailed failure reasons breakdown
        reasons_rows = con.execute(
            """SELECT state, reason, count(*)
               FROM engine_probe
               WHERE engine=? AND ts>=? AND state NOT IN ('ok','ok_no_results','skipped')
               GROUP BY state, reason""",
            (engine, since),
        ).fetchall()
        reasons_breakdown = [{"state": st, "reason": rsn, "count": cnt} for st, rsn, cnt in reasons_rows]

        metrics_list.append(
            {
                "engine": engine,
                "total": total,
                "ok_count": ok_cnt,
                "fail_count": fail_cnt,
                "fail_rate": fail_rate,
                "min_ms": min_lat,
                "max_ms": max_lat,
                "p50_ms": p50,
                "p95_ms": p95,
                "reasons": reasons_breakdown,
            }
        )
    return metrics_list
