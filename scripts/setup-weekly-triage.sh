#!/usr/bin/env bash
#
# scripts/setup-weekly-triage.sh
#
# Register a weekly job that runs `/triage` via scripts/run_triage.py.
# macOS uses a launchd LaunchAgent; Linux uses the user crontab. Windows has
# its own installer: scripts/setup-weekly-triage.ps1.
#
# Usage (from anywhere; the script locates its own repo):
#   bash scripts/setup-weekly-triage.sh
#   bash scripts/setup-weekly-triage.sh --day Monday --time 09:00
#   bash scripts/setup-weekly-triage.sh --status
#   bash scripts/setup-weekly-triage.sh --remove
#
# Idempotent: re-running replaces the existing entry rather than stacking one.
#
# What this does NOT do:
#   - No new permissions. Claude Code's own settings govern what /triage may do.
#   - Never pushes to main. Per the /triage workflow, only DEV is pushed.
#   - No sudo, no system-wide daemon: the job runs as you, in your session.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$SCRIPT_DIR/run_triage.py"
LOG_DIR="$REPO_ROOT/logs"

LABEL="com.tokenscope.weekly-triage"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CRON_BEGIN="# >>> tokenscope weekly triage >>>"
CRON_END="# <<< tokenscope weekly triage <<<"

DAY="Monday"
TIME="09:00"
ACTION="install"
PYTHON=""

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE_EOF'
Register a weekly job that runs /triage via scripts/run_triage.py.
macOS uses a launchd LaunchAgent; Linux uses the user crontab.
Windows has its own installer: scripts/setup-weekly-triage.ps1.

Usage:
  bash scripts/setup-weekly-triage.sh [options]

Options:
  --day NAME       weekday to run on (default: Monday)
  --time HH:MM     24-hour local time (default: 09:00)
  --python PATH    interpreter to use (default: python3, then python)
  --status         show the currently installed entry
  --remove         uninstall the entry
  -h, --help       this text

Re-running replaces the existing entry rather than stacking one.
USAGE_EOF
  exit "${1:-0}"
}

# --------------------------------------------------------------------------- #
# arguments
# --------------------------------------------------------------------------- #

while [[ $# -gt 0 ]]; do
  case "$1" in
    --day)    [[ $# -ge 2 ]] || die "--day needs a value";    DAY="$2";    shift 2 ;;
    --time)   [[ $# -ge 2 ]] || die "--time needs a value";   TIME="$2";   shift 2 ;;
    --python) [[ $# -ge 2 ]] || die "--python needs a value"; PYTHON="$2"; shift 2 ;;
    --remove) ACTION="remove"; shift ;;
    --status) ACTION="status"; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

day_number() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    sun|sunday)    echo 0 ;;
    mon|monday)    echo 1 ;;
    tue|tues|tuesday)   echo 2 ;;
    wed|weds|wednesday) echo 3 ;;
    thu|thur|thurs|thursday) echo 4 ;;
    fri|friday)    echo 5 ;;
    sat|saturday)  echo 6 ;;
    *) return 1 ;;
  esac
}

# --------------------------------------------------------------------------- #
# platform + interpreter
# --------------------------------------------------------------------------- #

case "$(uname -s)" in
  Darwin) PLATFORM="launchd" ;;
  Linux)  PLATFORM="cron" ;;
  *) die "unsupported platform '$(uname -s)'. On Windows run scripts/setup-weekly-triage.ps1 instead." ;;
esac

# Runs the candidate rather than trusting its name: `python` is 2.7 on some
# systems, and a venv shim can point at an interpreter that no longer exists.
# Checks the *output*, not just the exit status -- `--python /bin/echo` exits 0
# too, and would otherwise be happily scheduled.
PYTHON_PROBE='import sys; sys.stdout.write("tokenscope-ok" if sys.version_info[:2] >= (3, 8) else "tokenscope-old")'

python_is_supported() {
  [[ "$("$1" -c "$PYTHON_PROBE" 2>/dev/null || true)" == "tokenscope-ok" ]]
}

resolve_python() {
  local candidate resolved
  if [[ -n "$PYTHON" ]]; then
    resolved="$(command -v "$PYTHON" || true)"
    [[ -n "$resolved" ]] || die "--python '$PYTHON' is not executable"
    python_is_supported "$resolved" ||
      die "--python '$PYTHON' ($resolved) is not a working Python 3.8+ interpreter."
    printf '%s' "$resolved"
    return
  fi
  for candidate in python3 python; do
    resolved="$(command -v "$candidate" || true)"
    if [[ -n "$resolved" ]] && python_is_supported "$resolved"; then
      printf '%s' "$resolved"
      return
    fi
  done
  die "no Python 3.8+ found on PATH. Install one, or pass --python /path/to/python3."
}

# --------------------------------------------------------------------------- #
# launchd (macOS)
# --------------------------------------------------------------------------- #

launchd_domain() { printf 'gui/%s' "$(id -u)"; }

launchd_unload() {
  local domain
  domain="$(launchd_domain)"
  if launchctl bootout "$domain/$LABEL" 2>/dev/null; then return 0; fi
  # Older macOS, or an agent loaded the legacy way.
  launchctl unload -w "$PLIST" 2>/dev/null || true
}

launchd_load() {
  local domain
  domain="$(launchd_domain)"
  if launchctl bootstrap "$domain" "$PLIST" 2>/dev/null; then return 0; fi
  launchctl load -w "$PLIST" 2>/dev/null ||
    die "launchctl refused to load $PLIST. Load it manually with: launchctl bootstrap $domain '$PLIST'"
}

launchd_install() {
  local python weekday hour minute
  python="$(resolve_python)"
  weekday="$(day_number "$DAY")" || die "invalid --day '$DAY'"
  hour="${TIME%%:*}"
  minute="${TIME##*:}"

  mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"

  # Strip a leading zero so the plist carries a plain integer.
  hour=$((10#$hour))
  minute=$((10#$minute))

  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$python")</string>
    <string>-u</string>
    <string>$(xml_escape "$RUNNER")</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$REPO_ROOT")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(xml_escape "$PATH")</string>
    <key>HOME</key>
    <string>$(xml_escape "$HOME")</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>$weekday</integer>
    <key>Hour</key>
    <integer>$hour</integer>
    <key>Minute</key>
    <integer>$minute</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>/dev/null</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$LOG_DIR/launchd-error.log")</string>
</dict>
</plist>
PLIST_EOF

  launchd_unload
  launchd_load

  printf "Registered launchd agent '%s'.\n" "$LABEL"
  printf '  Repo:     %s\n' "$REPO_ROOT"
  printf '  Runner:   %s -u %s\n' "$python" "$RUNNER"
  printf '  Cadence:  every %s at %s (local time)\n' "$DAY" "$TIME"
  printf '  Plist:    %s\n' "$PLIST"
  printf '  Logs:     %s/triage-*.log (keeps the 12 newest)\n' "$LOG_DIR"
  printf '\nSmoke test (runs now):\n  launchctl kickstart %s/%s\n' "$(launchd_domain)" "$LABEL"
  printf '\nRemove:\n  bash scripts/setup-weekly-triage.sh --remove\n'
  printf '\nNote: launchd only fires while you are logged in. A missed run is\n'
  printf 'skipped, not queued.\n'
}

launchd_remove() {
  if [[ -f "$PLIST" ]]; then
    launchd_unload
    rm -f "$PLIST"
    printf "Removed launchd agent '%s' and %s.\n" "$LABEL" "$PLIST"
  else
    launchd_unload
    printf "No launchd agent '%s' found.\n" "$LABEL"
  fi
}

launchd_status() {
  if [[ -f "$PLIST" ]]; then
    printf 'Plist: %s\n' "$PLIST"
    launchctl print "$(launchd_domain)/$LABEL" 2>/dev/null |
      grep -E '^[[:space:]]+(state|program|last exit code) ' || printf '  (not currently loaded)\n'
  else
    printf "No launchd agent '%s' installed.\n" "$LABEL"
  fi
}

# --------------------------------------------------------------------------- #
# cron (Linux)
# --------------------------------------------------------------------------- #

cron_current() { crontab -l 2>/dev/null || true; }

# Everything except our managed block. Exits non-zero when the markers do not
# pair up (hand-edited crontab, half-deleted block): an unmatched opening marker
# would otherwise swallow every line after it, silently deleting entries this
# script never owned.
cron_without_block() {
  cron_current | awk -v begin="$CRON_BEGIN" -v end="$CRON_END" '
    $0 == begin { if (skip) bad = 1; skip = 1; next }
    $0 == end   { if (!skip) bad = 1; skip = 0; next }
    !skip       { print }
    END         { if (skip || bad) exit 3 }
  '
}

# Wrapper so every caller gets the same refusal message.
cron_kept_lines() {
  local kept
  if ! kept="$(cron_without_block)"; then
    die "your crontab has an unbalanced '$CRON_BEGIN' / '$CRON_END' block. Fix it with 'crontab -e' (delete the stray marker), then re-run."
  fi
  printf '%s' "$kept"
}

cron_write() {
  # `crontab -` replaces the whole crontab; an empty payload clears it.
  local payload="$1"
  if [[ -z "${payload//[[:space:]]/}" ]]; then
    crontab -r 2>/dev/null || true
  else
    printf '%s\n' "$payload" | crontab -
  fi
}

cron_install() {
  local python weekday hour minute block kept
  command -v crontab >/dev/null 2>&1 ||
    die "crontab not found. Install cron (e.g. 'cronie' or 'cron'), or schedule scripts/run_triage.py with your own timer."
  python="$(resolve_python)"
  weekday="$(day_number "$DAY")" || die "invalid --day '$DAY'"
  hour=$((10#${TIME%%:*}))
  minute=$((10#${TIME##*:}))

  mkdir -p "$LOG_DIR"

  # cron treats % as a newline marker in the command field.
  local cmd
  cmd="cd '$REPO_ROOT' && PATH='$PATH' '$python' -u '$RUNNER' >/dev/null 2>>'$LOG_DIR/cron-error.log'"
  cmd="${cmd//%/\\%}"

  block="$CRON_BEGIN
$minute $hour * * $weekday $cmd
$CRON_END"

  kept="$(cron_kept_lines)"
  if [[ -z "${kept//[[:space:]]/}" ]]; then
    cron_write "$block"
  else
    cron_write "$kept
$block"
  fi

  printf 'Registered cron entry for the weekly triage.\n'
  printf '  Repo:     %s\n' "$REPO_ROOT"
  printf '  Runner:   %s -u %s\n' "$python" "$RUNNER"
  printf '  Cadence:  every %s at %s (cron local time)\n' "$DAY" "$TIME"
  printf '  Logs:     %s/triage-*.log (keeps the 12 newest)\n' "$LOG_DIR"
  printf '\nSmoke test (runs now):\n  %s -u %s\n' "$python" "$RUNNER"
  printf '\nRemove:\n  bash scripts/setup-weekly-triage.sh --remove\n'
}

cron_remove() {
  command -v crontab >/dev/null 2>&1 || die "crontab not found; nothing to remove."
  if cron_current | grep -Fqx "$CRON_BEGIN"; then
    # Assign first: `cron_write "$(cron_kept_lines)"` would pass an empty string
    # when the refusal fires inside the substitution, and clear the crontab.
    local kept
    kept="$(cron_kept_lines)"
    cron_write "$kept"
    printf 'Removed the weekly triage cron entry.\n'
  else
    printf 'No weekly triage cron entry found.\n'
  fi
}

cron_status() {
  command -v crontab >/dev/null 2>&1 || die "crontab not found."
  if cron_current | grep -Fqx "$CRON_BEGIN"; then
    cron_current | awk -v begin="$CRON_BEGIN" -v end="$CRON_END" '
      $0 == begin { show = 1 }
      show        { print }
      $0 == end   { show = 0 }
    '
  else
    printf 'No weekly triage cron entry installed.\n'
  fi
}

# --------------------------------------------------------------------------- #
# helpers + dispatch
# --------------------------------------------------------------------------- #

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

validate_common() {
  [[ -f "$RUNNER" ]] || die "runner not found: $RUNNER (is scripts/run_triage.py committed?)"
  [[ "$TIME" =~ ^([01]?[0-9]|2[0-3]):[0-5][0-9]$ ]] ||
    die "invalid --time '$TIME'; expected HH:MM in 24-hour form (e.g. 09:00 or 21:30)"
  day_number "$DAY" >/dev/null || die "invalid --day '$DAY'; use a weekday name such as Monday"
  case "$REPO_ROOT$RUNNER$PATH$HOME" in
    *\'*) die "a path or PATH entry contains a single quote, which cannot be quoted safely here." ;;
  esac
}

case "$ACTION" in
  install)
    validate_common
    if [[ "$PLATFORM" == launchd ]]; then launchd_install; else cron_install; fi
    ;;
  remove)
    if [[ "$PLATFORM" == launchd ]]; then launchd_remove; else cron_remove; fi
    ;;
  status)
    if [[ "$PLATFORM" == launchd ]]; then launchd_status; else cron_status; fi
    ;;
esac
