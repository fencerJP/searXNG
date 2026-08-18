#!/usr/bin/env python3
"""test_searxng_cli.py — unit tests for CLI argument parsing, flags, metrics calculations, and outputs."""

import datetime
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from searxng_policy import (
    apply_cooldown,
    compute_engine_metrics,
    get_db_connection,
    init_db,
    record_probe_result,
)

PROBE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "searxng_engine_probe.py")


class TestSearxngCLI(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_cli.db")
        self.con = get_db_connection(self.db_path)
        init_db(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp_dir.cleanup()

    def test_help_flag_and_parameter_documentation(self):
        """Test that --help prints all required flags with descriptive documentation."""
        res = subprocess.run([sys.executable, PROBE_SCRIPT, "--help"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        out = res.stdout
        self.assertIn("-v, --verbose", out)
        self.assertIn("--searxng", out)
        self.assertIn("--db", out)
        self.assertIn("--query", out)
        self.assertIn("--parallel", out)
        self.assertIn("--limit", out)
        self.assertIn("--categories", out)
        self.assertIn("--list-only", out)
        self.assertIn("--status", out)
        self.assertIn("--metrics", out)
        self.assertIn("--clear-cooldowns", out)
        self.assertIn("--no-cooldown", out)

    def test_metrics_calculation_accuracy(self):
        """Test analytical calculation accuracy of p50, p95, min, max, and failure rate."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        latencies = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        for lat in latencies:
            record_probe_result(self.con, {
                "engine": "math_engine", "categories": "general", "query": "test",
                "http_status": 200, "state": "ok", "result_count": 5,
                "elapsed_ms": lat, "reason": None, "note": "", "skipped": 0,
                "ts": now,
            }, "run_test")

        record_probe_result(self.con, {
            "engine": "math_engine", "categories": "general", "query": "test",
            "http_status": 429, "state": "suspended", "result_count": 0,
            "elapsed_ms": 50, "reason": "rate limit", "note": "", "skipped": 0,
            "ts": now,
        }, "run_test")
        record_probe_result(self.con, {
            "engine": "math_engine", "categories": "general", "query": "test",
            "http_status": 500, "state": "access_denied", "result_count": 0,
            "elapsed_ms": 80, "reason": "server error", "note": "", "skipped": 0,
            "ts": now,
        }, "run_test")

        metrics = compute_engine_metrics(self.con, hours=24)
        self.assertEqual(len(metrics), 1)
        m = metrics[0]
        self.assertEqual(m["engine"], "math_engine")
        self.assertEqual(m["total"], 12)
        self.assertEqual(m["ok_count"], 10)
        self.assertEqual(m["fail_count"], 2)
        self.assertAlmostEqual(m["fail_rate"], 16.67, places=1)
        self.assertEqual(m["min_ms"], 100)
        self.assertEqual(m["max_ms"], 1000)
        self.assertEqual(m["p50_ms"], 600)
        self.assertEqual(m["p95_ms"], 1000)
        self.assertEqual(len(m["reasons"]), 2)

    def test_status_and_clear_cooldowns_cli(self):
        """Test --status and --clear-cooldowns via subprocess."""
        apply_cooldown(self.con, "test_engine_1", "timeout")

        res = subprocess.run([sys.executable, PROBE_SCRIPT, "--db", self.db_path, "--status"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("test_engine_1", res.stdout)
        self.assertIn("timeout", res.stdout)

        res_clear = subprocess.run([sys.executable, PROBE_SCRIPT, "--db", self.db_path, "--clear-cooldowns"], capture_output=True, text=True)
        self.assertEqual(res_clear.returncode, 0)
        self.assertIn("cooldowns cleared", res_clear.stdout)

        res_status2 = subprocess.run([sys.executable, PROBE_SCRIPT, "--db", self.db_path, "--status"], capture_output=True, text=True)
        self.assertEqual(res_status2.returncode, 0)
        self.assertIn("no cooldowns set", res_status2.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
