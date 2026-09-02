"""Synthetic, content-free Antigravity parser and ingestion coverage."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import antigravity
import pricing
from scanner import get_db, init_db, scan, SOURCE_ANTIGRAVITY


def varint(value):
    value = int(value)
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, value, wire=2):
    if wire == 0:
        return varint(number << 3) + (value if isinstance(value, bytes) else varint(value))
    if wire in (1, 5):
        return varint((number << 3) | wire) + value
    return varint((number << 3) | wire) + varint(len(value)) + value


def usage(model_id=312, inp=100, out=40, write=7, read=11,
          message="message-1", response="response-1", provider="provider-1",
          reasoning=10, visible=30):
    return b"".join([
        field(1, varint(model_id), 0), field(2, varint(inp), 0),
        field(3, varint(out), 0), field(4, varint(write), 0),
        field(5, varint(read), 0), field(7, message.encode()),
        field(9, varint(reasoning), 0), field(10, varint(visible), 0),
        field(11, response.encode()), field(12, provider.encode()),
    ])


def timestamp(seconds=1_700_000_000, nanos=123_456_789):
    return field(1, varint(seconds), 0) + field(2, varint(nanos), 0)


def generation(model_id=312, model="Gemini 2.5 Flash", inp=100, out=40):
    info = field(4, timestamp())
    chat = b"".join([
        field(3, varint(model_id), 0), field(4, usage(model_id=model_id, inp=inp, out=out)),
        field(9, info), field(19, model.encode()),
    ])
    return field(1, chat)


class TestProtoDecoder(unittest.TestCase):
    def test_supported_wire_types_and_last_scalar_first_message(self):
        message = antigravity.decode_message(
            field(1, varint(2), 0) + field(1, varint(3), 0)
            + field(2, b"first") + field(2, b"second")
            + field(3, b"12345678", 1) + field(4, b"1234", 5))
        self.assertEqual(message.last(1, 0), 3)
        self.assertEqual(message.first(2, 2), b"first")

    def test_rejects_truncated_and_unsupported_data(self):
        with self.assertRaises(antigravity.ProtoError):
            antigravity.decode_message(b"\x80")
        with self.assertRaises(antigravity.ProtoError):
            antigravity.decode_message(field(1, b"x", 3))
        with self.assertRaises(antigravity.ProtoError):
            antigravity._text(b"\xff")


class TestAntigravityDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "conversations"
        self.root.mkdir()
        self.path = self.root / "conversation-one.db"
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        conn.execute("CREATE TABLE steps (idx INTEGER, metadata BLOB)")
        conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (1, generation()))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_signature_tracks_wal_but_ignores_mutable_shm_index(self):
        initial = antigravity.database_signature(self.path)

        shm_path = Path(str(self.path) + "-shm")
        shm_path.write_bytes(b"sqlite coordination state")
        self.assertEqual(antigravity.database_signature(self.path), initial)

        wal_path = Path(str(self.path) + "-wal")
        wal_path.write_bytes(b"database content")
        self.assertNotEqual(antigravity.database_signature(self.path), initial)

    def test_parse_preserves_buckets_and_normalizes_output(self):
        events = antigravity.parse_database(self.path)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["model"], "gemini-2.5-flash")
        self.assertEqual(event["input_tokens"], 100)
        self.assertEqual(event["cache_creation_tokens"], 7)
        self.assertEqual(event["cache_read_tokens"], 11)
        self.assertEqual(event["output_tokens"], 40)
        self.assertEqual(event["reasoning_output_tokens"], 10)
        self.assertIn("response:response-1", event["identities"])

    def test_cost_uses_independent_input_and_cache_buckets(self):
        cost = pricing.calc_cost(
            "gemini-2.5-flash", 100, 40, 11, 7,
            source=SOURCE_ANTIGRAVITY)
        expected = (100 * 0.30 + 40 * 2.50 + 11 * 0.03 + 7 * 0.30) / 1_000_000
        self.assertAlmostEqual(cost, expected)

    def test_long_context_uses_fresh_plus_cache_tokens(self):
        self.assertFalse(pricing.is_long_context(
            "gemini-2.5-pro", 200_000, source=SOURCE_ANTIGRAVITY))
        self.assertTrue(pricing.is_long_context(
            "gemini-2.5-pro", 200_001, source=SOURCE_ANTIGRAVITY))

    def test_real_local_model_aliases_and_prices_are_explicit(self):
        self.assertEqual(
            antigravity.normalize_model("Claude Sonnet 4.6"),
            "claude-sonnet-4-6",
        )
        expected_models = {
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "claude-sonnet-4-6",
        }
        for model in expected_models:
            with self.subTest(model=model):
                self.assertIsNotNone(pricing.get_pricing(
                    model, source=SOURCE_ANTIGRAVITY))
        self.assertIsNone(pricing.get_pricing(
            "gemini-default", source=SOURCE_ANTIGRAVITY))

    def test_steps_and_retries_are_decoded_without_reading_content(self):
        step_model = field(1, varint(246), 0) + field(7, varint(3), 0) + field(12, b"Gemini 2.5 Pro")
        step = b"".join([
            field(8, timestamp(1_700_000_100)),
            field(9, usage(model_id=246, inp=200, out=20, message="step", response="step-r", provider="step-p")),
            field(24, step_model),
            field(28, field(2, usage(model_id=246, inp=220, out=22, message="retry", response="retry-r", provider="retry-p"))),
        ])
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO steps VALUES (?, ?)", (2, step))
        conn.commit()
        conn.close()

        events = antigravity.parse_database(self.path)
        step_events = [event for event in events if event["origin_key"].startswith("step:")]
        self.assertEqual(len(step_events), 2)
        self.assertEqual(step_events[0]["model"], "gemini-2.5-pro")
        self.assertEqual(step_events[1]["input_tokens"], 220)

    def test_scan_materializes_and_unchanged_scan_skips(self):
        db_path = Path(self.tmp.name) / "tokenscope.db"
        first = scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
                     db_path=db_path, verbose=False)
        self.assertEqual(first["by_source"][SOURCE_ANTIGRAVITY]["new"], 1)
        self.assertEqual(first["by_source"][SOURCE_ANTIGRAVITY]["turns"], 1)
        conn = get_db(db_path)
        self.assertEqual(tuple(conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens "
            "FROM turns WHERE source = ?", (SOURCE_ANTIGRAVITY,)).fetchone()),
            (100, 40, 11, 7))
        conn.close()
        second = scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
                      db_path=db_path, verbose=False)
        self.assertEqual(second["by_source"][SOURCE_ANTIGRAVITY]["skipped"], 1)
        self.assertEqual(second["by_source"][SOURCE_ANTIGRAVITY]["turns"], 0)

    def test_staging_and_materialization_commit_atomically(self):
        db_path = Path(self.tmp.name) / "tokenscope.db"
        with mock.patch("scanner._materialize_antigravity",
                        side_effect=RuntimeError("materialization failed")):
            with self.assertRaisesRegex(RuntimeError, "materialization failed"):
                scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
                     db_path=db_path, verbose=False)

        conn = get_db(db_path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM source_events WHERE source = ?",
            (SOURCE_ANTIGRAVITY,)).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM processed_files WHERE path = ?",
            (str(self.path.resolve()),)).fetchone()[0], 0)
        conn.close()

    def test_materialization_marker_repairs_retained_staging(self):
        db_path = Path(self.tmp.name) / "tokenscope.db"
        scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
             db_path=db_path, verbose=False)
        conn = get_db(db_path)
        conn.execute("DELETE FROM turns WHERE source = ?", (SOURCE_ANTIGRAVITY,))
        conn.execute("DELETE FROM sessions WHERE source = ?", (SOURCE_ANTIGRAVITY,))
        conn.execute("DELETE FROM schema_meta WHERE key = ?",
                     ("antigravity_materialized_revision",))
        conn.commit()
        conn.close()

        result = scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
                      db_path=db_path, verbose=False)
        self.assertEqual(result["by_source"][SOURCE_ANTIGRAVITY]["skipped"], 1)
        self.assertEqual(result["by_source"][SOURCE_ANTIGRAVITY]["turns"], 1)

    def test_updated_row_replaces_usage_without_duplication(self):
        db_path = Path(self.tmp.name) / "tokenscope.db"
        scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
             db_path=db_path, verbose=False)
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE gen_metadata SET data = ? WHERE idx = 1",
                     (generation(inp=900, out=80),))
        conn.commit()
        conn.close()
        scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
             db_path=db_path, verbose=False)
        conn = get_db(db_path)
        row = conn.execute(
            "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM turns "
            "WHERE source = ?", (SOURCE_ANTIGRAVITY,)).fetchone()
        self.assertEqual(tuple(row), (1, 900, 80))
        conn.close()

    def test_duplicate_identity_in_copied_database_counts_once(self):
        copy_path = self.root / "conversation-copy.db"
        source = sqlite3.connect(self.path)
        copied = sqlite3.connect(copy_path)
        copied.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        copied.execute("INSERT INTO gen_metadata VALUES (?, ?)", (1, generation()))
        copied.commit()
        copied.close()
        source.close()
        db_path = Path(self.tmp.name) / "tokenscope.db"
        scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
             db_path=db_path, verbose=False)
        conn = get_db(db_path)
        row = conn.execute(
            "SELECT COUNT(*), SUM(input_tokens) FROM turns WHERE source = ?",
            (SOURCE_ANTIGRAVITY,)).fetchone()
        self.assertEqual(tuple(row), (1, 100))
        conn.close()

    def test_invalid_required_schema_is_reported_without_db_creation(self):
        bad = self.root / "bad.db"
        conn = sqlite3.connect(bad)
        conn.execute("CREATE TABLE other (data BLOB)")
        conn.commit()
        conn.close()
        with self.assertRaises(antigravity.AntigravityError) as error:
            antigravity.parse_database(bad)
        self.assertIn(str(bad), str(error.exception))

        result = scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
                      db_path=Path(self.tmp.name) / "error-report.db", verbose=False)
        errors = result["by_source"][SOURCE_ANTIGRAVITY]["errors"]
        self.assertEqual(len(errors), 1)
        self.assertNotIn(str(Path.home()), errors[0]["message"])

    def test_missing_database_cannot_be_created_by_parser(self):
        missing = self.root / "missing.db"
        with self.assertRaises(antigravity.AntigravityError):
            antigravity.parse_database(missing)
        self.assertFalse(missing.exists())

    def test_session_context_extracts_workspace_branch_and_title(self):
        conn = sqlite3.connect(self.path)
        conn.execute("ALTER TABLE steps ADD COLUMN step_payload BLOB")
        conn.execute("CREATE TABLE trajectory_metadata_blob (id INTEGER, data BLOB)")
        workspace = field(1, b"file:///Users/dev/Projects/Demo%20App") + field(4, b"main")
        conn.execute("INSERT INTO trajectory_metadata_blob VALUES (?, ?)",
                     (1, field(1, workspace)))
        prompt = field(19, field(2, b"Fix the flaky login test"))
        conn.execute("INSERT INTO steps (idx, metadata, step_payload) VALUES (?, ?, ?)",
                     (0, b"", prompt))
        conn.commit()
        conn.close()

        events = antigravity.parse_database(self.path)
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event["project_name"], "/Users/dev/Projects/Demo App")
            self.assertEqual(event["git_branch"], "main")
            self.assertEqual(event["topic"], "Fix the flaky login test")

    def test_scan_populates_session_project_branch_and_topic(self):
        conn = sqlite3.connect(self.path)
        conn.execute("ALTER TABLE steps ADD COLUMN step_payload BLOB")
        conn.execute("CREATE TABLE trajectory_metadata_blob (id INTEGER, data BLOB)")
        workspace = field(1, b"file:///Users/dev/Projects/Demo") + field(4, b"release")
        conn.execute("INSERT INTO trajectory_metadata_blob VALUES (?, ?)",
                     (1, field(1, workspace)))
        prompt = field(19, field(2, b"Investigate the checkout timeout"))
        conn.execute("INSERT INTO steps (idx, metadata, step_payload) VALUES (?, ?, ?)",
                     (0, b"", prompt))
        conn.commit()
        conn.close()

        db_path = Path(self.tmp.name) / "tokenscope.db"
        scan(source=SOURCE_ANTIGRAVITY, antigravity_dir=self.root,
             db_path=db_path, verbose=False)
        conn = get_db(db_path)
        row = conn.execute(
            "SELECT project_name, git_branch, topic FROM sessions WHERE source = ?",
            (SOURCE_ANTIGRAVITY,)).fetchone()
        conn.close()
        self.assertEqual(row["project_name"], "Projects/Demo")
        self.assertEqual(row["git_branch"], "release")
        self.assertEqual(row["topic"], "Investigate the checkout timeout")

    def test_placeholder_models_fall_back_to_last_real_model(self):
        placeholder = generation(model_id=1020, model="gemini-default", inp=50, out=5)
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (2, placeholder))
        conn.commit()
        conn.close()

        events = antigravity.parse_database(self.path)
        models = {event["model"] for event in events}
        self.assertNotIn("gemini-default", models)
        self.assertIn("gemini-2.5-flash", models)


if __name__ == "__main__":
    unittest.main()
