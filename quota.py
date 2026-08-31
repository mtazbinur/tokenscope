"""Read provider quota snapshots from Claude Code and Codex sources."""

import glob
import json
import os
import shutil
import re
import subprocess
import threading
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

# State only — no remedy. The dashboard can start a sign-in itself where the
# Claude Code CLI is installed, and printing "run `claude auth login`" next to a
# button that does exactly that reads as a contradiction. The surface that knows
# which remedy applies is the one that offers it.
NO_CREDENTIAL_MESSAGE = "No Claude Code sign-in found"
AUTH_FAILED_MESSAGE = "Claude Code sign-in expired"
RATE_LIMITED_MESSAGE = "Claude's usage endpoint is busy — showing the last reading"
NETWORK_MESSAGE = "Could not reach Claude's usage endpoint"
CREDENTIAL_STALE_MESSAGE = "Could not confirm the Claude Code sign-in"
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

    # Both stores are read and the *freshest* credential wins.  Installs exist
    # where one of them lags the other (the Keychain item is not always rewritten
    # when the CLI renews in-process), and preferring whichever store has the
    # later expiry costs one file read and avoids reporting a signed-out user
    # who is signed in perfectly well somewhere else.
    candidates = []
    for reader in (_keychain_blob, _credentials_file_blob):
        try:
            raw = reader()
        except Exception:  # a broken store must not take the panel down
            continue
        if not raw:
            continue
        found, expires_at = _oauth_blob(raw)
        if found:
            candidates.append((found, expires_at))

    if not candidates:
        return "", None
    # `None` expiry means "no expiry recorded" — treat it as the best case, the
    # same assumption the env-var branch above makes.
    return max(candidates, key=lambda pair: float("inf") if pair[1] is None else pair[1])


def _keychain_blob():
    """Raw credential blob from the macOS Keychain, or ``""``.

    Keep the subprocess invocation argument-based; never invoke a shell and
    never include the secret in an exception or log message.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def _credentials_file_blob():
    """Raw credential blob from ``~/.claude/.credentials.json``, or ``""``."""
    try:
        with (Path.home() / ".claude" / ".credentials.json").open(encoding="utf-8") as stream:
            return stream.read()
    except (OSError, UnicodeDecodeError):
        return ""


_LAST_CLI_REFRESH_AT = 0.0
CLI_REFRESH_MIN_INTERVAL = 60

# A locally-expired token is not proof of a signed-out user (a stale `expiresAt`
# and a skewed clock both look identical from here), so the verdict comes from
# `/api/oauth/profile` instead.  The answer is cached per token so a dashboard
# polling every few seconds does not re-probe on every request.
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
PROBE_CACHE_SECONDS = 60
_PROBE_CACHE = {}


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
    """Nudge Claude Code to renew its own OAuth credential, then re-read it.

    `claude auth status` is read-only from the user's point of view and can make
    the CLI notice an expired access token and exchange its refresh token —
    writing the renewed credential back itself.  Letting the first-party tool
    own that exchange is why this file never touches the refresh token: those
    rotate single-use, and spending one here would log the user out of their
    editor.

    Success is judged by **the stored credential actually moving forward**, not
    by what the CLI printed.  `auth status` answers ``{"loggedIn": true}`` from
    cached state on installs where it performs no exchange at all, so trusting
    that field reported a successful refresh that never happened — and the
    caller then declared a signed-in user signed out.
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
        subprocess.run(
            [cli, "auth", "status"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False  # a CLI that will not run is not a signed-out user

    # The exit code and stdout are deliberately ignored: only the credential
    # store is authoritative about whether a renewal happened.
    try:
        _, expires_at = _claude_credentials()
    except Exception:
        return False
    return not _is_expired(expires_at)


def _token_is_live(token):
    """Ask the API whether ``token`` is still accepted.

    Returns ``True`` (accepted), ``False`` (rejected — genuinely signed out) or
    ``None`` (**unknown** — the probe itself failed).  The three-way answer is
    the point: a timeout or a 500 must not be reported to the user as an expired
    sign-in, and the usage endpoint cannot be used for this because it answers
    429 rather than 401 for a bad token.  `/api/oauth/profile` returns the
    honest 401.
    """
    cached = _PROBE_CACHE.get(token)
    if cached and time.monotonic() - cached[0] < PROBE_CACHE_SECONDS:
        return cached[1]

    request = urllib.request.Request(
        CLAUDE_PROFILE_URL,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code-usage-dashboard",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5):
            verdict = True
    except urllib.error.HTTPError as error:
        # Only an explicit rejection counts as "signed out".  A 429/5xx says
        # nothing about the credential.
        verdict = False if error.code in (401, 403) else None
    except Exception:
        verdict = None

    # Never cache "unknown" — the next poll should be free to try again.
    if verdict is not None:
        _PROBE_CACHE.clear()  # one credential at a time; keeps this bounded
        _PROBE_CACHE[token] = (time.monotonic(), verdict)
    return verdict


# ── Interactive sign-in ────────────────────────────────────────────────────
# `claude auth login` drives the whole OAuth flow itself: it opens the browser,
# holds the PKCE verifier, and writes the renewed credential to its own store.
# Running it as a subprocess is what lets the dashboard offer a sign-in button
# without this file ever handling an authorization code or a refresh token.
SIGN_IN_TIMEOUT_SECONDS = 300
_SIGN_IN_LOCK = threading.Lock()
_SIGN_IN_STATE = {"status": "idle", "message": "", "url": "", "started_at": 0.0}
_AUTHORIZE_URL = re.compile(r"https://\S*/oauth/authorize\S*")

SIGN_IN_UNAVAILABLE_MESSAGE = "Claude Code CLI not found on this machine"
SIGN_IN_RUNNING_MESSAGE = "Approve the sign-in in the browser tab that just opened."
SIGN_IN_OK_MESSAGE = "Signed in"
SIGN_IN_FAILED_MESSAGE = "The browser flow was closed before it finished."
SIGN_IN_TIMEOUT_MESSAGE = "The sign-in took too long and was cancelled."


def sign_in_available():
    """True when a sign-in can actually be started on this machine."""
    return bool(_claude_cli())


def sign_in_state():
    """Snapshot of the current/last sign-in attempt, safe to serialize."""
    with _SIGN_IN_LOCK:
        state = dict(_SIGN_IN_STATE)
    state["available"] = sign_in_available()
    return state


def _set_sign_in(status, message, url=None):
    with _SIGN_IN_LOCK:
        _SIGN_IN_STATE["status"] = status
        _SIGN_IN_STATE["message"] = message
        if url is not None:
            _SIGN_IN_STATE["url"] = url


def start_sign_in():
    """Begin a sign-in in the background; returns the state to report back.

    Returns immediately — the browser half of the flow can take minutes, and a
    request thread parked on it would look like a hung dashboard.  The caller
    polls `sign_in_state()`.
    """
    if not sign_in_available():
        _set_sign_in("unavailable", SIGN_IN_UNAVAILABLE_MESSAGE, "")
        return sign_in_state()

    with _SIGN_IN_LOCK:
        if _SIGN_IN_STATE["status"] == "running":
            return dict(_SIGN_IN_STATE, available=True)  # single-flight
        _SIGN_IN_STATE.update({
            "status": "running",
            "message": SIGN_IN_RUNNING_MESSAGE,
            "url": "",
            "started_at": time.time(),
        })

    threading.Thread(target=_run_sign_in, daemon=True).start()
    return sign_in_state()


def _run_sign_in():
    """Drive `claude auth login` to completion, then judge it by the store.

    The exit code is corroborating evidence only.  As with
    `_refresh_credentials_via_cli`, the credential itself is the authority on
    whether a sign-in happened — a CLI can exit 0 having changed nothing.
    """
    cli = _claude_cli()
    try:
        process = subprocess.Popen(
            [cli, "auth", "login"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _set_sign_in("failed", f"Could not start Claude Code: {exc}", "")
        return

    # A watchdog rather than `communicate(timeout=...)`: stdout is read live so
    # the authorize URL reaches the page while the flow is still open.
    timer = threading.Timer(SIGN_IN_TIMEOUT_SECONDS, process.kill)
    timer.daemon = True
    timer.start()
    try:
        for line in process.stdout or ():
            match = _AUTHORIZE_URL.search(line)
            if match:
                _set_sign_in("running", SIGN_IN_RUNNING_MESSAGE, match.group(0))
        process.wait()
    except Exception:
        process.kill()
    finally:
        timer.cancel()
        try:
            if process.stdout:
                process.stdout.close()
        except OSError:
            pass

    _finish_sign_in(process.returncode)


def _finish_sign_in(returncode):
    """Report the outcome, and clear the caches that would hide a success."""
    try:
        token, expires_at = _claude_credentials()
    except Exception:
        token, expires_at = "", None

    if token and not _is_expired(expires_at):
        # Without this the panel would keep serving the auth back-off for
        # another five minutes after a sign-in that plainly worked.
        reset_auth_backoff()
        _set_sign_in("ok", SIGN_IN_OK_MESSAGE, "")
        return

    if returncode is not None and returncode < 0:
        _set_sign_in("failed", SIGN_IN_TIMEOUT_MESSAGE, "")
    else:
        _set_sign_in("failed", SIGN_IN_FAILED_MESSAGE, "")


def reset_auth_backoff():
    """Forget cached auth failures so the next poll re-checks for real."""
    global _CLAUDE_API_RETRY_AFTER, _CLAUDE_API_MESSAGE, _LAST_CLI_REFRESH_AT
    _PROBE_CACHE.clear()
    _CLAUDE_API_RETRY_AFTER = 0.0
    _CLAUDE_API_MESSAGE = None
    _LAST_CLI_REFRESH_AT = 0.0


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


def _is_sign_in_message(message):
    """True only for messages that a sign-in would actually fix.

    `CREDENTIAL_STALE_MESSAGE` is deliberately excluded: it means the check was
    inconclusive, and offering a sign-in button there trains users to re-auth
    over what is usually a network blip.
    """
    return message in (NO_CREDENTIAL_MESSAGE, AUTH_FAILED_MESSAGE)


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
    stale["needs_sign_in"] = _is_sign_in_message(message)
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

    try:
        token, expires_at = _claude_credentials()
    except Exception:
        # An unreadable credential store is a local fault, not a sign-out.
        return _back_off("network", CREDENTIAL_STALE_MESSAGE)

    if _is_expired(expires_at) and _refresh_credentials_via_cli():
        token, expires_at = _claude_credentials()
    if not token:
        return _back_off("auth", NO_CREDENTIAL_MESSAGE)

    if _is_expired(expires_at):
        # A stored expiry in the past is a suspicion, not a verdict.  The CLI
        # and the desktop app renew in-process and do not always write the new
        # access token back to the store we just read, so this branch fires
        # routinely for users who are signed in — hence the probe.  See
        # `_token_is_live` for why the usage endpoint cannot answer this.
        verdict = _token_is_live(token)
        if verdict is False:
            return _back_off("auth", AUTH_FAILED_MESSAGE)
        if verdict is None:
            # Could not tell.  Say so and keep the last reading rather than
            # accusing a working sign-in of having expired.
            return _back_off("network", CREDENTIAL_STALE_MESSAGE)
        # Accepted: the stored expiry was stale.  Fall through and use it.

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
            snapshot["needs_sign_in"] = _is_sign_in_message(_CLAUDE_API_MESSAGE)
    return snapshot
