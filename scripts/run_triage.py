#!/usr/bin/env python3
"""Cross-platform runner for the weekly ``/triage`` routine.

Invoked by the scheduler entry created by ``setup-weekly-triage.ps1`` (Windows
Task Scheduler), ``setup-weekly-triage.sh`` (launchd on macOS, cron on Linux),
or by hand.  Stdlib only, Python 3.8+, same as the rest of the repo.

What it guarantees, on every platform:

* runs ``claude -p /triage`` with the repo root as the working directory;
* tees combined stdout+stderr to ``logs/triage-<timestamp>.log`` and to this
  process's stdout, so a scheduler that captures stdout sees it too;
* keeps only the N most recent logs (``--keep``, default 12) -- rotation runs
  even when the routine fails or is never started;
* refuses to start a second concurrent run -- a heartbeat lock, so a live run
  is never evicted however long it takes, and a crashed one frees up;
* kills a run that overshoots ``--timeout`` -- the whole process tree, so the
  node process behind the claude CLI goes with it -- instead of letting a
  scheduler pull the rug mid-merge;
* exits with claude's own exit code, so "Last Run Result" is meaningful.

Exit codes of its own: ``127`` claude not on PATH, ``124`` timed out,
``75`` another run holds the lock.
"""

import argparse
import errno
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"
LOG_PREFIX = "triage-"
LOG_SUFFIX = ".log"
LOCK_PATH = LOG_DIR / "triage.lock"

# triage-2026-08-31-091500.log, plus the -2 suffix used on a same-second collision.
LOG_RE = re.compile(r"^triage-\d{4}-\d{2}-\d{2}-\d{6}(-\d+)?\.log$")
# Written by the retired PowerShell runner; deleted on sight, never recreated.
LEGACY_LOG_GLOB = "triage-error-*.log"

KEEP_LOGS = 12
DEFAULT_TIMEOUT = 3 * 60 * 60  # /triage merges PRs and runs the full suite.
GRACE_SECONDS = 15  # terminate -> kill window.

# The lock is kept alive by a heartbeat rather than by a predicted expiry: a run
# started with `--timeout 0` may legitimately outlast any fixed ceiling, and a
# lock that expires under a live owner is worse than no lock at all -- the
# evicted owner would later delete its successor's lock on the way out.
LOCK_HEARTBEAT_INTERVAL = 60
LOCK_STALE_AFTER = 600  # 10 min without a heartbeat == the owner is gone.

EXIT_NO_CLAUDE = 127
EXIT_TIMEOUT = 124
EXIT_LOCKED = 75


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #

def _timestamp():
    return datetime.now().strftime("%Y-%m-%d-%H%M%S")


def _new_log_path():
    """A log path that does not collide even on two runs in the same second."""
    base = LOG_DIR / (LOG_PREFIX + _timestamp() + LOG_SUFFIX)
    if not base.exists():
        return base
    stem = base.name[: -len(LOG_SUFFIX)]
    for n in range(2, 100):
        candidate = LOG_DIR / ("%s-%d%s" % (stem, n, LOG_SUFFIX))
        if not candidate.exists():
            return candidate
    return base


def rotate_logs(keep):
    """Keep the `keep` newest ``triage-*.log`` files, delete the rest.

    Sorted by filename, not mtime: the timestamp is in the name and sorts
    lexicographically, which survives a copied/restored tree with rewritten
    mtimes.  Only files this script produces are considered -- a stray
    ``triage-notes.log`` must not eat a retention slot.  Never raises:
    rotation failing must not fail the run.
    """
    def drop(path):
        try:
            path.unlink()
        except OSError:
            pass

    try:
        for legacy in LOG_DIR.glob(LEGACY_LOG_GLOB):
            drop(legacy)
        logs = sorted(
            (p for p in LOG_DIR.glob(LOG_PREFIX + "*" + LOG_SUFFIX)
             if LOG_RE.match(p.name) and p.is_file()),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return
    if keep < 0:
        return
    for stale in logs[keep:]:
        drop(stale)


# --------------------------------------------------------------------------- #
# single-instance lock
# --------------------------------------------------------------------------- #

def _lock_field(name):
    """Read one ``name=value`` field out of the lock file, or None."""
    try:
        text = LOCK_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    prefix = name + "="
    for token in text.split():
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def _lock_is_abandoned():
    """True if nothing has touched the lock for LOCK_STALE_AFTER seconds.

    Liveness is judged by the heartbeat, not by any timeout: a contender knows
    nothing about how long the running job is entitled to take, and the owner
    refreshes the file for exactly as long as it is alive. A crashed or killed
    run stops refreshing, so the next scheduled run reclaims it.
    """
    try:
        return time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE_AFTER
    except OSError:
        return True  # Vanished between checks -- reclaimable.


def acquire_lock(token):
    """Create the lock file atomically, stamped with `token`. True on success.

    Cross-platform by design: no PID liveness check (which needs different
    syscalls per OS and can hit PID reuse).

    Fails *closed*: if the lock cannot be created for any reason other than
    "already exists", the run is refused. The log directory was created
    successfully moments earlier, so a failure here means something is wrong
    with the tree, and two concurrent /triage runs merging PRs is worse than a
    skipped week.
    """
    for attempt in (1, 2):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                sys.stderr.write("cannot create %s: %s\n" % (LOCK_PATH, exc))
                return False
            if attempt == 1 and _lock_is_abandoned():
                try:
                    LOCK_PATH.unlink()
                except OSError:
                    pass
                continue
            return False
        with os.fdopen(fd, "w") as handle:
            handle.write(
                "token=%s pid=%d start=%s\n"
                % (token, os.getpid(), datetime.now().isoformat())
            )
        return True
    return False


class LockHeartbeat(object):
    """Touches the lock file while the run is in progress.

    Stops early -- and says so -- if the lock stops being ours, which is the
    only way this process can learn it was evicted.
    """

    def __init__(self, token):
        self.token = token
        self._stop = threading.Event()
        self._thread = None
        self.lost = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(LOCK_HEARTBEAT_INTERVAL):
            if _lock_field("token") != self.token:
                self.lost = True
                sys.stderr.write(
                    "warning: %s is no longer ours; another run may have "
                    "started.\n" % LOCK_PATH
                )
                return
            try:
                os.utime(str(LOCK_PATH), None)
            except OSError:
                return

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def release_lock(token):
    """Remove the lock, but only while it is still ours.

    Without the token check, a run that was wrongly declared abandoned would
    delete its successor's lock when it finally finished.
    """
    if _lock_field("token") != token:
        return
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# claude discovery + execution
# --------------------------------------------------------------------------- #

def find_claude(explicit=None):
    """Absolute path to the claude CLI, or None.

    ``shutil.which`` honours PATHEXT on Windows, so an ``npm``-installed
    ``claude.cmd`` shim resolves the same way it does in a shell.
    """
    if explicit:
        found = shutil.which(explicit)
        return found or (explicit if os.path.isfile(explicit) else None)
    return shutil.which("claude")


def _pump(stream, handles):
    """Copy `stream` line by line into every handle in `handles`."""
    try:
        for line in stream:
            for handle in handles:
                try:
                    handle.write(line)
                    handle.flush()
                except (OSError, ValueError):
                    pass
    except (OSError, ValueError):
        pass


def _taskkill_tree(pid):
    """Windows: kill a whole process tree.

    `Popen.terminate` maps to TerminateProcess, which kills only the process it
    is given. The claude CLI on Windows is a `.cmd` shim around node, so
    terminating the shim would orphan the node process -- mid-merge, with the
    runner already gone. `taskkill /T` walks the tree.
    """
    try:
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _stop(proc):
    """Stop the run and everything it spawned.

    POSIX: signal the process group (the child is its own group leader).
    Windows: `taskkill /T` on the tree, since there are no process groups to
    signal and TerminateProcess does not recurse.
    """
    pgid = getattr(proc, "_tokenscope_pgid", None)

    if os.name == "nt":
        _taskkill_tree(proc.pid)
        try:
            proc.wait(timeout=GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        return

    try:
        if pgid:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if pgid:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def run_claude(claude, prompt, timeout, log_handle):
    """Run claude, tee its output, return its exit code."""
    cmd = [claude, "-p", prompt]

    popen_kwargs = dict(
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,  # never block waiting for a human.
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # line buffered, so the log stays live.
    )
    if hasattr(os, "killpg"):
        # Own process group: a timeout takes claude's children with it.
        # start_new_session is the supported API (no fork-time callback).
        popen_kwargs["start_new_session"] = True

    header = "$ %s\n" % " ".join(cmd)
    for handle in (sys.stdout, log_handle):
        handle.write(header)
        handle.flush()

    proc = subprocess.Popen(cmd, **popen_kwargs)
    if popen_kwargs.get("start_new_session"):
        # setsid() makes the child a group leader, so pgid == pid.
        proc._tokenscope_pgid = proc.pid

    pump = threading.Thread(
        target=_pump, args=(proc.stdout, (sys.stdout, log_handle)), daemon=True
    )
    pump.start()

    timed_out = False
    try:
        code = proc.wait(timeout=timeout if timeout > 0 else None)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop(proc)
        code = EXIT_TIMEOUT
    except KeyboardInterrupt:
        _stop(proc)
        raise

    pump.join(timeout=GRACE_SECONDS)
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except OSError:
        pass

    if timed_out:
        note = "\n[run_triage] timed out after %ds -- claude was terminated.\n" % timeout
        for handle in (sys.stdout, log_handle):
            handle.write(note)
            handle.flush()

    return code


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run the weekly /triage routine with logging and rotation."
    )
    parser.add_argument(
        "--prompt", default="/triage", help="prompt passed to claude -p (default: /triage)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="seconds before the run is killed; 0 disables (default: %d)" % DEFAULT_TIMEOUT,
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=KEEP_LOGS,
        help="how many logs to retain (default: %d)" % KEEP_LOGS,
    )
    parser.add_argument(
        "--claude", default=None, help="path to (or name of) the claude CLI"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.stderr.write("cannot create %s: %s\n" % (LOG_DIR, exc))
        return 1

    token = uuid.uuid4().hex
    if not acquire_lock(token):
        sys.stderr.write(
            "could not take %s (another triage run holds it) -- skipping this one.\n"
            % LOCK_PATH
        )
        return EXIT_LOCKED
    heartbeat = LockHeartbeat(token).start()

    log_path = _new_log_path()
    try:
        with open(str(log_path), "w", encoding="utf-8", errors="replace") as log_handle:
            log_handle.write(
                "# triage run started %s in %s\n" % (datetime.now().isoformat(), REPO_ROOT)
            )
            claude = find_claude(args.claude)
            if not claude:
                message = (
                    "claude CLI not found on PATH. Install it, or pass --claude "
                    "<path>. PATH was:\n%s\n" % os.environ.get("PATH", "")
                )
                log_handle.write(message)
                sys.stderr.write(message)
                return EXIT_NO_CLAUDE
            try:
                return run_claude(claude, args.prompt, args.timeout, log_handle)
            except KeyboardInterrupt:
                log_handle.write("\n[run_triage] interrupted.\n")
                return 130
    finally:
        heartbeat.stop()
        release_lock(token)
        rotate_logs(args.keep)


if __name__ == "__main__":
    sys.exit(main())
