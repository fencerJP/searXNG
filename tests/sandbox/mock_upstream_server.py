#!/usr/bin/env python3
"""mock_upstream_server.py — programmable mock search engine server with runtime fault injection."""

import argparse
import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

# Global behavior state per engine: {engine_name: behavior}
# Behaviors: "ok", "empty", "rate_limit", "captcha", "access_denied", "timeout", "server_error"
ENGINE_BEHAVIORS = {
    "mock google": "ok",
    "mock bing": "ok",
    "mock duckduckgo": "ok",
    "mock brave": "ok",
}


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class MockUpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write(f"[MOCK_UPSTREAM {time.strftime('%H:%M:%S')}] {format % args}\n")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Fault injection control endpoint: POST /mock/control
        if path == "/mock/control":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                data = json.loads(body.decode("utf-8"))
                engine = data.get("engine")
                behavior = data.get("behavior")
                if engine and behavior:
                    ENGINE_BEHAVIORS[engine] = behavior
                    if getattr(self.server, "verbose", False):
                        print(f"[MOCK_UPSTREAM] Set engine '{engine}' behavior to '{behavior}'")
                    self._send_json({"status": "ok", "engine": engine, "behavior": behavior}, 200)
                elif data.get("reset_all"):
                    for k in ENGINE_BEHAVIORS:
                        ENGINE_BEHAVIORS[k] = "ok"
                    self._send_json({"status": "ok", "message": "all engines reset to ok"}, 200)
                else:
                    self._send_json({"error": "missing engine or behavior"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._handle_search()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/healthz":
            self._send_json({"status": "ok", "service": "mock-upstream-server", "behaviors": ENGINE_BEHAVIORS}, 200)
            return

        if path == "/mock/status":
            self._send_json({"behaviors": ENGINE_BEHAVIORS}, 200)
            return

        self._handle_search()

    def _handle_search(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query_params = parse_qs(parsed.query)
        q = query_params.get("q", [""])[0]

        # Match engine name based on path
        engine = "mock duckduckgo"
        for candidate in ENGINE_BEHAVIORS:
            sanitized = candidate.replace(" ", "")
            if sanitized in path or candidate in path:
                engine = candidate
                break
        if "engine" in query_params:
            engine = query_params["engine"][0]

        behavior = ENGINE_BEHAVIORS.get(engine, "ok")
        if getattr(self.server, "verbose", False):
            print(f"[MOCK_UPSTREAM] Request for '{engine}' with behavior='{behavior}', query='{q}'")

        if behavior == "ok":
            resp = {
                "results": [
                    {
                        "title": f"Result 1 for {q} from {engine}",
                        "url": f"http://example.org/{engine.replace(' ', '_')}/1?utm_source=promo&ref=feed",
                        "content": f"Relevant search content for {q} provided by {engine}.",
                    },
                    {
                        "title": f"Result 2 for {q} from {engine}",
                        "url": f"http://example.org/{engine.replace(' ', '_')}/2",
                        "content": f"Additional informational snippet for {q} from {engine}.",
                    }
                ]
            }
            self._send_json(resp, 200)
        elif behavior == "with_spam":
            resp = {
                "results": [
                    {
                        "title": f"Valid Result for {q}",
                        "url": f"http://docs.example.org/{engine.replace(' ', '_')}?utm_source=analytics",
                        "content": f"Legitimate content for {q} from {engine}.",
                    },
                    {
                        "title": f"Spam Farm Result for {q}",
                        "url": f"https://geeksforgeeks.org/scraped-post?utm_source=seo",
                        "content": f"Low quality scraped snippet for {q}.",
                    },
                    {
                        "title": f"Scraper Mirror Result for {q}",
                        "url": f"https://scraper-clone.com/search?q={q}",
                        "content": f"Scraper search page for {q}.",
                    }
                ]
            }
            self._send_json(resp, 200)
        elif behavior == "empty":
            self._send_json({"results": []}, 200)
        elif behavior == "rate_limit":
            # HTTP 429
            self._send_json({"error": "Too Many Requests", "message": "Rate limit exceeded (suspended)"}, 429)
        elif behavior == "captcha":
            # HTTP 403 with CAPTCHA challenge
            self._send_json({"error": "CAPTCHA challenge", "message": "Cloudflare CAPTCHA detected"}, 403)
        elif behavior == "access_denied":
            # HTTP 403
            self._send_json({"error": "Forbidden", "message": "Access denied by security policy"}, 403)
        elif behavior == "timeout":
            # Sleep 12 seconds to exceed SearXNG timeout threshold
            time.sleep(12)
            self._send_json({"error": "timeout"}, 504)
        elif behavior == "server_error":
            # HTTP 500
            self._send_json({"error": "Internal Server Error"}, 500)
        else:
            self._send_json({"results": []}, 200)

    def _send_json(self, data: dict, status_code: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description="Mock Upstream Search Engine Server")
    ap.add_argument("--host", default="0.0.0.0", help="Host address to bind")
    ap.add_argument("--port", type=int, default=8890, help="Port to listen on")
    ap.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = ap.parse_args()

    server = ThreadedHTTPServer((args.host, args.port), MockUpstreamHandler)
    server.verbose = args.verbose
    print(f"[MOCK_UPSTREAM] Started on http://{args.host}:{args.port} (verbose={args.verbose})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[MOCK_UPSTREAM] Stopping...")
        server.server_close()


if __name__ == "__main__":
    main()
