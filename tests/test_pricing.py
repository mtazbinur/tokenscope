"""Tests for pricing.py — model coverage, prefix resolution, long-context tiers.

The table is the only thing standing between a real model and a silent $0, so
these tests care about two failure modes: a model that should be priced and
isn't, and a model priced from the wrong (shorter) key.
"""

import unittest

from pricing import (
    OPENAI_LONG_CONTEXT_THRESHOLD,
    SOURCE_CLAUDE,
    SOURCE_CODEX,
    calc_cost,
    get_pricing,
    is_long_context,
    long_context_price,
)


class TestClaudeCoverage(unittest.TestCase):
    def test_retired_models_are_still_priced(self):
        # Historical transcripts outlive a model's availability; an unpriced id
        # shows the whole session as $0 / n/a.
        for model, inp, out in (
            ("claude-opus-4-1-20250805", 15.00, 75.00),
            ("claude-opus-4-20250514", 15.00, 75.00),
            ("claude-sonnet-4-20250514", 3.00, 15.00),
            ("claude-3-7-sonnet-20250219", 3.00, 15.00),
            ("claude-3-5-sonnet-20241022", 3.00, 15.00),
            ("claude-3-5-haiku-20241022", 0.80, 4.00),
            ("claude-3-opus-20240229", 15.00, 75.00),
            ("claude-3-haiku-20240307", 0.25, 1.25),
        ):
            with self.subTest(model=model):
                price = get_pricing(model)
                self.assertIsNotNone(price, f"{model} is unpriced")
                self.assertEqual(price["input"], inp)
                self.assertEqual(price["output"], out)

    def test_cache_rates_follow_documented_multipliers(self):
        # Cache write is 1.25x base input, a cache hit is 0.1x base input.
        for model in ("claude-opus-5", "claude-sonnet-4-20250514", "claude-3-haiku"):
            with self.subTest(model=model):
                price = get_pricing(model)
                self.assertAlmostEqual(price["cache_write"], price["input"] * 1.25)
                self.assertAlmostEqual(price["cache_read"], price["input"] * 0.1)

    def test_longest_prefix_wins(self):
        # "claude-opus-4" is a prefix of "claude-opus-4-1-...", so a
        # shortest-match resolver would bill Opus 4.1 at Opus 4.5 rates.
        self.assertEqual(get_pricing("claude-opus-4-1-20250805")["input"], 15.00)
        self.assertEqual(get_pricing("claude-opus-4-5-20251101")["input"], 5.00)

    def test_unknown_models_stay_unpriced(self):
        for model in ("", None, "gemma-3-27b", "glm-4.6", "gpt-5.5"):
            with self.subTest(model=model):
                self.assertIsNone(get_pricing(model))


class TestCodexCoverage(unittest.TestCase):
    def test_gpt_5_x_families_are_priced(self):
        for model, inp, out in (
            ("gpt-5.6-sol", 4.00, 20.00),
            ("gpt-5.6-terra", 2.00, 12.00),
            ("gpt-5.6-luna", 0.20, 1.20),
            ("gpt-5.5", 5.00, 30.00),
            ("gpt-5.5-pro", 30.00, 180.00),
            ("gpt-5.4", 2.50, 15.00),
            ("gpt-5.4-mini", 0.75, 4.50),
            ("gpt-5.4-nano", 0.20, 1.25),
            ("gpt-5.3-codex", 1.75, 14.00),
            ("gpt-5.2", 1.75, 14.00),
            ("gpt-5.1", 1.25, 10.00),
            ("gpt-5", 1.25, 10.00),
        ):
            with self.subTest(model=model):
                price = get_pricing(model, source=SOURCE_CODEX)
                self.assertIsNotNone(price, f"{model} is unpriced")
                self.assertEqual(price["input"], inp)
                self.assertEqual(price["output"], out)

    def test_longest_prefix_wins(self):
        # gpt-5.4-mini is a quarter the price of gpt-5.4 — resolving it from the
        # shorter key would triple its cost.
        self.assertEqual(get_pricing("gpt-5.4-mini-2026", source=SOURCE_CODEX)["input"], 0.75)
        self.assertEqual(get_pricing("gpt-5.5-pro-2026", source=SOURCE_CODEX)["input"], 30.00)

    def test_claude_ids_are_not_priced_from_the_codex_table(self):
        self.assertIsNone(get_pricing("claude-opus-5", source=SOURCE_CODEX))


class TestLongContextTier(unittest.TestCase):
    def test_threshold_applies_to_every_family_that_has_one(self):
        for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4"):
            with self.subTest(model=model):
                price = get_pricing(model, source=SOURCE_CODEX)
                self.assertEqual(price["long_context_threshold"],
                                 OPENAI_LONG_CONTEXT_THRESHOLD)
                # Documented rule: 2x input, 1.5x output for the whole request.
                self.assertAlmostEqual(price["long_input"], price["input"] * 2)
                self.assertAlmostEqual(price["long_output"], price["output"] * 1.5)

    def test_short_context_families_have_no_tier(self):
        for model in ("gpt-5.3-codex", "gpt-5.1", "gpt-5.4-mini"):
            with self.subTest(model=model):
                self.assertNotIn("long_context_threshold",
                                 get_pricing(model, source=SOURCE_CODEX))
                self.assertFalse(is_long_context(model, 1_000_000, source=SOURCE_CODEX))

    def test_is_long_context_only_above_threshold(self):
        self.assertFalse(is_long_context("gpt-5.6-sol", 272_000, source=SOURCE_CODEX))
        self.assertTrue(is_long_context("gpt-5.6-sol", 272_001, source=SOURCE_CODEX))

    def test_overlay_leaves_short_context_prices_alone(self):
        price = get_pricing("gpt-5.3-codex", source=SOURCE_CODEX)
        self.assertEqual(long_context_price(price), price)

    def test_sol_long_context_reprices_whole_request(self):
        # 300K prompt, 100K of it cached: 200K at $8, 100K at $0.80, 1K out $30.
        cost = calc_cost("gpt-5.6-sol", 300_000, 1_000, 100_000, 0,
                         source=SOURCE_CODEX, long_context=True)
        expected = (200_000 * 8.00 + 100_000 * 0.80 + 1_000 * 30.00) / 1_000_000
        self.assertAlmostEqual(cost, expected)
        self.assertGreater(cost, calc_cost("gpt-5.6-sol", 300_000, 1_000, 100_000, 0,
                                           source=SOURCE_CODEX))

    def test_claude_ignores_the_flag(self):
        # Claude has no long-context tier; the flag must not change the bill.
        plain = calc_cost("claude-opus-5", 900_000, 1_000, 0, 0, source=SOURCE_CLAUDE)
        flagged = calc_cost("claude-opus-5", 900_000, 1_000, 0, 0,
                            source=SOURCE_CLAUDE, long_context=True)
        self.assertEqual(plain, flagged)


if __name__ == "__main__":
    unittest.main()
