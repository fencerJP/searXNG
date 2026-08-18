#!/usr/bin/env python3
"""test_searxng_cache.py — unit tests for normalized snippet_store (zlib BLOBs), query_engine_index, and duplicate pruning."""

import os
import sqlite3
import sys
import time
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from searxng_policy import (
    compress_blob,
    decompress_blob,
    get_db_connection,
    get_engine_cached_results,
    init_db,
    log_duplicate_candidate,
    make_engine_cache_key,
    make_url_hash,
    merge_duplicate_snippet,
    purge_expired_cache,
    store_engine_cached_results,
)
from searxng_duplicate_verifier import run_verifier


class TestSearxngPerEngineCache(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("PRAGMA foreign_keys=ON")
        init_db(self.con)

    def tearDown(self):
        self.con.close()

    def test_zlib_compression_exactness(self):
        """Test that zlib compression roundtrips accurately."""
        sample_text = "Asyncio provides a set of high-level APIs for running tasks concurrently."
        blob = compress_blob(sample_text)
        self.assertIsInstance(blob, bytes)
        decompressed = decompress_blob(blob)
        self.assertEqual(decompressed, sample_text)

    def test_per_engine_cache_storage_and_exact_hit(self):
        """Test storing and retrieving clean results from normalized tables."""
        results = [
            {"title": "Paxos Made Simple", "url": "https://lamport.azurewebsites.net/pubs/paxos-simple.pdf", "content": "The Paxos algorithm is...", "engine": "mock google"},
            {"title": "Raft Consensus Algorithm", "url": "https://raft.github.io/", "content": "Raft is a consensus algorithm...", "engine": "mock google"}
        ]
        store_engine_cached_results(self.con, "consensus algorithm", "mock google", "general", results, ttl_seconds=3600)

        # Verify snippet_store has 2 rows
        count = self.con.execute("SELECT COUNT(*) FROM snippet_store").fetchone()[0]
        self.assertEqual(count, 2)

        # Exact hit
        hit = get_engine_cached_results(self.con, "consensus algorithm", "mock google", "general")
        self.assertIsNotNone(hit)
        self.assertEqual(len(hit), 2)
        self.assertEqual(hit[0]["title"], "Paxos Made Simple")
        self.assertEqual(hit[0]["content"], "The Paxos algorithm is...")

    def test_multi_query_snippet_sharing(self):
        """Test that identical URLs across multiple queries share the same snippet_store row."""
        shared_snippet = {"title": "Python Asyncio Docs", "url": "https://docs.python.org/3/library/asyncio.html", "content": "Asyncio docs", "engine": "mock duckduckgo"}
        
        # Query 1
        store_engine_cached_results(self.con, "python asyncio", "mock duckduckgo", "", [shared_snippet], ttl_seconds=3600)
        # Query 2 (different query, same URL)
        store_engine_cached_results(self.con, "python async await", "mock duckduckgo", "", [shared_snippet], ttl_seconds=3600)

        # Verify query_engine_index has 2 rows, but snippet_store has only 1 row (zero wasted space!)
        qe_count = self.con.execute("SELECT COUNT(*) FROM query_engine_index").fetchone()[0]
        snip_count = self.con.execute("SELECT COUNT(*) FROM snippet_store").fetchone()[0]

        self.assertEqual(qe_count, 2)
        self.assertEqual(snip_count, 1)

    def test_per_engine_isolation_on_different_engine(self):
        """Test that caching for engine A does not hit for engine B."""
        results = [{"title": "Test Title", "url": "http://example.org/1", "content": "Test Snippet", "engine": "mock google"}]
        store_engine_cached_results(self.con, "quantum computing", "mock google", "general", results, ttl_seconds=3600)

        miss = get_engine_cached_results(self.con, "quantum computing", "mock bing", "general")
        self.assertIsNone(miss)

    def test_per_engine_ttl_expiration(self):
        """Test that cached results expire after TTL."""
        results = [{"title": "Ephemeral", "url": "http://example.org/temp", "content": "Short lived", "engine": "mock brave"}]
        now = time.time()
        store_engine_cached_results(self.con, "ephemeral query", "mock brave", "", results, ttl_seconds=1, now=now)

        hit = get_engine_cached_results(self.con, "ephemeral query", "mock brave", "", now=now + 0.5)
        self.assertIsNotNone(hit)

        miss = get_engine_cached_results(self.con, "ephemeral query", "mock brave", "", now=now + 1.5)
        self.assertIsNone(miss)

    def test_duplicate_snippet_merge_and_blob_dropping(self):
        """Test that merging duplicate snippets repoints query indexes and completely deletes the duplicate BLOB."""
        snip_canon = {"title": "Canonical Guide", "url": "https://example.com/guide", "content": "Guide Content", "engine": "mock google"}
        snip_dup = {"title": "Duplicate Guide", "url": "https://example.com/guide/index.html", "content": "Guide Content", "engine": "mock bing"}

        store_engine_cached_results(self.con, "guide query", "mock google", "", [snip_canon], ttl_seconds=3600)
        store_engine_cached_results(self.con, "guide query", "mock bing", "", [snip_dup], ttl_seconds=3600)

        canon_hash = make_url_hash(snip_canon["url"])
        dup_hash = make_url_hash(snip_dup["url"])

        # Prior to merge: 2 snippets in store
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM snippet_store").fetchone()[0], 2)

        # Execute merge
        repointed = merge_duplicate_snippet(self.con, canon_hash, dup_hash)
        self.assertEqual(repointed, 1)

        # After merge: duplicate blob is completely deleted from snippet_store!
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM snippet_store").fetchone()[0], 1)
        self.assertIsNone(self.con.execute("SELECT url FROM snippet_store WHERE url_hash=?", (dup_hash,)).fetchone())

        # And querying mock bing resolves to the canonical snippet!
        bing_results = get_engine_cached_results(self.con, "guide query", "mock bing", "")
        self.assertIsNotNone(bing_results)
        self.assertEqual(len(bing_results), 1)
        self.assertEqual(bing_results[0]["url"], "https://example.com/guide")

    def test_duplicate_candidate_logging(self):
        """Test recording candidate duplicates in duplicate_candidates table."""
        log_duplicate_candidate(self.con, "https://example.com/a", "https://example.com/b", "example.com", 0.95)
        count = self.con.execute("SELECT COUNT(*) FROM duplicate_candidates WHERE status='PENDING'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_purge_expired_cache(self):
        """Test that purge_expired_cache cleanly deletes expired indexes."""
        now = time.time()
        results = [{"title": "T", "url": "http://e.org/1", "content": "C", "engine": "m"}]
        store_engine_cached_results(self.con, "q1", "mock1", "", results, ttl_seconds=10, now=now)
        store_engine_cached_results(self.con, "q2", "mock2", "", results, ttl_seconds=1, now=now)

        deleted = purge_expired_cache(self.con, now=now + 5)
        self.assertEqual(deleted, 1)

        self.assertIsNotNone(get_engine_cached_results(self.con, "q1", "mock1", "", now=now + 5))
        self.assertIsNone(get_engine_cached_results(self.con, "q2", "mock2", "", now=now + 5))


if __name__ == "__main__":
    unittest.main()
