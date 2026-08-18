#!/usr/bin/env python3
"""searxng_gateway.py — low-overhead HTTP rate-protection, caching, anti-SEO, and Tor routing gateway for SearXNG.

Sits between clients (such as OpenClaw) and SearXNG:
  - Per-engine query & snippet caching with pre-filtering.
  - Pre-storage Anti-SEO & scraper domain pruning.
  - Token-optimized LLM context formatting (/search/agent, format=agent).
  - Optional selective Tor proxy routing (by engine or cooldown tier).
  - Real-time engine health and deterministic cooldown feedback.
"""

import argparse
import datetime
import html
import json
import os
import re
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse, parse_qsl

import requests

from searxng_policy import (
    apply_cooldown,
    classify,
    compute_engine_metrics,
    get_active_cooldowns,
    get_db_connection,
    get_engine_cached_results,
    init_db,
    log_duplicate_candidate,
    normalize_cooldown,
    store_engine_cached_results,
)

DEFAULT_PORT = 8880
DEFAULT_UPSTREAM = "http://localhost:8082"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "searxng_engine.db")
DEFAULT_BLACKLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domain_blacklist.txt")
DEFAULT_TOR_TIERS = "captcha,access_denied,suspended"

# Tracking query parameters to strip
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "fbclid", "gclid", "msclkid", "spm", "igshid",
    "session_id", "sessionid", "aff_id", "affid", "campaign_id",
    "source", "feature", "cid", "cmpid", "yclid", "mc_cid", "mc_eid"
}

BUILTIN_SPAM_DOMAINS = {
    "geeksforgeeks.org", "javatpoint.com", "w3schools.com", "tutorialspoint.com",
    "guru99.com", "c-sharpcorner.com", "codechef.com", "programiz.com",
    "studytonight.com", "scaler.com", "simplilearn.com", "quora.com",
    "pinterest.com", "softpedia.com", "filehorse.com", "findanswers.org",
    "answer-drive.com", "techcult.com", "appuals.com", "windowsreport.com",
    "couponbirds.com", "dontpayfull.com", "retailmenot.com", "dealspotr.com"
}


def load_domain_blacklist(file_path: str) -> set:
    """Load domain blacklist from file + builtins."""
    domains = set(BUILTIN_SPAM_DOMAINS)
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip().lower()
                    if line and not line.startswith("#"):
                        domains.add(line)
        except Exception as e:
            sys.stderr.write(f"[GATEWAY] Warning loading blacklist file '{file_path}': {e}\n")
    return domains


def is_blacklisted_domain(url: str, blacklist: set) -> bool:
    """Check if URL belongs to a blacklisted root domain or subdomain."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        for bl in blacklist:
            if host == bl or host.endswith("." + bl):
                return True
    except Exception:
        pass
    return False


def is_scraper_url(url: str) -> bool:
    """Heuristic check for search aggregator/scraper mirror links."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        if path.rstrip("/") in ("/search", "/find", "/results", "/query") and ("q=" in query or "query=" in query):
            return True
        if "/out/link" in path or "/aff/" in path or "/go/link" in path or "aff=" in query:
            return True
    except Exception:
        pass
    return False


def strip_tracking_params(url: str) -> str:
    """Strip tracking and analytics query parameters from URL."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        pairs = parse_qsl(parsed.query, keep_blank_values=False)
        clean_pairs = [
            (k, v) for k, v in pairs
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
        ]
        clean_query = urlencode(clean_pairs)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, clean_query, parsed.fragment))
    except Exception:
        return url


def clean_text(text: str, max_len: int = 0) -> str:
    """Clean text by decoding HTML entities, removing HTML tags, and collapsing whitespace."""
    if not text:
        return ""
    # Decode HTML entities
    text = html.unescape(text)
    # Remove leftover HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "..."
    return text


def clean_and_filter_results(results: list, blacklist: set, max_snippet_len: int = 300) -> tuple:
    """Pre-storage result sanitizer: prunes spam, strips tracking params, and cleans text.
    
    Returns (cleaned_results, filtered_count, filtered_domains).
    """
    cleaned = []
    filtered_count = 0
    filtered_domains = []
    seen_urls = set()

    for item in results:
        raw_url = item.get("url", "")
        # 1. Anti-SEO Domain & Scraper Check
        if is_blacklisted_domain(raw_url, blacklist) or is_scraper_url(raw_url):
            filtered_count += 1
            try:
                host = urlparse(raw_url).hostname or raw_url
                if host not in filtered_domains:
                    filtered_domains.append(host)
            except Exception:
                pass
            continue

        # 2. Clean URL
        clean_url = strip_tracking_params(raw_url)
        norm_url_key = clean_url.rstrip("/").lower()
        if norm_url_key in seen_urls:
            continue
        seen_urls.add(norm_url_key)

        # 3. Clean Title & Content
        title = clean_text(item.get("title", ""))
        content = clean_text(item.get("content", ""), max_len=max_snippet_len)
        engine = item.get("engine", "")

        cleaned_item = {
            "title": title,
            "url": clean_url,
            "content": content,
            "engine": engine,
        }
        # Keep optional metadata if present
        if "publishedDate" in item:
            cleaned_item["publishedDate"] = item["publishedDate"]
        cleaned.append(cleaned_item)

    return cleaned, filtered_count, filtered_domains


def format_agent_markdown(query: str, results: list, max_snippet_len: int = 300) -> str:
    """Compile high-density LLM markdown context."""
    lines = [f"# Search Results: {query} ({len(results)} results)\n"]
    for idx, item in enumerate(results, 1):
        title = item.get("title", "Untitled")
        url = item.get("url", "")
        engine = item.get("engine", "")
        snippet = item.get("content", "")
        lines.append(f"### [{idx}] {title}")
        lines.append(f"- **URL**: {url}")
        if engine:
            lines.append(f"- **Engine**: {engine}")
        lines.append(f"- **Snippet**: {snippet}\n")
    return "\n".join(lines)


def format_agent_json(query: str, results: list) -> dict:
    """Compile compact token-reduced JSON representation."""
    return {
        "query": query,
        "number_of_results": len(results),
        "results": [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "engine": r.get("engine", ""),
            }
            for r in results
        ],
    }


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server to handle concurrent search queries."""
    daemon_threads = True


class SearxngGatewayHandler(BaseHTTPRequestHandler):
    """HTTP Request handler for proxying, caching, filtering, and enforcing SearXNG policies."""

    def log_message(self, format, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write(f"[GATEWAY {datetime.datetime.now().strftime('%H:%M:%S')}] {format % args}\n")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        verbose = getattr(self.server, "verbose", False)
        upstream = getattr(self.server, "upstream", DEFAULT_UPSTREAM)
        db_path = getattr(self.server, "db_path", DEFAULT_DB)

        # Internal Gateway endpoints
        if path == "/healthz":
            self._send_json({"status": "ok", "service": "searxng-gateway", "upstream": upstream}, 200)
            return

        if path == "/gateway/status":
            try:
                con = get_db_connection(db_path)
                active = get_active_cooldowns(con)
                metrics = compute_engine_metrics(con, hours=24)
                cached_queries_count = con.execute("SELECT COUNT(*) FROM query_engine_index").fetchone()[0]
                unique_snippets_count = con.execute("SELECT COUNT(*) FROM snippet_store").fetchone()[0]
                pending_duplicates_count = con.execute("SELECT COUNT(*) FROM duplicate_candidates WHERE status='PENDING'").fetchone()[0]
                con.close()
                self._send_json({
                    "status": "ok",
                    "upstream": upstream,
                    "active_cooldowns_count": len(active),
                    "active_cooldowns": active,
                    "cached_queries_count": cached_queries_count,
                    "unique_snippets_count": unique_snippets_count,
                    "pending_duplicates_count": pending_duplicates_count,
                    "metrics_24h": metrics,
                    "tor_proxy": getattr(self.server, "tor_proxy", None),
                    "tor_engines": sorted(list(getattr(self.server, "tor_engines", []))),
                    "tor_tiers": sorted(list(getattr(self.server, "tor_tiers", []))),
                }, 200)
            except Exception as e:
                self._send_json({"status": "error", "error": str(e)}, 500)
            return

        flat_params = {k: v[-1] for k, v in query_params.items()}

        # Search routes: standard /search or dedicated /search/agent
        if path in ("/search", "/search/agent"):
            if path == "/search/agent" and "format" not in flat_params:
                flat_params["format"] = "agent"
            self._handle_search(flat_params, upstream, db_path, verbose)
            return

        # Passthrough all other routes
        self._proxy_passthrough("GET", path, flat_params, upstream, verbose)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        verbose = getattr(self.server, "verbose", False)
        upstream = getattr(self.server, "upstream", DEFAULT_UPSTREAM)
        db_path = getattr(self.server, "db_path", DEFAULT_DB)

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        flat_params = {k: v[-1] for k, v in query_params.items()}
        if path in ("/search", "/search/agent"):
            if path == "/search/agent" and "format" not in flat_params:
                flat_params["format"] = "agent"
            self._handle_search_post(flat_params, body, upstream, db_path, verbose)
            return

        self._proxy_passthrough_post(path, flat_params, body, upstream, verbose)

    def _handle_search(self, params: dict, upstream: str, db_path: str, verbose: bool):
        t0 = time.monotonic()
        con = get_db_connection(db_path)
        init_db(con)

        query = params.get("q", "").strip()
        category = params.get("categories", "").strip()
        requested_engines_str = params.get("engines", "").strip()
        req_format = params.get("format", "json").lower()
        is_fresh = params.get("fresh") == "1" or self.headers.get("Cache-Control") == "no-cache"
        
        blacklist = getattr(self.server, "blacklist", BUILTIN_SPAM_DOMAINS)
        tor_proxy = getattr(self.server, "tor_proxy", None)
        tor_engines = getattr(self.server, "tor_engines", set())
        tor_tiers = getattr(self.server, "tor_tiers", set())
        max_snippet_len = getattr(self.server, "max_snippet_len", 300)

        requested_engines = [e.strip() for e in requested_engines_str.split(",") if e.strip()] if requested_engines_str else []

        cached_results = []
        cached_engines = []
        uncached_engines = []

        now = time.time()

        # 1. Per-Engine Cache Lookup (Runs BEFORE Cooldown Filtering)
        if not is_fresh and requested_engines:
            for eng in requested_engines:
                c_res = get_engine_cached_results(con, query, eng, category, now=now)
                if c_res is not None:
                    cached_results.extend(c_res)
                    cached_engines.append(eng)
                else:
                    uncached_engines.append(eng)
        else:
            uncached_engines = list(requested_engines)

        # Determine Cache Status Header
        if not requested_engines:
            cache_status = "MISS"
        elif len(cached_engines) == len(requested_engines):
            cache_status = "HIT"
        elif len(cached_engines) > 0:
            cache_status = "PARTIAL"
        else:
            cache_status = "MISS"

        fresh_results = []
        excluded_engines = []
        tor_routed_engines = []
        fresh_queried_engines = []
        unresponsive_feedback = []

        # 2. If uncached engines remain, evaluate cooldowns and Tor routing
        if uncached_engines or not requested_engines:
            active_cds = get_active_cooldowns(con, now=now)
            direct_engines = []
            
            for eng in uncached_engines:
                cd_info = active_cds.get(eng)
                if cd_info and cd_info.get("remaining_seconds", 0) > 0:
                    last_type = cd_info.get("last_type", "")
                    rem_sec = cd_info.get("remaining_seconds", 0)
                    # Check if in Tor routing tiers
                    if tor_proxy and last_type in tor_tiers:
                        tor_routed_engines.append(eng)
                    else:
                        excluded_engines.append((eng, rem_sec, last_type))
                else:
                    if tor_proxy and eng in tor_engines:
                        tor_routed_engines.append(eng)
                    else:
                        direct_engines.append(eng)

            engines_to_query = direct_engines + tor_routed_engines

            if uncached_engines and not engines_to_query:
                # All requested uncached engines are excluded
                if verbose:
                    print(f"[GATEWAY] All uncached engines in cooldown: {excluded_engines}")
            else:
                # Prepare upstream parameters
                upstream_params = dict(params)
                # Ensure upstream returns json format for parsing
                upstream_params["format"] = "json"
                if engines_to_query:
                    upstream_params["engines"] = ",".join(engines_to_query)
                    fresh_queried_engines = list(engines_to_query)

                headers = {
                    "X-Forwarded-For": self.headers.get("X-Forwarded-For", "127.0.0.1"),
                    "X-Real-IP": self.headers.get("X-Real-IP", "127.0.0.1"),
                    "User-Agent": self.headers.get("User-Agent", "OpenClaw-SearxngGateway/1.0"),
                    "Accept": "application/json",
                }

                target_url = f"{upstream}/search"
                if verbose:
                    print(f"[GATEWAY] -> Forwarding to {target_url}?{urlencode(upstream_params)}")

                # Use Tor proxy if any engines require Tor routing
                req_proxies = None
                if tor_routed_engines and tor_proxy:
                    req_proxies = {"http": tor_proxy, "https": tor_proxy}
                    if verbose:
                        print(f"[GATEWAY] Routing query for {tor_routed_engines} through Tor proxy: {tor_proxy}")

                try:
                    r = requests.get(target_url, params=upstream_params, headers=headers, proxies=req_proxies, timeout=30)
                    if r.status_code == 200:
                        data = r.json()
                        raw_fresh = data.get("results") or []
                        unresp = data.get("unresponsive_engines") or []
                        unresponsive_feedback = unresp

                        # Process engine cooldowns & reset-on-success feedback
                        self._process_search_response_feedback(con, data, verbose)

                        # 3. Pre-Storage Filtering: Anti-SEO & URL Tracking Stripping
                        clean_fresh, f_cnt, f_doms = clean_and_filter_results(raw_fresh, blacklist, max_snippet_len)
                        fresh_results = clean_fresh

                        # Log semantic duplicate candidates for idle verification
                        self._check_and_log_duplicate_candidates(con, clean_fresh, verbose)

                        # 4. Store Cleaned Results Per Engine in SQLite Cache
                        # Partition fresh results by engine
                        by_engine = {}
                        for item in clean_fresh:
                            eng_name = item.get("engine")
                            if eng_name:
                                by_engine.setdefault(eng_name, []).append(item)

                        for eng_name, eng_res in by_engine.items():
                            store_engine_cached_results(con, query, eng_name, category, eng_res, ttl_seconds=86400, now=now)
                            if verbose:
                                print(f"[GATEWAY] Cached {len(eng_res)} clean snippets for engine '{eng_name}' (query='{query}')")
                    else:
                        if verbose:
                            print(f"[GATEWAY] Upstream returned status {r.status_code}: {r.text}")
                except Exception as e:
                    if verbose:
                        print(f"[GATEWAY] Error contacting upstream SearXNG: {e}")

        con.close()

        # 5. Merge Cached and Fresh Clean Results
        all_results = list(cached_results) + list(fresh_results)
        # Deduplicate merged results by normalized clean URL
        merged_results = []
        seen_urls = set()
        for item in all_results:
            u_key = item.get("url", "").rstrip("/").lower()
            if u_key not in seen_urls:
                seen_urls.add(u_key)
                merged_results.append(item)

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # 6. Format Response
        if req_format in ("agent", "agent_markdown"):
            formatted_body = format_agent_markdown(query, merged_results, max_snippet_len).encode("utf-8")
            content_type = "text/markdown; charset=utf-8"
        elif req_format == "agent_json":
            compact_dict = format_agent_json(query, merged_results)
            formatted_body = json.dumps(compact_dict, indent=2).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            # Standard SearXNG JSON compatible structure
            resp_dict = {
                "query": query,
                "results": merged_results,
                "answers": [],
                "corrections": [],
                "infoboxes": [],
                "suggestions": [],
                "unresponsive_engines": [
                    [e, f"cooldown_active ({s}s left, {t})"] for e, s, t in excluded_engines
                ] + unresponsive_feedback,
                "number_of_results": len(merged_results),
            }
            formatted_body = json.dumps(resp_dict, indent=2).encode("utf-8")
            content_type = "application/json; charset=utf-8"

        # Calculate Token/Character Reduction vs verbose raw JSON
        raw_est_chars = max(1, len(merged_results) * 450 + 200)
        reduction_pct = max(0, int((1.0 - (len(formatted_body) / raw_est_chars)) * 100)) if len(formatted_body) < raw_est_chars else 0

        # Send HTTP Response
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(formatted_body)))
        self.send_header("X-Cache", cache_status)
        self.send_header("X-Gateway-Latency-Ms", str(elapsed_ms))
        if cached_engines:
            self.send_header("X-Cached-Engines", ",".join(cached_engines))
        if fresh_queried_engines:
            self.send_header("X-Fresh-Engines", ",".join(fresh_queried_engines))
        if excluded_engines:
            self.send_header("X-Excluded-Engines", ",".join(e for e, _, _ in excluded_engines))
        if tor_routed_engines:
            self.send_header("X-Tor-Routed-Engines", ",".join(tor_routed_engines))
        self.send_header("X-Token-Reduction-Pct", f"{reduction_pct}%")
        self.end_headers()
        self.wfile.write(formatted_body)

    def _check_and_log_duplicate_candidates(self, con, results: list, verbose: bool):
        """Detect and log candidate duplicates sharing domain and high title similarity."""
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                u1 = results[i].get("url", "").strip()
                u2 = results[j].get("url", "").strip()
                if not u1 or not u2 or u1 == u2:
                    continue
                p1, p2 = urlparse(u1), urlparse(u2)
                d1, d2 = (p1.hostname or "").lower(), (p2.hostname or "").lower()
                if d1 and d1 == d2 and p1.path != p2.path:
                    t1 = set(results[i].get("title", "").lower().split())
                    t2 = set(results[j].get("title", "").lower().split())
                    if t1 and t2:
                        sim = len(t1 & t2) / float(len(t1 | t2))
                        if sim >= 0.70:
                            log_duplicate_candidate(con, u1, u2, d1, sim)
                            if verbose:
                                print(f"[GATEWAY] Logged candidate duplicate pair ({sim:.2f}): '{u1}' vs '{u2}'")

    def _process_search_response_feedback(self, con, data: dict, verbose: bool):
        now = time.time()
        unresponsive = data.get("unresponsive_engines") or []
        for item in unresponsive:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                engine_name, reason_str = item[0], item[1]
            elif isinstance(item, str):
                engine_name, reason_str = item, "unresponsive"
            else:
                continue

            state, _, _, _ = classify(engine_name, {"unresponsive_engines": [[engine_name, reason_str]], "results": []})
            cd_applied = apply_cooldown(con, engine_name, state, now=now, verbose=verbose)
            if verbose and cd_applied > 0:
                print(f"[GATEWAY] Applied real-time cooldown on '{engine_name}': {state} -> {cd_applied}s")

        # Reset on success
        results = data.get("results") or []
        successful_engines = {res.get("engine") for res in results if res.get("engine")}
        for eng in successful_engines:
            apply_cooldown(con, eng, "ok", now=now, verbose=verbose)

    def _handle_search_post(self, params: dict, body: bytes, upstream: str, db_path: str, verbose: bool):
        headers = {
            "X-Forwarded-For": self.headers.get("X-Forwarded-For", "127.0.0.1"),
            "X-Real-IP": self.headers.get("X-Real-IP", "127.0.0.1"),
            "Content-Type": self.headers.get("Content-Type", "application/x-www-form-urlencoded"),
            "User-Agent": self.headers.get("User-Agent", "OpenClaw-SearxngGateway/1.0"),
        }
        try:
            r = requests.post(f"{upstream}/search", params=params, data=body, headers=headers, timeout=30)
            self.send_response(r.status_code)
            for h, v in r.headers.items():
                if h.lower() not in ("transfer-encoding", "content-length", "content-encoding"):
                    self.send_header(h, v)
            self.send_header("Content-Length", str(len(r.content)))
            self.end_headers()
            self.wfile.write(r.content)
        except Exception as e:
            self._send_json({"error": "upstream_unavailable", "detail": str(e)}, 502)

    def _proxy_passthrough(self, method: str, path: str, params: dict, upstream: str, verbose: bool):
        headers = {
            "X-Forwarded-For": self.headers.get("X-Forwarded-For", "127.0.0.1"),
            "X-Real-IP": self.headers.get("X-Real-IP", "127.0.0.1"),
            "User-Agent": self.headers.get("User-Agent", "OpenClaw-SearxngGateway/1.0"),
        }
        try:
            r = requests.get(f"{upstream}{path}", params=params, headers=headers, timeout=30)
            self.send_response(r.status_code)
            for h, v in r.headers.items():
                if h.lower() not in ("transfer-encoding", "content-length", "content-encoding"):
                    self.send_header(h, v)
            self.send_header("Content-Length", str(len(r.content)))
            self.end_headers()
            self.wfile.write(r.content)
        except Exception as e:
            self._send_json({"error": "upstream_unavailable", "detail": str(e)}, 502)

    def _proxy_passthrough_post(self, path: str, params: dict, body: bytes, upstream: str, verbose: bool):
        headers = {
            "X-Forwarded-For": self.headers.get("X-Forwarded-For", "127.0.0.1"),
            "X-Real-IP": self.headers.get("X-Real-IP", "127.0.0.1"),
            "Content-Type": self.headers.get("Content-Type", "application/octet-stream"),
            "User-Agent": self.headers.get("User-Agent", "OpenClaw-SearxngGateway/1.0"),
        }
        try:
            r = requests.post(f"{upstream}{path}", params=params, data=body, headers=headers, timeout=30)
            self.send_response(r.status_code)
            for h, v in r.headers.items():
                if h.lower() not in ("transfer-encoding", "content-length", "content-encoding"):
                    self.send_header(h, v)
            self.send_header("Content-Length", str(len(r.content)))
            self.end_headers()
            self.wfile.write(r.content)
        except Exception as e:
            self._send_json({"error": "upstream_unavailable", "detail": str(e)}, 502)

    def _send_json(self, data: dict, status_code: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(
        description="SearXNG Gateway Proxy with Caching, Anti-SEO Filtering, and Tor Routing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--host", default="127.0.0.1", help="Host address to bind gateway server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="Base URL of upstream SearXNG instance")
    ap.add_argument("--db", default=DEFAULT_DB, help="Path to SQLite results and cooldown database")
    ap.add_argument("--blacklist-file", default=DEFAULT_BLACKLIST, help="Path to domain blacklist text file")
    ap.add_argument("--tor-proxy", default=None, help="Tor SOCKS5/HTTP proxy URL (e.g. socks5://127.0.0.1:9050)")
    ap.add_argument("--tor-engines", default="", help="Comma-separated list of engines to always route via Tor")
    ap.add_argument("--tor-tiers", default=DEFAULT_TOR_TIERS, help="Comma-separated cooldown tiers triggering Tor fallback")
    ap.add_argument("--max-snippet-len", type=int, default=300, help="Maximum snippet character length for LLM context")
    ap.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = ap.parse_args()

    # Pre-initialize DB schema
    con = get_db_connection(args.db)
    init_db(con, verbose=args.verbose)
    con.close()

    server = ThreadedHTTPServer((args.host, args.port), SearxngGatewayHandler)
    server.upstream = args.upstream.rstrip("/")
    server.db_path = args.db
    server.verbose = args.verbose
    server.blacklist = load_domain_blacklist(args.blacklist_file)
    server.tor_proxy = args.tor_proxy
    server.tor_engines = {e.strip().lower() for e in args.tor_engines.split(",") if e.strip()}
    server.tor_tiers = {t.strip().lower() for t in args.tor_tiers.split(",") if t.strip()}
    server.max_snippet_len = args.max_snippet_len

    print(f"[GATEWAY] Started SearXNG Gateway on http://{args.host}:{args.port}")
    print(f"[GATEWAY] Upstream SearXNG target: {server.upstream}")
    print(f"[GATEWAY] Cooldown & Cache Database: {server.db_path}")
    print(f"[GATEWAY] Blacklist domains loaded: {len(server.blacklist)}")
    if server.tor_proxy:
        print(f"[GATEWAY] Tor Proxy: {server.tor_proxy} (engines: {server.tor_engines or 'all on tier'}, tiers: {server.tor_tiers})")
    print(f"[GATEWAY] Ready to serve queries with per-engine caching and rate protection.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[GATEWAY] Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
