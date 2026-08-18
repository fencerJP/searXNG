#!/usr/bin/env bash
# searxng_daily_sweep.sh — scheduled daily health sweep and cooldown calculation
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEARXNG_URL="${SEARXNG_URL:-http://localhost:8888}"
DB_PATH="${DB_PATH:-$SCRIPT_DIR/searxng_engine.db}"
LOG_DIR="$SCRIPT_DIR/notes"
LOG_FILE="$LOG_DIR/searxng_daily.log"
VERBOSE_FLAG="${1:-}"

mkdir -p "$LOG_DIR"

TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================================" >> "$LOG_FILE"
echo "[$TIMESTAMP] Starting daily SearXNG health sweep" >> "$LOG_FILE"
echo "  Target instance : $SEARXNG_URL" >> "$LOG_FILE"
echo "  Database        : $DB_PATH" >> "$LOG_FILE"

RUN_ID="daily-$(date '+%Y%m%d-%H%M%S')"

if [ "$VERBOSE_FLAG" = "-v" ] || [ "$VERBOSE_FLAG" = "--verbose" ]; then
    /usr/bin/python3 "$SCRIPT_DIR/searxng_engine_probe.py" \
        --searxng "$SEARXNG_URL" \
        --db "$DB_PATH" \
        --run "$RUN_ID" \
        --parallel 5 \
        -v 2>&1 | tee -a "$LOG_FILE"

    echo "[$TIMESTAMP] Running duplicate snippet verification and BLOB cleanup..." | tee -a "$LOG_FILE"
    /usr/bin/python3 "$SCRIPT_DIR/searxng_duplicate_verifier.py" \
        --db "$DB_PATH" \
        -v 2>&1 | tee -a "$LOG_FILE"
else
    /usr/bin/python3 "$SCRIPT_DIR/searxng_engine_probe.py" \
        --searxng "$SEARXNG_URL" \
        --db "$DB_PATH" \
        --run "$RUN_ID" \
        --parallel 5 >> "$LOG_FILE" 2>&1

    /usr/bin/python3 "$SCRIPT_DIR/searxng_duplicate_verifier.py" \
        --db "$DB_PATH" >> "$LOG_FILE" 2>&1
fi

echo "[$TIMESTAMP] Daily health sweep completed (run_id: $RUN_ID)." >> "$LOG_FILE"
echo "========================================================" >> "$LOG_FILE"
