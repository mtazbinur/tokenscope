"""Tests for settings.py and the dashboard's settings endpoints."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pricing
import settings
from dashboard import DashboardHandler


class TestDefaults(unittest.TestCase):
    def test_both_providers_on_by_default(self):
        data = settings.defaults()
        self.assertEqual(data["sources"], {"claude_code": True, "codex": True})
        self.assertEqual(data["pricing_overrides"], {"claude_code": {}, "codex": {}})

    def test_defaults_are_not_shared_state(self):
        first = settings.defaults()
        first["sources"]["codex"] = False
        self.assertTrue(settings.defaults()["sources"]["codex"])

    def test_missing_file_reads_as_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(settings.load(Path(tmp) / "nope.json"), settings.defaults())

    def test_corrupt_file_reads_as_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.json"
            target.write_text("{not json at all", encoding="utf-8")
            self.assertEqual(settings.load(target), settings.defaults())


class TestNormalizeSources(unittest.TestCase):
    def test_only_explicit_false_disables(self):
        data = settings.normalize({"sources": {"codex": False}})
        self.assertTrue(data["sources"]["claude_code"])
        self.assertFalse(data["sources"]["codex"])

    def test_unknown_provider_is_ignored(self):
        data = settings.normalize({"sources": {"gemini": True}})
        self.assertEqual(sorted(data["sources"]), ["claude_code", "codex"])

    def test_all_disabled_falls_back_to_defaults_on_read(self):
        # A hand-edited file must not be able to leave the dashboard with
        # nothing to show and no UI left to fix it.
        data = settings.normalize({"sources": {"claude_code": False, "codex": False}})
        self.assertEqual(data["sources"], {"claude_code": True, "codex": True})

    def test_all_disabled_is_rejected_on_write(self):
        with self.assertRaises(settings.SettingsError):
            settings.normalize({"sources": {"claude_code": False, "codex": False}}, strict=True)

    def test_non_dict_payload(self):
        self.assertEqual(settings.normalize("nope"), settings.defaults())
        with self.assertRaises(settings.SettingsError):
            settings.normalize("nope", strict=True)


class TestNormalizePricing(unittest.TestCase):
    ENTRY = {"input": 1, "output": 2, "cache_read": 0.1, "cache_write": 1.25}

    def _normalized(self, model, entry, strict=False):
        data = settings.normalize(
            {"pricing_overrides": {"claude_code": {model: entry}}}, strict=strict)
        return data["pricing_overrides"]["claude_code"]

    def test_model_name_is_lowercased_and_trimmed(self):
        models = self._normalized("  Claude-Opus-9 ", self.ENTRY)
        self.assertIn("claude-opus-9", models)

    def test_rates_become_floats(self):
        entry = self._normalized("m", dict(self.ENTRY, input="3.5"))["m"]
        self.assertEqual(entry["input"], 3.5)
        self.assertIsInstance(entry["input"], float)

    def test_missing_required_rate_is_dropped_then_rejected(self):
        entry = dict(self.ENTRY)
        del entry["output"]
        self.assertEqual(self._normalized("m", entry), {})
        with self.assertRaises(settings.SettingsError):
            self._normalized("m", entry, strict=True)

    def test_negative_and_nan_rates_rejected(self):
        for bad in (-1, float("nan")):
            with self.assertRaises(settings.SettingsError):
                self._normalized("m", dict(self.ENTRY, input=bad), strict=True)

    def test_non_numeric_rate_rejected(self):
        with self.assertRaises(settings.SettingsError):
            self._normalized("m", dict(self.ENTRY, input="cheap"), strict=True)

    def test_whitespace_in_model_name_rejected(self):
        with self.assertRaises(settings.SettingsError):
            self._normalized("my model", self.ENTRY, strict=True)

    def test_empty_model_name_rejected(self):
        with self.assertRaises(settings.SettingsError):
            self._normalized("   ", self.ENTRY, strict=True)

    def test_long_context_tier_kept_with_threshold(self):
        entry = self._normalized("m", dict(self.ENTRY, long_context_threshold=272000,
                                           long_input=2, long_output=4))["m"]
        self.assertEqual(entry["long_context_threshold"], 272000)
        self.assertEqual(entry["long_input"], 2)

    def test_long_rates_without_threshold_are_dropped(self):
        # A long_* rate with no threshold can never fire; storing it would look
        # like a configured tier that silently does nothing.
        entry = self._normalized("m", dict(self.ENTRY, long_input=2))["m"]
        self.assertNotIn("long_input", entry)

    def test_unknown_provider_rejected_on_write(self):
        with self.assertRaises(settings.SettingsError):
            settings.normalize({"pricing_overrides": {"gemini": {}}}, strict=True)


class TestSaveAndLoad(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "settings.json"
            payload = {
                "sources": {"codex": False},
                "pricing_overrides": {"claude_code": {
                    "claude-opus-9": {"input": 4, "output": 20,
                                      "cache_read": 0.4, "cache_write": 5},
                }},
            }
            saved = settings.save(payload, path=target)
            self.assertTrue(target.exists())
            self.assertEqual(settings.load(target), saved)
            self.assertFalse(saved["sources"]["codex"])

    def test_save_rejects_bad_payload_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.json"
            with self.assertRaises(settings.SettingsError):
                settings.save({"sources": {"claude_code": False, "codex": False}}, path=target)
            self.assertFalse(target.exists())

    def test_save_leaves_no_temp_files_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.json"
            settings.save({}, path=target)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["settings.json"])


class TestEnabledSources(unittest.TestCase):
    def test_order_is_canonical(self):
        self.assertEqual(settings.enabled_sources(settings.defaults()),
                         ["claude_code", "codex"])

    def test_single_provider(self):
        data = settings.normalize({"sources": {"claude_code": False}})
        self.assertEqual(settings.enabled_sources(data), ["codex"])

    def test_scan_source_maps_to_scanner_argument(self):
        self.assertEqual(settings.scan_source(settings.defaults()), "all")
        one = settings.normalize({"sources": {"codex": False}})
        self.assertEqual(settings.scan_source(one), "claude_code")

    def test_empty_flags_never_yield_no_provider(self):
        self.assertEqual(settings.enabled_sources({"sources": {}}),
                         ["claude_code", "codex"])


class TestPricingOverrideLayer(unittest.TestCase):
    """set_overrides must layer cleanly and be fully reversible."""

    def tearDown(self):
        pricing.set_overrides(None)

    def test_override_replaces_builtin_rate(self):
        pricing.set_overrides({"claude_code": {"claude-opus-5": {
            "input": 99, "output": 1, "cache_read": 1, "cache_write": 1}}})
        self.assertEqual(pricing.get_pricing("claude-opus-5")["input"], 99)

    def test_clearing_restores_the_builtin_table(self):
        builtin = pricing.BUILTIN_PRICING_BY_SOURCE["claude_code"]["claude-opus-5"]["input"]
        pricing.set_overrides({"claude_code": {"claude-opus-5": {
            "input": 99, "output": 1, "cache_read": 1, "cache_write": 1}}})
        pricing.set_overrides(None)
        self.assertEqual(pricing.get_pricing("claude-opus-5")["input"], builtin)

    def test_builtin_table_is_never_mutated(self):
        pricing.set_overrides({"claude_code": {"claude-opus-5": {
            "input": 99, "output": 1, "cache_read": 1, "cache_write": 1}}})
        self.assertNotEqual(
            pricing.BUILTIN_PRICING_BY_SOURCE["claude_code"]["claude-opus-5"]["input"], 99)

    def test_added_model_resolves_by_prefix_like_a_builtin(self):
        pricing.set_overrides({"claude_code": {"claude-opus-9": {
            "input": 4, "output": 20, "cache_read": 0.4, "cache_write": 5}}})
        self.assertEqual(pricing.get_pricing("claude-opus-9-20260101")["input"], 4)

    def test_longest_prefix_still_wins_over_an_override(self):
        # "gpt-5.4-mini" must not be priced as an overridden "gpt-5.4".
        pricing.set_overrides({"codex": {"gpt-5.4": {
            "input": 50, "output": 50, "cache_read": 5, "cache_write": 60}}})
        mini = pricing.get_pricing("gpt-5.4-mini-20260101", source="codex")
        self.assertNotEqual(mini["input"], 50)

    def test_incomplete_override_is_ignored(self):
        builtin = pricing.get_pricing("claude-opus-5")["input"]
        pricing.set_overrides({"claude_code": {"claude-opus-5": {"input": 99}}})
        self.assertEqual(pricing.get_pricing("claude-opus-5")["input"], builtin)

    def test_calc_cost_uses_the_override(self):
        pricing.set_overrides({"claude_code": {"claude-opus-5": {
            "input": 10, "output": 0, "cache_read": 0, "cache_write": 0}}})
        self.assertAlmostEqual(
            pricing.calc_cost("claude-opus-5", 1_000_000, 0, 0, 0), 10.0)

    def test_apply_installs_the_stored_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.json"
            settings.save({"pricing_overrides": {"claude_code": {"claude-opus-9": {
                "input": 7, "output": 1, "cache_read": 1, "cache_write": 1}}}}, path=target)
            original = settings.SETTINGS_PATH
            settings.SETTINGS_PATH = target
            try:
                settings.apply()
                self.assertEqual(pricing.get_pricing("claude-opus-9")["input"], 7)
            finally:
                settings.SETTINGS_PATH = original


class TestSettingsEndpoints(unittest.TestCase):
    """GET/POST /api/settings, against a real server on a throwaway file."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._original_path = settings.SETTINGS_PATH
        settings.SETTINGS_PATH = Path(cls._tmpdir.name) / "tokenscope-settings.json"
        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        settings.SETTINGS_PATH = cls._original_path
        pricing.set_overrides(None)
        cls._tmpdir.cleanup()

    def setUp(self):
        if settings.SETTINGS_PATH.exists():
            settings.SETTINGS_PATH.unlink()
        pricing.set_overrides(None)

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path):
        with urllib.request.urlopen(self._url(path)) as resp:
            return resp.status, json.loads(resp.read())

    def _post(self, path, payload):
        request = urllib.request.Request(
            self._url(path), data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_get_returns_defaults_and_the_builtin_table(self):
        status, payload = self._get("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"], settings.defaults())
        self.assertIn("claude-opus-5", payload["builtin_pricing"]["claude_code"])
        self.assertEqual(payload["rate_fields"], list(settings.RATE_FIELDS))
        self.assertEqual(sorted(payload["sources"]), ["claude_code", "codex"])

    def test_displayed_path_is_home_relative(self):
        # The path is rendered on the page (and in screenshots), so it must not
        # carry an absolute home directory.
        _status, payload = self._get("/api/settings")
        self.assertNotIn("/Users/", payload["path"])
        self.assertNotIn("\\Users\\", payload["path"])

    def test_post_persists_and_applies(self):
        status, payload = self._post("/api/settings", {
            "sources": {"codex": False},
            "pricing_overrides": {"claude_code": {"claude-opus-5": {
                "input": 42, "output": 1, "cache_read": 1, "cache_write": 1}}},
        })
        self.assertEqual(status, 200)
        self.assertFalse(payload["settings"]["sources"]["codex"])
        # Written to disk...
        self.assertTrue(settings.SETTINGS_PATH.exists())
        self.assertEqual(settings.load()["sources"]["codex"], False)
        # ...applied in-process...
        self.assertEqual(pricing.get_pricing("claude-opus-5")["input"], 42)
        # ...and reflected in the effective table the browser is handed back.
        self.assertEqual(payload["pricing"]["claude_code"]["claude-opus-5"]["input"], 42)

    def test_post_rejects_bad_rates_with_a_message(self):
        status, payload = self._post("/api/settings", {
            "pricing_overrides": {"claude_code": {"m": {"input": -1, "output": 1,
                                                        "cache_read": 1, "cache_write": 1}}},
        })
        self.assertEqual(status, 400)
        self.assertIn("zero or greater", payload["error"])
        self.assertFalse(settings.SETTINGS_PATH.exists())

    def test_post_rejects_disabling_every_provider(self):
        status, payload = self._post(
            "/api/settings", {"sources": {"claude_code": False, "codex": False}})
        self.assertEqual(status, 400)
        self.assertIn("at least one", payload["error"].lower())

    def test_post_rejects_a_non_json_body(self):
        request = urllib.request.Request(
            self._url("/api/settings"), data=b"not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)

    def test_index_injects_settings_for_the_first_paint(self):
        with urllib.request.urlopen(self._url("/")) as resp:
            body = resp.read().decode()
        self.assertIn('"settings"', body)
        self.assertIn('"builtin_pricing"', body)

    def test_favicon_is_served(self):
        with urllib.request.urlopen(self._url("/favicon.svg")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "image/svg+xml")
            self.assertIn(b"<svg", resp.read())


class TestSettingsPageMarkup(unittest.TestCase):
    """The settings UI is part of HTML_TEMPLATE; guard its load-bearing pieces."""

    @classmethod
    def setUpClass(cls):
        from dashboard import HTML_TEMPLATE
        cls.html = HTML_TEMPLATE

    def test_favicon_is_linked(self):
        self.assertIn('<link rel="icon" type="image/svg+xml" href="/favicon.svg">', self.html)

    def test_panels_and_nav_exist(self):
        self.assertIn('id="settings-panel"', self.html)
        self.assertIn('id="nav-settings"', self.html)
        self.assertIn("onclick=\"setView('settings')\"", self.html)

    def test_settings_path_is_shown_on_the_page(self):
        # The page tells the user where its state lives; initSettings fills this
        # element from the server payload, so both halves have to exist.
        self.assertIn('id="settings-path"', self.html)
        self.assertIn("getElementById('settings-path')", self.html)

    def test_save_goes_through_the_confirm_dialog(self):
        # Saving must never be a single unconfirmed click.
        self.assertIn("async function requestSettingsSave()", self.html)
        self.assertIn("await openConfirm(", self.html)
        self.assertIn('id="confirm-modal"', self.html)

    def test_unsaved_draft_warns_before_unload(self):
        self.assertIn("beforeunload", self.html)

    def test_pricing_is_reassignable_for_live_repricing(self):
        # A save changes the effective table; a `const` here would leave the
        # page showing costs the CLI no longer agrees with.
        self.assertIn("let PRICING = (window.APP_CONFIG || {}).pricing", self.html)


if __name__ == "__main__":
    unittest.main()
