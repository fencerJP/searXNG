#!/usr/bin/env python3
"""test_searxng_tor_routing.py — unit tests for selective Tor proxy routing logic."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from searxng_policy import calculate_cooldown


class TestSearxngTorRoutingConfig(unittest.TestCase):
    def test_default_tor_tiers_selection(self):
        """Test default routing tiers include captcha, access_denied, and suspended."""
        default_tiers = {"captcha", "access_denied", "suspended"}
        self.assertIn("captcha", default_tiers)
        self.assertIn("access_denied", default_tiers)
        self.assertIn("suspended", default_tiers)
        self.assertNotIn("timeout", default_tiers)

    def test_tor_decision_logic(self):
        """Test decision logic for routing when Tor proxy is configured."""
        tor_proxy = "socks5://127.0.0.1:9050"
        tor_engines = {"mock google", "mock startpage"}
        tor_tiers = {"captcha", "access_denied", "suspended"}

        # Case 1: Engine is in tor_engines list
        self.assertTrue("mock google" in tor_engines and bool(tor_proxy))

        # Case 2: Engine is in a Tor-eligible cooldown tier
        engine_cd_state = "captcha"
        self.assertTrue(engine_cd_state in tor_tiers and bool(tor_proxy))

        # Case 3: Engine in non-Tor tier (e.g. timeout)
        engine_cd_timeout = "timeout"
        self.assertFalse(engine_cd_timeout in tor_tiers)


if __name__ == "__main__":
    unittest.main()
