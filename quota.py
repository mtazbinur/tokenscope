"""Read provider quota snapshots from Claude Code and Codex sources."""

import glob
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


SOURCE_CLAUDE = "claude_code"
SOURCE_CODEX = "codex"

_CACHE = {}
_CLAUDE_API_CACHE = None
_CLAUDE_API_CACHE_AT = 0.0
CLAUDE_API_CACHE_SECONDS = 60

# The usage endpoint is aggressively throttled — Claude Code polls it too, and
# the quota is shared per account.  Re-requesting on every dashboard poll only
# extends the block, so each failure class gets its own cool-off and the last
# good reading keeps being shown (flagged stale) instead of blanking the panel.
CLAUDE_API_BACKOFF_SECONDS = {"auth": 300, "rate_limited": 180, "network": 60}
CLAUDE_API_MAX_BACKOFF_SECONDS = 900
_CLAUDE_API_RETRY_AFTER = 0.0
_CLAUDE_API_MESSAGE = None

# The last successful reading is mirrored to disk so a restart (or a cold VS
# Code webview) still has percentages to show while the endpoint is throttled.
CLAUDE_API_CACHE_PATH = Path.home() / ".claude" / "usage-quota-cache.json"
CLAUDE_API_DISK_MAX_AGE_SECONDS = 24 * 60 * 60

NO_CREDENTIAL_MESSAGE = "No Claude Code sign-in found — run `claude auth login`"
AUTH_FAILED_MESSAGE = "Claude Code sign-in expired — run `claude auth login`, then retry"
RATE_LIMITED_MESSAGE = "Claude's usage endpoint is busy — showing the last reading"
NETWORK_MESSAGE = "Could not reach Claude's usage endpoint"
NO_READING_YET_MESSAGE = "Waiting for Claude's first usage reading"


def _as_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _clamp_percent(value):
    number = _as_number(value)
    if number is None:
        return None
    return max(0.0, min(100.0, number))


def _remaining_percent(info):
    for key in ("remaining_percent", "remainingPercent"):
        value = _as_number(info.get(key))
        if value is not None:
            return _clamp_percent(value)

    value = _as_number(info.get("remaining"))
    if value is not None:
        return _clamp_percent(value * 100 if 0 <= value <= 1 else value)

    for key in ("used_percent", "usedPercent", "utilization", "usage"):
        value = _as_number(info.get(key))
        if value is not None:
            used = value * 100 if 0 <= value <= 1 else value
            return _clamp_percent(100 - used)
    return None


def _iso_timestamp(value):
    if value is None or value == "":
        return None
    number = _as_number(value)
    if number is not None:
        # Unix milliseconds are occasionally used by CLI integrations.
        if number > 100000000000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_value(value):
    normalized = _iso_timestamp(value)
    if not normalized:
        return 0.0
    return datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp()


def _window_key(info, default=None):
    raw = " ".join(str(info.get(key, "")) for key in (
        "rateLimitType", "rate_limit_type", "window", "window_type", "limit_id",
    )).lower()
    minutes = _as_number(info.get("window_minutes", info.get("windowMinutes")))
    if "five" in raw or minutes == 300:
        return "five_hour"
    if "seven" in raw or "week" in raw or minutes == 10080:
        return "weekly"
    return default


def _event_timestamp(record, info):
    return _iso_timestamp(record.get("timestamp") or info.get("timestamp") or info.get("observed_at"))


def _oauth_blob(raw):
    """Pull ``(access_token, expires_at_seconds)`` out of a credentials blob."""
    try:
        oauth = json.loads(raw).get("claudeAiOauth", {})
    except (TypeError, json.JSONDecodeError, AttributeError):
        return "", None
    if not isinstance(oauth, dict):
        return "", None
    token = oauth.get("accessToken", "")
    if not isinstance(token, str) or not token:
        return "", None
    expires_at = _as_number(oauth.get("expiresAt"))
    if expires_at is not None and expires_at > 100000000000:
        expires_at /= 1000  # stored in milliseconds
    return token, expires_at


def _claude_credentials():
    """Read the same first-party OAuth credential locations Claude Code uses.

    Returns ``(token, expires_at)``; ``expires_at`` is ``None`` when the source
    doesn't carry one (env-var tokens), which is treated as "assume valid".

    The expiry matters more than it looks: ``/api/oauth/usage`` answers **429
    "Rate limited"** for an expired token rather than 401, so the response alone
    cannot tell "signed out" from "throttled".  ``/api/oauth/profile`` returns
    the honest 401, and the stored ``expiresAt`` agrees with it — so we trust
    the local expiry rather than mislabelling a signed-out user as throttled.
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        return token, None

    # Claude Code stores this credential in the macOS Keychain on supported
    # installs. Keep the subprocess invocation argument-based; never invoke a
    # shell and never include the secret in an exception or log message.
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result and result.returncode == 0 and result.stdout:
        token, expires_at = _oauth_blob(result.stdout)
        if token:
            return token, expires_at

    credentials_path = Path.home() / ".claude" / ".credentials.json"
    try:
        with credentials_path.open(encoding="utf-8") as stream:
            return _oauth_blob(stream.read())
    except OSError:
        return "", None


_LAST_CLI_REFRESH_AT = 0.0
CLI_REFRESH_MIN_INTERVAL = 60


def _is_expired(expires_at):
    """True when a stored credential's own expiry has passed (or is about to)."""
    return expires_at is not None and expires_at <= time.time() + 30


def _claude_cli():
    """Path to the Claude Code CLI, if it is installed for this user."""
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.exists() else ""


def _refresh_credentials_via_cli():
    """Ask Claude Code to renew its own OAuth credential, then re-read it.

    `claude auth status` is read-only from the user's point of view, but it
    makes the CLI notice an expired access token and exchange its refresh token
    — writing the renewed credential back to the Keychain itself.  Letting the
    first-party tool own that exchange is why this file never touches the
    refresh token: those rotate single-use, and spending one here would log the
    user out of their editor.
    """
    global _LAST_CLI_REFRESH_AT
    now = time.monotonic()
    if now - _LAST_CLI_REFRESH_AT < CLI_REFRESH_MIN_INTERVAL:
        return False
    _LAST_CLI_REFRESH_AT = now

    cli = _claude_cli()
    if not cli:
        return False
    try:
        result = subprocess.run(
            [cli, "auth", "status"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        return bool(json.loads(result.stdout or "{}").get("loggedIn"))
    except json.JSONDecodeError:
        return False


def _load_disk_snapshot():
    """Last good reading from a previous process, if it is still recent."""
    try:
        with CLAUDE_API_CACHE_PATH.open(encoding="utf-8") as stream:
            snapshot = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict) or not snapshot.get("windows"):
        return None
    observed = _timestamp_value(snapshot.get("updated_at"))
    if not observed or time.time() - observed > CLAUDE_API_DISK_MAX_AGE_SECONDS:
        return None
    return snapshot


def _save_disk_snapshot(snapshot):
    try:
        CLAUDE_API_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CLAUDE_API_CACHE_PATH.open("w", encoding="utf-8") as stream:
            json.dump(snapshot, stream)
    except OSError:
        pass  # a cache we can't persist is not worth failing the request over


def _unexpired_windows(windows):
    """Drop windows whose reset time has already passed.

    A window that has reset has refilled, so replaying its last "% left" is
    worse than showing nothing — same rule the local-event path applies.
    """
    now = time.time()
    return [
        window for window in windows or []
        if not window.get("reset_at") or _timestamp_value(window["reset_at"]) > now
    ]


def _stale_claude_snapshot(message):
    """Reuse the last good live reading when a refresh fails.

    Blanking the panel on a transient 429 is worse than showing the previous
    percentages with their own (older) timestamp, so callers get the cached
    windows back flagged as ``live_api_stale``.  Windows that have since reset
    are dropped, and a snapshot with nothing left to show returns None so the
    caller falls through to the "why" message (and the sign-in button).
    """
    global _CLAUDE_API_CACHE, _CLAUDE_API_MESSAGE
    _CLAUDE_API_MESSAGE = message
    if _CLAUDE_API_CACHE is None:
        _CLAUDE_API_CACHE = _load_disk_snapshot()
    if _CLAUDE_API_CACHE is None:
        return None
    windows = _unexpired_windows(_CLAUDE_API_CACHE.get("windows"))
    if not windows:
        return None
    stale = dict(_CLAUDE_API_CACHE)
    stale["windows"] = windows
    stale["source"] = "live_api_stale"
    stale["message"] = message
    # A stale reading does not mean the credential is fine: when the refresh
    # failed *because* the user is signed out, the panel still has to offer the
    # sign-in action instead of just older percentages.
    stale["needs_sign_in"] = message in (NO_CREDENTIAL_MESSAGE, AUTH_FAILED_MESSAGE)
    return stale


def _back_off(kind, message, retry_after=None):
    """Cool off before touching the endpoint again.

    A server-supplied ``Retry-After`` always wins over our own guess; both are
    capped so a bad header can't wedge the panel for hours.
    """
    global _CLAUDE_API_RETRY_AFTER
    delay = _as_number(retry_after) or CLAUDE_API_BACKOFF_SECONDS[kind]
    delay = max(CLAUDE_API_BACKOFF_SECONDS[kind], min(delay, CLAUDE_API_MAX_BACKOFF_SECONDS))
    _CLAUDE_API_RETRY_AFTER = time.monotonic() + delay
    return _stale_claude_snapshot(message)


def _claude_api_snapshot(force_refresh=False):
    global _CLAUDE_API_CACHE, _CLAUDE_API_CACHE_AT, _CLAUDE_API_MESSAGE, _CLAUDE_API_RETRY_AFTER
    now = time.monotonic()
    if not force_refresh and _CLAUDE_API_CACHE is not None and now - _CLAUDE_API_CACHE_AT < CLAUDE_API_CACHE_SECONDS:
        # A window can reset inside the cache TTL; re-poll rather than report a
        # limit that has already refilled.
        fresh_windows = _unexpired_windows(_CLAUDE_API_CACHE.get("windows"))
        if fresh_windows:
            _CLAUDE_API_MESSAGE = None
            return dict(_CLAUDE_API_CACHE, windows=fresh_windows)
    if not force_refresh and now < _CLAUDE_API_RETRY_AFTER:
        return _stale_claude_snapshot(_CLAUDE_API_MESSAGE)

    token, expires_at = _claude_credentials()
    if _is_expired(expires_at) and _refresh_credentials_via_cli():
        token, expires_at = _claude_credentials()
    if not token:
        return _back_off("auth", NO_CREDENTIAL_MESSAGE)
    if _is_expired(expires_at):
        # See `_claude_credentials`: the usage endpoint would answer 429 here
        # and we'd report "throttled" for a user who is actually signed out.
        return _back_off("auth", AUTH_FAILED_MESSAGE)

    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code-usage-dashboard",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if error.code in (401, 403):
            return _back_off("auth", AUTH_FAILED_MESSAGE)
        if error.code == 429:
            return _back_off("rate_limited", RATE_LIMITED_MESSAGE, retry_after)
        return _back_off("network", NETWORK_MESSAGE, retry_after)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return _back_off("network", NETWORK_MESSAGE)
    if not isinstance(payload, dict):
        return _back_off("network", NETWORK_MESSAGE)

    windows = []
    for key, label in (("five_hour", "Current session"), ("seven_day", "Weekly")):
        info = payload.get(key)
        if not isinstance(info, dict):
            continue
        used = _as_number(info.get("utilization"))
        remaining = _clamp_percent(100 - (used * 100 if used is not None and 0 <= used <= 1 else used)) if used is not None else None
        if remaining is None:
            continue
        windows.append({
            "key": key,
            "label": label,
            "remaining_percent": round(remaining, 1),
            "reset_at": _iso_timestamp(info.get("resets_at", info.get("resetsAt"))),
        })
    if not windows:
        return _back_off("network", NETWORK_MESSAGE)

    snapshot = {
        "available": True,
        "windows": windows,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "live_api",
        "message": "Live usage from Claude Code",
    }
    _CLAUDE_API_CACHE = snapshot
    _CLAUDE_API_CACHE_AT = now
    _CLAUDE_API_RETRY_AFTER = 0.0
    _CLAUDE_API_MESSAGE = None
    _save_disk_snapshot(snapshot)
    return snapshot


def _claude_rate_limit_info(record):
    """Locate the rate-limit block in whichever shape Claude Code wrote it.

    Two shapes exist in the wild: a dedicated ``rate_limit_event`` record, and
    — far more commonly — a ``quotaLimits`` object hung off the assistant record
    for a request the API pushed back on.  Only reading the first shape is why
    the Claude panel stayed empty while Codex's worked.
    """
    if record.get("type") == "rate_limit_event":
        info = record.get("rate_limit_info") or record.get("rateLimitInfo")
        if not isinstance(info, dict):
            payload = record.get("payload")
            info = payload.get("rate_limit_info") if isinstance(payload, dict) else None
        return info if isinstance(info, dict) else None

    info = record.get("quotaLimits") or record.get("quota_limits")
    return info if isinstance(info, dict) else None


def _claude_events(record):
    info = _claude_rate_limit_info(record)
    if info is None:
        return []

    status = str(info.get("status", "")).lower()
    remaining = _remaining_percent(info)
    if remaining is None and status == "rejected":
        remaining = 0.0
    key = _window_key(info)
    if key is None or remaining is None:
        return []
    return [{
        "key": key,
        "remaining_percent": remaining,
        "reset_at": _iso_timestamp(
            info.get("resetsAt", info.get("resets_at", info.get("resetAt", info.get("reset_at"))))
        ),
        "observed_at": _event_timestamp(record, info),
    }]


def _codex_events(record):
    if record.get("type") != "event_msg":
        return []
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return []
    rate_limits = payload.get("rate_limits") or record.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return []

    events = []
    for source_key in ("primary", "secondary"):
        info = rate_limits.get(source_key)
        if not isinstance(info, dict):
            continue
        key = _window_key(info)
        if key is None:
            continue
        used = _as_number(info.get("used_percent", info.get("usedPercent")))
        remaining = _clamp_percent(100 - used) if used is not None else None
        if remaining is None:
            continue
        events.append({
            "key": key,
            "remaining_percent": remaining,
            "reset_at": _iso_timestamp(info.get("resets_at", info.get("resetsAt"))),
            "observed_at": _iso_timestamp(record.get("timestamp")),
        })
    return events


def _read_file(path, source):
    events = []
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.extend(_claude_events(record) if source == SOURCE_CLAUDE else _codex_events(record))
    except OSError:
        return []
    return events


def _source_files(source, claude_dirs=None, codex_dir=None):
    if source == SOURCE_CLAUDE:
        directories = claude_dirs if claude_dirs is not None else [
            Path.home() / ".claude" / "projects",
            Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects",
        ]
    elif source == SOURCE_CODEX:
        directories = [codex_dir or Path.home() / ".codex" / "sessions"]
    else:
        return []

    paths = []
    for directory in directories:
        root = Path(directory)
        if root.exists():
            paths.extend(glob.glob(str(root / "**" / "*.jsonl"), recursive=True))
    return sorted(set(paths))


def get_quota_snapshot(source, claude_dirs=None, codex_dir=None, force_refresh=False):
    """Return the latest locally observed quota windows for one provider.

    The file-level cache avoids rereading unchanged transcript history on every
    dashboard refresh while still noticing newly appended usage snapshots.
    """
    paths = _source_files(source, claude_dirs=claude_dirs, codex_dir=codex_dir)
    signature = []
    for path in paths:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        signature.append((path, stat.st_mtime_ns, stat.st_size))

    cache_key = (source, tuple(str(Path(d)) for d in (claude_dirs or [])), str(codex_dir or ""))
    cached = _CACHE.get(cache_key)
    old_files = cached.get("files", {}) if cached else {}
    files = {}
    for path, mtime_ns, size in signature:
        previous = old_files.get(path)
        if previous and previous[0] == mtime_ns and previous[1] == size:
            files[path] = previous
        else:
            files[path] = (mtime_ns, size, _read_file(path, source))

    if cached and cached.get("signature") == tuple(signature):
        latest = cached["latest"]
    else:
        latest = {}
        for value in files.values():
            for event in value[2]:
                current = latest.get(event["key"])
                if current is None or _timestamp_value(event.get("observed_at")) >= _timestamp_value(current.get("observed_at")):
                    latest[event["key"]] = event
        _CACHE[cache_key] = {"signature": tuple(signature), "files": files, "latest": latest}

    # Windows are rebuilt (not cached) on every call: a window whose reset has
    # already passed has refilled, so reporting its last reading would show a
    # stale "3% left" long after the limit lifted.
    now = time.time()
    windows = []
    five_hour_label = "Current session" if source == SOURCE_CLAUDE else "5h"
    for key, label in (("five_hour", five_hour_label), ("weekly", "Weekly")):
        event = latest.get(key)
        if event is None:
            continue
        reset_at = event.get("reset_at")
        if reset_at and _timestamp_value(reset_at) <= now:
            continue
        windows.append({
            "key": key,
            "label": label,
            "remaining_percent": round(event["remaining_percent"], 1),
            "reset_at": reset_at,
        })

    observed = [event.get("observed_at") for event in latest.values() if event.get("observed_at")]
    snapshot = {
        "available": bool(windows),
        "windows": windows,
        "updated_at": max(observed, key=_timestamp_value) if observed and windows else None,
        "source": "local_event" if windows else "unavailable",
        "message": None if windows else "No recent quota data in local logs",
    }
    if source == SOURCE_CLAUDE:
        live_snapshot = _claude_api_snapshot(force_refresh=force_refresh)
        if live_snapshot is not None:
            return live_snapshot
        if not windows:
            # `_claude_api_snapshot` recorded *why* it couldn't answer; that is
            # far more actionable than a generic "unavailable".  Only a missing
            # or rejected credential is something the user can fix by signing
            # in, so only that state offers the button.
            snapshot["message"] = _CLAUDE_API_MESSAGE or NO_READING_YET_MESSAGE
            snapshot["needs_sign_in"] = _CLAUDE_API_MESSAGE in (NO_CREDENTIAL_MESSAGE, AUTH_FAILED_MESSAGE)
    return snapshot
