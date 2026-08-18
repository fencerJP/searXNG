#!/usr/bin/env bash
# run_sandbox_tests.sh — self-contained end-to-end test orchestrator for SearXNG sandbox
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TOOLS_DIR="$REPO_ROOT/tools"
TESTS_DIR="$REPO_ROOT/tests"
VERBOSE_FLAG="${1:-}"

GATEWAY_PID=""

cleanup() {
    echo ""
    echo "================================================================================"
    echo "                      TEARING DOWN TEST SANDBOX"
    echo "================================================================================"
    if [ -n "$GATEWAY_PID" ]; then
        echo "[SANDBOX] Stopping test gateway (PID: $GATEWAY_PID)..."
        kill -9 "$GATEWAY_PID" 2>/dev/null || true
    fi
    echo "[SANDBOX] Shutting down isolated sandbox docker containers..."
    docker compose -f "$SCRIPT_DIR/docker-compose.sandbox.yml" down -v 2>/dev/null || true
    rm -f "$SCRIPT_DIR/test_engine.db" "$SCRIPT_DIR/test_engine.db-shm" "$SCRIPT_DIR/test_engine.db-wal"
    echo "[SANDBOX] Cleanup complete."
}

trap cleanup EXIT INT TERM

echo "================================================================================"
echo "           STARTING FULL ISOLATED SEARXNG SANDBOX TEST SUITE"
echo "================================================================================"
echo "  Sandbox directory : $SCRIPT_DIR"
echo "  Tools directory   : $TOOLS_DIR"
echo "  Tests directory   : $TESTS_DIR"
echo "  Production check  : Production port 8082 is STRICTLY UNTOUCHED."
echo ""

# 1. Start Sandbox Docker Containers (Mock Upstream, Valkey, SearXNG)
echo "[Step 1/6] Starting Isolated Sandbox Docker Containers (8888, 8890)..."
docker compose -f "$SCRIPT_DIR/docker-compose.sandbox.yml" up -d --force-recreate

echo "  -> Waiting for Mock Upstream Search Engine (http://localhost:8890/healthz)..."
for i in {1..30}; do
    if curl -s http://127.0.0.1:8890/healthz >/dev/null 2>&1; then
        echo "  ✓ Mock Upstream Server is healthy."
        break
    fi
    sleep 0.3
done

echo "  -> Waiting for sandbox SearXNG instance (http://localhost:8888/config)..."
for i in {1..50}; do
    if curl -s http://127.0.0.1:8888/config >/dev/null 2>&1; then
        echo "  ✓ Sandbox SearXNG Core is up and healthy."
        break
    fi
    sleep 0.5
done

# 2. Verify ResultContainer in container
echo ""
echo "[Step 2/6] Verifying ResultContainer post-close patch inside Docker sandbox container..."
docker exec searxng-test-core /usr/local/searxng/.venv/bin/python3 -c "
from searx.results import ResultContainer
c = ResultContainer()
c.close()
c.add_unresponsive_engine('dummy_engine', 'timeout')
c.add_timing('dummy_engine', 1.0, 0.5)
assert len(c.unresponsive_engines) == 0
assert len(c.timings) == 0
print('  ✓ ResultContainer post-close patch verified inside container (no errors logged).')
"

# 3. Start Sandbox Gateway Proxy on port 8880
echo ""
echo "[Step 3/6] Starting Test Gateway Proxy on port 8880..."
if [ "$VERBOSE_FLAG" = "-v" ] || [ "$VERBOSE_FLAG" = "--verbose" ]; then
    /usr/bin/python3 "$TOOLS_DIR/searxng_gateway.py" \
        --port 8880 \
        --upstream http://127.0.0.1:8888 \
        --db "$SCRIPT_DIR/test_engine.db" \
        --blacklist-file "$TOOLS_DIR/domain_blacklist.txt" \
        --tor-proxy "http://127.0.0.1:8890" \
        -v &
else
    /usr/bin/python3 "$TOOLS_DIR/searxng_gateway.py" \
        --port 8880 \
        --upstream http://127.0.0.1:8888 \
        --db "$SCRIPT_DIR/test_engine.db" \
        --blacklist-file "$TOOLS_DIR/domain_blacklist.txt" \
        --tor-proxy "http://127.0.0.1:8890" &
fi
GATEWAY_PID=$!

for i in {1..30}; do
    if curl -s http://127.0.0.1:8880/healthz >/dev/null 2>&1; then
        echo "  ✓ Test Gateway Proxy is healthy and ready."
        break
    fi
    sleep 0.2
done

# 4. Run Unit & Integration Test Suite
echo ""
echo "[Step 4/6] Executing Unit & Integration Test Suites (Verbose Mode)..."
/usr/bin/python3 -m unittest discover -s "$TESTS_DIR" -p "test_searxng_*.py" -v

# 5. Run Realistic Multi-Agent Client Simulation
echo ""
echo "[Step 5/6] Running Realistic Multi-Agent Client Search Simulation..."
if [ "$VERBOSE_FLAG" = "-v" ] || [ "$VERBOSE_FLAG" = "--verbose" ]; then
    /usr/bin/python3 "$SCRIPT_DIR/realistic_client_simulation.py" -v
else
    /usr/bin/python3 "$SCRIPT_DIR/realistic_client_simulation.py"
fi

# 6. Verify Engine Probe & Metrics against Sandbox DB
echo ""
echo "[Step 6/6] Executing Engine Probe Sweep & Metrics against Sandbox DB..."
/usr/bin/python3 "$TOOLS_DIR/searxng_engine_probe.py" \
    --searxng http://127.0.0.1:8888 \
    --db "$SCRIPT_DIR/test_engine.db" \
    --run "sandbox-sweep" \
    --parallel 4 \
    -v

echo ""
echo "  -> Displaying 24h Engine Metrics & Latency Percentiles..."
/usr/bin/python3 "$TOOLS_DIR/searxng_engine_probe.py" \
    --searxng http://127.0.0.1:8888 \
    --db "$SCRIPT_DIR/test_engine.db" \
    --metrics -v

echo ""
echo "================================================================================"
echo "       ✓ ALL SANDBOX TESTS & REALISTIC CLIENT SIMULATIONS PASSED (100%)"
echo "================================================================================"
