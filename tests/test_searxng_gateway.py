#!/usr/bin/env python3
"""test_searxng_gateway.py — integration tests for SearXNG gateway proxy."""

import json
import os
import sys
import tempfile
import time
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from searxng_gateway import SearxngGatewayHandler, ThreadedHTTPServer
from searxng_policy import apply_cooldown, get_db_connection, init_db, normalize_cooldown


class MockSearxngHandler(BaseHTTPRequestHandler):
    """Mock SearXNG instance recording incoming requests and returning controlled responses."""

    last_request_headers = {}
    last_request_params = {}
    mock_response_data = {}
    mock_status_code = 200

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        MockSearxngHandler.last_request_headers = dict(self.headers)
        MockSearxngHandler.last_request_params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}

        body = json.dumps(MockSearxngHandler.mock_response_data).encode("utf-8")
        self.send_response(MockSearxngHandler.mock_status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestSearxngGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Start Mock SearXNG server
        cls.mock_port = 18888
        cls.mock_server = HTTPServer(("127.0.0.1", cls.mock_port), MockSearxngHandler)
        cls.mock_thread = Thread(target=cls.mock_server.serve_forever, daemon=True)
        cls.mock_thread.start()

        # 2. Start Gateway server
        cls.gw_port = 18880
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp_dir.name, "test_gw.db")

        con = get_db_connection(cls.db_path)
        init_db(con)
        con.close()

        cls.gw_server = ThreadedHTTPServer(("127.0.0.1", cls.gw_port), SearxngGatewayHandler)
        cls.gw_server.upstream = f"http://127.0.0.1:{cls.mock_port}"
        cls.gw_server.db_path = cls.db_path
        cls.gw_server.verbose = False
        cls.gw_thread = Thread(target=cls.gw_server.serve_forever, daemon=True)
        cls.gw_thread.start()

        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.gw_server.shutdown()
        cls.mock_server.shutdown()
        cls.tmp_dir.cleanup()

    def setUp(self):
        MockSearxngHandler.mock_status_code = 200
        MockSearxngHandler.mock_response_data = {
            "query": "test",
            "results": [{"title": "Test Title", "url": "http://test.com", "engine": "duckduckgo"}],
            "unresponsive_engines": [],
        }

    def test_healthz_and_status_endpoints(self):
        """Test /healthz and /gateway/status endpoints."""
        r = requests.get(f"http://127.0.0.1:{self.gw_port}/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("status"), "ok")

        r_stat = requests.get(f"http://127.0.0.1:{self.gw_port}/gateway/status")
        self.assertEqual(r_stat.status_code, 200)
        self.assertIn("active_cooldowns", r_stat.json())

    def test_forwarded_headers_injection(self):
        """Test that gateway passes X-Forwarded-For and X-Real-IP to upstream."""
        r = requests.get(f"http://127.0.0.1:{self.gw_port}/search?q=test&format=json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("X-Forwarded-For", MockSearxngHandler.last_request_headers)
        self.assertIn("X-Real-IP", MockSearxngHandler.last_request_headers)

    def test_engine_cooldown_filtering(self):
        """Test that cooled-down engines are filtered out from requested engines parameter."""
        con = get_db_connection(self.db_path)
        apply_cooldown(con, "broken_engine", "timeout")
        con.close()

        r = requests.get(f"http://127.0.0.1:{self.gw_port}/search?q=test&engines=broken_engine,good_engine&format=json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(MockSearxngHandler.last_request_params.get("engines"), "good_engine")
        self.assertIn("broken_engine", r.headers.get("X-Excluded-Engines", ""))

    def test_response_feedback_loop_triggers_cooldown(self):
        """Test that unresponsive_engines in response dynamically triggers cooldown."""
        MockSearxngHandler.mock_response_data = {
            "query": "test",
            "results": [],
            "unresponsive_engines": [["failing_engine", "Too many requests"]],
        }

        r = requests.get(f"http://127.0.0.1:{self.gw_port}/search?q=test&engines=failing_engine&format=json")
        self.assertEqual(r.status_code, 200)

        con = get_db_connection(self.db_path)
        cd_left = normalize_cooldown(con, "failing_engine")
        con.close()
        self.assertGreater(cd_left, 0)

    def test_response_feedback_loop_resets_on_success(self):
        """Test that successful engine results clear active cooldowns."""
        con = get_db_connection(self.db_path)
        apply_cooldown(con, "recovering_engine", "captcha")
        self.assertGreater(normalize_cooldown(con, "recovering_engine"), 0)
        con.close()

        MockSearxngHandler.mock_response_data = {
            "query": "test",
            "results": [{"title": "Success", "url": "http://test.com", "engine": "recovering_engine"}],
            "unresponsive_engines": [],
        }

        r = requests.get(f"http://127.0.0.1:{self.gw_port}/search?q=test&format=json")
        self.assertEqual(r.status_code, 200)

        con = get_db_connection(self.db_path)
        cd_left = normalize_cooldown(con, "recovering_engine")
        con.close()
        self.assertEqual(cd_left, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
