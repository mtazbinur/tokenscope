"""
cli.py - Command-line interface for the TokenScope dashboard.

Commands:
  scan      - Scan JSONL files and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

import settings
from scanner import VERSION, SOURCE_CLAUDE
from pricing import PRICING, get_pricing, calc_cost  # noqa: F401  (PRICING re-exported for callers/tests)

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def hr(char="-", width=60):
    print(char * width)

def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Ensure the schema is current before querying. The read commands query the
    # `agents` table and the `is_subagent`/`agent_id` columns, so a pre-existing
    # DB from before those were added would raise "no such column" when a read
    # command runs before the next scan migrates it. init_db is idempotent
    # (CREATE ... IF NOT EXISTS + additive column checks), so this is a cheap
    # no-op once migrated. Mirrors get_dashboard_data in dashboard.py.
    from scanner import init_db
    init_db(conn)
    return conn


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(projects_dir=None, source=None, codex_dir=None):
    from scanner import scan
    # A custom projects directory has historically meant Claude transcripts;
    # the normal command scans both providers.
    # With no explicit --source, honor the Settings page: a provider the user
    # switched off is never walked, so its directory isn't read at all.
    resolved_source = source or (SOURCE_CLAUDE if projects_dir else settings.scan_source())
    scan(projects_dir=Path(projects_dir) if projects_dir else None,
         source=resolved_source, codex_dir=Path(codex_dir) if codex_dir else None)


def cmd_today():
    conn = require_db()
    today = date.today().isoformat()

    rows = conn.execute("""
        SELECT
            source,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            is_long_context,
            COUNT(*)                   as turns
        FROM turns
        WHERE date(timestamp, 'localtime') = ?
        GROUP BY source, model, is_long_context
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT source || ':' || session_id) as cnt
        FROM turns
        WHERE date(timestamp, 'localtime') = ?
    """, (today,)).fetchone()

    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE date(timestamp, 'localtime') = ?
          AND COALESCE(is_subagent, 0) = 1
          AND source = ?
    """, (today, SOURCE_CLAUDE)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        conn.close()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0,
                         source=r["source"], long_context=bool(r["is_long_context"]))
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  source={r['source']:<12}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']}")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0)}  ({fmt(subagent['turns'] or 0)} turns)")
    print(f"  Cached input:     {fmt(total_cr)}  (included in Codex prompt input)")
    print(f"  Cache writes:     {fmt(total_cc)}  (Codex logs may not report these)")
    hr()
    print()
    conn.close()


def cmd_week():
    conn = require_db()

    today_d = date.today()
    start_d = today_d - timedelta(days=6)
    start = start_d.isoformat()
    end = today_d.isoformat()

    by_day_model = conn.execute("""
        SELECT
            date(timestamp, 'localtime') as day,
            source,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            is_long_context,
            COUNT(*)                   as turns
        FROM turns
        WHERE date(timestamp, 'localtime') BETWEEN ? AND ?
        GROUP BY day, source, model, is_long_context
    """, (start, end)).fetchall()

    by_model = conn.execute("""
        SELECT
            source,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            is_long_context,
            COUNT(*)                   as turns
        FROM turns
        WHERE date(timestamp, 'localtime') BETWEEN ? AND ?
        GROUP BY source, model, is_long_context
        ORDER BY inp + out DESC
    """, (start, end)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT source || ':' || session_id) as cnt
        FROM turns
        WHERE date(timestamp, 'localtime') BETWEEN ? AND ?
    """, (start, end)).fetchone()

    print()
    hr()
    print(f"  Weekly Usage  ({start} to {end})")
    hr()

    if not by_model:
        print("  No usage recorded in the last 7 days.")
        print()
        conn.close()
        return

    # Aggregate per-day across models (with per-turn cost attribution)
    per_day = {}
    for r in by_day_model:
        d = r["day"]
        bucket = per_day.setdefault(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        bucket["turns"] += r["turns"]
        bucket["inp"]   += r["inp"] or 0
        bucket["out"]   += r["out"] or 0
        bucket["cost"]  += calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0,
                                      source=r["source"], long_context=bool(r["is_long_context"]))

    print("  By Day:")
    for i in range(7):
        d = (start_d + timedelta(days=i)).isoformat()
        b = per_day.get(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"    {d}  turns={b['turns']:<4}  in={fmt(b['inp']):<8}  out={fmt(b['out']):<8}  cost={fmt_cost(b['cost'])}")

    hr()
    print("  By Model:")

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0,
                         source=r["source"], long_context=bool(r["is_long_context"]))
        total_cost  += cost
        total_inp   += r["inp"] or 0
        total_out   += r["out"] or 0
        total_cr    += r["cr"]  or 0
        total_cc    += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"    {r['model']:<30}  source={r['source']:<12}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"    {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions this week:  {sessions['cnt']}")
    print(f"  Cached input:        {fmt(total_cr)}  (included in Codex prompt input)")
    print(f"  Cache writes:        {fmt(total_cc)}  (Codex logs may not report these)")
    hr()
    print()
    conn.close()


def cmd_stats():
    conn = require_db()

    # Session-level info (count, date range)
    session_info = conn.execute("""
        SELECT
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # All-time totals from turns (more accurate — per-turn model attribution)
    totals = conn.execute("""
        SELECT
            SUM(input_tokens)             as inp,
            SUM(output_tokens)            as out,
            SUM(cache_read_tokens)        as cr,
            SUM(cache_creation_tokens)    as cc,
            COUNT(*)                      as turns
        FROM turns
    """).fetchone()

    # By model from turns (each turn has the actual model used)
    by_model = conn.execute("""
        SELECT
            source,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            is_long_context,
            COUNT(*)                   as turns,
            COUNT(DISTINCT source || ':' || session_id) as sessions
        FROM turns
        GROUP BY source, model, is_long_context
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects from turns (join with sessions for project name)
    top_projects = conn.execute("""
        SELECT
            COALESCE(s.project_name, 'unknown') as project_name,
            SUM(t.input_tokens)  as inp,
            SUM(t.output_tokens) as out,
            COUNT(*)             as turns,
            COUNT(DISTINCT t.source || ':' || t.session_id) as sessions
        FROM turns t
        LEFT JOIN sessions s ON t.source = s.source AND t.session_id = s.session_id
        GROUP BY s.project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Subagent totals (subagent tokens are included in the all-time totals above)
    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE COALESCE(is_subagent, 0) = 1
          AND source = ?
    """, (SOURCE_CLAUDE,)).fetchone()

    # Daily average (last 30 days)
    # Local calendar days, matching `today` / `week` / the dashboard. The old
    # form bucketed by the UTC date and compared an ISO 'T'-separated timestamp
    # against datetime('now')'s space-separated format, so both the buckets and
    # the cutoff drifted for anyone not on UTC.
    daily_avg = conn.execute("""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out
        FROM (
            SELECT
                date(timestamp, 'localtime') as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out
            FROM turns
            WHERE date(timestamp, 'localtime')
                  BETWEEN date('now', 'localtime', '-29 days')
                      AND date('now', 'localtime')
            GROUP BY day
        )
    """).fetchone()

    # Build total cost across all models
    total_cost = sum(
        calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0,
                  source=r["source"], long_context=bool(r["is_long_context"]))
        for r in by_model
    )

    print()
    hr("=")
    print("  Coding Usage - All-Time Statistics")
    hr("=")

    first_date = (session_info["first"] or "")[:10]
    last_date = (session_info["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {session_info['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print(f"  Subagent turns:   {fmt(subagent['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cached input:     {fmt(totals['cr'] or 0):<12}  (included in Codex prompt input)")
    print(f"  Cache writes:     {fmt(totals['cc'] or 0):<12}  (Codex logs may not report these)")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0):<12}  (included in totals)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0,
                         source=r["source"], long_context=bool(r["is_long_context"]))
        print(f"    {r['model']:<30}  source={r['source']:<12}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def cmd_dashboard(projects_dir=None, host=None, port=None, no_browser=False,
                  source=None, codex_dir=None):
    import threading
    import time

    from dashboard import serve

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", "8080"))

    # Bind and serve the port *first*, then scan in the background. A cold scan
    # over a large ~/.claude/projects backlog can take well over a minute, so
    # serving up front makes the dashboard available immediately while the scan
    # fills in new data.
    #
    # Capture cmd_scan into a local so the background thread closes over the
    # current binding — keeps the test suite's mock.patch(cli.cmd_scan) effective
    # and prevents the thread from ever touching the real DB after a patch lifts.
    scan = cmd_scan

    def background_scan():
        print("Scanning in the background...")
        scan(projects_dir=projects_dir, source=source, codex_dir=codex_dir)
        print("Background scan complete.")

    threading.Thread(target=background_scan, daemon=True).start()

    # Open a browser for users running this as a script (see README).
    if not no_browser:
        import webbrowser

        def open_browser():
            time.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=open_browser, daemon=True).start()

    serve(host=host, port=port)


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
TokenScope

Usage:
  python cli.py scan [--projects-dir PATH] [--source SOURCE] [--codex-dir PATH]
                                                 Scan JSONL files and update database
  python cli.py today                        Show today's usage summary
  python cli.py week                         Show last 7 days (per-day + by-model)
  python cli.py stats                        Show all-time statistics
  python cli.py dashboard [--projects-dir PATH] [--host HOST] [--port PORT] [--no-browser]
                                                 Scan + start dashboard (opens a browser unless --no-browser)
  python cli.py --version                    Print the version and exit
"""

COMMANDS = {
    "scan": cmd_scan,
    "today": cmd_today,
    "week": cmd_week,
    "stats": cmd_stats,
    "dashboard": cmd_dashboard,
}

def parse_named_arg(args, flag):
    """Extract a --flag VALUE pair from an argument list."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None

def main():
    """Console entry point (``tokenscope``) and ``python cli.py`` dispatch."""
    # Load user settings once up front so every command prices turns with the
    # same overrides the dashboard uses.
    settings.apply()
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        print(VERSION)
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]
    projects_dir = parse_named_arg(rest, "--projects-dir")
    source = parse_named_arg(rest, "--source")
    codex_dir = parse_named_arg(rest, "--codex-dir")

    if command == "dashboard":
        cmd_dashboard(
            projects_dir=projects_dir,
            source=source,
            codex_dir=codex_dir,
            host=parse_named_arg(rest, "--host"),
            port=parse_named_arg(rest, "--port"),
            no_browser="--no-browser" in rest,
        )
    elif command == "scan":
        cmd_scan(projects_dir=projects_dir, source=source, codex_dir=codex_dir)
    else:
        COMMANDS[command]()


if __name__ == "__main__":
    main()
