import json
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch
from pathlib import Path

import quota
from quota import get_quota_snapshot


# Windows whose reset has already passed are dropped as refilled, so fixtures
# must sit in the future rather than at a frozen epoch that eventually rots.
SOON = int(time.time()) + 3600
LATER = int(time.time()) + 3 * 86400


def _usage_response(utilization):
    """One urlopen result shaped like the OAuth usage endpoint's payload."""
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "five_hour": {"utilization": utilization, "resets_at": "2026-08-31T10:00:00Z"},
                "seven_day": {"utilization": 11, "resets_at": "2026-09-07T05:00:00Z"},
            }).encode("utf-8")

    return [Response()]


class TestQuotaSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        # The live-API path mirrors its last good reading to disk. Point that at
        # the temp dir so a test can neither read nor clobber the real
        # ~/.claude cache — without this, one test's snapshot leaks into the
        # next one's assertions.
        patcher = patch.object(quota, "CLAUDE_API_CACHE_PATH", self.root / "quota-cache.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmpdir.cleanup()

    def write_jsonl(self, name, records):
        path = self.root / name
        with path.open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
        return path

    def test_reads_codex_five_hour_and_weekly_windows(self):
        self.write_jsonl("rollout.jsonl", [{
            "timestamp": "2026-08-31T06:15:24.521Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {"used_percent": 69.0, "window_minutes": 300, "resets_at": SOON},
                    "secondary": {"used_percent": 11.0, "window_minutes": 10080, "resets_at": LATER},
                },
            },
        }])

        snapshot = get_quota_snapshot("codex", codex_dir=self.root)

        self.assertTrue(snapshot["available"])
        self.assertEqual([window["key"] for window in snapshot["windows"]], ["five_hour", "weekly"])
        self.assertEqual(snapshot["windows"][0]["remaining_percent"], 31.0)
        self.assertEqual(snapshot["windows"][1]["remaining_percent"], 89.0)
        self.assertTrue(snapshot["windows"][0]["reset_at"].endswith("Z"))

    def test_reads_claude_rate_limit_event(self):
        self.write_jsonl("claude.jsonl", [{
            "timestamp": "2026-08-31T06:20:00Z",
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "rateLimitType": "five_hour",
                "utilization": 0.25,
                "resetsAt": SOON,
            },
        }])

        # Isolate the local parser: with a live credential on the host this
        # would otherwise reach the real usage endpoint and assert on the
        # developer's own quota.
        with patch("quota._claude_api_snapshot", return_value=None):
            snapshot = get_quota_snapshot("claude_code", claude_dirs=[self.root])

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["windows"][0]["label"], "Current session")
        self.assertEqual(snapshot["windows"][0]["remaining_percent"], 75.0)

    def test_reports_unavailable_without_local_quota_signal(self):
        snapshot = get_quota_snapshot("codex", codex_dir=self.root)

        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["windows"], [])
        self.assertIn("No recent quota data", snapshot["message"])

    def test_reads_claude_live_usage_api_when_logs_have_no_snapshot(self):
        class Response:
            def __init__(self, utilization):
                self.utilization = utilization

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "five_hour": {"utilization": self.utilization, "resets_at": "2026-08-31T10:00:00Z"},
                    "seven_day": {"utilization": 11, "resets_at": "2026-09-07T05:00:00Z"},
                }).encode("utf-8")

        with patch("quota._claude_credentials", return_value=("test-token", None)), \
             patch("quota.urllib.request.urlopen", side_effect=[Response(25), Response(40)]) as urlopen, \
             patch("quota._CLAUDE_API_CACHE", None), \
             patch("quota._CLAUDE_API_CACHE_AT", 0.0), \
             patch("quota._CLAUDE_API_RETRY_AFTER", 0.0), \
             patch("quota._load_disk_snapshot", return_value=None):
            snapshot = get_quota_snapshot(
                "claude_code",
                claude_dirs=[self.root],
                force_refresh=True,
            )

            self.assertTrue(snapshot["available"])
            self.assertEqual(snapshot["source"], "live_api")
            self.assertEqual(snapshot["windows"][0]["remaining_percent"], 75.0)
            self.assertEqual(snapshot["windows"][1]["remaining_percent"], 89.0)

            refreshed = get_quota_snapshot(
                "claude_code",
                claude_dirs=[self.root],
                force_refresh=True,
            )
            self.assertEqual(refreshed["windows"][0]["remaining_percent"], 60.0)
            self.assertEqual(urlopen.call_count, 2)

    def test_reads_claude_quota_limits_on_assistant_record(self):
        """Claude Code hangs `quotaLimits` off the rejected assistant turn.

        This is the shape that actually appears in real transcripts; only
        handling `rate_limit_event` left the Claude panel permanently empty.
        """
        self.write_jsonl("claude.jsonl", [{
            "type": "assistant",
            "timestamp": "2026-08-31T06:20:00Z",
            "quotaLimits": {
                "status": "rejected",
                "resetsAt": SOON,
                "rateLimitType": "five_hour",
            },
        }])

        with patch("quota._claude_api_snapshot", return_value=None):
            snapshot = get_quota_snapshot("claude_code", claude_dirs=[self.root])

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["source"], "local_event")
        self.assertEqual(snapshot["windows"][0]["key"], "five_hour")
        self.assertEqual(snapshot["windows"][0]["remaining_percent"], 0.0)

    def test_drops_windows_whose_reset_already_passed(self):
        self.write_jsonl("rollout.jsonl", [{
            "timestamp": "2026-08-31T06:15:24.521Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {"used_percent": 97.0, "window_minutes": 300, "resets_at": int(time.time()) - 60},
                    "secondary": {"used_percent": 11.0, "window_minutes": 10080, "resets_at": LATER},
                },
            },
        }])

        snapshot = get_quota_snapshot("codex", codex_dir=self.root)

        self.assertEqual([window["key"] for window in snapshot["windows"]], ["weekly"])

    def test_expired_credential_is_reported_without_calling_the_api(self):
        with patch("quota._claude_credentials", return_value=("token", time.time() - 10)), \
             patch("quota.urllib.request.urlopen") as urlopen, \
             patch("quota._CLAUDE_API_CACHE", None), \
             patch("quota._CLAUDE_API_RETRY_AFTER", 0.0), \
             patch("quota._load_disk_snapshot", return_value=None):
            snapshot = get_quota_snapshot("claude_code", claude_dirs=[self.root], force_refresh=True)

        urlopen.assert_not_called()
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["message"], quota.AUTH_FAILED_MESSAGE)

    def test_rate_limited_refresh_keeps_the_last_good_snapshot(self):
        cached = {
            "available": True,
            "windows": [{"key": "five_hour", "label": "Current session", "remaining_percent": 42.0, "reset_at": None}],
            "updated_at": "2026-08-31T06:00:00Z",
            "source": "live_api",
            "message": "Live usage from Claude Code",
        }
        error = urllib.error.HTTPError("https://api.anthropic.com", 429, "Too Many Requests", {}, None)

        with patch("quota._claude_credentials", return_value=("token", None)), \
             patch("quota.urllib.request.urlopen", side_effect=error), \
             patch("quota._CLAUDE_API_CACHE", cached), \
             patch("quota._CLAUDE_API_CACHE_AT", 0.0), \
             patch("quota._CLAUDE_API_RETRY_AFTER", 0.0), \
             patch("quota._load_disk_snapshot", return_value=None):
            snapshot = get_quota_snapshot("claude_code", claude_dirs=[self.root], force_refresh=True)

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["source"], "live_api_stale")
        self.assertEqual(snapshot["windows"][0]["remaining_percent"], 42.0)
        self.assertEqual(snapshot["message"], quota.RATE_LIMITED_MESSAGE)

    def test_backs_off_instead_of_retrying_a_failed_endpoint(self):
        error = urllib.error.HTTPError("https://api.anthropic.com", 429, "Too Many Requests", {}, None)

        with patch("quota._claude_credentials", return_value=("token", None)), \
             patch("quota.urllib.request.urlopen", side_effect=error) as urlopen, \
             patch("quota._CLAUDE_API_CACHE", None), \
             patch("quota._CLAUDE_API_CACHE_AT", 0.0), \
             patch("quota._CLAUDE_API_RETRY_AFTER", 0.0), \
             patch("quota._load_disk_snapshot", return_value=None):
            get_quota_snapshot("claude_code", claude_dirs=[self.root], force_refresh=True)
            get_quota_snapshot("claude_code", claude_dirs=[self.root])
            get_quota_snapshot("claude_code", claude_dirs=[self.root])

        self.assertEqual(urlopen.call_count, 1)

    def test_expired_credential_is_renewed_through_the_claude_cli(self):
        """An expired token is refreshed by Claude Code itself, not by us.

        The refresh token rotates single-use, so spending it here would log the
        user out of their editor; `claude auth status` makes the first-party CLI
        do the exchange and write the result back.
        """
        stale = ("stale-token", time.time() - 10)
        fresh = ("fresh-token", time.time() + 3600)

        with patch("quota._claude_credentials", side_effect=[stale, fresh]) as creds, \
             patch("quota._refresh_credentials_via_cli", return_value=True) as refresh, \
             patch("quota.urllib.request.urlopen", side_effect=_usage_response(30)), \
             patch("quota._CLAUDE_API_CACHE", None), \
             patch("quota._CLAUDE_API_CACHE_AT", 0.0), \
             patch("quota._CLAUDE_API_RETRY_AFTER", 0.0):
            snapshot = get_quota_snapshot("claude_code", claude_dirs=[self.root], force_refresh=True)

        refresh.assert_called_once()
        self.assertEqual(creds.call_count, 2)
        self.assertEqual(snapshot["source"], "live_api")
        self.assertEqual(snapshot["windows"][0]["remaining_percent"], 70.0)

    def test_sign_in_is_offered_only_when_the_refresh_fails(self):
        with patch("quota._claude_credentials", return_value=("stale", time.time() - 10)), \
             patch("quota._refresh_credentials_via_cli", return_value=False), \
             patch("quota.urllib.request.urlopen") as urlopen, \
             patch("quota._CLAUDE_API_CACHE", None), \
             patch("quota._CLAUDE_API_RETRY_AFTER", 0.0):
            snapshot = get_quota_snapshot("claude_code", claude_dirs=[self.root], force_refresh=True)

        urlopen.assert_not_called()
        self.assertTrue(snapshot["needs_sign_in"])
        self.assertEqual(snapshot["message"], quota.AUTH_FAILED_MESSAGE)

    def test_cli_refresh_is_throttled(self):
        with patch("quota.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("quota.subprocess.run") as run, \
             patch("quota._LAST_CLI_REFRESH_AT", 0.0):
            run.return_value = type("R", (), {"returncode": 0, "stdout": '{"loggedIn": true}'})()
            self.assertTrue(quota._refresh_credentials_via_cli())
            # A second attempt inside the interval must not re-spawn the CLI.
            self.assertFalse(quota._refresh_credentials_via_cli())
            self.assertEqual(run.call_count, 1)


class TestStaleClaudeSnapshot(unittest.TestCase):
    """What the panel shows when a live refresh fails but a reading exists."""

    def setUp(self):
        self.snapshot = {
            "available": True,
            "windows": [
                {"key": "five_hour", "label": "Current session",
                 "remaining_percent": 3.0,
                 "reset_at": "2020-01-01T00:00:00Z"},        # long since reset
                {"key": "seven_day", "label": "Weekly",
                 "remaining_percent": 40.0,
                 "reset_at": _iso(LATER)},
            ],
            "updated_at": _iso(int(time.time()) - 600),
            "source": "live_api",
            "message": "Live usage from Claude Code",
        }

    def test_reset_windows_are_dropped_from_a_stale_reading(self):
        # The disk cache lives 24h, longer than a 5h window, so replaying its
        # last "3% left" would report a limit that has already refilled.
        with patch("quota._CLAUDE_API_CACHE", self.snapshot):
            stale = quota._stale_claude_snapshot(quota.RATE_LIMITED_MESSAGE)

        self.assertIsNotNone(stale)
        self.assertEqual([w["key"] for w in stale["windows"]], ["seven_day"])
        self.assertEqual(stale["source"], "live_api_stale")
        self.assertEqual(stale["message"], quota.RATE_LIMITED_MESSAGE)
        self.assertFalse(stale["needs_sign_in"])

    def test_a_fully_reset_reading_is_not_reused(self):
        expired = dict(self.snapshot, windows=[self.snapshot["windows"][0]])
        with patch("quota._CLAUDE_API_CACHE", expired):
            # Nothing left to show, so the caller falls through to the "why"
            # message rather than rendering an empty panel.
            self.assertIsNone(quota._stale_claude_snapshot(quota.NETWORK_MESSAGE))

    def test_a_signed_out_user_still_gets_the_sign_in_action(self):
        # A cached reading must not hide the one failure the user can fix.
        for message in (quota.NO_CREDENTIAL_MESSAGE, quota.AUTH_FAILED_MESSAGE):
            with self.subTest(message=message):
                with patch("quota._CLAUDE_API_CACHE", self.snapshot):
                    stale = quota._stale_claude_snapshot(message)
                self.assertTrue(stale["needs_sign_in"])
                self.assertEqual(stale["message"], message)

    def test_cached_live_reading_is_repolled_once_its_windows_reset(self):
        expired = dict(self.snapshot, windows=[self.snapshot["windows"][0]])
        with patch("quota._CLAUDE_API_CACHE", expired), \
             patch("quota._CLAUDE_API_CACHE_AT", time.monotonic()), \
             patch("quota._CLAUDE_API_RETRY_AFTER", 0.0), \
             patch("quota._claude_credentials", return_value=("", None)):
            # Cache is inside its TTL but has nothing valid left, so the code
            # falls through to a refresh instead of serving the reset window.
            snapshot = quota._claude_api_snapshot()

        self.assertIsNone(snapshot)


def _iso(epoch_seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
