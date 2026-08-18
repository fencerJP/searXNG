#!/usr/bin/env python3
"""test_searxng_agent_formatter.py — unit tests for tracking param stripping, HTML cleanup, and markdown context formatting."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from searxng_gateway import (
    clean_text,
    format_agent_json,
    format_agent_markdown,
    strip_tracking_params,
)


class TestSearxngAgentFormatter(unittest.TestCase):
    def test_tracking_params_stripping(self):
        """Test stripping 20+ tracking parameters while preserving clean query parameters."""
        raw_url = "https://example.org/guide?id=42&utm_source=twitter&utm_medium=social&fbclid=IwAR123&ref=header"
        clean = strip_tracking_params(raw_url)
        self.assertEqual(clean, "https://example.org/guide?id=42")

    def test_html_entity_and_tag_cleanup(self):
        """Test decoding HTML entities and stripping residual tags."""
        raw = "Asyncio &amp; <b>Concurrency</b> in Python &quot;3.14&#39;s&quot; modern runtime"
        cleaned = clean_text(raw)
        self.assertEqual(cleaned, "Asyncio & Concurrency in Python \"3.14's\" modern runtime")

    def test_markdown_agent_layout_generation(self):
        """Test compiling high-density markdown context for LLMs."""
        results = [
            {
                "title": "Python Asyncio Docs",
                "url": "https://docs.python.org/3/library/asyncio.html",
                "content": "Asyncio is a library to write concurrent code.",
                "engine": "duckduckgo",
            },
            {
                "title": "CPython Source Code",
                "url": "https://github.com/python/cpython",
                "content": "The Python programming language.",
                "engine": "brave",
            },
        ]

        md = format_agent_markdown("python asyncio", results)
        self.assertIn("# Search Results: python asyncio (2 results)", md)
        self.assertIn("### [1] Python Asyncio Docs", md)
        self.assertIn("- **URL**: https://docs.python.org/3/library/asyncio.html", md)
        self.assertIn("- **Engine**: duckduckgo", md)
        self.assertIn("- **Snippet**: Asyncio is a library to write concurrent code.", md)

    def test_compact_agent_json_structure(self):
        """Test compiling compact agent JSON without redundant SearXNG metadata."""
        results = [
            {"title": "Test Title", "url": "https://example.com", "content": "Snippet text", "engine": "google"}
        ]
        compact = format_agent_json("test query", results)
        self.assertEqual(compact["query"], "test query")
        self.assertEqual(compact["number_of_results"], 1)
        self.assertIn("snippet", compact["results"][0])
        # Ensure redundant fields are omitted
        self.assertNotIn("positions", compact["results"][0])
        self.assertNotIn("template", compact["results"][0])


if __name__ == "__main__":
    unittest.main()
