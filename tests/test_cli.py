"""Tests for cli.py - pricing, formatting, and cost calculation."""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock
import cli
from cli import get_pricing, calc_cost, fmt, fmt_cost, PRICING
from scanner import SOURCE_CODEX


class TestGetPricing(unittest.TestCase):
    def test_exact_model_match(self):
        p = get_pricing("claude-opus-4-6")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_all_known_models_have_pricing(self):
        for model in ("claude-fable-5", "claude-mythos-5",
                       "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
                       "claude-sonnet-5", "claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5",
                       "claude-haiku-4-7", "claude-haiku-4-6", "claude-haiku-4-5"):
            p = get_pricing(model)
            self.assertGreater(p["input"], 0, f"Missing input price for {model}")
            self.assertGreater(p["output"], 0, f"Missing output price for {model}")

    def test_fable_and_mythos_have_explicit_entries(self):
        """Regression guard for #136/#137 — Fable 5 and Mythos 5 must be priced
        explicitly at 2x Opus, not fall through to $0/n/a or an Opus rate."""
        for model in ("claude-fable-5", "claude-mythos-5"):
            self.assertIn(model, PRICING)
            p = get_pricing(model)
            self.assertEqual(p["input"], 10.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 50.00, f"{model} output price wrong")
            self.assertEqual(p["cache_read"], 1.00, f"{model} cache_read wrong")
            self.assertEqual(p["cache_write"], 12.50, f"{model} cache_write wrong")

    def test_fable_date_suffix_matches(self):
        """JSONL model strings may carry a date suffix."""
        p = get_pricing("claude-fable-5-20260601")
        self.assertEqual(p["input"], 10.00)
        self.assertEqual(p["output"], 50.00)

    def test_unknown_version_is_not_assigned_a_family_price(self):
        """An unlisted version must not inherit another version's price."""
        for model in ("claude-fable-6", "claude-opus-9", "claude-sonnet-9",
                      "claude-haiku-9", "some-fable-variant", "internal-mythos-test"):
            self.assertIsNone(get_pricing(model), model)

    def test_opus_4_8_has_explicit_entry(self):
        """Regression guard for issue #133 — Opus 4.8 must be present, not just
        resolved via the generic 'opus' substring fallback."""
        self.assertIn("claude-opus-4-8", PRICING)
        p = get_pricing("claude-opus-4-8")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_opus_4_7_has_explicit_entry(self):
        """Regression guard for issue #61 — Opus 4.7 must be present."""
        p = get_pricing("claude-opus-4-7")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_opus_4_7_with_date_suffix(self):
        """Model strings from JSONL often have date suffixes."""
        p = get_pricing("claude-opus-4-7-20260215")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_prefix_match(self):
        # A model name with a suffix should still match the base
        p = get_pricing("claude-sonnet-4-6-20260401")
        self.assertEqual(p["input"], 3.00)
        self.assertEqual(p["output"], 15.00)

    def test_known_model_match_is_case_insensitive(self):
        p = get_pricing("CLAUDE-OPUS-4-6")
        self.assertEqual(p["input"], 5.00)

    def test_known_model_prefix_allows_suffix(self):
        # API model IDs may carry a dated or provider-specific suffix.
        p = get_pricing("claude-opus-4-6-preview")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(get_pricing("glm-5.1"))
        self.assertIsNone(get_pricing("gpt-4o"))
        self.assertIsNone(get_pricing("some-unknown-model"))

    def test_none_model_returns_none(self):
        self.assertIsNone(get_pricing(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(get_pricing(""))

    def test_codex_models_have_explicit_source_pricing(self):
        price = get_pricing("gpt-5.6-terra", source=SOURCE_CODEX)
        self.assertEqual(price["input"], 2.00)
        self.assertEqual(price["output"], 12.00)


class TestCalcCost(unittest.TestCase):
    def test_basic_cost_calculation(self):
        # 1M input tokens of Sonnet at $3/MTok = $3.00
        cost = calc_cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 3.00)

    def test_output_tokens(self):
        # 1M output tokens of Sonnet at $15/MTok = $15.00
        cost = calc_cost("claude-sonnet-4-6", 0, 1_000_000, 0, 0)
        self.assertAlmostEqual(cost, 15.00)

    def test_cache_read_discount(self):
        # Cache read = 10% of input price
        # 1M cache_read of Opus at $5 * 0.10 = $0.50
        cost = calc_cost("claude-opus-4-6", 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(cost, 0.50)

    def test_cache_creation_premium(self):
        # Cache creation = 125% of input price
        # 1M cache_creation of Opus at $5 * 1.25 = $6.25
        cost = calc_cost("claude-opus-4-6", 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(cost, 6.25)

    def test_combined_cost(self):
        cost = calc_cost("claude-haiku-4-5",
                         inp=500_000, out=100_000,
                         cache_read=200_000, cache_creation=50_000)
        expected = (
            500_000 * 1.00 / 1_000_000 +   # input
            100_000 * 5.00 / 1_000_000 +    # output
            200_000 * 1.00 * 0.10 / 1_000_000 +  # cache read
            50_000 * 1.00 * 1.25 / 1_000_000     # cache creation
        )
        self.assertAlmostEqual(cost, expected)

    def test_zero_tokens(self):
        cost = calc_cost("claude-opus-4-6", 0, 0, 0, 0)
        self.assertEqual(cost, 0.0)

    def test_unknown_model_costs_zero(self):
        cost = calc_cost("glm-5.1", 1_000_000, 500_000, 100_000, 50_000)
        self.assertEqual(cost, 0.0)

    def test_non_anthropic_model_costs_zero(self):
        cost = calc_cost("gpt-4o", 1_000_000, 500_000, 0, 0)
        self.assertEqual(cost, 0.0)

    def test_codex_cached_input_is_not_double_billed(self):
        cost = calc_cost("gpt-5.6-sol", 1_000_000, 0, 750_000, 0,
                         source=SOURCE_CODEX)
        self.assertAlmostEqual(cost, 1.30)

    def test_codex_cache_writes_replace_standard_input_rate(self):
        # Codex cache writes are a subset of uncached prompt input.  They are
        # charged at 125% of input, not added on top of the same input tokens.
        cost = calc_cost("gpt-5.6-terra", 1_000_000, 0, 200_000, 100_000,
                         source=SOURCE_CODEX)
        self.assertAlmostEqual(cost, 1.69)

    def test_gpt_5_5_long_context_uses_documented_request_tier(self):
        cost = calc_cost("gpt-5.5", 300_000, 1_000, 100_000, 0,
                         source=SOURCE_CODEX, long_context=True)
        # 200K ordinary input at $10, 100K cached at $1, and 1K output at $45.
        self.assertAlmostEqual(cost, 2.145)


class TestFmt(unittest.TestCase):
    def test_millions(self):
        self.assertEqual(fmt(1_500_000), "1.50M")
        self.assertEqual(fmt(1_000_000), "1.00M")

    def test_thousands(self):
        self.assertEqual(fmt(1_500), "1.5K")
        self.assertEqual(fmt(1_000), "1.0K")

    def test_small_numbers(self):
        self.assertEqual(fmt(999), "999")
        self.assertEqual(fmt(0), "0")


class TestFmtCost(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(fmt_cost(3.0), "$3.0000")
        self.assertEqual(fmt_cost(0.0001), "$0.0001")
        self.assertEqual(fmt_cost(0), "$0.0000")


class TestPricingConsistency(unittest.TestCase):
    """Ensure CLI pricing matches known Anthropic API rates."""

    def test_opus_pricing(self):
        for model in ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 5.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 25.00, f"{model} output price wrong")

    def test_sonnet_pricing(self):
        for model in ("claude-sonnet-5", "claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5"):
            p = get_pricing(model)
            expected = (2.00, 10.00) if model == "claude-sonnet-5" else (3.00, 15.00)
            self.assertEqual(p["input"], expected[0], f"{model} input price wrong")
            self.assertEqual(p["output"], expected[1], f"{model} output price wrong")

    def test_haiku_pricing(self):
        for model in ("claude-haiku-4-7", "claude-haiku-4-6", "claude-haiku-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 1.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 5.00, f"{model} output price wrong")


class TestStatsQueries(unittest.TestCase):
    """`stats` reports in the same local-day frame as `today` and `week`."""

    def setUp(self):
        import tempfile, sqlite3
        from datetime import datetime, timezone
        from pathlib import Path
        from scanner import get_db, init_db, insert_turns

        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        insert_turns(conn, [{
            "session_id": "sess-stats", "timestamp": now,
            "model": "claude-opus-5", "input_tokens": 1000,
            "output_tokens": 400, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()
        self._original_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        import os
        cli.DB_PATH = self._original_db
        os.unlink(self.db_path)

    def test_daily_average_counts_a_turn_from_today(self):
        # Guards the rewritten window: the old form bucketed by UTC date and
        # string-compared an ISO timestamp against datetime('now')'s format.
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_stats()
        text = out.getvalue()
        self.assertIn("Daily Average (last 30 days)", text)
        self.assertIn("Input:", text)


class TestDashboardNoBrowser(unittest.TestCase):
    """The no-browser flag suppresses the browser for scripted CLI use."""

    def test_no_browser_suppresses_webbrowser(self):
        with mock.patch.object(cli, "cmd_scan"), \
             mock.patch("dashboard.serve") as mock_serve, \
             mock.patch("webbrowser.open") as mock_open, \
             redirect_stdout(io.StringIO()):
            cli.cmd_dashboard(host="127.0.0.1", port=9999, no_browser=True)
            mock_open.assert_not_called()
            mock_serve.assert_called_once()


class TestProviderScanDispatch(unittest.TestCase):
    def test_default_scan_command_requests_both_sources(self):
        with mock.patch("scanner.scan") as mock_scan:
            cli.cmd_scan()

        self.assertIn(mock_scan.call_args.kwargs["source"],
                      ("all", ("claude_code", "codex"),
                       ("claude_code", "codex", "antigravity")))

    def test_custom_projects_dir_remains_claude_only(self):
        with mock.patch("scanner.scan") as mock_scan:
            cli.cmd_scan(projects_dir="/tmp/claude-projects")

        self.assertEqual(mock_scan.call_args.kwargs["source"], "claude_code")


if __name__ == "__main__":
    unittest.main()
