"""Tests for dashboard.py - API endpoint and data retrieval."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from scanner import get_db, init_db, upsert_sessions, insert_turns, SOURCE_CLAUDE, SOURCE_CODEX
import dashboard
import quota
from dashboard import get_dashboard_data, DashboardHandler, HTML_TEMPLATE

try:
    from http.server import HTTPServer
except ImportError:
    HTTPServer = None


class TestGetDashboardData(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        # Insert sample data
        sessions = [{
            "session_id": "sess-abc123", "project_name": "user/myproject",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 5000, "total_output_tokens": 2000,
            "total_cache_read": 500, "total_cache_creation": 200,
            "turn_count": 10,
        }]
        upsert_sessions(conn, sessions)
        turns = [
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T09:30:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 500,
                "output_tokens": 200, "cache_read_tokens": 50,
                "cache_creation_tokens": 20, "tool_name": None, "cwd": "/tmp",
            },
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T14:15:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 300,
                "output_tokens": 150, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            },
        ]
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_returns_valid_structure(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("all_models", data)
        self.assertIn("daily_by_model", data)
        self.assertIn("sessions_all", data)
        self.assertIn("generated_at", data)

    def test_models_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("claude-sonnet-4-6", data["all_models"])

    def test_sessions_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(len(data["sessions_all"]), 1)
        session = data["sessions_all"][0]
        self.assertEqual(session["project"], "user/myproject")
        self.assertEqual(session["model"], "claude-sonnet-4-6")
        self.assertEqual(session["input"], 5000)

    def test_daily_by_model_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertGreater(len(data["daily_by_model"]), 0)
        day = data["daily_by_model"][0]
        self.assertIn("day", day)
        self.assertIn("model", day)
        self.assertIn("input", day)

    def test_missing_db_returns_error(self):
        data = get_dashboard_data(db_path=Path("/nonexistent/path/usage.db"))
        self.assertIn("error", data)

    def test_session_id_sent_in_full(self):
        # The API returns the full session id; the table truncates it for
        # display client-side, but the CSV export needs the whole value.
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(session["session_id"], "sess-abc123")

    def test_session_duration_calculated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        # 1 hour = 60 minutes
        self.assertEqual(session["duration_min"], 60.0)

    def test_hourly_by_model_present(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("hourly_by_model", data)
        self.assertIsInstance(data["hourly_by_model"], list)

    def test_hourly_by_model_buckets_by_utc_hour(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        # Two turns at UTC 09:30 and 14:15 → two hour buckets
        by_hour = {r["hour"]: r for r in rows}
        self.assertIn(9, by_hour)
        self.assertIn(14, by_hour)
        self.assertEqual(by_hour[9]["turns"], 1)
        self.assertEqual(by_hour[9]["output"], 200)
        self.assertEqual(by_hour[14]["turns"], 1)
        self.assertEqual(by_hour[14]["output"], 150)

    def test_hourly_by_model_carries_day_and_model(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        self.assertTrue(all("day" in r and "model" in r for r in rows))
        self.assertTrue(all(r["model"] == "claude-sonnet-4-6" for r in rows))
        self.assertTrue(all(r["day"] == "2026-04-08" for r in rows))


class TestCodexDashboardData(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "source": SOURCE_CODEX, "session_id": "codex-session",
            "project_name": "user/codex-project",
            "first_timestamp": "2026-08-30T10:00:00Z",
            "last_timestamp": "2026-08-30T10:02:00Z", "git_branch": "",
            "model": "gpt-5.6-sol", "total_input_tokens": 100,
            "total_output_tokens": 50, "total_cache_read": 10,
            "total_cache_creation": 5, "total_reasoning_output": 7,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "source": SOURCE_CODEX, "session_id": "codex-session",
            "timestamp": "2026-08-30T10:02:00Z", "model": "gpt-5.6-sol",
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 10, "cache_creation_tokens": 5,
            "reasoning_output_tokens": 7, "tool_name": None, "cwd": "/tmp",
            "source_record_id": "codex-session:turn-1",
        }])
        # Prove every dashboard query is source-scoped.
        upsert_sessions(conn, [{
            "source": SOURCE_CLAUDE, "session_id": "claude-session",
            "project_name": "user/claude-project",
            "first_timestamp": "2026-08-30T10:00:00Z",
            "last_timestamp": "2026-08-30T10:01:00Z", "git_branch": "",
            "model": "claude-sonnet-4-6", "total_input_tokens": 200,
            "total_output_tokens": 80, "total_cache_read": 0,
            "total_cache_creation": 0, "turn_count": 1,
        }])
        insert_turns(conn, [{
            "source": SOURCE_CLAUDE, "session_id": "claude-session",
            "timestamp": "2026-08-30T10:01:00Z", "model": "claude-sonnet-4-6",
            "input_tokens": 200, "output_tokens": 80,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_codex_data_is_provider_scoped_and_includes_reasoning(self):
        data = get_dashboard_data(db_path=self.db_path, source=SOURCE_CODEX)

        self.assertEqual(data["source"], SOURCE_CODEX)
        self.assertEqual(data["provider"]["label"], "Codex")
        self.assertEqual(data["label"], "Codex")
        self.assertEqual(data["capabilities"], {
            "cache": True, "reasoning_tokens": True, "subagents": False,
        })
        self.assertEqual(data["all_models"], ["gpt-5.6-sol"])
        self.assertEqual(data["daily_by_model"][0]["reasoning_output"], 7)
        self.assertEqual(data["sessions_all"][0]["reasoning_output"], 7)
        self.assertEqual(data["subagent_by_type"], [])

    def test_claude_query_excludes_codex_rows(self):
        data = get_dashboard_data(db_path=self.db_path, source=SOURCE_CLAUDE)

        self.assertEqual(data["all_models"], ["claude-sonnet-4-6"])
        self.assertEqual(data["sessions_all"][0]["session_id"], "claude-session")

    def test_unknown_source_returns_error(self):
        data = get_dashboard_data(db_path=self.db_path, source="unknown")
        self.assertIn("error", data)

    def test_codex_long_context_cost_is_calculated_per_turn_tier(self):
        conn = get_db(self.db_path)
        insert_turns(conn, [{
            "source": SOURCE_CODEX, "session_id": "long-context-session",
            "timestamp": "2026-08-30T12:00:00Z", "model": "gpt-5.5",
            "input_tokens": 300_000, "output_tokens": 1_000,
            "cache_read_tokens": 100_000, "cache_creation_tokens": 0,
            "is_long_context": 1, "tool_name": None, "cwd": "/tmp",
            "source_record_id": "long-context-turn",
        }])
        upsert_sessions(conn, [{
            "source": SOURCE_CODEX, "session_id": "long-context-session",
            "project_name": "user/codex-project",
            "first_timestamp": "2026-08-30T12:00:00Z",
            "last_timestamp": "2026-08-30T12:00:00Z", "git_branch": "",
            "model": "gpt-5.5", "total_input_tokens": 300_000,
            "total_output_tokens": 1_000, "total_cache_read": 100_000,
            "total_cache_creation": 0, "turn_count": 1,
        }])
        conn.commit()
        conn.close()

        data = get_dashboard_data(db_path=self.db_path, source=SOURCE_CODEX)
        long_day = next(row for row in data["daily_by_model"] if row["model"] == "gpt-5.5")
        long_session = next(row for row in data["sessions_all"] if row["session_id"] == "long-context-session")
        self.assertAlmostEqual(long_day["cost"], 2.145)
        self.assertAlmostEqual(long_session["cost"], 2.145)



class TestEmptyStringModelNormalization(unittest.TestCase):
    """Regression: turns with model='' (empty string) must group as 'unknown'.
    COALESCE(model, 'unknown') alone returns '' because empty string isn't NULL;
    NULLIF(model, '') is needed first."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-empty", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T09:05:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 100, "total_output_tokens": 50,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "sess-empty", "timestamp": "2026-04-08T09:05:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("unknown", data["all_models"])
        self.assertNotIn("", data["all_models"])

    def test_daily_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["daily_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)

    def test_hourly_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["hourly_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)


class TestMixedNullAndEmptyModel(unittest.TestCase):
    """Regression: a mix of model=NULL and model='' rows must collapse into a
    SINGLE 'unknown' group across all aggregations. Without `GROUP BY
    COALESCE(NULLIF(model, ''), 'unknown')` (matching the SELECT expression),
    SQLite groups by raw value and emits two distinct 'unknown' rows."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-mix", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 200, "total_output_tokens": 100,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 2,
        }])
        # Insert one turn with model='' and one with model=NULL on the same day.
        # Use raw INSERT for the NULL row because insert_turns() requires the
        # model key to exist (would error on missing key, not on None).
        insert_turns(conn, [{
            "session_id": "sess-mix", "timestamp": "2026-04-08T09:00:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd)
            VALUES ('sess-mix', '2026-04-08T09:30:00Z', NULL, 100, 50, 0, 0, NULL, '/tmp')
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknowns = [m for m in data["all_models"] if m == "unknown"]
        self.assertEqual(len(unknowns), 1, f"got duplicate 'unknown' rows: {data['all_models']}")

    def test_daily_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknown_rows = [r for r in data["daily_by_model"] if r["model"] == "unknown"]
        # One day, one model bucket
        self.assertEqual(len(unknown_rows), 1, f"got {unknown_rows}")
        self.assertEqual(unknown_rows[0]["turns"], 2)
        self.assertEqual(unknown_rows[0]["input"], 200)

    def test_hourly_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        # Both turns are in UTC hour 9 — must be one row, not two
        hour9 = [r for r in data["hourly_by_model"]
                 if r["hour"] == 9 and r["model"] == "unknown"]
        self.assertEqual(len(hour9), 1, f"got {hour9}")
        self.assertEqual(hour9[0]["turns"], 2)


class TestNonBillableModelFallback(unittest.TestCase):
    """Regression: when the user has only non-billable models (e.g. gemma, glm,
    local LLMs) — or all turns lack a model field — the default model selection
    must fall back to ALL models so the dashboard isn't blank."""

    def test_readurlmodels_fallback_in_html_template(self):
        # The fallback logic is JS; we assert the source contains the guard so
        # a future refactor doesn't silently remove it.
        self.assertIn("billable.length ? billable : allModels", HTML_TEMPLATE)


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        # Redirect DB_PATH + projects dirs to a tempdir so /api/rescan
        # writes to a throwaway DB and scans a throwaway transcript dir
        # instead of the user's real ~/.claude/usage.db and transcripts.
        import dashboard as _d
        import scanner as _s
        import settings as _set
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        tmp_projects = tmp / "projects"
        tmp_projects.mkdir()
        tmp_codex = tmp / "codex"
        tmp_codex.mkdir()
        cls._patches = {
            (_d, "DB_PATH"):                (_d.DB_PATH,                tmp / "usage.db"),
            (_s, "DB_PATH"):                (_s.DB_PATH,                tmp / "usage.db"),
            (_s, "PROJECTS_DIR"):           (_s.PROJECTS_DIR,           tmp_projects),
            (_s, "DEFAULT_PROJECTS_DIRS"):  (_s.DEFAULT_PROJECTS_DIRS,  [tmp_projects]),
            (_s, "CODEX_SESSIONS_DIR"):     (_s.CODEX_SESSIONS_DIR,     tmp_codex),
            # Every request calls settings.apply(); without this the handler
            # would read (and /api/settings would write) the developer's real
            # ~/.claude/tokenscope-settings.json, and a disabled provider there
            # would change what /api/rescan scans.
            (_set, "SETTINGS_PATH"):        (_set.SETTINGS_PATH,        tmp / "tokenscope-settings.json"),
        }
        for (mod, name), (_orig, new) in cls._patches.items():
            setattr(mod, name, new)

        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for (mod, name), (orig, _new) in cls._patches.items():
            setattr(mod, name, orig)
        cls._tmpdir.cleanup()

    def test_index_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_index_with_query_string_returns_html(self):
        # Regression: ?range=... and ?models=... must not 404. The dashboard
        # itself rewrites the URL with these params via history.replaceState,
        # so anything that reloads or bookmarks the page hits this path.
        for qs in ("?range=all", "?range=30d&models=claude-opus-4-7"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{qs}") as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(b"TokenScope", resp.read())

    def test_api_data_with_query_string(self):
        # /api/data is fetched without query parameters today, but the route
        # should be tolerant if any are tacked on (e.g. cache-busting).
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/data?_=cachebust"
        ) as resp:
            self.assertEqual(resp.status, 200)

    def test_api_data_can_select_codex_source(self):
        import dashboard as _d
        conn = get_db(_d.DB_PATH)
        init_db(conn)
        conn.close()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/data?source=codex"
        ) as resp:
            data = json.loads(resp.read())
        self.assertEqual(data["source"], "codex")
        self.assertEqual(data["provider"]["label"], "Codex")

    def test_api_data_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/data"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Should have expected keys (or error if no DB)
            self.assertTrue("all_models" in data or "error" in data)

    # ── Sign-in route ──────────────────────────────────────────────────────
    # The route spawns `claude auth login`, so its gates matter more than its
    # payload: an unauthenticated localhost server must not let any page in the
    # browser start a login, and a LAN-exposed one must not offer it at all.

    def _signin(self, method="GET", token=dashboard.CONTROL_TOKEN):
        url = f"http://127.0.0.1:{self.port}/api/signin"
        headers = {} if token is None else {dashboard.CONTROL_TOKEN_HEADER: token}
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_signin_status_requires_the_control_token(self):
        self.assertEqual(self._signin(token=None)[0], 403)
        self.assertEqual(self._signin(token="wrong")[0], 403)

    def test_signin_status_is_served_with_the_control_token(self):
        status, payload = self._signin()
        self.assertEqual(status, 200)
        self.assertIn(payload["status"], ("idle", "running", "ok", "failed", "unavailable"))

    def test_signin_start_requires_the_control_token(self):
        with patch.object(quota, "start_sign_in") as start:
            self.assertEqual(self._signin("POST", token=None)[0], 403)
            self.assertEqual(self._signin("POST", token="wrong")[0], 403)
        start.assert_not_called()

    def test_signin_start_is_refused_off_loopback(self):
        """A dashboard on 0.0.0.0 must not spawn a login for the whole LAN."""
        with patch.object(dashboard, "SERVE_HOST", "0.0.0.0"), \
             patch.object(quota, "start_sign_in") as start:
            status, _ = self._signin("POST")
        self.assertEqual(status, 403)
        start.assert_not_called()

    def test_signin_start_reports_a_missing_cli(self):
        with patch.object(quota, "sign_in_available", return_value=False), \
             patch.object(quota, "start_sign_in") as start:
            status, payload = self._signin("POST")
        self.assertEqual(status, 501)
        self.assertIn("error", payload)
        start.assert_not_called()

    def test_signin_start_accepts_and_returns_immediately(self):
        """202: the browser half of the flow outlives the response."""
        state = {"status": "running", "message": "…", "url": "", "started_at": 0.0, "available": True}
        with patch.object(quota, "sign_in_available", return_value=True), \
             patch.object(quota, "start_sign_in", return_value=state) as start:
            status, payload = self._signin("POST")
        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "running")
        start.assert_called_once()

    def test_page_carries_the_control_token_and_capability(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn(dashboard.CONTROL_TOKEN, html)
        self.assertIn('"canSignIn"', html)

    def test_signin_is_not_offered_off_loopback(self):
        with patch.object(dashboard, "SERVE_HOST", "0.0.0.0"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as resp:
                html = resp.read().decode("utf-8")
        self.assertIn('"canSignIn": false', html)

    def test_api_rescan_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("new", data)
            self.assertIn("updated", data)
            self.assertIn("skipped", data)

    def test_api_rescan_is_non_destructive(self):
        # Regression (#138): /api/rescan must NOT wipe the DB. usage.db is the
        # only durable store of history once Claude Code prunes old transcripts
        # (cleanupPeriodDays), so a rescan with nothing left on disk must keep
        # the existing rows. Seed history that has no corresponding JSONL file
        # (the projects dir is empty), rescan, and assert it survives.
        import dashboard as _d
        db_path = _d.DB_PATH
        conn = get_db(db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "pruned-sess", "project_name": "user/oldproject",
            "first_timestamp": "2026-01-01T09:00:00Z",
            "last_timestamp": "2026-01-01T10:00:00Z",
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 1000, "total_output_tokens": 400,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "pruned-sess", "timestamp": "2026-01-01T09:30:00Z",
            "model": "claude-opus-4-8", "input_tokens": 1000,
            "output_tokens": 400, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            "message_id": "msg-pruned-1",
        }])
        conn.commit()
        conn.close()

        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)

        conn = sqlite3.connect(db_path)
        try:
            turn_count = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE session_id = 'pruned-sess'"
            ).fetchone()[0]
            sess_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE session_id = 'pruned-sess'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(turn_count, 1, "rescan must not delete existing turns")
        self.assertEqual(sess_count, 1, "rescan must not delete existing sessions")

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_index_injects_app_config(self):
        # do_GET must substitute the __APP_CONFIG_JSON__ placeholder with a real
        # JSON object (version + pricing). The raw placeholder must never reach
        # the browser, or window.APP_CONFIG would be a syntax error.
        from scanner import VERSION
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            body = resp.read().decode("utf-8")
        self.assertNotIn("__APP_CONFIG_JSON__", body)
        self.assertIn("window.APP_CONFIG =", body)
        self.assertIn(VERSION, body)
        self.assertIn('"pricing":', body)


class TestHTMLTemplate(unittest.TestCase):
    def test_template_is_valid_html(self):
        self.assertIn("<!DOCTYPE html>", HTML_TEMPLATE)
        self.assertIn("</html>", HTML_TEMPLATE)

    def test_template_has_esc_function(self):
        """Verify XSS protection is present (PR #10)."""
        self.assertIn("function esc(", HTML_TEMPLATE)

    def test_template_has_chart_js(self):
        self.assertIn("chart.js", HTML_TEMPLATE.lower())

    def test_provider_tabs_are_present(self):
        self.assertIn('id="tab-claude_code"', HTML_TEMPLATE)
        self.assertIn('id="tab-codex"', HTML_TEMPLATE)
        self.assertIn('<h1 id="page-title">Claude Code</h1>', HTML_TEMPLATE)
        self.assertIn("title.textContent = provider.label", HTML_TEMPLATE)
        self.assertIn("setSource('claude_code')", HTML_TEMPLATE)
        self.assertIn("setSource('codex')", HTML_TEMPLATE)
        self.assertIn("/api/data?source=", HTML_TEMPLATE)

    def test_provider_tab_always_leaves_settings_before_source_short_circuit(self):
        """The active provider tab is also the way back from Settings."""
        start = HTML_TEMPLATE.index("async function setSource(source)")
        end = HTML_TEMPLATE.index("// ── Peak-hour config", start)
        handler = HTML_TEMPLATE[start:end]
        self.assertIn("await setView('dashboard')", handler)
        self.assertLess(
            handler.index("await setView('dashboard')"),
            handler.index("source === selectedSource"),
        )
        self.assertIn("if (currentView !== 'dashboard') return;", handler)

    def test_auto_rescan_runs_every_thirty_minutes_without_overlap(self):
        self.assertIn(
            "const AUTO_RESCAN_INTERVAL_MS = 30 * 60 * 1000;",
            HTML_TEMPLATE,
        )
        self.assertIn(
            "autoRescanTimer = setInterval(triggerRescan, AUTO_RESCAN_INTERVAL_MS);",
            HTML_TEMPLATE,
        )
        self.assertIn("scheduleAutoRescan();", HTML_TEMPLATE)

        start = HTML_TEMPLATE.index("async function triggerRescan()")
        end = HTML_TEMPLATE.index("// ── Data loading", start)
        handler = HTML_TEMPLATE[start:end]
        self.assertIn("if (rescanInFlight) return;", handler)
        self.assertIn("rescanInFlight = true;", handler)
        self.assertIn("rescanInFlight = false;", handler)

    def test_auto_rescan_does_not_replace_five_minute_data_refresh(self):
        self.assertIn(
            "const DATA_REFRESH_INTERVAL_MS = 5 * 60 * 1000;",
            HTML_TEMPLATE,
        )
        self.assertIn(
            "autoRefreshTimer = setInterval(loadData, DATA_REFRESH_INTERVAL_MS);",
            HTML_TEMPLATE,
        )
        self.assertIn("Auto-refresh every 5m", HTML_TEMPLATE)
        self.assertIn("Auto-rescan every 30m", HTML_TEMPLATE)

    def test_sidebar_has_provider_aware_identity_and_quota_panel(self):
        self.assertIn('id="quota-panel"', HTML_TEMPLATE)
        self.assertIn('id="quota-rows"', HTML_TEMPLATE)
        self.assertIn('id="quota-refresh"', HTML_TEMPLATE)
        self.assertIn('codex-icon.svg', HTML_TEMPLATE)
        self.assertIn("renderQuota", HTML_TEMPLATE)
        self.assertIn("refreshQuota", HTML_TEMPLATE)
        self.assertIn("remaining_percent", HTML_TEMPLATE)
        self.assertIn("quotaResetText", HTML_TEMPLATE)

    def test_provider_capabilities_drive_provider_specific_ui(self):
        self.assertIn("reasoning_tokens", HTML_TEMPLATE)
        self.assertIn("capabilities", HTML_TEMPLATE)
        self.assertIn("sec-subagents", HTML_TEMPLATE)
        self.assertIn("reasoning-col", HTML_TEMPLATE)

    def test_codex_prompt_and_cache_accounting_are_not_additive(self):
        """Codex input includes cached input; cache writes are also prompt input."""
        self.assertIn("function uncachedInputTokens", HTML_TEMPLATE)
        self.assertIn("function totalTokenCount", HTML_TEMPLATE)
        self.assertIn("? input + output", HTML_TEMPLATE)
        self.assertIn("Prompt Tokens", HTML_TEMPLATE)
        self.assertIn("Cached Input", HTML_TEMPLATE)
        self.assertIn("input - cacheRead - cacheCreation", HTML_TEMPLATE)
        self.assertIn("function rowCost", HTML_TEMPLATE)
        self.assertIn("not reported by local Codex logs", HTML_TEMPLATE)
        self.assertIn("Cache Writes", HTML_TEMPLATE)

    def test_template_has_model_specific_matching(self):
        """Verify pricing is resolved by explicit model/version entries."""
        self.assertIn("const normalized = model.trim().toLowerCase();", HTML_TEMPLATE)
        self.assertIn("normalized.startsWith(key + '-')", HTML_TEMPLATE)
        self.assertNotIn("m.includes('opus')", HTML_TEMPLATE)
        self.assertNotIn("m.includes('sonnet')", HTML_TEMPLATE)
        self.assertNotIn("m.includes('haiku')", HTML_TEMPLATE)

    def test_unknown_models_return_null(self):
        """Verify getPricing returns null for non-Anthropic models."""
        self.assertIn("return null;", HTML_TEMPLATE)

    def test_hourly_chart_canvas_present(self):
        """Hourly distribution chart has a canvas + TZ toggle."""
        self.assertIn('id="chart-hourly"', HTML_TEMPLATE)
        self.assertIn('data-tz="local"', HTML_TEMPLATE)
        self.assertIn('data-tz="utc"', HTML_TEMPLATE)

    def test_hourly_peak_hour_constants(self):
        """Peak-hour set covers UTC 12–17 (Mon–Fri 05:00–11:00 PT)."""
        self.assertIn('PEAK_HOURS_UTC', HTML_TEMPLATE)
        self.assertIn('[12, 13, 14, 15, 16, 17]', HTML_TEMPLATE)

    def test_range_menu_has_today_and_custom_date_controls(self):
        """The range control is a custom downward-opening menu, including
        a validated start/end-date path rather than a native select."""
        self.assertIn('id="range-trigger"', HTML_TEMPLATE)
        self.assertIn('id="range-panel"', HTML_TEMPLATE)
        self.assertIn('class="model-panel range-panel"', HTML_TEMPLATE)
        self.assertIn('top: calc(100% + 6px)', HTML_TEMPLATE)
        self.assertIn('data-range="today"', HTML_TEMPLATE)
        self.assertIn('data-range="custom"', HTML_TEMPLATE)
        self.assertIn('id="custom-range-start"', HTML_TEMPLATE)
        self.assertIn('id="custom-range-end"', HTML_TEMPLATE)
        self.assertIn('function applyCustomRange(event)', HTML_TEMPLATE)
        self.assertIn('function isValidDateRange(start, end)', HTML_TEMPLATE)
        self.assertNotIn('<select id="range-select"', HTML_TEMPLATE)
        self.assertIn("'today': 'Today'", HTML_TEMPLATE)
        self.assertIn("'today': 1", HTML_TEMPLATE)
        # Bounds case: today returns start === end === today's ISO date
        self.assertIn("range === 'today'", HTML_TEMPLATE)

    def test_jump_scroll_spy_accounts_for_filter_bar_offset(self):
        """A section reached through Graphs/Tables must become active itself,
        rather than leaving the previous menu entry selected."""
        self.assertIn('bar.getBoundingClientRect().top + bar.offsetHeight + 16', HTML_TEMPLATE)
        self.assertIn(".jump-panel .jump-link.selected", HTML_TEMPLATE)
        self.assertIn("item.classList.toggle('selected', item === link)", HTML_TEMPLATE)

    def test_app_config_placeholder_present(self):
        """The head carries the server-substituted config placeholder and the
        footer carries the element + JS that renders the version and attribution."""
        self.assertIn("__APP_CONFIG_JSON__", HTML_TEMPLATE)
        self.assertIn("window.APP_CONFIG", HTML_TEMPLATE)
        self.assertIn('id="footer-meta"', HTML_TEMPLATE)
        self.assertIn("function initFooterMeta(", HTML_TEMPLATE)

    def test_footer_has_attribution_without_extension_or_update_links(self):
        self.assertIn("Inspired by <a href=\"https://github.com/phuryn/claude-usage\"", HTML_TEMPLATE)
        self.assertIn(">claude-usage</a>", HTML_TEMPLATE)
        self.assertNotIn("Get the VS Code extension", HTML_TEMPLATE)
        self.assertNotIn("Update to v", HTML_TEMPLATE)
        self.assertNotIn("marketplace.visualstudio.com", HTML_TEMPLATE)
        self.assertNotIn("api.github.com/repos/phuryn/claude-usage/releases/latest", HTML_TEMPLATE)


class TestSignInAffordance(unittest.TestCase):
    """The sign-in button must not be tied to an empty window list.

    Regression: `needs_sign_in` arrives with a *stale* live reading, which still
    has windows, so the panel rendered the rows plus an "expired" footnote and
    no way to act on it. The action is keyed on `needs_sign_in` alone.
    """

    def test_the_stale_reading_branch_offers_the_action(self):
        stale_note = [line for line in HTML_TEMPLATE.splitlines() if "quota-note" in line and "html +=" in line]
        self.assertTrue(stale_note, "stale-reading note line not found in the template")
        self.assertIn("signInAction(quota)", stale_note[0])

    def test_the_empty_branch_offers_the_action(self):
        empty = [line for line in HTML_TEMPLATE.splitlines() if "quota-empty" in line and "Usage unavailable" in line]
        self.assertTrue(empty, "empty-state line not found in the template")
        self.assertIn("signInAction(quota)", HTML_TEMPLATE)

    def test_the_action_is_gated_on_needs_sign_in(self):
        self.assertIn("if (!quota || !quota.needs_sign_in) return '';", HTML_TEMPLATE)


class TestPricingParity(unittest.TestCase):
    """The browser receives the Python pricing structure, not a copy."""

    def test_dashboard_uses_server_injected_pricing(self):
        self.assertIn("(window.APP_CONFIG || {}).pricing", HTML_TEMPLATE)
        self.assertNotIn("'claude-opus-4-6':", HTML_TEMPLATE)

    def test_dashboard_mirrors_long_context_tier(self):
        # pricing.calc_cost reprices the whole request above a family's
        # threshold; the client-side fallback has to do the same.
        self.assertIn("function longContextPrice(p)", HTML_TEMPLATE)
        self.assertIn("long_context_threshold", HTML_TEMPLATE)


class TestTimezoneFrames(unittest.TestCase):
    """Every date the client filters on has to be in the client's frame."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        # 23:30 UTC — a different calendar day in every timezone east of UTC.
        insert_turns(conn, [{
            "session_id": "sess-tz", "timestamp": "2026-04-08T23:30:00Z",
            "model": "claude-sonnet-5", "input_tokens": 100,
            "output_tokens": 50, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            "is_subagent": 1, "agent_id": "agent-1",
        }])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_dispatch_start_date_matches_the_local_day(self):
        # Dispatch rows are filtered against local range bounds, so a raw UTC
        # slice put edge-of-day dispatches in the wrong bucket (cf. #151).
        data = get_dashboard_data(self.db_path)
        daily_days = {row["day"] for row in data["daily_by_model"]}
        self.assertEqual(len(data["top_dispatches"]), 1)
        self.assertIn(data["top_dispatches"][0]["start_date"], daily_days)

    def test_hourly_rows_carry_both_timezone_frames(self):
        # The client buckets by hour and day; pairing a local day with a UTC
        # hour shifted rows a day. Both frames ship per row instead.
        data = get_dashboard_data(self.db_path)
        self.assertEqual(len(data["hourly_by_model"]), 1)
        row = data["hourly_by_model"][0]
        self.assertEqual(row["hour"], 23)          # UTC hour, parsed not sliced
        for key in ("day", "day_utc", "hour_local"):
            self.assertIn(key, row)
        self.assertEqual(row["day_utc"], "2026-04-08")
        self.assertIsInstance(row["hour_local"], int)

    def test_daily_rows_expose_the_long_context_flag(self):
        # The browser prices fallback rows itself; without the flag it would
        # bill a long-context Codex request at short-context rates.
        data = get_dashboard_data(self.db_path)
        self.assertIn("long_context", data["daily_by_model"][0])
        self.assertIs(data["daily_by_model"][0]["long_context"], False)


class TestRangeBoundsInPage(unittest.TestCase):
    def test_last_n_days_is_inclusive_of_today(self):
        # "Last 7 Days" must span 7 calendar days, not 8.
        self.assertIn("d.setDate(d.getDate() - (days - 1));", HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
