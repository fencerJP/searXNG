#!/usr/bin/env python3
"""searxng_duplicate_verifier.py — asynchronous idle duplicate snippet verification and blob pruning worker."""

import argparse
import hashlib
import json
import os
import sys
import time
from urllib.parse import urlparse

import requests

from searxng_policy import get_db_connection, init_db, merge_duplicate_snippet

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "searxng_engine.db")


def calculate_text_similarity(t1: str, t2: str) -> float:
    """Calculate token Jaccard similarity between two text snippets."""
    words1 = set(t1.lower().split())
    words2 = set(t2.lower().split())
    if not words1 or not words2:
        return 0.0
    return len(words1 & words2) / float(len(words1 | words2))


def evaluate_candidate_pair(
    url_a: str,
    url_b: str,
    timeout: int = 5,
    verbose: bool = False,
    mock_mode: bool = False,
) -> tuple:
    """Evaluate if url_b is a confirmed duplicate of url_a.
    
    Returns (is_duplicate, canonical_url, duplicate_url, reason).
    """
    if mock_mode:
        # In mock testing mode, evaluate URL path patterns (e.g. /path.html vs /path/index.html vs /path)
        p_a = urlparse(url_a).path.lower().replace("/index.html", "").replace("/index.php", "").rstrip("/").rstrip(".html")
        p_b = urlparse(url_b).path.lower().replace("/index.html", "").replace("/index.php", "").rstrip("/").rstrip(".html")
        if p_a and p_a == p_b:
            return True, url_a, url_b, "mock_identical_paths"
        return False, url_a, url_b, "mock_distinct_paths"

    headers = {"User-Agent": "OpenClaw-DuplicateVerifier/1.0"}

    try:
        r_a = requests.get(url_a, headers=headers, timeout=timeout, allow_redirects=True)
        r_b = requests.get(url_b, headers=headers, timeout=timeout, allow_redirects=True)

        final_a = r_a.url.rstrip("/")
        final_b = r_b.url.rstrip("/")

        # Rule 1: HTTP Redirect to identical destination
        if final_a == final_b:
            return True, url_a, url_b, "redirect_to_same_destination"

        # Rule 2: Identical HTTP response body hashes
        hash_a = hashlib.sha256(r_a.content).hexdigest()
        hash_b = hashlib.sha256(r_b.content).hexdigest()
        if hash_a == hash_b:
            return True, url_a, url_b, "identical_body_hash"

        # Rule 3: High body text similarity (>= 90%)
        sim = calculate_text_similarity(r_a.text[:3000], r_b.text[:3000])
        if sim >= 0.90:
            return True, url_a, url_b, f"high_content_similarity ({sim:.2f})"

        return False, url_a, url_b, f"distinct_content ({sim:.2f})"

    except Exception as e:
        if verbose:
            print(f"[VERIFIER] Network error checking '{url_a}' vs '{url_b}': {e}")
        return False, url_a, url_b, f"fetch_error: {e}"


def run_verifier(db_path: str, limit: int = 50, timeout: int = 5, mock_mode: bool = False, verbose: bool = False) -> dict:
    """Process pending duplicate candidates, repoint indexes, and drop duplicate blobs."""
    con = get_db_connection(db_path)
    init_db(con, verbose=verbose)

    rows = con.execute(
        """SELECT id, url_hash_a, url_hash_b, domain, similarity_score
           FROM duplicate_candidates
           WHERE status='PENDING'
           ORDER BY id ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    stats = {
        "candidates_checked": len(rows),
        "confirmed_duplicates": 0,
        "confirmed_distinct": 0,
        "blobs_dropped": 0,
        "query_indexes_repointed": 0,
    }

    if not rows:
        if verbose:
            print("[VERIFIER] No pending duplicate candidates found.")
        con.close()
        return stats

    if verbose:
        print(f"[VERIFIER] Processing {len(rows)} pending duplicate candidates in '{db_path}'...")

    for row_id, hash_a, hash_b, domain, score in rows:
        # Fetch URLs from snippet_store
        s_a = con.execute("SELECT url FROM snippet_store WHERE url_hash=?", (hash_a,)).fetchone()
        s_b = con.execute("SELECT url FROM snippet_store WHERE url_hash=?", (hash_b,)).fetchone()

        if not s_a or not s_b:
            # One of the snippets was already purged or merged
            con.execute("UPDATE duplicate_candidates SET status='RESOLVED_MISSING', checked_at=? WHERE id=?", (time.time(), row_id))
            con.commit()
            continue

        url_a = s_a[0]
        url_b = s_b[0]

        is_dup, canon_url, dup_url, reason = evaluate_candidate_pair(
            url_a, url_b, timeout=timeout, verbose=verbose, mock_mode=mock_mode
        )

        now = time.time()

        if is_dup:
            canon_hash = hash_a if canon_url == url_a else hash_b
            dup_hash = hash_b if canon_hash == hash_a else hash_a

            repointed = merge_duplicate_snippet(con, canon_hash, dup_hash)
            con.execute("UPDATE duplicate_candidates SET status='CONFIRMED_DUP', checked_at=? WHERE id=?", (now, row_id))
            con.commit()

            stats["confirmed_duplicates"] += 1
            stats["blobs_dropped"] += 1
            stats["query_indexes_repointed"] += repointed

            if verbose:
                print(f"[VERIFIER] Confirmed DUPLICATE ({reason}): repointed {repointed} queries and dropped blob for '{dup_url}'")
        else:
            con.execute("UPDATE duplicate_candidates SET status='CONFIRMED_DISTINCT', checked_at=? WHERE id=?", (now, row_id))
            con.commit()
            stats["confirmed_distinct"] += 1
            if verbose:
                print(f"[VERIFIER] Confirmed DISTINCT ({reason}): preserved '{url_a}' and '{url_b}'")

    con.close()
    return stats


def main():
    ap = argparse.ArgumentParser(description="SearXNG Duplicate Snippet Verifier and Blob Pruner")
    ap.add_argument("--db", default=DEFAULT_DB, help="Path to SQLite database")
    ap.add_argument("--limit", type=int, default=50, help="Maximum candidate pairs to check per run")
    ap.add_argument("--timeout", type=int, default=5, help="HTTP request timeout in seconds")
    ap.add_argument("--mock-mode", action="store_true", help="Run in mock evaluation mode (offline testing)")
    ap.add_argument("-v", "--verbose", action="store_true", help="Enable verbose diagnostic logging")
    args = ap.parse_args()

    stats = run_verifier(
        db_path=args.db,
        limit=args.limit,
        timeout=args.timeout,
        mock_mode=args.mock_mode,
        verbose=args.verbose,
    )
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
