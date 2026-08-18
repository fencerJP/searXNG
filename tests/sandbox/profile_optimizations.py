#!/usr/bin/env python3
"""profile_optimizations.py — profiling tool to measure raw vs DB vs LLM token usage and storage space."""

import json
import os
import sqlite3
import sys
import time
import zlib
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
from searxng_policy import get_db_connection

GATEWAY_URL = "http://127.0.0.1:8880"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_engine.db")

SAMPLE_QUERIES = [
    ("python asyncio taskgroup tutorial", "mock duckduckgo,mock brave"),
    ("raft vs paxos distributed consensus", "mock google,mock bing"),
    ("linux io_uring zero-copy networking", "mock duckduckgo,mock brave"),
    ("vllm pagedattention memory optimization", "mock google,mock brave"),
    ("quantum computing decoherence time", "mock bing,mock duckduckgo"),
]


def estimate_tokens(text: str) -> int:
    """Rough estimation of LLM tokens (~4 chars per token for English text/markdown)."""
    return max(1, len(text) // 4)


def run_profiling():
    print("================================================================================")
    print("        POST-VERIFICATION TOKEN USAGE & STORAGE SPACE PROFILING")
    print("================================================================================")

    # 1. Execute sample queries via Gateway
    print("\n[Step 1] Issuing representative sample queries through the Gateway...")
    profiling_data = []

    for q, engs in SAMPLE_QUERIES:
        # 1a. Standard JSON
        r_json = requests.get(f"{GATEWAY_URL}/search", params={"q": q, "engines": engs, "fresh": "1"}, timeout=10)
        json_len = len(r_json.content)
        json_tokens = estimate_tokens(r_json.text)

        # 1b. Agent Markdown
        r_agent = requests.get(f"{GATEWAY_URL}/search/agent", params={"q": q, "engines": engs}, timeout=10)
        md_len = len(r_agent.content)
        md_tokens = estimate_tokens(r_agent.text)

        # 1c. Agent Compact JSON
        r_cjson = requests.get(f"{GATEWAY_URL}/search", params={"q": q, "engines": engs, "format": "agent_json"}, timeout=10)
        cjson_len = len(r_cjson.content)
        cjson_tokens = estimate_tokens(r_cjson.text)

        char_saved = json_len - md_len
        reduction_pct = (char_saved / json_len) * 100 if json_len > 0 else 0

        profiling_data.append({
            "query": q,
            "engines": engs,
            "raw_json_bytes": json_len,
            "raw_json_tokens": json_tokens,
            "compact_json_bytes": cjson_len,
            "compact_json_tokens": cjson_tokens,
            "agent_md_bytes": md_len,
            "agent_md_tokens": md_tokens,
            "token_reduction_pct": reduction_pct,
        })
        print(f"  Query: '{q}'")
        print(f"    - Raw JSON     : {json_len:5d} bytes | ~{json_tokens:4d} tokens")
        print(f"    - Compact JSON : {cjson_len:5d} bytes | ~{cjson_tokens:4d} tokens (-{((json_len - cjson_len)/json_len)*100:.1f}%)")
        print(f"    - Agent MD     : {md_len:5d} bytes | ~{md_tokens:4d} tokens (-{reduction_pct:.1f}%)")

    # 2. Analyze Normalized Database Storage
    print(f"\n[Step 2] Analyzing Normalized SQLite Storage & Entry Layout in {DB_PATH}...")
    con = get_db_connection(DB_PATH)
    qe_rows = con.execute("SELECT query, engine, result_count, url_hashes_json FROM query_engine_index").fetchall()
    snip_rows = con.execute("SELECT url, title, content_blob FROM snippet_store").fetchall()

    total_uncompressed_bytes = 0
    total_zlib_compressed_bytes = 0

    print(f"\n  Found {len(qe_rows)} query index rows and {len(snip_rows)} unique snippets in SQLite:")
    for url, title, blob in snip_rows:
        blob_len = len(blob)
        try:
            raw_text = zlib.decompress(blob).decode("utf-8")
            raw_len = len(raw_text.encode("utf-8"))
        except Exception:
            raw_len = blob_len
        total_uncompressed_bytes += raw_len
        total_zlib_compressed_bytes += blob_len
        print(f"    [Snippet] '{url[:40]:40s}' | decompressed: {raw_len:4d}B | zlib BLOB: {blob_len:4d}B (-{((raw_len-blob_len)/raw_len)*100:.1f}%)")

    con.close()

    # 3. Summary Metrics
    print("\n" + "="*80)
    print("                    AGGREGATED PROFILING SUMMARY")
    print("="*80)
    avg_json_tokens = sum(d["raw_json_tokens"] for d in profiling_data) / len(profiling_data)
    avg_md_tokens = sum(d["agent_md_tokens"] for d in profiling_data) / len(profiling_data)
    avg_reduction = sum(d["token_reduction_pct"] for d in profiling_data) / len(profiling_data)

    print(f"  • Average Prompt Tokens per Search (Raw JSON) : {avg_json_tokens:.1f} tokens")
    print(f"  • Average Prompt Tokens per Search (Agent MD) : {avg_md_tokens:.1f} tokens")
    print(f"  • Net Context Window Token Savings           : {avg_reduction:.1f}% reduction")
    print(f"  • Total Cached Queries in Index              : {len(qe_rows)}")
    print(f"  • Total Unique Snippets Stored               : {len(snip_rows)}")
    if total_uncompressed_bytes > 0:
        ratio = ((total_uncompressed_bytes - total_zlib_compressed_bytes) / total_uncompressed_bytes) * 100
        print(f"  • Total Snippet Text (Uncompressed)          : {total_uncompressed_bytes} bytes")
        print(f"  • Total SQLite BLOB Storage (zlib)           : {total_zlib_compressed_bytes} bytes ({ratio:.1f}% compression ratio)")
    print("="*80)


if __name__ == "__main__":
    run_profiling()
