#!/usr/bin/env python3
"""test_searxng_policy.py — comprehensive unit tests for searxng_policy.py in project workspace."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from searxng_policy import (
    COOLDOWN_BASE,
    COOLDOWN_CAP,
    DEGRADE_TRIGGER,
    apply_cooldown,
    calculate_cooldown,
    classify,
    compute_engine_metrics,
    get_active_cooldowns,
    get_db_connection,
    init_db,
    normalize_cooldown,
    record_probe_result,
)


class TestSearxngPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_policy.db")
        self.con = get_db_connection(self.db_path)
        init_db(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp_dir.cleanup()

    def test_base_cooldown_tiers(self):
        """Test that each failure type gets its agreed base cooldown."""
        self.assertEqual(calculate_cooldown("suspended", 1, 0), 180)
        self.assertEqual(calculate_cooldown("captcha", 1, 0), 60)
        self.assertEqual(calculate_cooldown("timeout", 1, 0), 300)
        self.assertEqual(calculate_cooldown("access_denied", 1, 0), 3600)
        self.assertEqual(calculate_cooldown("ok", 1, 0), 0)
        self.assertEqual(calculate_cooldown("ok_no_results", 1, 0), 0)
        self.assertEqual(calculate_cooldown("our_error", 1, 0), 0)

    def test_exponential_escalation(self):
        """Test 2^(consec-1) escalation."""
        self.assertEqual(calculate_cooldown("captcha", 1, 0), 60)
        self.assertEqual(calculate_cooldown("captcha", 2, 0), 120)
        self.assertEqual(calculate_cooldown("captcha", 3, 0), 240)
        self.assertEqual(calculate_cooldown("captcha", 4, 0), 480)

        self.assertEqual(calculate_cooldown("suspended", 1, 0), 180)
        self.assertEqual(calculate_cooldown("suspended", 2, 0), 360)
        self.assertEqual(calculate_cooldown("suspended", 3, 0), 720)

    def test_cooldown_cap(self):
        """Test that cooldown never exceeds 24h (86400s)."""
        self.assertEqual(calculate_cooldown("access_denied", 10, 0), COOLDOWN_CAP)
        self.assertEqual(calculate_cooldown("suspended", 20, 0), COOLDOWN_CAP)

    def test_degraded_tier_escalation(self):
        """Test that >=3 failures in 24h window forces cooldown to >= 3600s."""
        self.assertEqual(calculate_cooldown("captcha", 1, DEGRADE_TRIGGER), 3600)
        self.assertEqual(calculate_cooldown("timeout", 1, DEGRADE_TRIGGER), 3600)
        self.assertEqual(calculate_cooldown("suspended", 1, DEGRADE_TRIGGER + 2), 3600)

    def test_apply_cooldown_and_normalize(self):
        """Test database interaction for applying cooldowns and reading remaining seconds."""
        now = time.time()
        secs = apply_cooldown(self.con, "test_engine", "timeout", now=now)
        self.assertEqual(secs, 300)
        left = normalize_cooldown(self.con, "test_engine", now=now + 50)
        self.assertEqual(left, 250)

        secs2 = apply_cooldown(self.con, "test_engine", "timeout", now=now + 50)
        self.assertEqual(secs2, 600)

    def test_reset_on_success(self):
        """Test that ok or ok_no_results clears active cooldown and failure count."""
        now = time.time()
        apply_cooldown(self.con, "engine_a", "access_denied", now=now)
        self.assertGreater(normalize_cooldown(self.con, "engine_a", now=now), 0)

        res = apply_cooldown(self.con, "engine_a", "ok", now=now + 10)
        self.assertEqual(res, 0)
        self.assertEqual(normalize_cooldown(self.con, "engine_a", now=now + 10), 0)

        apply_cooldown(self.con, "engine_b", "captcha", now=now)
        apply_cooldown(self.con, "engine_b", "ok_no_results", now=now + 5)
        self.assertEqual(normalize_cooldown(self.con, "engine_b", now=now + 5), 0)

    def test_client_error_immunity(self):
        """Test that our_error never creates or updates cooldowns."""
        now = time.time()
        secs = apply_cooldown(self.con, "engine_c", "our_error", now=now)
        self.assertEqual(secs, 0)
        self.assertEqual(normalize_cooldown(self.con, "engine_c", now=now), 0)

    def test_classify_variants(self):
        """Test reason classification on various SearXNG response structures."""
        state, rc, rsn, _ = classify("bing", {"results": [{"url": "http://a.com"}], "unresponsive_engines": []})
        self.assertEqual(state, "ok")
        self.assertEqual(rc, 1)

        state, rc, rsn, _ = classify("bing", {"results": [], "unresponsive_engines": []})
        self.assertEqual(state, "ok_no_results")
        self.assertEqual(rc, 0)

        state, rc, rsn, _ = classify("bing", {"results": [], "unresponsive_engines": [["bing", "Too many requests"]]})
        self.assertEqual(state, "suspended")

        state, rc, rsn, _ = classify("google", {"results": [], "unresponsive_engines": [["google", "CAPTCHA required"]]})
        self.assertEqual(state, "captcha")

        state, rc, rsn, _ = classify("yahoo", {"results": [], "unresponsive_engines": [["yahoo", "Engine timeout"]]})
        self.assertEqual(state, "timeout")

        state, rc, rsn, _ = classify("duck", {"results": [], "unresponsive_engines": [["duck", "HTTP error 403 Forbidden"]]})
        self.assertEqual(state, "access_denied")

    def test_get_active_cooldowns_dict(self):
        """Test get_active_cooldowns dictionary structure."""
        now = time.time()
        apply_cooldown(self.con, "eng1", "timeout", now=now)
        apply_cooldown(self.con, "eng2", "captcha", now=now)

        active = get_active_cooldowns(self.con, now=now)
        self.assertEqual(len(active), 2)
        self.assertIn("eng1", active)
        self.assertIn("eng2", active)
        self.assertEqual(active["eng1"]["last_type"], "timeout")
        self.assertEqual(active["eng2"]["last_type"], "captcha")


if __name__ == "__main__":
    unittest.main(verbosity=2)
