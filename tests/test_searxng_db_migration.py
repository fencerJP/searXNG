#!/usr/bin/env python3
"""test_searxng_db_migration.py — unit tests for database schema creation and auto-migration."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from searxng_policy import get_db_connection, init_db


class TestSearxngDbMigration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_migration.db")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_fresh_database_initialization(self):
        """Test creating schema from scratch on an empty database."""
        con = get_db_connection(self.db_path)
        init_db(con)

        cols = {row[1] for row in con.execute("PRAGMA table_info(engine_probe)").fetchall()}
        expected = {
            "id", "run_id", "ts", "engine", "categories", "query",
            "http_status", "state", "result_count", "elapsed_ms",
            "reason", "note", "skipped", "cooldown_seconds", "cooldown_type"
        }
        self.assertTrue(expected.issubset(cols))

        cd_cols = {row[1] for row in con.execute("PRAGMA table_info(cooldowns)").fetchall()}
        self.assertEqual(cd_cols, {"engine", "last_type", "fail_count", "last_failure_at", "cooldown_until"})
        con.close()

    def test_legacy_database_migration_and_backfill(self):
        """Test migrating a legacy database with missing columns and backfilling state from connectivity."""
        con = sqlite3.connect(self.db_path)
        con.execute(
            """CREATE TABLE engine_probe(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT, ts TEXT, engine TEXT, categories TEXT, query TEXT,
                http_status INTEGER, connectivity TEXT, result_count INTEGER,
                ttfb_ms INTEGER, elapsed_ms INTEGER, target_unresponsive INTEGER,
                reason TEXT, note TEXT
            )"""
        )
        con.execute(
            """INSERT INTO engine_probe(run_id, ts, engine, connectivity, elapsed_ms)
               VALUES ('run1', '2026-08-10T12:00:00', 'duckduckgo', 'ok', 120)"""
        )
        con.commit()
        con.close()

        con = get_db_connection(self.db_path)
        init_db(con, verbose=True)

        cols = {row[1] for row in con.execute("PRAGMA table_info(engine_probe)").fetchall()}
        self.assertIn("state", cols)
        self.assertIn("skipped", cols)
        self.assertIn("cooldown_seconds", cols)
        self.assertIn("cooldown_type", cols)

        row = con.execute("SELECT engine, connectivity, state, elapsed_ms FROM engine_probe WHERE run_id='run1'").fetchone()
        self.assertEqual(row[0], "duckduckgo")
        self.assertEqual(row[1], "ok")
        self.assertEqual(row[2], "ok")
        self.assertEqual(row[3], 120)
        con.close()

    def test_idempotent_migrations(self):
        """Test that running init_db multiple times is completely idempotent."""
        con = get_db_connection(self.db_path)
        init_db(con)
        init_db(con)
        init_db(con)

        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("engine_probe", tables)
        self.assertIn("cooldowns", tables)
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
