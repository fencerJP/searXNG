#!/usr/bin/env python3
"""test_searxng_domain_filter.py — unit tests for anti-SEO domain blacklist & scraper heuristics."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from searxng_gateway import (
    clean_and_filter_results,
    is_blacklisted_domain,
    is_scraper_url,
    load_domain_blacklist,
)


class TestSearxngDomainFilter(unittest.TestCase):
    def setUp(self):
        self.blacklist = {"geeksforgeeks.org", "javatpoint.com", "w3schools.com", "spamfarm.xyz"}

    def test_exact_domain_block(self):
        """Test blocking exact root domains in blacklist."""
        self.assertTrue(is_blacklisted_domain("https://geeksforgeeks.org/python-asyncio", self.blacklist))
        self.assertTrue(is_blacklisted_domain("http://javatpoint.com/c-programming", self.blacklist))

    def test_subdomain_wildcard_matching(self):
        """Test blocking subdomains of blacklisted domains."""
        self.assertTrue(is_blacklisted_domain("https://www.geeksforgeeks.org/article", self.blacklist))
        self.assertTrue(is_blacklisted_domain("https://blog.sub.spamfarm.xyz/post/1", self.blacklist))

    def test_legitimate_domains_preserved(self):
        """Test that legitimate domains are not blocked."""
        self.assertFalse(is_blacklisted_domain("https://docs.python.org/3/library/asyncio.html", self.blacklist))
        self.assertFalse(is_blacklisted_domain("https://github.com/searxng/searxng", self.blacklist))
        self.assertFalse(is_blacklisted_domain("https://en.wikipedia.org/wiki/Raft_(algorithm)", self.blacklist))

    def test_scraper_heuristics(self):
        """Test heuristic detection of search aggregators and scraper mirrors."""
        self.assertTrue(is_scraper_url("https://random-scraper.com/search?q=python+asyncio"))
        self.assertTrue(is_scraper_url("https://aggregator.net/find/?query=test"))
        self.assertTrue(is_scraper_url("https://affiliates.com/out/link/?aff=123"))
        self.assertFalse(is_scraper_url("https://docs.python.org/3/search.html?q=asyncio"))

    def test_clean_and_filter_results_batch(self):
        """Test filtering a batch of search results containing both good and bad items."""
        results = [
            {
                "title": "Legitimate Python Asyncio Docs",
                "url": "https://docs.python.org/3/library/asyncio.html?utm_source=google",
                "content": "Asyncio is a library...",
                "engine": "duckduckgo",
            },
            {
                "title": "GeeksForGeeks Scraped Article",
                "url": "https://www.geeksforgeeks.org/asyncio-in-python/",
                "content": "Click here to read...",
                "engine": "google",
            },
            {
                "title": "Raw Scraper Search Query",
                "url": "https://cheap-search.com/search?q=asyncio",
                "content": "Search results for asyncio",
                "engine": "bing",
            },
            {
                "title": "GitHub Asyncio Implementation",
                "url": "https://github.com/python/cpython/tree/main/Lib/asyncio",
                "content": "CPython asyncio implementation",
                "engine": "duckduckgo",
            },
        ]

        cleaned, filtered_count, filtered_domains = clean_and_filter_results(results, self.blacklist)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(filtered_count, 2)
        self.assertEqual(cleaned[0]["title"], "Legitimate Python Asyncio Docs")
        self.assertEqual(cleaned[0]["url"], "https://docs.python.org/3/library/asyncio.html")  # utm_ tag stripped!
        self.assertEqual(cleaned[1]["title"], "GitHub Asyncio Implementation")


if __name__ == "__main__":
    unittest.main()
