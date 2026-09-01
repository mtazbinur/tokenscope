"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import ipaddress
import json
import os
import secrets
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from datetime import datetime, timezone

import pricing
import scanner
import settings
from scanner import VERSION, SOURCE_CLAUDE, SOURCE_CODEX, init_db
from pricing import BUILTIN_PRICING_BY_SOURCE, calc_cost
import quota
from quota import get_quota_snapshot

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# The sign-in route spawns a process, so it is not something any page in the
# user's browser may trigger.  Two gates guard it: the server must be bound to
# loopback, and the caller must present this token.  The token is embedded in
# the page (window.APP_CONFIG), which a cross-origin page cannot read — there
# are no cookies here, so same-origin readability *is* the authentication.
CONTROL_TOKEN = secrets.token_urlsafe(32)
CONTROL_TOKEN_HEADER = "X-Tokenscope-Control"

# Set by serve(); the default matches serve()'s own default bind.
SERVE_HOST = "localhost"


def is_loopback_host(host):
    """True when `host` binds only to this machine.

    An empty host, "0.0.0.0" or "::" means every interface, so a sign-in button
    would be reachable from the LAN — that must not spawn a login on someone
    else's behalf, nor hand out a live authorize URL.
    """
    if host is None:
        return False
    name = host.strip().strip("[]").lower()
    if name in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False

SOURCE_CONFIG = {
    SOURCE_CLAUDE: {
        "label": "Claude Code",
        "short_label": "Claude",
        "pricing_basis": "Anthropic API pricing",
        "capabilities": {"cache": True, "reasoning_tokens": False, "subagents": True},
    },
    SOURCE_CODEX: {
        "label": "Codex",
        "short_label": "Codex",
        "pricing_basis": "OpenAI API-equivalent estimate; not Codex plan billing",
        "capabilities": {"cache": True, "reasoning_tokens": True, "subagents": False},
    },
}


def resolve_quota(source, force_refresh=False):
    """Plan-limit windows for one provider, plus the title the panel shows."""
    snapshot = get_quota_snapshot(
        source,
        claude_dirs=scanner.DEFAULT_PROJECTS_DIRS,
        codex_dir=scanner.CODEX_SESSIONS_DIR,
        force_refresh=force_refresh,
    )
    return dict(snapshot, title="Usage remaining")


def _local_date(timestamp):
    """Return a timestamp's host-local calendar date for dashboard filtering."""
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except (AttributeError, TypeError, ValueError):
        return (timestamp or "")[:10]


def get_dashboard_data(db_path=DB_PATH, source=SOURCE_CLAUDE, quota_force_refresh=False):
    if source not in SOURCE_CONFIG:
        return {"error": f"Unknown source: {source}"}
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    # The dashboard reads while a background scan may be committing (cmd_dashboard
    # serves first, scans in a background thread; /api/rescan scans in-process too).
    # Wait briefly for write locks instead of raising "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    # Ensure the schema is current before querying. cmd_dashboard binds and serves
    # *before* its background scan runs init_db, so on the first load after an
    # upgrade a pre-existing DB may still be on the old schema — the subagent
    # queries below reference the `agents` table and the `is_subagent`/`agent_id`
    # columns and would raise "no such table: agents" until the scan caught up.
    # init_db is idempotent (CREATE ... IF NOT EXISTS + additive column checks),
    # so this is a cheap no-op once migrated.
    init_db(conn)

    # ── All models (for filter UI) ────────────────────────────────────────────
    # GROUP BY uses the normalised expression too so NULL and '' don't end up
    # as two separate "unknown" rows.
    model_rows = conn.execute("""
        SELECT COALESCE(NULLIF(model, ''), 'unknown') as model
        FROM turns
        WHERE source = ?
        GROUP BY COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """, (source,)).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            date(timestamp, 'localtime') as day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            SUM(reasoning_output_tokens) as reasoning_output,
            is_long_context,
            COUNT(*)                   as turns
        FROM turns
        WHERE source = ?
        GROUP BY day, COALESCE(NULLIF(model, ''), 'unknown'), is_long_context
        ORDER BY day, model
    """, (source,)).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "long_context":   bool(r["is_long_context"]),
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "reasoning_output": r["reasoning_output"] or 0,
        "cost": calc_cost(
            r["model"], r["input"] or 0, r["output"] or 0,
            r["cache_read"] or 0, r["cache_creation"] or 0,
            source=source, long_context=bool(r["is_long_context"]),
        ),
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── Hourly per-day per-model (client filters by range, picks a TZ) ────────
    # Both frames are emitted per row so the client never pairs a local day with
    # a UTC hour: strftime parses the timestamp (including a 'Z' or ±HH:MM
    # suffix) instead of slicing characters, which silently assumed UTC, and the
    # local hour is exact for half-hour zones that an hour-rounded client-side
    # shift could not represent.
    hourly_rows = conn.execute("""
        SELECT
            date(timestamp, 'localtime')                     as day,
            date(timestamp)                                  as day_utc,
            CAST(strftime('%H', timestamp) AS INTEGER)             as hour,
            CAST(strftime('%H', timestamp, 'localtime') AS INTEGER) as hour_local,
            COALESCE(NULLIF(model, ''), 'unknown')           as model,
            SUM(output_tokens)                               as output,
            COUNT(*)                                         as turns
        FROM turns
        WHERE source = ? AND timestamp IS NOT NULL
          AND strftime('%H', timestamp) IS NOT NULL
        GROUP BY day, day_utc, hour, hour_local, COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, hour, model
    """, (source,)).fetchall()

    hourly_by_model = [{
        "day":        r["day"],
        "day_utc":    r["day_utc"] or r["day"],
        "hour":       r["hour"] if r["hour"] is not None else 0,
        "hour_local": r["hour_local"] if r["hour_local"] is not None else (r["hour"] or 0),
        "model":      r["model"],
        "output":     r["output"] or 0,
        "turns":      r["turns"] or 0,
    } for r in hourly_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, total_reasoning_output,
            model, turn_count,
            git_branch, topic
        FROM sessions
        WHERE source = ?
        ORDER BY last_timestamp DESC
    """, (source,)).fetchall()

    session_costs = {}
    session_cost_rows = conn.execute("""
        SELECT session_id, COALESCE(NULLIF(model, ''), 'unknown') AS model,
               is_long_context,
               SUM(input_tokens) AS input, SUM(output_tokens) AS output,
               SUM(cache_read_tokens) AS cache_read,
               SUM(cache_creation_tokens) AS cache_creation
        FROM turns
        WHERE source = ?
        GROUP BY session_id, COALESCE(NULLIF(model, ''), 'unknown'), is_long_context
    """, (source,)).fetchall()
    for cost_row in session_cost_rows:
        session_costs[cost_row["session_id"]] = session_costs.get(cost_row["session_id"], 0.0) + calc_cost(
            cost_row["model"], cost_row["input"] or 0, cost_row["output"] or 0,
            cost_row["cache_read"] or 0, cost_row["cache_creation"] or 0,
            source=source, long_context=bool(cost_row["is_long_context"]),
        )

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            # Full id: the table truncates for display, but the CSV export
            # needs the whole thing (an 8-char prefix isn't uniquely useful).
            "session_id":    r["session_id"],
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
            "topic":         r["topic"] or "",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     _local_date(r["last_timestamp"]),
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
            "reasoning_output": r["total_reasoning_output"] or 0,
            "cost":          session_costs.get(r["session_id"], 0.0),
        })

    # ── Subagent breakdown by type, by day & model ────────────────────────────
    # JOIN turns to agents (parent tool_result metadata captured by the scanner).
    # acompact-* ids are Claude Code's auto-compaction subagent (no parent
    # dispatch record); anything else without a match is shown as 'unknown'.
    AGENT_TYPE_EXPR = (
        "COALESCE(a.agent_type, "
        "CASE WHEN t.agent_id LIKE 'acompact-%' THEN 'auto-compact' "
        "ELSE 'unknown' END)"
    )

    subagent_daily_rows = conn.execute(f"""
        SELECT
            date(t.timestamp, 'localtime')           as day,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            COUNT(DISTINCT t.agent_id)               as dispatches,
            COUNT(*)                                 as turns
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
        WHERE t.source = ? AND t.is_subagent = 1
        GROUP BY day, agent_type, model
        ORDER BY day, agent_type
    """, (source,)).fetchall()

    subagent_by_type = [{
        "day":            r["day"],
        "agent_type":     r["agent_type"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "dispatches":     r["dispatches"] or 0,
        "turns":          r["turns"] or 0,
    } for r in subagent_daily_rows]

    # ── Top individual subagent dispatches (one row per agent_id) ─────────────
    top_dispatch_rows = conn.execute(f"""
        SELECT
            t.agent_id                               as agent_id,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            MIN(t.timestamp)                         as start_ts,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            COUNT(*)                                 as turns,
            a.dispatched_in_session                  as parent_session,
            a.total_duration_ms                      as duration_ms,
            a.tool_use_count                         as tool_uses,
            a.status                                 as status
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
        WHERE t.source = ? AND t.is_subagent = 1 AND t.agent_id IS NOT NULL
        GROUP BY t.agent_id
        ORDER BY (SUM(t.input_tokens) + SUM(t.output_tokens)
                  + SUM(t.cache_read_tokens) + SUM(t.cache_creation_tokens)) DESC
    """, (source,)).fetchall()

    top_dispatches = [{
        "agent_id":       r["agent_id"],
        "agent_type":     r["agent_type"],
        "model":          r["model"],
        "start":          (r["start_ts"] or "")[:16].replace("T", " "),
        # Local date, not a raw UTC slice: the client compares this against
        # local range bounds (same fix as sessions' last_date, #151).
        "start_date":     _local_date(r["start_ts"]),
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
        "duration_ms":    r["duration_ms"],
        "tool_uses":      r["tool_uses"],
        "status":         r["status"],
    } for r in top_dispatch_rows]

    quota = resolve_quota(source, quota_force_refresh)

    conn.close()

    return {
        "source":          source,
        "label":           SOURCE_CONFIG[source]["label"],
        "capabilities":    SOURCE_CONFIG[source]["capabilities"],
        "provider":        SOURCE_CONFIG[source],
        "available_sources": list(SOURCE_CONFIG),
        "all_models":      all_models,
        "daily_by_model":  daily_by_model,
        "hourly_by_model": hourly_by_model,
        "sessions_all":    sessions_all,
        "subagent_by_type": subagent_by_type,
        "top_dispatches":  top_dispatches,
        "quota":          quota,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TokenScope</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>window.APP_CONFIG = __APP_CONFIG_JSON__;</script>
<style>
  :root {
    --bg: #101014;
    --card: #17171d;
    --card-strong: #1c1c23;
    --border: #2a2a34;
    --border-soft: #202028;
    --text: #f1f1f5;
    --muted: #9494a3;
    --muted-strong: #c7c7d2;
    --accent: #8b5cf6;
    --accent-soft: rgba(139, 92, 246, 0.14);
    --provider-accent: #a78bfa;
    --provider-accent-soft: rgba(167, 139, 250, 0.12);
    --blue: #7aa2f7;
    --green: #75d39b;
    --red: #ed7b72;
    --raised: #22222b;
    --selected: #282832;
    --jump-h: 45px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    min-height: 100vh;
    display: grid;
    grid-template-columns: 238px minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr) auto;
    background: var(--bg);
    color: var(--text);
    font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 13px;
    line-height: 1.45;
    letter-spacing: -0.01em;
  }

  /* Keep scrollbars visually consistent with the dashboard's dark theme. */
  * { scrollbar-width: auto; scrollbar-color: #28292B #121314; }
  ::-webkit-scrollbar { width: 21px; height: 21px; }
  ::-webkit-scrollbar-track { background: #121314; }
  ::-webkit-scrollbar-thumb { background-color: #28292B; border: 3px solid transparent; background-clip: padding-box; }
  ::-webkit-scrollbar-thumb:hover { background-color: #8B8B8D; }
  ::-webkit-scrollbar-thumb:active { background-color: #8B8B8D; }
  ::-webkit-scrollbar-corner { background: #121314; }

  header {
    grid-column: 1;
    grid-row: 1 / -1;
    position: sticky;
    top: 0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 18px;
    padding: 20px 14px 16px;
    background: #15151b;
    border-right: 1px solid var(--border);
  }
  header h1 { font-size: 14px; font-weight: 640; color: var(--text); letter-spacing: -0.02em; }
  header .header-title { display: flex; align-items: center; gap: 10px; padding: 0 7px; }
  .header-copy { min-width: 0; }
  .header-eyebrow { display: block; margin-bottom: 1px; color: var(--muted); font-size: 9px; font-weight: 700; letter-spacing: 0.11em; }
  .source-tabs { display: grid; gap: 3px; padding: 0; background: transparent; border: 0; border-radius: 0; }
  .source-tab { width: 100%; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--muted); padding: 8px 10px; font-size: 12px; text-align: left; cursor: pointer; white-space: nowrap; transition: background 140ms ease, color 140ms ease, border-color 140ms ease; }
  .source-tab:hover { color: var(--text); background: var(--raised); }
  .source-tab[aria-selected="true"] { color: var(--text); background: var(--accent-soft); border-color: rgba(139, 92, 246, 0.32); font-weight: 600; }
  .source-tab:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  /* The icon is a monochrome silhouette (white shape on transparent). We paint
     it with the title color via a CSS mask + background-color, so it matches
     `header h1` — the lightest text color. */
  header .header-icon {
    width: 24px; height: 24px; flex-shrink: 0; display: block;
    background-color: #a78bfa;
    -webkit-mask: url("icon.svg") no-repeat center / contain;
    mask: url("icon.svg") no-repeat center / contain;
  }
  body[data-source="codex"] { --provider-accent: #63d7a0; --provider-accent-soft: rgba(99, 215, 160, 0.12); }
  /* Codex keeps its green accent everywhere else; the header mark stays the
     same violet as the Claude one so the two tabs read as one product. */
  body[data-source="codex"] header .header-icon {
    -webkit-mask-image: url("codex-icon.svg");
    mask-image: url("codex-icon.svg");
  }
  .quota-panel {
    margin: 0 1px 2px;
    padding: 12px 11px 10px;
    background: rgba(255,255,255,0.025);
    border: 1px solid var(--border-soft);
    border-radius: 8px;
    animation: rise-in 360ms both;
  }
  .quota-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 3px; }
  .quota-title { color: var(--muted-strong); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; }
  .quota-title::before { content: ""; display: inline-block; width: 6px; height: 6px; margin: 0 6px 1px 0; border-radius: 50%; background: var(--provider-accent); box-shadow: 0 0 0 3px var(--provider-accent-soft); }
  .quota-heading-actions { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
  /* The reading's provenance sits on its own line. Sharing the heading row with
     a two-line title and the refresh button over-subscribed ~200px of sidebar,
     so the ellipsis always fired and the timestamp was never readable. Given
     the full width back, the title also settles onto one line — the panel is
     no taller than before. */
  .quota-updated { display: block; margin-bottom: 9px; color: var(--muted); font-size: 9px; line-height: 1.4; font-variant-numeric: tabular-nums; }
  .quota-refresh { width: 22px; height: 22px; flex: 0 0 22px; display: grid; place-items: center; padding: 0; border: 1px solid var(--border); border-radius: 5px; background: transparent; color: var(--muted-strong); cursor: pointer; font: inherit; font-size: 14px; line-height: 1; transition: background 140ms ease, color 140ms ease, border-color 140ms ease; }
  .quota-refresh:hover { color: var(--text); background: var(--raised); border-color: var(--border-strong); }
  .quota-refresh:focus-visible { outline: 2px solid var(--provider-accent); outline-offset: 2px; }
  .quota-refresh:disabled { opacity: 0.55; cursor: wait; }
  .quota-refresh.is-refreshing { animation: quota-spin 700ms linear infinite; }
  @keyframes quota-spin { to { transform: rotate(360deg); } }
  .quota-rows { display: grid; gap: 1px; }
  .quota-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 1px 8px; padding: 8px 0; border-top: 1px solid var(--border-soft); }
  .quota-row:first-child { border-top: 0; }
  .quota-window { color: var(--muted-strong); font-size: 11px; font-weight: 600; }
  .quota-value { color: var(--text); font-size: 16px; line-height: 1.1; font-weight: 650; font-variant-numeric: tabular-nums; letter-spacing: -0.03em; text-align: right; }
  .quota-reset { color: var(--muted); font-size: 10px; }
  .quota-signin { display: block; margin-top: 8px; padding: 5px 10px; border: 1px solid var(--border-strong); border-radius: 5px; background: var(--raised); color: var(--text); font: inherit; font-size: 11px; font-weight: 600; cursor: pointer; }
  .quota-signin:hover { border-color: var(--provider-accent); }
  .quota-signin:focus-visible { outline: 2px solid var(--provider-accent); outline-offset: 2px; }
  .quota-signin:disabled { opacity: 0.6; cursor: progress; }
  /* Shown only if the CLI could not open a browser itself. */
  .quota-authhint { display: block; margin-top: 6px; color: var(--muted); font-size: 10px; line-height: 1.45; }
  .quota-authhint code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10px; color: var(--muted-strong); }
  .quota-authlink { display: block; margin-top: 6px; color: var(--provider-accent); font-size: 10px; line-height: 1.4; word-break: break-all; }
  .quota-note { color: var(--muted); font-size: 10px; line-height: 1.4; padding: 7px 0 0; border-top: 1px solid var(--border-soft); }
  .quota-empty { color: var(--muted); font-size: 11px; line-height: 1.45; padding: 4px 0 2px; }
  .quota-empty strong { display: block; color: var(--muted-strong); font-size: 11px; font-weight: 600; margin-bottom: 2px; }
  header .meta { order: 2; margin-top: auto; color: var(--muted); font-size: 11px; line-height: 1.55; padding: 0 7px; }
  #rescan-btn { order: 3; width: 100%; background: var(--accent); border: 1px solid var(--accent); color: #fff; padding: 8px 10px; border-radius: 6px; cursor: pointer; font: inherit; font-size: 12px; font-weight: 600; text-align: left; transition: filter 140ms ease, transform 140ms ease; }
  #rescan-btn:hover { color: #fff; filter: brightness(1.11); transform: translateY(-1px); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #dashboard-panel { grid-column: 2; grid-row: 1; min-width: 0; }
  #filter-bar { position: sticky; top: 0; z-index: 30; min-height: 60px; background: rgba(16, 16, 20, 0.9); backdrop-filter: blur(16px); border-bottom: 1px solid var(--border); padding: 13px 32px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.09em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  /* Model multi-select: a compact trigger in the bar that opens a grouped panel. */
  .model-select { position: relative; flex-shrink: 0; }
  .model-trigger { display: flex; align-items: center; gap: 8px; min-width: 170px; max-width: 320px; padding: 6px 10px; background: var(--card); border: 1px solid var(--border); border-radius: 6px; color: var(--muted-strong); font: inherit; font-size: 12px; cursor: pointer; transition: border-color 0.15s, background 0.15s; }
  .model-trigger:hover, .model-trigger.open { border-color: rgba(139, 92, 246, 0.7); background: var(--card-strong); }
  #model-trigger-label { flex: 1; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .model-caret { color: var(--muted); font-size: 10px; flex-shrink: 0; transition: transform 0.15s; }
  .model-trigger.open .model-caret { transform: rotate(180deg); }
  .model-panel { position: absolute; top: calc(100% + 6px); left: 0; z-index: 50; min-width: 250px; max-width: 340px; max-height: 360px; overflow-y: auto; background: #202027; border: 1px solid #343441; border-radius: 8px; padding: 8px; box-shadow: 0 18px 42px rgba(0,0,0,0.4); }
  .model-panel[hidden] { display: none; }
  .model-panel-actions { display: flex; gap: 6px; padding-bottom: 8px; margin-bottom: 4px; border-bottom: 1px solid var(--border); }
  .model-group-label { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); padding: 8px 8px 4px; }
  .model-cb-label { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 12px; color: var(--muted); transition: background 0.12s, color 0.12s; user-select: none; }
  .model-cb-label:hover { background: var(--raised); color: var(--text); }
  .model-cb-label.checked { color: var(--text); }
  .model-cb-label input { display: none; }
  .model-cb-box { width: 15px; height: 15px; flex-shrink: 0; border-radius: 4px; border: 1px solid var(--border); display: flex; align-items: center; justify-content: center; font-size: 10px; line-height: 1; color: transparent; transition: background 0.12s, border-color 0.12s; }
  .model-cb-label.checked .model-cb-box { background: var(--accent); border-color: var(--accent); color: #fff; }
  .model-cb-text { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font: inherit; font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: rgba(139, 92, 246, 0.75); color: var(--text); }
  /* Date range uses the same explicit trigger/panel pattern as Models. */
  .range-select { position: relative; flex-shrink: 0; }
  .range-trigger { min-width: 150px; max-width: 230px; }
  .range-panel { min-width: 220px; max-height: min(430px, calc(100vh - 92px)); overflow-y: auto; }
  .range-option { display: flex; align-items: center; width: 100%; padding: 6px 8px; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--muted); font: inherit; font-size: 12px; text-align: left; cursor: pointer; transition: background 0.12s, color 0.12s, border-color 0.12s; }
  .range-option:hover, .range-option:focus-visible { color: var(--text); background: var(--raised); outline: none; }
  .range-option.selected { color: var(--text); background: var(--accent-soft); border-color: rgba(139, 92, 246, 0.32); font-weight: 600; }
  .range-divider { height: 1px; margin: 5px 0; background: var(--border); }
  .custom-range-form { display: grid; gap: 7px; padding: 8px; margin-top: 5px; border-top: 1px solid var(--border); }
  .custom-range-form[hidden] { display: none; }
  .custom-range-field { display: grid; gap: 4px; color: var(--muted); font-size: 10px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; }
  .custom-range-field input { width: 100%; min-height: 30px; padding: 5px 7px; border: 1px solid var(--border); border-radius: 5px; background: var(--card); color: var(--text); color-scheme: dark; font: inherit; font-size: 12px; }
  .custom-range-field input:hover { border-color: var(--border-strong); }
  .custom-range-field input:focus-visible { border-color: var(--accent); outline: 2px solid var(--accent); outline-offset: 1px; }
  .custom-range-error { min-height: 14px; color: var(--red); font-size: 10px; line-height: 1.35; }
  .custom-range-actions { display: flex; justify-content: flex-end; gap: 6px; }

  .container { max-width: 1560px; margin: 0 auto; padding: 30px 32px 52px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 0; margin-bottom: 32px; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
  .stat-card { position: relative; min-height: 106px; padding: 17px 16px 15px; border-right: 1px solid var(--border); animation: rise-in 360ms both; }
  .stat-card:last-child { border-right: 0; }
  .stat-card .label { color: var(--muted); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.09em; margin-bottom: 10px; }
  .stat-card .value { font-size: clamp(20px, 2vw, 27px); font-weight: 650; letter-spacing: -0.045em; font-variant-numeric: tabular-nums; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 5px; }
  .stat-card:hover { background: rgba(255,255,255,0.015); }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 28px; }
  /* min-width:0 lets the grid column shrink below the canvas's intrinsic
     pixel width; without it, narrowing the window can't narrow the container,
     so Chart.js's ResizeObserver never fires until a data refresh rebuilds the
     canvas. (Expanding already works — 1fr columns grow freely.) */
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 7px; padding: 18px 18px 14px; min-width: 0; transition: border-color 180ms ease, transform 180ms ease; }
  .chart-card:hover { border-color: #3b3b48; transform: translateY(-1px); }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 11px; font-weight: 700; color: var(--muted-strong); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 250px; }
  .chart-wrap.tall { height: 320px; }
  .chart-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .chart-header h2 { margin-bottom: 0; }
  .chart-header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chart-day-count { font-size: 11px; color: var(--muted); }
  .tz-group { display: flex; border: 1px solid var(--border); border-radius: 5px; overflow: hidden; }
  .tz-btn { padding: 3px 10px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 11px; cursor: pointer; transition: background 0.15s, color 0.15s; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .tz-btn:last-child { border-right: none; }
  .tz-btn:hover { background: var(--raised); color: var(--text); }
  .tz-btn.active { background: var(--accent-soft); color: #c4b5fd; }
  .peak-legend { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
  .peak-swatch { width: 10px; height: 10px; background: var(--red); border-radius: 2px; display: inline-block; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 9px 12px; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; opacity: 0.8; }
  td { padding: 11px 12px; border-bottom: 1px solid var(--border-soft); font-size: 12px; color: var(--muted-strong); }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(139, 92, 246, 0.045); }
  .model-tag { display: inline-block; padding: 3px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; background: rgba(122,162,247,0.12); color: #a8c1ff; }
  .cost { color: var(--green); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-variant-numeric: tabular-nums; }
  .cost-na { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
  .num { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-variant-numeric: tabular-nums; }
  .provider-no-reasoning .reasoning-col { display: none; }
  .muted { color: var(--muted); }
  .topic-cell { box-sizing: border-box; min-width: 160px; max-width: 260px; overflow-wrap: anywhere; font-size: 12px; color: var(--text); }
  .untitled { color: var(--muted); font-style: italic; }
  .section-title { font-size: 11px; font-weight: 700; color: var(--muted-strong); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header .section-title { margin-bottom: 0; }
  .export-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 4px 9px; border-radius: 5px; cursor: pointer; font: inherit; font-size: 11px; transition: color 140ms ease, border-color 140ms ease; }
  .export-btn:hover { color: #c4b5fd; border-color: rgba(139, 92, 246, 0.7); }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 7px; padding: 18px; margin-bottom: 16px; overflow-x: auto; }
  .table-foot { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-top: 12px; }
  .table-foot:empty { margin-top: 0; }
  .show-more-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .show-more-btn:hover { color: var(--text); border-color: var(--accent); }
  .show-more-link { color: var(--blue); text-decoration: none; font-size: 12px; cursor: pointer; }
  .show-more-link:hover { text-decoration: underline; }

  footer { grid-column: 2; border-top: 1px solid var(--border); padding: 18px 32px; }
  .footer-content { max-width: 1560px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  /* Jump bar — a sticky table-of-contents for a long report. Styled as a sibling
     of the filter bar (same card surface + bottom border) so it reads as part of
     the same control strip. It pins to the viewport top once the header/filter
     scroll away. z-index sits below the model panel (50) so the dropdown still
     overlays it. */
  /* Sticky table-of-contents for the long report: three compact entries —
     Overview, plus Graphs and Tables menus that reveal their sections on hover
     or keyboard focus. */
  #jump-bar { position: sticky; top: 60px; z-index: 20; background: rgba(16, 16, 20, 0.9); backdrop-filter: blur(16px); border-bottom: 1px solid var(--border-soft); padding: 9px 32px; display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
  .jump-menu { position: relative; }
  .jump-trigger { display: inline-flex; align-items: center; gap: 6px; padding: 3px 11px; border-radius: 6px; border: 1px solid transparent; background: transparent; color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.12s, color 0.12s, border-color 0.12s; }
  .jump-trigger svg { display: block; }
  .jump-caret { font-size: 9px; }
  .jump-trigger:hover, .jump-menu:focus-within .jump-trigger { color: var(--text); background: var(--raised); }
  .jump-trigger.active { color: #c4b5fd; border-color: rgba(139, 92, 246, 0.32); }
  .jump-panel { position: absolute; top: calc(100% + 5px); left: 0; z-index: 50; min-width: 160px; display: none; flex-direction: column; gap: 2px; padding: 6px; background: #202027; border: 1px solid #343441; border-radius: 7px; box-shadow: 0 18px 42px rgba(0,0,0,0.4); }
  /* Invisible bridge over the 5px gap so the menu doesn't close as the pointer
     travels from the trigger down to the panel. */
  .jump-panel::before { content: ""; position: absolute; left: 0; right: 0; top: -8px; height: 8px; }
  .jump-menu-end .jump-panel { left: auto; right: 0; }
  .jump-menu:hover .jump-panel, .jump-menu:focus-within .jump-panel { display: flex; }
  .jump-link { padding: 3px 11px; border-radius: 6px; border: 1px solid transparent; background: transparent; color: var(--muted); font-size: 12px; cursor: pointer; white-space: nowrap; transition: background 0.12s, color 0.12s, border-color 0.12s; }
  .jump-panel .jump-link { display: block; width: 100%; text-align: left; padding: 5px 10px; }
  .jump-link:hover { color: var(--text); background: var(--raised); }
  .jump-link.active { color: #c4b5fd; background: var(--accent-soft); border-color: rgba(139, 92, 246, 0.32); font-weight: 600; }
  /* The scroll-spy still marks the current row, but a Graphs/Tables menu item
     keeps the explicit item the user selected. This avoids a sibling in the
     same grid row borrowing the violet selected treatment during a jump. */
  .jump-panel .jump-link.active { color: var(--muted); background: transparent; border-color: transparent; font-weight: 400; }
  .jump-panel .jump-link.selected { color: #c4b5fd; background: var(--accent-soft); border-color: rgba(139, 92, 246, 0.32); font-weight: 600; }
  /* Inline info affordance (e.g. the dispatches table) — native title tooltip. */
  .info-icon { display: inline-flex; align-items: center; vertical-align: middle; margin-left: 3px; color: var(--muted); cursor: help; }
  .info-icon svg { display: block; }
  .info-icon:hover { color: var(--text); }
  /* Anchored sections clear the sticky bar when jumped/collapsed to. */
  .stats-row, .chart-card, .table-card { scroll-margin-top: calc(var(--jump-h) + 14px); }

  /* Collapsible cards — a full section fold, independent of in-table Show
     more/less (which only pages rows). Collapsing hides the card body and its
     header controls, leaving just the caret + title. State persists per card in
     localStorage. */
  .card-caret { display: inline-block; width: 0.9em; margin-right: 7px; font-size: 13px; line-height: 1; color: #a78bfa; transform: rotate(90deg); transition: transform 0.15s; }
  .collapsed .card-caret { transform: rotate(0deg); }
  .chart-card > h2, .chart-header > h2, .section-title { cursor: pointer; user-select: none; }
  .chart-card > h2:hover, .chart-header > h2:hover, .section-title:hover { color: var(--text); }
  .jump-link:focus-visible, .jump-trigger:focus-visible, .info-icon:focus-visible, .chart-card > h2:focus-visible, .chart-header > h2:focus-visible, .section-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chart-card.collapsed > h2, .chart-card.collapsed > .chart-header { margin-bottom: 0; }
  .table-card.collapsed > .section-title, .table-card.collapsed > .section-header { margin-bottom: 0; }
  .chart-card.collapsed > *:not(h2):not(.chart-header),
  .chart-card.collapsed .chart-header > *:not(h2),
  .table-card.collapsed > *:not(.section-title):not(.section-header),
  .table-card.collapsed .section-header > *:not(.section-title) { display: none; }


  /* ── Sidebar: settings nav ──────────────────────────────────────────────
     Last in the sidebar's flex order, so the bottom group reads
     meta text → Rescan → Settings. `margin-top: auto` lives on `header .meta`,
     the first of the three, and is what pins the group to the foot. */
  .nav-settings { order: 4; display: flex; align-items: center; gap: 8px; width: 100%; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--muted); padding: 8px 10px; font: inherit; font-size: 12px; text-align: left; cursor: pointer; transition: background 140ms ease, color 140ms ease, border-color 140ms ease; }
  .nav-settings:hover { color: var(--text); background: var(--raised); }
  .nav-settings[aria-current="page"] { color: var(--text); background: var(--accent-soft); border-color: rgba(139, 92, 246, 0.32); font-weight: 600; }
  .nav-settings:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .nav-settings svg { flex-shrink: 0; }
  /* Unsaved-changes dot, so leaving the page can't quietly lose an edit. */
  .nav-dot { margin-left: auto; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

  /* ── Settings view ──────────────────────────────────────────────────────── */
  #settings-panel { grid-column: 2; grid-row: 1; min-width: 0; }
  .settings-wrap { max-width: 1120px; margin: 0 auto; padding: 26px 32px 24px; }
  .settings-title { font-size: 19px; font-weight: 650; letter-spacing: -0.02em; }
  .settings-lede { margin-top: 5px; color: var(--muted); font-size: 12.5px; max-width: 74ch; }
  .settings-card { margin-top: 18px; padding: 18px; background: var(--card); border: 1px solid var(--border); border-radius: 8px; animation: rise-in 320ms both; }
  .settings-card > h2 { font-size: 13px; font-weight: 650; letter-spacing: -0.01em; }
  .settings-card > .settings-hint { margin-top: 4px; color: var(--muted); font-size: 12px; max-width: 82ch; }

  .toggle-row { display: flex; align-items: center; gap: 12px; margin-top: 12px; padding: 11px 12px; background: var(--card-strong); border: 1px solid var(--border-soft); border-radius: 7px; }
  .toggle-row .toggle-copy { min-width: 0; flex: 1; }
  .toggle-row .toggle-name { font-size: 12.5px; font-weight: 600; }
  .toggle-row .toggle-note { margin-top: 2px; color: var(--muted); font-size: 11.5px; }
  .toggle-row input[type="checkbox"] { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; flex-shrink: 0; }

  .price-group { margin-top: 16px; }
  .price-group + .price-group { padding-top: 16px; border-top: 1px solid var(--border-soft); }
  .price-group-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 9px; }
  .price-group-title { font-size: 12px; font-weight: 650; }
  .price-group-basis { color: var(--muted); font-size: 11.5px; }
  .price-filter { margin-left: auto; background: var(--raised); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font: inherit; font-size: 11.5px; padding: 5px 8px; min-width: 170px; }
  .price-filter::placeholder { color: var(--muted); }

  /* Capped height so two ~45-row tables don't turn the page into a scroll
     marathon — and so the sticky header has a container to stick inside. */
  .price-scroll { max-height: 430px; overflow: auto; border: 1px solid var(--border-soft); border-radius: 7px; }
  table.price-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  table.price-table th { position: sticky; top: 0; z-index: 1; background: var(--card-strong); color: var(--muted-strong); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; padding: 8px 9px; white-space: nowrap; border-bottom: 1px solid var(--border); }
  /* Rates read as a column of numbers, so every cell but the model name is
     right-aligned and the name column absorbs the slack. */
  table.price-table th, table.price-table td { text-align: right; }
  table.price-table th:first-child, table.price-table td:first-child { text-align: left; width: 34%; }
  table.price-table td { padding: 5px 9px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
  table.price-table tr:last-child td { border-bottom: 0; }
  table.price-table tbody tr:hover { background: rgba(255,255,255,0.018); }
  .price-model { display: flex; align-items: center; gap: 7px; white-space: nowrap; }
  .price-model code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; color: var(--text); }
  .price-badge { border-radius: 999px; padding: 1px 6px; font-size: 9.5px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; }
  .price-badge.modified { background: var(--accent-soft); color: #c4b5fd; }
  .price-badge.custom { background: rgba(117, 211, 155, 0.14); color: var(--green); }
  .rate-input { width: 92px; background: var(--raised); border: 1px solid var(--border); border-radius: 5px; color: var(--text); font: inherit; font-size: 11.5px; padding: 4px 6px; text-align: right; }
  .rate-input:focus { outline: none; border-color: var(--accent); }
  .rate-input.dirty { border-color: var(--accent); background: var(--accent-soft); }
  .rate-input.invalid { border-color: var(--red); }
  .row-btn { background: transparent; border: 1px solid var(--border); border-radius: 5px; color: var(--muted); font: inherit; font-size: 11px; padding: 3px 7px; cursor: pointer; }
  .row-btn:hover { color: var(--text); border-color: var(--muted); }
  .row-btn.danger:hover { color: var(--red); border-color: var(--red); }
  .row-actions { display: flex; gap: 5px; justify-content: flex-end; white-space: nowrap; }
  .lc-row td { background: rgba(255,255,255,0.02); text-align: left; }
  .lc-fields { display: flex; flex-wrap: wrap; gap: 10px; padding: 3px 0 5px; }
  .lc-field { display: flex; flex-direction: column; gap: 3px; color: var(--muted); font-size: 10px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
  .lc-note { flex: 1 1 100%; color: var(--muted); font-size: 11.5px; font-weight: 400; letter-spacing: 0; text-transform: none; }
  .price-empty { padding: 14px; color: var(--muted); font-size: 12px; text-align: center; }

  .add-model { margin-top: 11px; padding: 12px; background: var(--card-strong); border: 1px dashed var(--border); border-radius: 7px; }
  .add-model-title { font-size: 11.5px; font-weight: 650; margin-bottom: 8px; }
  .add-model-fields { display: flex; flex-wrap: wrap; gap: 9px; align-items: flex-end; }
  .add-field { display: flex; flex-direction: column; gap: 3px; color: var(--muted); font-size: 10px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
  .add-field input { background: var(--raised); border: 1px solid var(--border); border-radius: 5px; color: var(--text); font: inherit; font-size: 11.5px; padding: 5px 7px; width: 92px; }
  .add-field.wide input { width: 230px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .add-field input:focus { outline: none; border-color: var(--accent); }
  .add-model button { background: var(--accent); border: 1px solid var(--accent); border-radius: 6px; color: #fff; font: inherit; font-size: 11.5px; font-weight: 600; padding: 6px 12px; cursor: pointer; }
  .add-model button:hover { filter: brightness(1.11); }

  /* Sticky action bar: the page is long, so Save must never scroll away. */
  .settings-bar { position: sticky; bottom: 0; z-index: 4; display: flex; align-items: center; gap: 12px; margin-top: 18px; padding: 12px 32px; background: rgba(21, 21, 27, 0.96); backdrop-filter: blur(6px); border-top: 1px solid var(--border); }
  .settings-bar-inner { max-width: 1120px; width: 100%; margin: 0 auto; display: flex; align-items: center; gap: 12px; }
  .settings-status { color: var(--muted); font-size: 12px; min-width: 0; flex: 1; }
  .settings-status.is-error { color: var(--red); }
  .settings-status.is-ok { color: var(--green); }
  .btn-primary { background: var(--accent); border: 1px solid var(--accent); border-radius: 6px; color: #fff; font: inherit; font-size: 12px; font-weight: 600; padding: 8px 15px; cursor: pointer; transition: filter 140ms ease; }
  .btn-primary:hover:not(:disabled) { filter: brightness(1.11); }
  .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-ghost { background: transparent; border: 1px solid var(--border); border-radius: 6px; color: var(--muted); font: inherit; font-size: 12px; padding: 8px 13px; cursor: pointer; }
  .btn-ghost:hover:not(:disabled) { color: var(--text); border-color: var(--muted); }
  .btn-ghost:disabled { opacity: 0.45; cursor: not-allowed; }

  /* ── Confirm dialog ─────────────────────────────────────────────────────── */
  .modal-backdrop { position: fixed; inset: 0; z-index: 60; display: flex; align-items: center; justify-content: center; padding: 24px; background: rgba(6, 6, 9, 0.66); }
  .modal-backdrop[hidden] { display: none; }
  .modal { width: min(520px, 100%); max-height: 80vh; overflow-y: auto; padding: 20px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 24px 60px rgba(0,0,0,0.5); animation: rise-in 200ms both; }
  .modal h2 { font-size: 14px; font-weight: 650; }
  .modal p { margin-top: 6px; color: var(--muted); font-size: 12.5px; }
  .modal ul { margin: 12px 0 0; padding: 11px 12px 11px 26px; background: var(--card-strong); border: 1px solid var(--border-soft); border-radius: 7px; font-size: 12px; }
  .modal li + li { margin-top: 4px; }
  .modal li code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
  .modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }

  @keyframes rise-in { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
  @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; transition-duration: 0.01ms !important; } }
  @media (max-width: 900px) {
    body { display: block; }
    header { position: static; width: auto; height: auto; display: grid; grid-template-columns: 1fr auto; gap: 12px; padding: 14px 18px; border-right: 0; border-bottom: 1px solid var(--border); }
    header .header-title { padding: 0; }
    .header-eyebrow { display: none; }
    .source-tabs { grid-column: 1 / -1; grid-row: 2; grid-template-columns: 1fr 1fr; }
    .source-tab { text-align: center; }
    .quota-panel { grid-column: 1 / -1; }
    #rescan-btn { grid-column: 2; grid-row: 1; width: auto; margin: 0; padding: 7px 10px; }
    header .meta { display: none; }
    #filter-bar { top: 0; padding: 11px 18px; }
    #jump-bar { top: 58px; padding: 8px 18px; }
    .container { padding: 22px 18px 40px; }
    .settings-wrap { padding: 20px 18px; }
    .settings-bar { padding: 12px 18px; }
    .nav-settings { grid-column: 1 / -1; margin-top: 0; }
    footer { padding: 18px; }
  }
  @media (max-width: 640px) {
    .stats-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .stat-card:nth-child(2n) { border-right: 0; }
    .stat-card { border-bottom: 1px solid var(--border-soft); }
    .charts-grid { grid-template-columns: 1fr; gap: 12px; }
    .chart-card.wide { grid-column: 1; }
    .chart-card, .table-card { padding: 14px; border-radius: 6px; }
    .chart-wrap { height: 220px; }
    .chart-wrap.tall { height: 270px; }
    #jump-bar { overflow-x: auto; flex-wrap: nowrap; }
    .jump-menu { flex-shrink: 0; }
  }
</style>
</head>
<body>
<header>
  <div class="header-title">
    <span class="header-icon" role="img" aria-label="TokenScope"></span>
    <div class="header-copy"><span class="header-eyebrow">TOKENSCOPE</span><h1 id="page-title">Claude Code</h1></div>
  </div>
  <div class="source-tabs" role="tablist" aria-label="Usage source">
    <button class="source-tab" id="tab-claude_code" role="tab" aria-selected="true" aria-controls="dashboard-panel" onclick="setSource('claude_code')">Claude Code</button>
    <button class="source-tab" id="tab-codex" role="tab" aria-selected="false" aria-controls="dashboard-panel" onclick="setSource('codex')">Codex</button>
  </div>
  <section class="quota-panel" id="quota-panel" aria-labelledby="quota-title">
    <div class="quota-heading"><span class="quota-title" id="quota-title">Usage remaining</span><span class="quota-heading-actions"><button class="quota-refresh" id="quota-refresh" type="button" aria-label="Refresh usage limits" title="Refresh usage limits" onclick="refreshQuota()">&#x21bb;</button></span></div>
    <span class="quota-updated" id="quota-updated">Loading</span>
    <div class="quota-rows" id="quota-rows"><div class="quota-empty"><strong>Loading limits</strong>Reading the latest local usage signal.</div></div>
  </section>
  <div class="meta" id="meta">Loading...</div>
  <button class="nav-settings" id="nav-settings" type="button" aria-current="false" onclick="setView('settings')">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    Settings
    <span class="nav-dot" id="nav-settings-dot" hidden title="Unsaved changes"></span>
  </button>
  <button id="rescan-btn" onclick="triggerRescan()" title="Scan now for new usage. Automatic rescans run every 30 minutes and never remove existing history.">&#x21bb; Rescan</button>
</header>

<div id="dashboard-panel" role="tabpanel" aria-live="polite">
<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div class="model-select" id="model-select">
    <button class="model-trigger" id="model-trigger" aria-haspopup="true" aria-expanded="false" onclick="toggleModelPanel(event)">
      <span id="model-trigger-label">All models</span>
      <span class="model-caret">&#9662;</span>
    </button>
    <div class="model-panel" id="model-panel" hidden>
      <div class="model-panel-actions">
        <button class="filter-btn" onclick="selectAllModels()">All</button>
        <button class="filter-btn" onclick="clearAllModels()">None</button>
      </div>
      <div id="model-checkboxes"></div>
    </div>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-select model-select" id="range-select">
    <button class="model-trigger range-trigger" id="range-trigger" type="button" aria-haspopup="true" aria-expanded="false" aria-controls="range-panel" onclick="toggleRangePanel(event)">
      <span id="range-trigger-label">Last 30 Days</span>
      <span class="model-caret" aria-hidden="true">&#9662;</span>
    </button>
    <div class="model-panel range-panel" id="range-panel" hidden>
      <button class="range-option" type="button" data-range="today" onclick="setRange('today')">Today</button>
      <button class="range-option" type="button" data-range="week" onclick="setRange('week')">This Week</button>
      <button class="range-option" type="button" data-range="month" onclick="setRange('month')">This Month</button>
      <button class="range-option" type="button" data-range="prev-month" onclick="setRange('prev-month')">Previous Month</button>
      <button class="range-option" type="button" data-range="7d" onclick="setRange('7d')">Last 7 Days</button>
      <button class="range-option" type="button" data-range="30d" onclick="setRange('30d')">Last 30 Days</button>
      <button class="range-option" type="button" data-range="90d" onclick="setRange('90d')">Last 90 Days</button>
      <button class="range-option" type="button" data-range="all" onclick="setRange('all')">All Time</button>
      <div class="range-divider"></div>
      <button class="range-option" type="button" data-range="custom" onclick="showCustomRangeForm()">Custom range…</button>
      <form class="custom-range-form" id="custom-range-form" hidden novalidate onsubmit="applyCustomRange(event)">
        <label class="custom-range-field">Start date <input id="custom-range-start" type="date" required></label>
        <label class="custom-range-field">End date <input id="custom-range-end" type="date" required></label>
        <div class="custom-range-error" id="custom-range-error" role="alert" aria-live="polite"></div>
        <div class="custom-range-actions"><button class="filter-btn" type="button" onclick="hideCustomRangeForm()">Cancel</button><button class="filter-btn" type="submit">Apply</button></div>
      </form>
    </div>
  </div>
</div>

<nav id="jump-bar" aria-label="Jump to section">
  <button class="jump-link" data-target="stats-row">Overview</button>
  <div class="jump-menu">
    <button type="button" class="jump-trigger" aria-haspopup="true" aria-expanded="false">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M8 17v-4"/><path d="M13 17V8"/><path d="M18 17v-7"/></svg>
      Graphs <span class="jump-caret">&#9662;</span>
    </button>
    <div class="jump-panel">
      <button class="jump-link" data-target="sec-daily">Daily</button>
      <button class="jump-link" data-target="sec-hourly">Distribution</button>
      <button class="jump-link" data-target="sec-models">By Model</button>
      <button class="jump-link" data-target="sec-projects">Top Projects</button>
      <button class="jump-link" data-target="sec-subagents">Subagents</button>
    </div>
  </div>
  <div class="jump-menu jump-menu-end">
    <button type="button" class="jump-trigger" aria-haspopup="true" aria-expanded="false">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18"/><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/></svg>
      Tables <span class="jump-caret">&#9662;</span>
    </button>
    <div class="jump-panel">
      <button class="jump-link" data-target="sec-cost-model">Cost by Model</button>
      <button class="jump-link" data-target="sec-dispatches">Dispatches</button>
      <button class="jump-link" data-target="sec-sessions">Sessions</button>
      <button class="jump-link" data-target="sec-cost-project">Cost by Project</button>
      <button class="jump-link" data-target="sec-cost-branch">Cost by Project &amp; Branch</button>
    </div>
  </div>
</nav>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide" id="sec-daily" data-card="daily">
      <h2><span class="card-caret">&#9656;</span><span id="daily-chart-title">Daily Token Usage</span></h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-hourly" data-card="hourly">
      <div class="chart-header">
        <h2><span class="card-caret">&#9656;</span><span id="hourly-chart-title">Average Hourly Distribution</span></h2>
        <div class="chart-header-right">
          <span class="peak-legend" title="Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window"><span class="peak-swatch"></span>Peak hours (PT)</span>
          <span class="chart-day-count" id="hourly-day-count"></span>
          <div class="tz-group">
            <button class="tz-btn" data-tz="local" onclick="setHourlyTZ('local')">Local</button>
            <button class="tz-btn" data-tz="utc"   onclick="setHourlyTZ('utc')">UTC</button>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chart-hourly"></canvas></div>
    </div>
    <div class="chart-card" id="sec-models" data-card="model-chart">
      <h2><span class="card-caret">&#9656;</span>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card" id="sec-projects" data-card="project-chart">
      <h2><span class="card-caret">&#9656;</span>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-subagents" data-card="subagent-chart">
      <h2><span class="card-caret">&#9656;</span><span id="subagent-chart-title">Subagent Tokens by Type</span></h2>
      <div class="chart-wrap"><canvas id="chart-subagent"></canvas></div>
    </div>
  </div>
  <div class="table-card" id="sec-cost-model" data-card="cost-by-model">
    <div class="section-title"><span class="card-caret">&#9656;</span><span id="model-table-title">Cost by Model</span></div>
    <table>
      <thead><tr>
        <th>Model</th>
        <th class="sortable" onclick="setModelSort('turns')">Turns <span class="sort-icon" id="msort-turns"></span></th>
        <th class="sortable" onclick="setModelSort('input')"><span class="input-token-label">Input</span> <span class="sort-icon" id="msort-input"></span></th>
        <th class="sortable" onclick="setModelSort('output')">Output <span class="sort-icon" id="msort-output"></span></th>
        <th class="sortable reasoning-col" id="reasoning-header" onclick="setModelSort('reasoning_output')">Reasoning <span class="sort-icon" id="msort-reasoning_output"></span></th>
        <th class="sortable" onclick="setModelSort('cache_read')"><span class="cache-read-label">Cache Read</span> <span class="sort-icon" id="msort-cache_read"></span></th>
        <th class="sortable" onclick="setModelSort('cache_creation')"><span class="cache-write-label">Cache Creation</span> <span class="sort-icon" id="msort-cache_creation"></span></th>
        <th class="sortable" onclick="setModelSort('cost')">Est. Cost <span class="sort-icon" id="msort-cost"></span></th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
    <div class="table-foot" id="model-cost-foot"></div>
  </div>
  <div class="table-card" id="sec-dispatches" data-card="dispatches">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Top Subagent Dispatches <span class="info-icon" tabindex="0" role="img" aria-label="About this table" title="Ranked by total tokens. &quot;unknown&quot; means the parent dispatch record wasn't found."><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg></span></div><button class="export-btn" onclick="exportDispatchesCSV()" title="Export all filtered subagent dispatches to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Type</th><th>Started</th><th>Model</th><th>Turns</th><th>Tool Uses</th>
        <th>Duration</th><th><span class="input-token-label">Input</span></th><th>Output</th><th><span class="cache-read-label">Cache Read</span></th><th>Tokens</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="dispatches-body"></tbody>
    </table>
    <div class="table-foot" id="dispatches-foot"></div>
  </div>
  <div class="table-card" id="sec-sessions" data-card="sessions">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Recent Sessions</div><button class="export-btn" onclick="exportSessionsCSV()" title="Export all filtered sessions to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Session</th>
        <th>Project</th>
        <th>Title</th>
        <th class="sortable" onclick="setSessionSort('last')">Last Active <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')">Duration <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th>Model</th>
        <th class="sortable" onclick="setSessionSort('turns')">Turns <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')"><span class="input-token-label">Input</span> <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')">Output <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')">Est. Cost <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
    <div class="table-foot" id="sessions-foot"></div>
  </div>
  <div class="table-card" id="sec-cost-project" data-card="cost-by-project">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Cost by Project</div><button class="export-btn" onclick="exportProjectsCSV()" title="Export all projects to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th class="sortable" onclick="setProjectSort('sessions')">Sessions <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')">Turns <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')"><span class="input-token-label">Input</span> <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')">Output <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')">Est. Cost <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
    <div class="table-foot" id="project-cost-foot"></div>
  </div>
  <div class="table-card" id="sec-cost-branch" data-card="cost-by-branch">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Cost by Project &amp; Branch</div><button class="export-btn" onclick="exportProjectBranchCSV()" title="Export project+branch breakdown to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th>Branch</th>
        <th class="sortable" onclick="setProjectBranchSort('sessions')">Sessions <span class="sort-icon" id="pbsort-sessions"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('turns')">Turns <span class="sort-icon" id="pbsort-turns"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('input')"><span class="input-token-label">Input</span> <span class="sort-icon" id="pbsort-input"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('output')">Output <span class="sort-icon" id="pbsort-output"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('cost')">Est. Cost <span class="sort-icon" id="pbsort-cost"></span></th>
      </tr></thead>
      <tbody id="project-branch-cost-body"></tbody>
    </table>
    <div class="table-foot" id="project-branch-cost-foot"></div>
  </div>
</div>
</div>

<div id="settings-panel" hidden>
  <div class="settings-wrap">
    <h1 class="settings-title">Settings</h1>
    <p class="settings-lede">Stored in <code id="settings-path">~/.claude/tokenscope-settings.json</code> and read by both the dashboard and the <code>tokenscope</code> CLI, so the two always agree.</p>
    <section class="settings-card" aria-labelledby="settings-sources-title">
      <h2 id="settings-sources-title">Providers</h2>
      <p class="settings-hint">A provider you switch off is hidden from the sidebar and skipped by every scan — its log directory is never read and its usage limits are never polled. At least one has to stay on.</p>
      <div id="settings-sources"></div>
    </section>

    <section class="settings-card" aria-labelledby="settings-pricing-title">
      <h2 id="settings-pricing-title">Model pricing</h2>
      <p class="settings-hint">USD per million tokens. These are the rates every cost on the dashboard and in the CLI is computed from. Editing a built-in rate overrides it; adding a model teaches this install about one that shipped after this release. A model with no entry costs nothing and shows as <code>n/a</code>.</p>
      <div id="settings-pricing"></div>
    </section>
  </div>

  <div class="settings-bar">
    <div class="settings-bar-inner">
      <span class="settings-status" id="settings-status">No changes yet.</span>
      <button class="btn-ghost" type="button" id="settings-discard" onclick="discardSettings()" disabled>Discard changes</button>
      <button class="btn-primary" type="button" id="settings-save" onclick="requestSettingsSave()" disabled>Save changes</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="confirm-modal" hidden role="dialog" aria-modal="true" aria-labelledby="confirm-title">
  <div class="modal">
    <h2 id="confirm-title">Save settings?</h2>
    <p id="confirm-body"></p>
    <ul id="confirm-list"></ul>
    <div class="modal-actions">
      <button class="btn-ghost" type="button" id="confirm-cancel">Cancel</button>
      <button class="btn-primary" type="button" id="confirm-accept">Save</button>
    </div>
  </div>
</div>

<footer>
  <div class="footer-content"><p id="footer-meta"></p></div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function quotaDuration(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return 'now';
  const totalMinutes = Math.ceil(ms / 60000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days) return days + 'd ' + hours + 'h';
  if (hours) return hours + 'h ' + minutes + 'm';
  return minutes + 'm';
}

function quotaResetText(resetAt) {
  if (!resetAt) return 'Reset time unavailable';
  const reset = new Date(resetAt);
  if (Number.isNaN(reset.getTime())) return 'Reset time unavailable';
  const relative = quotaDuration(reset.getTime() - Date.now());
  const clock = reset.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const day = reset.toLocaleDateString([], { month: 'short', day: 'numeric' });
  return relative === 'now' ? 'Resetting now' : 'Resets in ' + relative + ' · ' + day + ', ' + clock;
}

// How the panel labels its freshness, per snapshot origin. `local_usage` rows
// are spend totals we derived ourselves, not a plan reading from the provider.
const QUOTA_SOURCE_LABELS = {
  live_api: 'Live',
  live_api_stale: 'Last known',
  local_event: 'Local',
  local_usage: 'Measured',
};

// The remedy depends on what this machine can actually do: start the flow here,
// or say where to start it. Keyed on `needs_sign_in` alone so every state that
// a sign-in would fix offers one.
function signInAction(quota) {
  if (!quota || !quota.needs_sign_in) return '';
  return APP_CONFIG.canSignIn
    ? '<button class="quota-signin" type="button" onclick="startSignIn()">Sign in to Claude Code</button>'
    : '<span class="quota-authhint">Run <code>claude auth login</code> in your terminal, then check again.</span>'
      + '<button class="quota-signin" type="button" onclick="refreshQuota(true)">Check again</button>';
}

function renderQuota(quota) {
  const rows = document.getElementById('quota-rows');
  const updated = document.getElementById('quota-updated');
  const title = document.getElementById('quota-title');
  if (!rows) return;
  const windows = quota && Array.isArray(quota.windows) ? quota.windows : [];
  if (title) title.textContent = (quota && quota.title) || 'Usage remaining';
  if (!windows.length) {
    // Only a credential problem is user-fixable, so only that state gets an
    // action. Where the Claude Code CLI is present and we are on loopback, the
    // button runs the real sign-in; otherwise it can only re-poll, and says so.
    rows.innerHTML = '<div class="quota-empty"><strong>Usage unavailable</strong>'
      + esc((quota && quota.message) || 'No recent local quota signal.')
      + signInAction(quota) + '</div>';
    if (updated) updated.textContent = 'Unavailable';
    return;
  }
  let html = windows.map(window => {
    const percent = Number(window.remaining_percent);
    const value = Number.isFinite(percent) ? Math.round(percent) + '% left' : '\u2014';
    return '<div class="quota-row"><span class="quota-window">' + esc(window.label || window.key) + '</span>'
      + '<span class="quota-value">' + esc(value) + '</span>'
      + '<span class="quota-reset">' + esc(quotaResetText(window.reset_at)) + '</span></div>';
  }).join('');
  // A stale reading still shows its percentages; the note says why they are old.
  // The remedy belongs here too: an expired sign-in is just as fixable when the
  // panel still has old numbers to display as when it has none, and hanging the
  // button off an empty window list left the one state that always has windows
  // — a stale live reading — with nothing to press.
  if (quota.source === 'live_api_stale' && quota.message) {
    html += '<div class="quota-note">' + esc(quota.message) + signInAction(quota) + '</div>';
  }
  rows.innerHTML = html;
  if (updated) {
    const observed = quota.updated_at ? new Date(quota.updated_at) : null;
    const sourceLabel = QUOTA_SOURCE_LABELS[quota.source] || 'Local';
    updated.textContent = observed && !Number.isNaN(observed.getTime())
      ? sourceLabel + ' · ' + observed.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      : sourceLabel;
  }
}

// ── Sign-in ────────────────────────────────────────────────────────────────
// The dashboard never handles a token. It asks the server to run
// `claude auth login`, which owns the browser flow and writes its own
// credential; we only watch for the result. See quota.start_sign_in.
const SIGN_IN_POLL_MS = 1500;
const SIGN_IN_GIVE_UP_MS = 5 * 60 * 1000;
let signInPollTimer = null;

function controlHeaders() {
  return { 'Content-Type': 'application/json', 'X-Tokenscope-Control': APP_CONFIG.controlToken || '' };
}

function renderSignIn(state) {
  const rows = document.getElementById('quota-rows');
  if (!rows) return;
  const running = state.status === 'running';
  // The action keeps its name across the flow, so the button the user pressed
  // is still recognisably the same control while it works.
  const label = running ? 'Signing in\u2026' : 'Sign in to Claude Code';
  // Heading names the state, body says what to do about it — neither repeats
  // the other.
  const heading = running ? 'Waiting for your browser' : 'Sign-in didn\u2019t finish';
  const link = running && state.url
    ? '<a class="quota-authlink" href="' + esc(state.url) + '" target="_blank" rel="noopener">'
      + 'Open the sign-in page manually</a>'
    : '';
  rows.innerHTML = '<div class="quota-empty"><strong>' + esc(heading) + '</strong>'
    + esc(state.message || '')
    + '<button class="quota-signin" type="button" onclick="startSignIn()"' + (running ? ' disabled' : '')
    + '>' + label + '</button>' + link + '</div>';
}

async function startSignIn() {
  if (signInPollTimer) return;  // single-flight, mirroring the server's own lock
  try {
    const resp = await fetch('/api/signin', { method: 'POST', headers: controlHeaders() });
    const state = await resp.json();
    if (!resp.ok) {
      renderSignIn({ status: 'failed', message: state.error || 'Could not start sign-in.' });
      return;
    }
    renderSignIn(state);
    pollSignIn(Date.now());
  } catch (e) {
    renderSignIn({ status: 'failed', message: 'Could not reach the dashboard server.' });
  }
}

function pollSignIn(startedAt) {
  signInPollTimer = setTimeout(async () => {
    signInPollTimer = null;
    try {
      const resp = await fetch('/api/signin', { headers: controlHeaders() });
      const state = await resp.json();
      if (resp.ok && state.status === 'running') {
        renderSignIn(state);
        // A flow left open forever would poll forever; stop and let the user retry.
        if (Date.now() - startedAt < SIGN_IN_GIVE_UP_MS) pollSignIn(startedAt);
        else renderSignIn({ status: 'failed', message: 'Sign-in timed out. Try again.' });
        return;
      }
      if (resp.ok && state.status === 'ok') {
        refreshQuota(true);   // credential is good; let the panel repopulate
        return;
      }
      renderSignIn({ status: 'failed', message: (state && (state.message || state.error)) || 'Sign-in did not complete.' });
    } catch (e) {
      renderSignIn({ status: 'failed', message: 'Could not reach the dashboard server.' });
    }
  }, SIGN_IN_POLL_MS);
}

let quotaRequestInFlight = false;
async function refreshQuota(manual = true) {
  if (quotaRequestInFlight) return;
  const button = document.getElementById('quota-refresh');
  const updated = document.getElementById('quota-updated');
  const requestSource = selectedSource;
  quotaRequestInFlight = true;
  if (button) { button.disabled = true; button.classList.add('is-refreshing'); }
  if (manual && updated) updated.textContent = 'Refreshing';
  try {
    const refreshParam = '&refresh=' + (manual ? '1' : '0') + '&_=' + Date.now();
    const resp = await fetch('/api/data?source=' + encodeURIComponent(requestSource) + refreshParam, { cache: 'no-store' });
    const data = await resp.json();
    if (requestSource !== selectedSource) return;
    if (data.error) {
      if (manual && updated) updated.textContent = 'Unavailable';
      return;
    }
    if (rawData) rawData.quota = data.quota;
    renderQuota(data.quota);
  } catch(e) {
    if (manual && updated) updated.textContent = 'Refresh failed';
    console.error(e);
  } finally {
    quotaRequestInFlight = false;
    if (button) { button.disabled = false; button.classList.remove('is-refreshing'); }
  }
}

const SOURCE_LABELS = { claude_code: 'Claude Code', codex: 'Codex' };
const SOURCE_ORDER = ['claude_code', 'codex'];

// ── Settings state ─────────────────────────────────────────────────────────
// The server injects the stored settings alongside prices (see do_GET), so the
// first paint already knows which providers to show — no extra round trip, and
// no flash of a provider the user switched off. Read from window.APP_CONFIG
// rather than the APP_CONFIG const, which is declared further down and is still
// in its temporal dead zone while this line runs.
const SETTINGS_BOOT = (window.APP_CONFIG || {}).settings || {};

function cloneSettings(value) {
  return JSON.parse(JSON.stringify(value));
}

// Fill in anything the payload is missing so every later reader can assume the
// full shape. A provider flag is only off when it is explicitly false.
function normalizeSettings(raw) {
  const sources = (raw && raw.sources) || {};
  const overrides = (raw && raw.pricing_overrides) || {};
  const out = { sources: {}, pricing_overrides: {} };
  SOURCE_ORDER.forEach(src => {
    out.sources[src] = sources[src] !== false;
    const models = overrides[src] || {};
    out.pricing_overrides[src] = {};
    Object.keys(models).forEach(model => {
      out.pricing_overrides[src][model] = Object.assign({}, models[model]);
    });
  });
  if (!SOURCE_ORDER.some(src => out.sources[src])) {
    SOURCE_ORDER.forEach(src => { out.sources[src] = true; });
  }
  return out;
}

let savedSettings = normalizeSettings(SETTINGS_BOOT.settings);
let draftSettings = cloneSettings(savedSettings);

function enabledSourceList(from) {
  const resolved = from || savedSettings;
  const active = SOURCE_ORDER.filter(src => resolved.sources[src]);
  return active.length ? active : SOURCE_ORDER.slice();
}

function readURLSource() {
  const source = new URLSearchParams(window.location.search).get('source');
  const enabled = enabledSourceList();
  return enabled.indexOf(source) !== -1 ? source : enabled[0];
}

function readURLView() {
  return new URLSearchParams(window.location.search).get('view') === 'settings' ? 'settings' : 'dashboard';
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let currentView = readURLView();
let selectedSource = readURLSource();
let currentProvider = null;
let selectedModels = new Set();
let allModelsList = [];
let selectedRange = '30d';
let customRange = { start: null, end: null };
let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let branchSortCol = 'cost';
let branchSortDir = 'desc';
let lastFilteredSessions = [];
let lastByModel = [];
let lastByProject = [];
let lastByProjectBranch = [];
let lastFilteredDispatches = [];
let sessionSortDir = 'desc';

// Tables reveal rows in steps: 10 -> 25 -> 50, capped at 50 because rendering
// more than that visibly hurts performance. Past 50 the footer offers a
// "Download CSV to see more" link instead of another in-table step, plus a
// Show less button that resets straight back to 10. Limits persist across
// re-renders so sorting/filtering keeps the user's chosen depth (visible rows
// always reflect the active sort).
const TABLE_STEPS = [10, 25, 50];
const TABLE_MAX = TABLE_STEPS[TABLE_STEPS.length - 1];  // hard cap on in-table rows
// Don't paginate a table that barely exceeds the first step — paging away one or
// two rows just to show a "Show more" button is more annoying than helpful. Below
// this many rows a table always renders in full (no toggle).
const PAGINATE_THRESHOLD = 12;
function nextTableLimit(current, total) {
  for (const s of TABLE_STEPS) {
    if (s > current && s < total) return s;
  }
  return Math.min(total, TABLE_MAX);  // reveal everything, but never past the cap
}
// Rows to actually show: everything when the table is small enough to skip
// paging, otherwise the user's current step.
function shownCount(limit, total) {
  return total <= PAGINATE_THRESHOLD ? total : limit;
}
let modelLimit = TABLE_STEPS[0];
let sessionsLimit = TABLE_STEPS[0];
let projectLimit = TABLE_STEPS[0];
let branchLimit = TABLE_STEPS[0];
let dispatchesLimit = TABLE_STEPS[0];
let hourlyTZ = 'local';  // 'local' or 'utc'

function updateSourceTabs() {
  const enabled = enabledSourceList();
  document.querySelectorAll('.source-tab').forEach(tab => {
    const source = tab.id.slice('tab-'.length);
    tab.hidden = enabled.indexOf(source) === -1;
    const active = tab.id === 'tab-' + selectedSource;
    tab.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  // With a single provider there is nothing to switch between, so the strip is
  // just a row that can never change — hide it and let the title carry the name.
  const strip = document.querySelector('.source-tabs');
  if (strip) strip.hidden = enabled.length < 2;
}

// Called after a settings save: drop the selection if its provider just went
// away, and reload for whichever provider is left.
function applyEnabledSources() {
  const enabled = enabledSourceList();
  updateSourceTabs();
  if (enabled.indexOf(selectedSource) !== -1) return;
  selectedSource = enabled[0];
  rawData = null;
  currentProvider = null;
  selectedModels = new Set();
  allModelsList = [];
  updateProviderUI();
  updateURL();
  loadData();
}

function updateProviderUI() {
  const provider = currentProvider || { label: SOURCE_LABELS[selectedSource], capabilities: { reasoning_tokens: selectedSource === 'codex', subagents: selectedSource === 'claude_code' } };
  const capabilities = provider.capabilities || {};
  const supportsReasoning = capabilities.reasoning_tokens === true;
  const supportsSubagents = capabilities.subagents === true;
  const isCodex = selectedSource === 'codex';
  document.body.dataset.source = selectedSource;
  document.body.classList.toggle('provider-no-reasoning', !supportsReasoning);
  const title = document.getElementById('page-title');
  if (title) title.textContent = provider.label;
  document.title = 'TokenScope · ' + provider.label;
  const modelTableTitle = document.getElementById('model-table-title');
  if (modelTableTitle) modelTableTitle.textContent = supportsReasoning ? 'Usage by Model' : 'Cost by Model';
  const icon = document.querySelector('.header-icon');
  if (icon) icon.setAttribute('aria-label', 'TokenScope');
  document.querySelectorAll('#sec-subagents, #sec-dispatches, [data-target="sec-subagents"], [data-target="sec-dispatches"]').forEach(el => {
    el.hidden = !supportsSubagents;
  });
  document.querySelectorAll('.reasoning-col').forEach(el => { el.hidden = !supportsReasoning; });
  document.querySelectorAll('.input-token-label').forEach(el => { el.textContent = isCodex ? 'Uncached Input' : 'Input'; });
  document.querySelectorAll('.cache-read-label').forEach(el => { el.textContent = isCodex ? 'Cached Input' : 'Cache Read'; });
  document.querySelectorAll('.cache-write-label').forEach(el => { el.textContent = isCodex ? 'Cache Writes' : 'Cache Creation'; });
  const peakLegend = document.querySelector('.peak-legend');
  if (peakLegend) peakLegend.hidden = selectedSource !== 'claude_code';
  renderQuota(rawData && rawData.quota);
  updateSourceTabs();
}

async function setSource(source) {
  if (enabledSourceList().indexOf(source) === -1) return;
  await setView('dashboard');
  if (currentView !== 'dashboard') return;   // the user cancelled leaving Settings
  if (source === selectedSource) return;
  selectedSource = source;
  rawData = null;
  currentProvider = null;
  selectedModels = new Set();
  allModelsList = [];
  updateSourceTabs();
  updateProviderUI();
  updateURL();
  loadData();
}

// ── Peak-hour config ───────────────────────────────────────────────────────
// Anthropic throttles Mon–Fri 05:00–11:00 PT. We approximate as fixed UTC hours
// 12–17 (matches PDT; during PST the window shifts by 1h — accepted simplification).
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

// Local-timezone offset in hours (signed). Fractional offsets (e.g. India UTC+5:30)
// are rounded to the nearest hour for bucket alignment.
function localOffsetHours() {
  return Math.round(-new Date().getTimezoneOffset() / 60);
}

// Return the UTC hour (0–23) corresponding to a displayed-hour bucket.
function displayHourToUTC(displayHour, tzMode) {
  if (tzMode === 'utc') return displayHour;
  return ((displayHour - localOffsetHours()) % 24 + 24) % 24;
}

// Return the displayed-hour bucket for a UTC hour.
function utcHourToDisplay(utcHour, tzMode) {
  if (tzMode === 'utc') return utcHour;
  return ((utcHour + localOffsetHours()) % 24 + 24) % 24;
}

function isPeakHour(displayHour, tzMode) {
  return PEAK_HOURS_UTC.has(displayHourToUTC(displayHour, tzMode));
}

function formatHourLabel(h) {
  return String(h).padStart(2, '0') + ':00';
}

function tzDisplayName(tzMode) {
  if (tzMode === 'utc') return 'UTC';
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
  } catch(e) {
    return 'Local';
  }
}

// Prices come from pricing.py through APP_CONFIG. Keeping the browser table
// server-injected prevents the CLI and dashboard from drifting apart.
let PRICING = (window.APP_CONFIG || {}).pricing || {};

function isBillable(model) {
  return getPricing(model) !== null;
}

function getPricing(model) {
  if (!model) return null;
  const normalized = model.trim().toLowerCase();
  const sourcePricing = PRICING[selectedSource] || {};
  if (sourcePricing[normalized]) return sourcePricing[normalized];
  for (const key of Object.keys(sourcePricing)) {
    if (normalized.startsWith(key + '-')) return sourcePricing[key];
  }
  return null;
}

// Mirror of pricing.calc_cost's long-context overlay: crossing a family's
// documented prompt threshold reprices the whole request, so a row flagged
// long_context must not be billed at short-context rates.
function longContextPrice(p) {
  if (!p || p.long_context_threshold == null) return p;
  return Object.assign({}, p, {
    input:       p.long_input       != null ? p.long_input       : p.input,
    output:      p.long_output      != null ? p.long_output      : p.output,
    cache_read:  p.long_cache_read  != null ? p.long_cache_read  : p.cache_read,
    cache_write: p.long_cache_write != null ? p.long_cache_write : p.cache_write,
  });
}

function calcCost(model, inp, out, cacheRead, cacheCreation, longContext) {
  if (!isBillable(model)) return 0;
  let p = getPricing(model);
  if (!p) return 0;
  if (longContext) p = longContextPrice(p);
  if (selectedSource === 'codex') {
    const nonCachedInput = Math.max(inp - cacheRead, 0);
    const cacheWrites = Math.min(cacheCreation || 0, nonCachedInput);
    return ((nonCachedInput - cacheWrites) * p.input + out * p.output
      + cacheRead * p.cache_read + cacheWrites * p.cache_write) / 1e6;
  }
  return (inp * p.input + out * p.output + cacheRead * p.cache_read
    + cacheCreation * p.cache_write) / 1e6;
}

// Codex reports cached input inside input_tokens.  Cache writes, when they are
// present at all, are likewise part of prompt input rather than an extra token
// category.  Claude Code records these fields independently.
function uncachedInputTokens(input, cacheRead, cacheCreation = 0) {
  return selectedSource === 'codex' ? Math.max(input - cacheRead - cacheCreation, 0) : input;
}

function displayInputTokens(row) {
  return uncachedInputTokens(row.input, row.cache_read, row.cache_creation);
}

function rowCost(row) {
  return Number.isFinite(row.cost)
    ? row.cost
    : calcCost(row.model, row.input, row.output, row.cache_read, row.cache_creation,
               row.long_context);
}

function totalTokenCount(input, output, cacheRead, cacheCreation) {
  return selectedSource === 'codex'
    ? input + output
    : input + output + cacheRead + cacheCreation;
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }); }
function fmtCostBig(c) { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

// ── Chart colors ───────────────────────────────────────────────────────────
// Cool-neutral palette kept in sync with the CSS :root variables. The violet
// accent carries selected and cost states; secondary series stay quiet enough
// for long operational scans.
const C = {
  text:   '#F1F1F5',
  muted:  '#9494A3',
  axis:   '#A8A8B6',
  border: '#2A2A34',
  card:   '#17171D',
  blue:   '#7AA2F7',
  green:  '#75D39B',
  red:    '#ED7B72',
  accent: '#8B5CF6',
  amber:  '#F0B56B',
  purple: '#A78BFA',
  teal:   '#64D7CA',
  mauve:  '#E58BB8',
};
const TOKEN_COLORS = {
  input:          'rgba(122,162,247,0.88)',
  output:         'rgba(139,92,246,0.88)',
  cache_read:     'rgba(117,211,155,0.78)',
  cache_creation: 'rgba(240,181,107,0.78)',
};
// Hover lifts each series without introducing another visual language.
const TOKEN_HOVER = {
  input:          'rgba(154,184,255,1)',
  output:         'rgba(167,139,250,1)',
  cache_read:     'rgba(137,225,172,1)',
  cache_creation: 'rgba(251,202,127,1)',
};
const MODEL_COLORS = ['#8B5CF6','#7AA2F7','#64D7CA','#75D39B','#F0B56B','#E58BB8','#A78BFA','#ED7B72'];

// Subagent type swatches match the dashboard's cool-neutral palette.
const AGENT_TYPE_COLORS = {
  'general-purpose':   '#7AA2F7',
  'Explore':           '#A78BFA',
  'Plan':              '#F0B56B',
  'claude-code-guide': '#64D7CA',
  'auto-compact':      '#E58BB8',
  'unknown':           '#9494A3',
};
function colorForAgentType(t) { return AGENT_TYPE_COLORS[t] || '#75D39B'; }
function fmtDuration(ms) {
  if (!ms || ms < 0) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}m${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}m`;
}

// Tooltip color swatches: solid fill, no border (Chart.js's default draws a
// bordered box that looked offset/inconsistent). Lines use their solid stroke
// color instead of the translucent area fill.
Chart.defaults.color = C.axis;
// multiKeyBackground defaults to white and is drawn behind each tooltip swatch,
// peeking out as a thin white border on plain-box charts — make it transparent.
Chart.defaults.plugins.tooltip.multiKeyBackground = 'transparent';
Chart.defaults.plugins.tooltip.callbacks.labelColor = (ctx) => {
  const ds = ctx.dataset || {};
  let col = Array.isArray(ds.backgroundColor) ? ds.backgroundColor[ctx.dataIndex] : ds.backgroundColor;
  if (ds.type === 'line') col = ds.borderColor;
  return { borderColor: col, backgroundColor: col, borderWidth: 0 };
};

// Legend visibility must survive repaints (filter changes, auto-refresh, sort) —
// the charts are destroyed and rebuilt each render, which otherwise resets any
// series the user toggled off. We track hidden series by label per chart and
// reapply on rebuild: dataset charts via `dataset.hidden`, the doughnut via
// per-slice data visibility (see applyModelHidden).
const hiddenSeries = { daily: new Set(), hourly: new Set(), project: new Set(), model: new Set(), subagent: new Set() };
function legendToggle(key) {
  return (e, item, legend) => {
    const ci = legend.chart;
    const ds = ci.data.datasets[item.datasetIndex];
    ds.hidden = !ds.hidden;
    if (ds.hidden) hiddenSeries[key].add(ds.label); else hiddenSeries[key].delete(ds.label);
    ci.update();
  };
}

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'today': 'Today', 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time', 'custom': 'Custom range' };
const RANGE_TICKS  = { 'today': 1, 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12, 'custom': 15 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

// Local calendar date as YYYY-MM-DD. NOT toISOString(), which formats in UTC and
// shifts the day back in UTC+ timezones (that was the "This Month" bug, #151).
function localISODate(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = localISODate(new Date());
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function getRangeBounds(range) {
  if (range === 'custom') return customRange;
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = localISODate;
  if (range === 'today') {
    const t = iso(today);
    return { start: t, end: t };
  }
  if (range === 'week') {
    const day = today.getDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(today); mon.setDate(today.getDate() - diffToMon);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(today.getFullYear(), today.getMonth(), 1);
    const end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const end = new Date(today.getFullYear(), today.getMonth(), 0);
    return { start: iso(start), end: iso(end) };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  // Inclusive of today, so "Last 7 Days" covers 7 calendar days (today - 6),
  // not 8. Matches `python cli.py week`.
  d.setDate(d.getDate() - (days - 1));
  return { start: iso(d), end: null };
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  if (p !== 'custom') return VALID_RANGES.includes(p) ? p : '30d';
  const params = new URLSearchParams(window.location.search);
  const start = params.get('start');
  const end = params.get('end');
  if (!isValidDateRange(start, end)) return '30d';
  customRange = { start, end };
  return 'custom';
}

function setRange(range) {
  if (!VALID_RANGES.includes(range) || (range === 'custom' && !isValidDateRange(customRange.start, customRange.end))) return;
  selectedRange = range;
  updateRangeTrigger();
  hideCustomRangeForm();
  closeRangePanel();
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
}

function isValidDateRange(start, end) {
  return /^\d{4}-\d{2}-\d{2}$/.test(start || '') && /^\d{4}-\d{2}-\d{2}$/.test(end || '') && start <= end;
}

function customRangeLabel() {
  return customRange.start && customRange.end ? `${customRange.start} – ${customRange.end}` : RANGE_LABELS.custom;
}

function updateRangeTrigger() {
  const label = document.getElementById('range-trigger-label');
  if (label) label.textContent = selectedRange === 'custom' ? customRangeLabel() : RANGE_LABELS[selectedRange];
  document.querySelectorAll('.range-option').forEach(option =>
    option.classList.toggle('selected', option.dataset.range === selectedRange)
  );
}

function toggleRangePanel(event) {
  event.stopPropagation();
  const panel = document.getElementById('range-panel');
  const trigger = document.getElementById('range-trigger');
  if (!panel || !trigger) return;
  const opening = panel.hidden;
  closeModelPanel();
  panel.hidden = !opening;
  trigger.classList.toggle('open', opening);
  trigger.setAttribute('aria-expanded', String(opening));
  if (opening) updateRangeTrigger();
}

function closeRangePanel() {
  const panel = document.getElementById('range-panel');
  const trigger = document.getElementById('range-trigger');
  if (panel) panel.hidden = true;
  if (trigger) { trigger.classList.remove('open'); trigger.setAttribute('aria-expanded', 'false'); }
}

function showCustomRangeForm() {
  const form = document.getElementById('custom-range-form');
  const start = document.getElementById('custom-range-start');
  const end = document.getElementById('custom-range-end');
  const error = document.getElementById('custom-range-error');
  if (!form || !start || !end) return;
  start.value = customRange.start || '';
  end.value = customRange.end || '';
  if (error) error.textContent = '';
  form.hidden = false;
  start.focus();
}

function hideCustomRangeForm() {
  const form = document.getElementById('custom-range-form');
  const error = document.getElementById('custom-range-error');
  if (form) form.hidden = true;
  if (error) error.textContent = '';
}

function applyCustomRange(event) {
  event.preventDefault();
  const start = document.getElementById('custom-range-start').value;
  const end = document.getElementById('custom-range-end').value;
  const error = document.getElementById('custom-range-error');
  if (!isValidDateRange(start, end)) {
    error.textContent = start && end ? 'The end date must be on or after the start date.' : 'Choose both a start and end date.';
    return;
  }
  customRange = { start, end };
  setRange('custom');
}

function setHourlyTZ(mode) {
  hourlyTZ = mode;
  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tz === mode)
  );
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('fable') || ml.includes('mythos')) return 0;
  if (ml.includes('opus'))   return 1;
  if (ml.includes('sonnet')) return 2;
  if (ml.includes('haiku'))  return 3;
  return 4;
}

function sortedModels(models) {
  return [...models].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
}

// Compact display name for the collapsed trigger, e.g. "claude-opus-4-8" ->
// "Opus 4.8", "claude-fable-5" -> "Fable 5". Non-Anthropic ids fall back to the
// basename with any provider prefix and trailing date suffix stripped.
function shortModelName(m) {
  const ml = m.toLowerCase();
  let family = null;
  if (ml.includes('fable'))       family = 'Fable';
  else if (ml.includes('mythos')) family = 'Mythos';
  else if (ml.includes('opus'))   family = 'Opus';
  else if (ml.includes('sonnet')) family = 'Sonnet';
  else if (ml.includes('haiku'))  family = 'Haiku';
  if (family) {
    const two = m.match(/(\d+)[._-](\d+)/);
    if (two) return family + ' ' + two[1] + '.' + two[2];
    const one = m.match(/(\d+)/);
    return one ? family + ' ' + one[1] : family;
  }
  let base = m.split('/').pop().split(':')[0];
  base = base.replace(/[-_]?\d{6,}.*$/, '');
  return base || m;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) {
    const billable = allModels.filter(m => isBillable(m));
    // Fallback: if the user only has non-billable / unknown models (e.g. all
    // local-LLM runs), default to all models so the dashboard isn't blank.
    return new Set(billable.length ? billable : allModels);
  }
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  const expected = billable.length ? billable : allModels;
  if (selectedModels.size !== expected.length) return false;
  return expected.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  allModelsList = [...allModels];
  selectedModels = readURLModels(allModels);
  const sorted = sortedModels(allModels);
  const anthropic = sorted.filter(m => isBillable(m));
  const other     = sorted.filter(m => !isBillable(m));
  const rowHTML = m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}" title="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      <span class="model-cb-box">&#10003;</span>
      <span class="model-cb-text">${esc(m)}</span>
    </label>`;
  };
  let html = '';
  // Only show a group heading when both groups are present — a single-group
  // list doesn't need a label.
  const labelled = anthropic.length && other.length;
  if (anthropic.length) {
    if (labelled) html += '<div class="model-group-label">Anthropic</div>';
    html += anthropic.map(rowHTML).join('');
  }
  if (other.length) {
    if (labelled) html += '<div class="model-group-label">Other providers</div>';
    html += other.map(rowHTML).join('');
  }
  document.getElementById('model-checkboxes').innerHTML = html;
  updateModelTriggerLabel();
}

// Collapsed trigger text, in priority order:
//   "All models"     — everything selected
//   "No models"      — nothing selected
//   "All Anthropic"  — every Anthropic model (opus/sonnet/haiku/mythos/fable)
//                      selected and no other provider; "+N" if some others too
//   "Fable 5, Opus 4.7 +5" — otherwise, first two names + overflow count
function updateModelTriggerLabel() {
  const labelEl = document.getElementById('model-trigger-label');
  if (!labelEl) return;
  const n = selectedModels.size;
  if (n === 0)                    { labelEl.textContent = 'No models';  return; }
  if (n === allModelsList.length) { labelEl.textContent = 'All models'; return; }
  const anthropic = allModelsList.filter(m => isBillable(m));
  const others    = allModelsList.filter(m => !isBillable(m));
  if (anthropic.length && anthropic.every(m => selectedModels.has(m))) {
    // n < total (handled above), so when others exist at least one is unselected.
    const otherSel = others.filter(m => selectedModels.has(m)).length;
    labelEl.textContent = otherSel ? 'All Anthropic +' + otherSel : 'All Anthropic';
    return;
  }
  const chosen = sortedModels(allModelsList).filter(m => selectedModels.has(m));
  const shown = chosen.slice(0, 2).map(shortModelName);
  const extra = chosen.length - shown.length;
  labelEl.textContent = shown.join(', ') + (extra > 0 ? ' +' + extra : '');
}

function toggleModelPanel(event) {
  if (event) event.stopPropagation();
  const panel = document.getElementById('model-panel');
  const trigger = document.getElementById('model-trigger');
  const open = panel.hidden;
  panel.hidden = !open;
  trigger.classList.toggle('open', open);
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeModelPanel() {
  const panel = document.getElementById('model-panel');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  const trigger = document.getElementById('model-trigger');
  trigger.classList.remove('open');
  trigger.setAttribute('aria-expanded', 'false');
}

// Close the panel on outside click or Escape. Clicks inside #model-select
// (including the checkboxes and All/None) keep it open so multiple models can
// be toggled in one pass.
document.addEventListener('click', (e) => {
  const sel = document.getElementById('model-select');
  if (sel && !sel.contains(e.target)) closeModelPanel();
  const range = document.getElementById('range-select');
  if (range && !range.contains(e.target)) closeRangePanel();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeModelPanel(); closeRangePanel(); }
});

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateModelTriggerLabel();
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (currentView === 'settings') params.set('view', 'settings');
  if (selectedSource !== 'claude_code') params.set('source', selectedSource);
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (selectedRange === 'custom') {
    params.set('start', customRange.start);
    params.set('end', customRange.end);
  }
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Session sort ───────────────────────────────────────────────────────────
function setSessionSort(col) {
  if (sessionSortCol === col) {
    sessionSortDir = sessionSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    sessionSortCol = col;
    sessionSortDir = 'desc';
  }
  updateSortIcons();
  applyFilter();
}

function updateSortIcons() {
  document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
  const icon = document.getElementById('sort-icon-' + sessionSortCol);
  if (icon) icon.textContent = sessionSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortSessions(sessions) {
  return [...sessions].sort((a, b) => {
    let av, bv;
    if (sessionSortCol === 'cost') {
      av = rowCost(a);
      bv = rowCost(b);
    } else if (sessionSortCol === 'duration_min') {
      av = parseFloat(a.duration_min) || 0;
      bv = parseFloat(b.duration_min) || 0;
    } else {
      av = a[sessionSortCol] ?? 0;
      bv = b[sessionSortCol] ?? 0;
    }
    if (av < bv) return sessionSortDir === 'desc' ? 1 : -1;
    if (av > bv) return sessionSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const { start, end } = getRangeBounds(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0, reasoning_output: 0, cost: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
    d.reasoning_output += r.reasoning_output || 0;
    d.cost           += rowCost(r);
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, reasoning_output: 0, turns: 0, sessions: 0, cost: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.reasoning_output += r.reasoning_output || 0;
    m.turns          += r.turns;
    m.cost           += rowCost(r);
  }

  // Filter sessions by model + date range
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!start || s.last_date >= start) && (!end || s.last_date <= end)
  );

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const p = projMap[s.project];
    p.input          += s.input;
    p.output         += s.output;
    p.cache_read     += s.cache_read;
    p.cache_creation += s.cache_creation;
    p.turns          += s.turns;
    p.sessions++;
    p.cost += rowCost(s);
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project+branch: aggregate from filtered sessions
  const projBranchMap = {};
  for (const s of filteredSessions) {
    const key = s.project + '\x00' + (s.branch || '');
    if (!projBranchMap[key]) projBranchMap[key] = { project: s.project, branch: s.branch || '', input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const pb = projBranchMap[key];
    pb.input          += s.input;
    pb.output         += s.output;
    pb.cache_read     += s.cache_read;
    pb.cache_creation += s.cache_creation;
    pb.turns          += s.turns;
    pb.sessions++;
    pb.cost += rowCost(s);
  }
  const byProjectBranch = Object.values(projBranchMap).sort((a, b) => b.cost - a.cost);

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + rowCost(m), 0),
    subagent_tokens: (rawData.subagent_by_type || [])
      .filter(r => selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end))
      .reduce((s, r) => s + totalTokenCount(r.input, r.output, r.cache_read, r.cache_creation), 0),
    reasoning_output: filteredDaily.reduce((s, r) => s + (r.reasoning_output || 0), 0),
  };

  // Hourly aggregation (filtered by model + range, then bucketed by UTC hour)
  const hourlySrc = (rawData.hourly_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  const hourlyAgg = aggregateHourly(hourlySrc, hourlyTZ);

  // Subagent breakdown by type (filtered by range + selected models)
  const subagentTypeMap = {};
  for (const r of (rawData.subagent_by_type || [])) {
    if (!selectedModels.has(r.model)) continue;
    if (start && r.day < start) continue;
    if (end && r.day > end) continue;
    const k = r.agent_type;
    if (!subagentTypeMap[k]) subagentTypeMap[k] = { agent_type: k, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0 };
    const m = subagentTypeMap[k];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
    m.turns += r.turns;
  }
  const byAgentType = Object.values(subagentTypeMap).sort((a, b) =>
    totalTokenCount(b.input, b.output, b.cache_read, b.cache_creation) -
    totalTokenCount(a.input, a.output, a.cache_read, a.cache_creation));

  // Top dispatches: filter by range + selected model. Keep the full filtered set
  // (already ranked by tokens server-side) so the table can page it like Recent
  // Sessions — show more/less plus CSV export of everything.
  const filteredDispatches = (rawData.top_dispatches || []).filter(d =>
    selectedModels.has(d.model) && (!start || d.start_date >= start) && (!end || d.start_date <= end)
  );

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('hourly-chart-title').textContent = 'Average Hourly Distribution \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('subagent-chart-title').textContent = 'Subagent Tokens by Type \u2014 ' + RANGE_LABELS[selectedRange];

  updateProviderUI();
  renderStats(totals);
  renderDailyChart(daily);
  renderHourlyChart(hourlyAgg);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderSubagentChart(byAgentType);
  lastFilteredDispatches = filteredDispatches;
  renderTopDispatches(lastFilteredDispatches);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByModel = byModel;
  lastByProject = sortProjects(byProject);
  lastByProjectBranch = sortProjectBranch(byProjectBranch);
  renderSessionsTable(lastFilteredSessions);
  renderModelCostTable(lastByModel);
  renderProjectCostTable(lastByProject);
  renderProjectBranchCostTable(lastByProjectBranch);
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
  const provider = currentProvider || {};
  const capabilities = provider.capabilities || {};
  const codexStats = selectedSource === 'codex';
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    ...(codexStats ? [
      { label: 'Prompt Tokens', value: fmt(t.input), sub: 'includes cached input' },
      { label: 'Uncached Input', value: fmt(uncachedInputTokens(t.input, t.cache_read, t.cache_creation)), sub: 'prompt tokens not served from cache' },
    ] : [{ label: 'Input Tokens', value: fmt(t.input), sub: rangeLabel }]),
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: codexStats ? 'Cached Input' : 'Cache Read', value: fmt(t.cache_read), sub: codexStats ? 'included in prompt tokens' : 'from prompt cache' },
    { label: codexStats ? 'Cache Writes' : 'Cache Creation', value: fmt(t.cache_creation), sub: codexStats
      ? (t.cache_creation ? 'reported by local Codex logs; included in prompt tokens' : 'not reported by local Codex logs')
      : 'writes to prompt cache' },
    ...(capabilities.reasoning_tokens ? [{ label: 'Reasoning Tokens', value: fmt(t.reasoning_output || 0), sub: 'included in output' }] : []),
    ...(capabilities.subagents ? [{ label: 'Subagent Tokens', value: fmt(t.subagent_tokens || 0), sub: 'included in totals' }] : []),
    { label: 'Est. Cost',      value: isBillableModelPresent(t) ? fmtCostBig(t.cost) : 'n/a', sub: provider.pricing_basis || 'Provider pricing', color: isBillableModelPresent(t) ? C.green : null },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

function isBillableModelPresent(t) {
  return t.cost > 0 || (rawData && rawData.all_models
    && rawData.all_models.some(model => selectedModels.has(model) && isBillable(model)));
}

// Bucket rows into 24 hours (display-TZ), summing turns + output, and count
// the unique days in the input so the caller can compute per-day averages.
function aggregateHourly(rows, tzMode) {
  const byHour = {};
  for (let h = 0; h < 24; h++) byHour[h] = { turns: 0, output: 0 };
  const days = new Set();
  for (const r of rows) {
    // The server sends both frames. Use the hour and the day from the *same*
    // frame — pairing a local day with a UTC hour shifted edge-of-day rows into
    // the wrong bucket, and the server's local hour is exact for half-hour
    // offsets that localOffsetHours() has to round.
    const displayHour = tzMode === 'utc'
      ? r.hour
      : (r.hour_local != null ? r.hour_local : utcHourToDisplay(r.hour, tzMode));
    const day = tzMode === 'utc' ? (r.day_utc || r.day) : r.day;
    byHour[displayHour].turns  += r.turns  || 0;
    byHour[displayHour].output += r.output || 0;
    if (day) days.add(day);
  }
  const dayCount = days.size;
  const hours = [];
  for (let h = 0; h < 24; h++) {
    hours.push({
      hour:       h,
      avgTurns:   dayCount ? byHour[h].turns  / dayCount : 0,
      avgOutput:  dayCount ? byHour[h].output / dayCount : 0,
      totalTurns: byHour[h].turns,
      peak:       isPeakHour(h, tzMode),
    });
  }
  return { hours, dayCount };
}

function renderHourlyChart(agg) {
  const dayCountEl = document.getElementById('hourly-day-count');
  dayCountEl.textContent = agg.dayCount
    ? agg.dayCount + ' day' + (agg.dayCount === 1 ? '' : 's') + ' averaged · ' + tzDisplayName(hourlyTZ)
    : 'No data · ' + tzDisplayName(hourlyTZ);

  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  const barColors      = agg.hours.map(h => h.peak ? 'rgba(237,123,114,0.9)' : TOKEN_COLORS.input);
  const barHoverColors = agg.hours.map(h => h.peak ? 'rgba(255,147,138,1)'   : TOKEN_HOVER.input);

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          label: 'Avg turns / hour',
          hidden: hiddenSeries.hourly.has('Avg turns / hour'),
          data: turns,
          backgroundColor: barColors,
          hoverBackgroundColor: barHoverColors,
          pointStyle: 'rect',
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'Avg output tokens / hour',
          hidden: hiddenSeries.hourly.has('Avg output tokens / hour'),
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: 'rgba(217,119,87,0.15)',
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: TOKEN_HOVER.output,
          pointStyle: 'circle',
          pointBackgroundColor: TOKEN_COLORS.output,
          pointBorderColor: TOKEN_COLORS.output,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { onClick: legendToggle('hourly'), labels: { color: C.axis, usePointStyle: true, boxWidth: 8, boxHeight: 8 } },
        tooltip: {
          usePointStyle: true,
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(hourlyTZ);
              return h.peak ? base + ' · Peak — Anthropic US hours' : base;
            },
            label: (item) => {
              if (item.dataset.label && item.dataset.label.indexOf('turns') !== -1) {
                return ' Avg turns: ' + item.parsed.y.toFixed(2);
              }
              return ' Avg output: ' + fmt(item.parsed.y);
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: C.axis, maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: { color: C.border } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: C.axis, callback: v => v.toFixed(1) },     grid: { color: C.border }, title: { display: true, text: 'Avg turns / hour',         color: C.axis, font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: true, text: 'Avg output tokens / hour', color: C.axis, font: { size: 11 } } },
      }
    }
  });
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  const codexStats = selectedSource === 'codex';
  const inputLabel = codexStats ? 'Uncached Input' : 'Input';
  const cacheReadLabel = codexStats ? 'Cached Input' : 'Cache Read';
  const tokenDatasets = [
    { label: inputLabel, hidden: hiddenSeries.daily.has(inputLabel), data: daily.map(d => uncachedInputTokens(d.input, d.cache_read, d.cache_creation)), backgroundColor: TOKEN_COLORS.input, hoverBackgroundColor: TOKEN_HOVER.input, stack: 'io', yAxisID: 'y1' },
    { label: 'Output', hidden: hiddenSeries.daily.has('Output'), data: daily.map(d => d.output), backgroundColor: TOKEN_COLORS.output, hoverBackgroundColor: TOKEN_HOVER.output, stack: 'io', yAxisID: 'y1' },
    { label: cacheReadLabel, hidden: hiddenSeries.daily.has(cacheReadLabel), data: daily.map(d => d.cache_read), backgroundColor: TOKEN_COLORS.cache_read, hoverBackgroundColor: TOKEN_HOVER.cache_read, stack: 'cache', yAxisID: 'y' },
  ];
  if (!codexStats) {
    tokenDatasets.push({ label: 'Cache Creation', hidden: hiddenSeries.daily.has('Cache Creation'), data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'cache', yAxisID: 'y' });
  }
  tokenDatasets.push({ type: 'line', label: 'Est. Cost', hidden: hiddenSeries.daily.has('Est. Cost'), data: daily.map(d => d.cost), borderColor: C.accent, backgroundColor: 'transparent', pointBackgroundColor: C.accent, pointRadius: 3, tension: 0.3, yAxisID: 'y2' });
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: tokenDatasets
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('daily'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: item => item.dataset.label === 'Est. Cost'
            ? ` Est. Cost: ${fmtCost(item.raw)}`
            : ` ${item.dataset.label}: ${fmt(item.raw)}`
        }}
      },
      scales: {
        x:  { ticks: { color: C.axis, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: C.border } },
        y:  { position: 'left',  ticks: { color: C.green,  callback: v => fmt(v) },         grid: { color: C.border },          title: { display: true, text: codexStats ? 'Cached Input' : 'Cache', color: C.green } },
        y1: { position: 'right', ticks: { color: C.blue,   callback: v => fmt(v) },         grid: { drawOnChartArea: false },    title: { display: true, text: codexStats ? 'Uncached Input / Output' : 'Input / Output', color: C.blue } },
        y2: { position: 'right', ticks: { color: C.accent, callback: v => '$' + v.toFixed(2) }, grid: { drawOnChartArea: false }, title: { display: true, text: 'Est. Cost', color: C.accent }, offset: true },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, hoverBackgroundColor: MODEL_COLORS, hoverOffset: 8, borderWidth: 2, borderColor: C.card, hoverBorderColor: C.card }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: C.axis, boxWidth: 12, font: { size: 11 } },
          onClick: (e, item, legend) => {
            const ci = legend.chart;
            ci.toggleDataVisibility(item.index);
            const label = ci.data.labels[item.index];
            if (!ci.getDataVisibility(item.index)) hiddenSeries.model.add(label); else hiddenSeries.model.delete(label);
            ci.update();
          },
        },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
  // Reapply any slices the user toggled off in a previous render.
  byModel.forEach((m, i) => {
    if (hiddenSeries.model.has(m.model) && charts.model.getDataVisibility(i)) charts.model.toggleDataVisibility(i);
  });
  charts.model.update();
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: selectedSource === 'codex' ? 'Uncached Input' : 'Input', hidden: hiddenSeries.project.has(selectedSource === 'codex' ? 'Uncached Input' : 'Input'), data: top.map(p => displayInputTokens(p)), backgroundColor: TOKEN_COLORS.input, hoverBackgroundColor: TOKEN_HOVER.input },
        { label: 'Output', hidden: hiddenSeries.project.has('Output'), data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output, hoverBackgroundColor: TOKEN_HOVER.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: { legend: { onClick: legendToggle('project'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

function renderSubagentChart(byType) {
  const ctx = document.getElementById('chart-subagent').getContext('2d');
  if (charts.subagent) charts.subagent.destroy();
  if (!byType.length) { charts.subagent = null; return; }
  charts.subagent = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: byType.map(t => t.agent_type),
      datasets: [
        { label: 'Input',          hidden: hiddenSeries.subagent.has('Input'),          data: byType.map(t => t.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'tokens' },
        { label: 'Output',         hidden: hiddenSeries.subagent.has('Output'),         data: byType.map(t => t.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'tokens' },
        { label: 'Cache Read',     hidden: hiddenSeries.subagent.has('Cache Read'),     data: byType.map(t => t.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', hidden: hiddenSeries.subagent.has('Cache Creation'), data: byType.map(t => t.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('subagent'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: ctx => ` ${ctx.dataset.label}: ${fmt(ctx.raw)}`,
          footer: items => {
            const total = items.reduce((s, it) => s + it.raw, 0);
            const row = byType[items[0].dataIndex];
            return ` Total: ${fmt(total)} · ${row.turns} turns`;
          }
        } }
      },
      scales: {
        x: { stacked: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { stacked: true, ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

function renderTopDispatches(rows) {
  const body = document.getElementById('dispatches-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="11" class="muted" style="text-align:center;padding:24px">No subagent dispatches in selected range.</td></tr>';
    renderTableToggle('dispatches-foot', 0, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
    return;
  }
  const shown = rows.slice(0, shownCount(dispatchesLimit, rows.length));
  body.innerHTML = shown.map(d => {
    const tokensTotal = totalTokenCount(d.input, d.output, d.cache_read, d.cache_creation);
    const cost = calcCost(d.model, d.input, d.output, d.cache_read, d.cache_creation);
    const costCell = isBillable(d.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const col = colorForAgentType(d.agent_type);
    const typeStyle = `background:${col}22;color:${col};border:1px solid ${col}44`;
    return `<tr>
      <td><span class="model-tag" style="${typeStyle}">${esc(d.agent_type)}</span></td>
      <td class="muted">${esc(d.start || '—')}</td>
      <td><span class="model-tag">${esc(d.model)}</span></td>
      <td class="num">${d.turns}</td>
      <td class="num">${d.tool_uses != null ? d.tool_uses : '—'}</td>
      <td class="muted">${fmtDuration(d.duration_ms)}</td>
      <td class="num">${fmt(d.input)}</td>
      <td class="num">${fmt(d.output)}</td>
      <td class="num">${fmt(d.cache_read)}</td>
      <td class="num"><strong>${fmt(tokensTotal)}</strong></td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('dispatches-foot', rows.length, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
}

// Fills a table card's footer with the row-reveal control. Three states:
//   - more rows fit under the cap        -> "Show more" (plus "Show less" once expanded)
//   - cap reached but more records exist -> "Download CSV to see all (N)" + "Show less"
//   - every row is already visible       -> "Show less"
// "Show less" is hidden at the initial step (nothing to collapse yet). Renders
// nothing when the whole table fits in the first step. Carets: more = down (▾),
// less = up (▴).
function renderTableToggle(footId, total, limit, lessName, moreName, csvName) {
  const foot = document.getElementById(footId);
  if (!foot) return;
  if (total <= PAGINATE_THRESHOLD) { foot.innerHTML = ''; return; }
  const less = '<button class="show-more-btn" onclick="' + lessName + '()">Show less ▴</button>';
  const more = '<button class="show-more-btn" onclick="' + moreName + '()">Show more ▾</button>';
  let html;
  if (limit < total && limit < TABLE_MAX) {
    // more rows fit under the cap; Show less only once we're past the first step
    html = (limit > TABLE_STEPS[0] ? less : '') + more;
  } else if (limit < total) {           // cap reached, remaining rows only via CSV
    html = '<a class="show-more-link" href="#" onclick="' + csvName + '(); return false;">Download CSV to see all (' + total + ')</a>' + less;
  } else {                              // everything already visible
    html = less;
  }
  foot.innerHTML = html;
}

// After collapsing a table, bring its top back into view — the user may have
// scrolled down through the expanded rows.
function scrollTableToTop(bodyId) {
  const card = document.getElementById(bodyId)?.closest('.table-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// "Show more" advances one step (capped at TABLE_MAX); "Show less" resets to the
// first step and scrolls back to the top of that table.
function moreModelRows()   { modelLimit    = nextTableLimit(modelLimit,    lastByModel.length);        renderModelCostTable(lastByModel); }
function lessModelRows()   { modelLimit    = TABLE_STEPS[0]; renderModelCostTable(lastByModel);            scrollTableToTop('model-cost-body'); }
function moreSessionRows() { sessionsLimit = nextTableLimit(sessionsLimit, lastFilteredSessions.length); renderSessionsTable(lastFilteredSessions); }
function lessSessionRows() { sessionsLimit = TABLE_STEPS[0]; renderSessionsTable(lastFilteredSessions);    scrollTableToTop('sessions-body'); }
function moreProjectRows() { projectLimit  = nextTableLimit(projectLimit,  lastByProject.length);       renderProjectCostTable(lastByProject); }
function lessProjectRows() { projectLimit  = TABLE_STEPS[0]; renderProjectCostTable(lastByProject);        scrollTableToTop('project-cost-body'); }
function moreBranchRows()  { branchLimit   = nextTableLimit(branchLimit,   lastByProjectBranch.length); renderProjectBranchCostTable(lastByProjectBranch); }
function lessBranchRows()  { branchLimit   = TABLE_STEPS[0]; renderProjectBranchCostTable(lastByProjectBranch); scrollTableToTop('project-branch-cost-body'); }
function moreDispatchRows(){ dispatchesLimit = nextTableLimit(dispatchesLimit, lastFilteredDispatches.length); renderTopDispatches(lastFilteredDispatches); }
function lessDispatchRows(){ dispatchesLimit = TABLE_STEPS[0]; renderTopDispatches(lastFilteredDispatches);            scrollTableToTop('dispatches-body'); }

function renderSessionsTable(sessions) {
  const shown = sessions.slice(0, shownCount(sessionsLimit, sessions.length));
  document.getElementById('sessions-body').innerHTML = shown.map(s => {
    const cost = rowCost(s);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const titleCell = s.topic
      ? `<td class="topic-cell" title="${esc(s.topic)}">${esc(s.topic)}</td>`
      : `<td class="topic-cell"><span class="untitled">Untitled</span></td>`;
    return `<tr>
      <td class="muted" style="font-family:monospace">${esc(s.session_id.slice(0, 8))}&hellip;</td>
      <td>${esc(s.project)}</td>
      ${titleCell}
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(displayInputTokens(s))}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('sessions-foot', sessions.length, sessionsLimit, 'lessSessionRows', 'moreSessionRows', 'exportSessionsCSV');
}

function setModelSort(col) {
  if (modelSortCol === col) {
    modelSortDir = modelSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    modelSortCol = col;
    modelSortDir = 'desc';
  }
  updateModelSortIcons();
  applyFilter();
}

function updateModelSortIcons() {
  document.querySelectorAll('[id^="msort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('msort-' + modelSortCol);
  if (icon) icon.textContent = modelSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortModels(byModel) {
  return [...byModel].sort((a, b) => {
    let av, bv;
    if (modelSortCol === 'cost') {
      av = rowCost(a);
      bv = rowCost(b);
    } else {
      av = a[modelSortCol] ?? 0;
      bv = b[modelSortCol] ?? 0;
    }
    if (av < bv) return modelSortDir === 'desc' ? 1 : -1;
    if (av > bv) return modelSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderModelCostTable(byModel) {
  const sorted = sortModels(byModel);
  const shown = sorted.slice(0, shownCount(modelLimit, sorted.length));
  document.getElementById('model-cost-body').innerHTML = shown.map(m => {
    const cost = rowCost(m);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(displayInputTokens(m))}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num reasoning-col">${fmt(m.reasoning_output || 0)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('model-cost-foot', sorted.length, modelLimit, 'lessModelRows', 'moreModelRows', 'exportModelCSV');
}

// ── Project cost table sorting ────────────────────────────────────────────
function setProjectSort(col) {
  if (projectSortCol === col) {
    projectSortDir = projectSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    projectSortCol = col;
    projectSortDir = 'desc';
  }
  updateProjectSortIcons();
  applyFilter();
}

function updateProjectSortIcons() {
  document.querySelectorAll('[id^="psort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('psort-' + projectSortCol);
  if (icon) icon.textContent = projectSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjects(byProject) {
  return [...byProject].sort((a, b) => {
    const av = a[projectSortCol] ?? 0;
    const bv = b[projectSortCol] ?? 0;
    if (av < bv) return projectSortDir === 'desc' ? 1 : -1;
    if (av > bv) return projectSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectCostTable(byProject) {
  const sorted = sortProjects(byProject);
  const shown = sorted.slice(0, shownCount(projectLimit, sorted.length));
  document.getElementById('project-cost-body').innerHTML = shown.map(p => {
    return `<tr>
      <td>${esc(p.project)}</td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(displayInputTokens(p))}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="cost">${fmtCost(p.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-cost-foot', sorted.length, projectLimit, 'lessProjectRows', 'moreProjectRows', 'exportProjectsCSV');
}

// ── Project+Branch cost table sorting ────────────────────────────────────
function setProjectBranchSort(col) {
  if (branchSortCol === col) {
    branchSortDir = branchSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    branchSortCol = col;
    branchSortDir = 'desc';
  }
  updateProjectBranchSortIcons();
  applyFilter();
}

function updateProjectBranchSortIcons() {
  document.querySelectorAll('[id^="pbsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('pbsort-' + branchSortCol);
  if (icon) icon.textContent = branchSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjectBranch(rows) {
  // Sort by the selected column (default: cost desc), consistent with the Cost by
  // Model / Cost by Project tables. Project name is only a stable tiebreaker when
  // the sorted column ties, so a project's branches stay grouped & deterministic
  // without overriding the primary order.
  return [...rows].sort((a, b) => {
    const av = a[branchSortCol] ?? 0;
    const bv = b[branchSortCol] ?? 0;
    if (av < bv) return branchSortDir === 'desc' ? 1 : -1;
    if (av > bv) return branchSortDir === 'desc' ? -1 : 1;
    const pa = (a.project || '').toLowerCase();
    const pb = (b.project || '').toLowerCase();
    return pa < pb ? -1 : pa > pb ? 1 : 0;
  });
}

function renderProjectBranchCostTable(rows) {
  const sorted = sortProjectBranch(rows);
  const shown = sorted.slice(0, shownCount(branchLimit, sorted.length));
  document.getElementById('project-branch-cost-body').innerHTML = shown.map(pb => {
    return `<tr>
      <td>${esc(pb.project)}</td>
      <td class="muted" style="font-family:monospace">${esc(pb.branch || '\u2014')}</td>
      <td class="num">${pb.sessions}</td>
      <td class="num">${fmt(pb.turns)}</td>
      <td class="num">${fmt(displayInputTokens(pb))}</td>
      <td class="num">${fmt(pb.output)}</td>
      <td class="cost">${fmtCost(pb.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-branch-cost-foot', sorted.length, branchLimit, 'lessBranchRows', 'moreBranchRows', 'exportProjectBranchCSV');
}

// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function codexTokenHeaders() {
  return selectedSource === 'codex'
    ? ['Prompt Tokens', 'Uncached Input', 'Cached Input (included in prompt)', 'Cache Writes']
    : ['Input', 'Cache Read', 'Cache Creation'];
}

function csvTokenValues(row) {
  return selectedSource === 'codex'
    ? [row.input, uncachedInputTokens(row.input, row.cache_read, row.cache_creation), row.cache_read, row.cache_creation]
    : [row.input, row.cache_read, row.cache_creation];
}

function exportModelCSV() {
  const header = ['Model', 'Turns', ...codexTokenHeaders(), 'Output', 'Reasoning', 'Est. Cost'];
  const rows = sortModels(lastByModel).map(m => {
    const cost = rowCost(m);
    return [m.model, m.turns, ...csvTokenValues(m), m.output, m.reasoning_output || 0, cost.toFixed(4)];
  });
  downloadCSV('cost_by_model', header, rows);
}

function exportSessionsCSV() {
  const header = ['Session', 'Project', 'Title', 'Last Active', 'Duration (min)', 'Model', 'Turns', ...codexTokenHeaders(), 'Output', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = rowCost(s);
    return [s.session_id, s.project, s.topic, s.last, s.duration_min, s.model, s.turns, ...csvTokenValues(s), s.output, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}

function exportProjectsCSV() {
  const header = ['Project', 'Sessions', 'Turns', ...codexTokenHeaders(), 'Output', 'Est. Cost'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, ...csvTokenValues(p), p.output, p.cost.toFixed(4)];
  });
  downloadCSV('projects', header, rows);
}

function exportProjectBranchCSV() {
  const header = ['Project', 'Branch', 'Sessions', 'Turns', ...codexTokenHeaders(), 'Output', 'Est. Cost'];
  const rows = lastByProjectBranch.map(pb => {
    return [pb.project, pb.branch, pb.sessions, pb.turns, ...csvTokenValues(pb), pb.output, pb.cost.toFixed(4)];
  });
  downloadCSV('projects_by_branch', header, rows);
}

function exportDispatchesCSV() {
  const header = ['Type', 'Agent ID', 'Started', 'Model', 'Turns', 'Tool Uses', 'Duration (ms)', ...codexTokenHeaders(), 'Output', 'Total Tokens', 'Est. Cost', 'Status'];
  const rows = lastFilteredDispatches.map(d => {
    const total = totalTokenCount(d.input, d.output, d.cache_read, d.cache_creation);
    const cost = calcCost(d.model, d.input, d.output, d.cache_read, d.cache_creation);
    return [d.agent_type, d.agent_id, d.start, d.model, d.turns,
            d.tool_uses != null ? d.tool_uses : '', d.duration_ms != null ? d.duration_ms : '',
            ...csvTokenValues(d), d.output, total, cost.toFixed(4), d.status || ''];
  });
  downloadCSV('subagent_dispatches', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
let rescanInFlight = false;
let rescanStatusResetTimer = null;
async function triggerRescan() {
  if (rescanInFlight) return;
  const btn = document.getElementById('rescan-btn');
  if (!btn) return;
  rescanInFlight = true;
  btn.disabled = true;
  btn.textContent = '\u21bb Scanning...';
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    if (!resp.ok || d.error) throw new Error(d.error || 'Rescan request failed');
    btn.textContent = '\u21bb Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
    await loadData();
  } catch(e) {
    btn.textContent = '\u21bb Rescan (error)';
    console.error(e);
  } finally {
    rescanInFlight = false;
    if (rescanStatusResetTimer) clearTimeout(rescanStatusResetTimer);
    rescanStatusResetTimer = setTimeout(() => {
      btn.textContent = '\u21bb Rescan';
      btn.disabled = false;
    }, 3000);
  }
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const requestSource = selectedSource;
    const resp = await fetch('/api/data?source=' + encodeURIComponent(requestSource) + '&_=' + Date.now(), { cache: 'no-store' });
    const d = await resp.json();
    if (requestSource !== selectedSource) return;
    if (d.error) {
      // The server binds and serves before the initial scan finishes, so on a
      // fresh start the DB may not exist yet. Show a non-destructive notice and
      // retry instead of nuking the page — once the background scan creates the
      // DB, the next poll renders normally.
      const meta = document.getElementById('meta');
      if (meta) meta.innerHTML = esc(d.error) + ' — retrying…';
      if (rawData === null) setTimeout(loadData, 3000);
      return;
    }
    const refreshNotes = ['Auto-rescan every 30m'];
    if (rangeIncludesToday(selectedRange)) refreshNotes.unshift('Auto-refresh every 5m');
    document.getElementById('meta').innerHTML = 'Updated: ' + esc(d.generated_at) + '<br>' + refreshNotes.join(' · ');

    const isFirstLoad = rawData === null;
    rawData = d;
    currentProvider = d.provider || { label: SOURCE_LABELS[selectedSource], capabilities: d.capabilities || {} };
    updateProviderUI();

    if (isFirstLoad) {
      // Restore range (and any custom bounds) from the URL into the custom menu.
      selectedSource = d.source || selectedSource;
      selectedRange = readURLRange();
      updateRangeTrigger();
      // Mark default TZ button active
      document.querySelectorAll('.tz-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.tz === hourlyTZ)
      );
      // Build model filter (reads URL for model selection too)
      buildFilterUI(d.all_models);
      updateSortIcons();
      updateModelSortIcons();
      updateProjectSortIcons();
      updateProjectBranchSortIcons();
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

const DATA_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
const AUTO_RESCAN_INTERVAL_MS = 30 * 60 * 1000;
let autoRefreshTimer = null;
let autoRescanTimer = null;
let quotaRefreshTimer = null;
function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (quotaRefreshTimer) { clearInterval(quotaRefreshTimer); quotaRefreshTimer = null; }
  quotaRefreshTimer = setInterval(() => {
    if (rangeIncludesToday(selectedRange)) renderQuota(rawData && rawData.quota);
    else refreshQuota(false);
  }, DATA_REFRESH_INTERVAL_MS);
  if (rangeIncludesToday(selectedRange)) {
    autoRefreshTimer = setInterval(loadData, DATA_REFRESH_INTERVAL_MS);
  }
}

function scheduleAutoRescan() {
  if (autoRescanTimer) clearInterval(autoRescanTimer);
  autoRescanTimer = setInterval(triggerRescan, AUTO_RESCAN_INTERVAL_MS);
}

// APP_CONFIG is injected server-side (see do_GET).
const APP_CONFIG = window.APP_CONFIG || { version: '', pricing: {} };

function initFooterMeta() {
  const el = document.getElementById('footer-meta');
  if (!el) return;
  const v = APP_CONFIG.version || '';
  const parts = [];
  if (v) {
    parts.push('Version v' + esc(v));
  }
  parts.push('Inspired by <a href="https://github.com/phuryn/claude-usage" target="_blank" rel="noopener">claude-usage</a>');
  el.innerHTML = parts.join('&nbsp;&middot;&nbsp;');
}

// ── Section nav + collapsible cards ─────────────────────────────────────────
// The dashboard is one long scroll. The sticky jump bar teleports between
// sections; collapsible cards fold away the ones you don't use. Collapse state
// persists per card in localStorage and is independent of in-table Show
// more/less (which only pages rows within a single table).
const COLLAPSE_KEY = 'cu_collapsed_cards';
const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function loadCollapsedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveCollapsedSet(set) {
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) {}
}

// Charts created while their card is collapsed (display:none) lay out at zero
// size; resize them once the card is shown again so Chart.js repaints to fit.
function resizeChartsIn(card) {
  card.querySelectorAll('canvas').forEach(cv => {
    const ch = Object.values(charts).find(c => c && c.canvas === cv);
    if (ch) ch.resize();
  });
}

function setCardCollapsed(card, collapsed) {
  card.classList.toggle('collapsed', collapsed);
  const title = card.querySelector('h2, .section-title');
  if (title) title.setAttribute('aria-expanded', String(!collapsed));
}

function toggleCard(card) {
  const collapsed = !card.classList.contains('collapsed');
  setCardCollapsed(card, collapsed);
  const set = loadCollapsedSet();
  if (collapsed) set.add(card.dataset.card); else set.delete(card.dataset.card);
  saveCollapsedSet(set);
  if (!collapsed) requestAnimationFrame(() => resizeChartsIn(card));
}

function jumpToSection(id) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.dataset.card && el.classList.contains('collapsed')) toggleCard(el);  // expand before scrolling
  el.scrollIntoView({ behavior: prefersReducedMotion ? 'auto' : 'smooth', block: 'start' });
}

function initSectionNav() {
  const bar = document.getElementById('jump-bar');
  const container = document.querySelector('.container');
  if (!container) return;
  // A grid row can contain more than one section. Keep the explicit menu
  // choice active while that row is visible instead of letting DOM order make
  // its later sibling look like the chosen Graphs/Tables item.
  let selectedJumpTarget = null;
  let selectedJumpTargetReached = false;

  // Keep --jump-h synced to the complete sticky control stack (the filter bar
  // plus the section nav) so jumps never land underneath either control.
  const syncJumpHeight = () => {
    if (bar) {
      const stickyOffset = parseFloat(getComputedStyle(bar).top) || 0;
      document.documentElement.style.setProperty('--jump-h', (bar.offsetHeight + stickyOffset) + 'px');
    }
  };
  syncJumpHeight();
  window.addEventListener('resize', syncJumpHeight);

  // Restore persisted collapse state + make each title an accessible toggle.
  const collapsed = loadCollapsedSet();
  document.querySelectorAll('[data-card]').forEach(card => {
    const title = card.querySelector('h2, .section-title');
    if (title) {
      title.setAttribute('role', 'button');
      title.setAttribute('tabindex', '0');
      title.title = 'Collapse / expand section';
    }
    setCardCollapsed(card, collapsed.has(card.dataset.card));
  });

  // Toggle a card from its title (caret included). Inner controls (CSV, TZ, sort
  // headers) sit outside the title selector, so they keep their own behaviour.
  const TITLE_SEL = '.chart-card > h2, .chart-header > h2, .table-card > .section-title, .section-header > .section-title';
  const onTitleActivate = (e) => {
    if (e.target.closest('.info-icon')) return;  // info tooltip, not a collapse toggle
    if (e.type === 'keydown') { if (e.key !== 'Enter' && e.key !== ' ') return; e.preventDefault(); }
    const title = e.target.closest(TITLE_SEL);
    const card = title && title.closest('[data-card]');
    if (card) toggleCard(card);
  };
  container.addEventListener('click', onTitleActivate);
  container.addEventListener('keydown', onTitleActivate);

  // Jump links teleport to a section (expanding it first if collapsed). Blur the
  // clicked item so the hover/focus dropdown it lives in closes after the jump.
  if (bar) bar.addEventListener('click', (e) => {
    const link = e.target.closest('.jump-link');
    if (link) {
      selectedJumpTarget = link.dataset.target;
      selectedJumpTargetReached = false;
      document.querySelectorAll('.jump-panel .jump-link').forEach(item => item.classList.toggle('selected', item === link));
      jumpToSection(link.dataset.target);
      link.blur();
    }
  });

  // Mirror open/closed state on the menu triggers for assistive tech, and let
  // Escape close an open menu.
  document.querySelectorAll('.jump-menu').forEach(menu => {
    const trig = menu.querySelector('.jump-trigger');
    const sync = (open) => { if (trig) trig.setAttribute('aria-expanded', String(open)); };
    // A mouse click must not focus (and thus pin) the trigger — otherwise the
    // panel stays open after the pointer leaves and fights the next hover. Tab
    // focus still works (it doesn't go through mousedown), keeping it keyboard-open.
    if (trig) trig.addEventListener('mousedown', (e) => e.preventDefault());
    menu.addEventListener('mouseenter', () => sync(true));
    menu.addEventListener('mouseleave', () => sync(false));
    menu.addEventListener('focusin', () => sync(true));
    menu.addEventListener('focusout', () => sync(false));
    menu.addEventListener('keydown', (e) => { if (e.key === 'Escape' && document.activeElement) document.activeElement.blur(); });
  });

  // Scroll-spy: highlight the link for the topmost section under the bar, and
  // mark the parent Graphs/Tables trigger so the closed menu shows where you are.
  const links = [...document.querySelectorAll('.jump-link')];
  const menus = [...document.querySelectorAll('.jump-menu')];
  const targets = links.map(l => document.getElementById(l.dataset.target)).filter(Boolean)
    .sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  let spyScheduled = false;
  const updateActive = () => {
    spyScheduled = false;
    const visibleTargets = targets.filter(t => !t.hidden);
    // Include the filter bar above this sticky nav.  Previously this line only
    // used the nav's height, so a click could land on a section while scroll-spy
    // still marked the preceding Graphs/Tables entry as active.
    const line = bar ? bar.getBoundingClientRect().top + bar.offsetHeight + 16 : 45;
    let activeId = visibleTargets.length ? visibleTargets[0].id : null;
    for (const t of visibleTargets) {
      if (t.getBoundingClientRect().top - line <= 1) activeId = t.id; else break;
    }
    // At the very bottom the last (often short) section may never reach the line.
    if (visibleTargets.length && (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 4)
      activeId = visibleTargets[visibleTargets.length - 1].id;
    if (selectedJumpTarget) {
      const selected = document.getElementById(selectedJumpTarget);
      const scrollActive = document.getElementById(activeId);
      if (selected && !selectedJumpTargetReached) {
        // Ignore transitional scroll positions on the way to the clicked item.
        // Without this, the old section wins the first scroll event and clears
        // the user's selection before the smooth jump has reached its target.
        if (selected.getBoundingClientRect().top - line <= 20) selectedJumpTargetReached = true;
        else activeId = selectedJumpTarget;
      }
      // Equal top positions mean sibling cards share the visible grid row.
      // The click tells us which one the user meant; once rows diverge, normal
      // scroll-spy resumes and clears this temporary tie-breaker.
      if (selectedJumpTargetReached && selected && scrollActive && Math.abs(selected.getBoundingClientRect().top - scrollActive.getBoundingClientRect().top) < 4) {
        activeId = selectedJumpTarget;
      } else if (selectedJumpTargetReached) {
        selectedJumpTarget = null;
      }
    }
    links.forEach(l => l.classList.toggle('active', !l.hidden && l.dataset.target === activeId));
    menus.forEach(menu => {
      const trig = menu.querySelector('.jump-trigger');
      if (trig) trig.classList.toggle('active', !!menu.querySelector('.jump-link.active'));
    });
  };
  window.addEventListener('scroll', () => {
    if (!spyScheduled) { spyScheduled = true; requestAnimationFrame(updateActive); }
  }, { passive: true });
  updateActive();
}


// ── Settings page ──────────────────────────────────────────────────────────
// The whole page is a draft-then-confirm editor: every control writes to
// `draftSettings`, nothing reaches disk until Save is confirmed, and the sidebar
// carries an unsaved-changes dot so a draft can't be lost silently.
const SETTINGS_SOURCE_META = SETTINGS_BOOT.sources || {};
const RATE_FIELDS = SETTINGS_BOOT.rate_fields || ['input', 'output', 'cache_read', 'cache_write'];
const LC_FIELDS = SETTINGS_BOOT.long_context_fields ||
  ['long_context_threshold', 'long_input', 'long_output', 'long_cache_read', 'long_cache_write'];
const FIELD_LABELS = {
  input: 'Input', output: 'Output', cache_read: 'Cache read', cache_write: 'Cache write',
  long_context_threshold: 'Threshold (tokens)', long_input: 'Long input',
  long_output: 'Long output', long_cache_read: 'Long cache read', long_cache_write: 'Long cache write',
};

let priceFilter = { claude_code: '', codex: '' };
let expandedLongContext = new Set();
let settingsSaving = false;

function builtinPricing(source) {
  return (SETTINGS_BOOT.builtin_pricing || {})[source] || {};
}

function overrideMap(source, from) {
  return ((from || draftSettings).pricing_overrides || {})[source] || {};
}

function effectiveEntry(source, model) {
  return overrideMap(source)[model] || builtinPricing(source)[model] || null;
}

// Built-in order is meaningful (newest model first), so keep it and append
// user-added models alphabetically at the end.
function modelRows(source) {
  const builtin = builtinPricing(source);
  const overrides = overrideMap(source);
  const rows = Object.keys(builtin);
  Object.keys(overrides).sort().forEach(model => {
    if (rows.indexOf(model) === -1) rows.push(model);
  });
  return rows;
}

function fmtRate(value) {
  return value == null || value === '' ? '' : String(Number(value));
}

function entriesEqual(a, b) {
  if (!a || !b) return false;
  const fields = RATE_FIELDS.concat(LC_FIELDS);
  return fields.every(f => {
    const left = a[f] == null ? null : Number(a[f]);
    const right = b[f] == null ? null : Number(b[f]);
    return left === right;
  });
}

// Stable, key-sorted serialization: the only honest way to answer "is this
// draft different from what is on disk?" without false positives from key order.
function canonicalSettings(source) {
  const out = { sources: {}, pricing_overrides: {} };
  SOURCE_ORDER.forEach(src => {
    out.sources[src] = !!source.sources[src];
    const models = (source.pricing_overrides || {})[src] || {};
    const dest = {};
    Object.keys(models).sort().forEach(model => {
      const entry = models[model];
      const row = {};
      RATE_FIELDS.concat(LC_FIELDS).forEach(f => {
        if (entry[f] != null) row[f] = Number(entry[f]);
      });
      dest[model] = row;
    });
    out.pricing_overrides[src] = dest;
  });
  return JSON.stringify(out);
}

function isSettingsDirty() {
  return canonicalSettings(savedSettings) !== canonicalSettings(draftSettings);
}

function hasInvalidRates() {
  return !!document.querySelector('#settings-pricing .rate-input.invalid');
}

// ── Draft mutation ─────────────────────────────────────────────────────────
function pruneOverride(source, model) {
  const builtin = builtinPricing(source)[model];
  const overrides = draftSettings.pricing_overrides[source];
  // An override that matches the built-in rate exactly is not an override —
  // dropping it keeps the saved file (and the "modified" badge) truthful.
  if (builtin && overrides[model] && entriesEqual(builtin, overrides[model])) {
    delete overrides[model];
  }
}

function writeRate(source, model, field, raw) {
  const overrides = draftSettings.pricing_overrides[source];
  const current = overrides[model] || builtinPricing(source)[model] || {};
  const entry = Object.assign({}, current);
  const text = String(raw == null ? '' : raw).trim();
  if (text === '') {
    if (RATE_FIELDS.indexOf(field) !== -1) return false;   // the four core rates are required
    delete entry[field];
  } else {
    const number = Number(text);
    if (!isFinite(number) || number < 0) return false;
    entry[field] = number;
  }
  // A long_* rate with no threshold can never fire, so a cleared threshold
  // clears the whole tier rather than leaving dead fields behind.
  if (entry.long_context_threshold == null) {
    LC_FIELDS.forEach(f => { delete entry[f]; });
  }
  overrides[model] = entry;
  pruneOverride(source, model);
  return true;
}

function resetModel(source, model) {
  delete draftSettings.pricing_overrides[source][model];
  renderSettings();
}

function removeModel(source, model) {
  delete draftSettings.pricing_overrides[source][model];
  expandedLongContext.delete(source + '::' + model);
  renderSettings();
}

function toggleLongContext(source, model) {
  const key = source + '::' + model;
  if (expandedLongContext.has(key)) expandedLongContext.delete(key);
  else expandedLongContext.add(key);
  renderSettings();
}

function toggleSourceEnabled(source, enabled) {
  draftSettings.sources[source] = enabled;
  // Refuse in the UI rather than at save time, so the user finds out while the
  // click that caused it is still on screen.
  if (!SOURCE_ORDER.some(src => draftSettings.sources[src])) {
    draftSettings.sources[source] = true;
    renderSettings();
    setSettingsStatus('Keep at least one provider enabled.', 'error');
    return;
  }
  renderSettings();
}

function addModel(source) {
  const idField = document.getElementById('add-model-' + source);
  const model = String(idField.value || '').trim().toLowerCase();
  if (!model) { setSettingsStatus('Enter a model name to add.', 'error'); idField.focus(); return; }
  if (!/^[a-z0-9][a-z0-9\-._:\/]*$/.test(model)) {
    setSettingsStatus('Model names use letters, digits and - . _ : / only.', 'error');
    idField.focus();
    return;
  }
  if (builtinPricing(source)[model] || overrideMap(source)[model]) {
    setSettingsStatus('"' + model + '" is already listed — edit its rates in the table.', 'error');
    return;
  }
  const entry = {};
  for (const field of RATE_FIELDS) {
    const input = document.getElementById('add-' + field + '-' + source);
    const number = Number(String(input.value || '').trim());
    if (input.value === '' || !isFinite(number) || number < 0) {
      setSettingsStatus(FIELD_LABELS[field] + ' must be a number of zero or more.', 'error');
      input.focus();
      return;
    }
    entry[field] = number;
  }
  draftSettings.pricing_overrides[source][model] = entry;
  priceFilter[source] = '';
  renderSettings();
  setSettingsStatus('Added ' + model + '. Save to apply it.', null);
}

// ── Rendering ──────────────────────────────────────────────────────────────
function renderSettings() {
  renderSettingsSources();
  renderSettingsPricing();
  updateSettingsChrome();
}

function renderSettingsSources() {
  const host = document.getElementById('settings-sources');
  if (!host) return;
  host.innerHTML = SOURCE_ORDER.map(source => {
    const meta = SETTINGS_SOURCE_META[source] || {};
    const label = meta.label || SOURCE_LABELS[source] || source;
    const on = !!draftSettings.sources[source];
    return `<label class="toggle-row">
      <div class="toggle-copy">
        <div class="toggle-name">${esc(label)}</div>
        <div class="toggle-note">${esc(meta.pricing_basis || '')}</div>
      </div>
      <input type="checkbox" data-source-toggle="${esc(source)}" ${on ? 'checked' : ''}>
    </label>`;
  }).join('');
}

function renderSettingsPricing() {
  const host = document.getElementById('settings-pricing');
  if (!host) return;
  host.innerHTML = SOURCE_ORDER.map(source => {
    const meta = SETTINGS_SOURCE_META[source] || {};
    const label = meta.label || SOURCE_LABELS[source] || source;
    const filter = priceFilter[source] || '';
    const rows = modelRows(source).filter(m => !filter || m.indexOf(filter) !== -1);
    const body = rows.length
      ? rows.map(model => priceRowHTML(source, model)).join('')
      : `<tr><td colspan="${RATE_FIELDS.length + 2}" class="price-empty">No model matches "${esc(filter)}".</td></tr>`;
    return `<div class="price-group">
      <div class="price-group-head">
        <span class="price-group-title">${esc(label)}</span>
        <span class="price-group-basis">${esc(meta.pricing_basis || '')}</span>
        <input class="price-filter" type="search" placeholder="Filter models" data-price-filter="${esc(source)}" value="${esc(filter)}">
      </div>
      <div class="price-scroll">
        <table class="price-table">
          <thead><tr>
            <th>Model</th>
            ${RATE_FIELDS.map(f => `<th>${esc(FIELD_LABELS[f] || f)}</th>`).join('')}
            <th></th>
          </tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
      ${addModelHTML(source, label)}
    </div>`;
  }).join('');
}

function priceRowHTML(source, model) {
  const builtin = builtinPricing(source)[model] || null;
  const override = overrideMap(source)[model] || null;
  const entry = override || builtin || {};
  const isCustom = !builtin;
  const isModified = !!override && !!builtin;
  const lcKey = source + '::' + model;
  const expanded = expandedLongContext.has(lcKey);
  const badge = isCustom
    ? '<span class="price-badge custom">Custom</span>'
    : (isModified ? '<span class="price-badge modified">Modified</span>' : '');
  const cells = RATE_FIELDS.map(field => {
    const dirty = !!override && (!builtin || Number(builtin[field]) !== Number(entry[field]));
    return `<td><input class="rate-input${dirty ? ' dirty' : ''}" type="number" min="0" step="any"
      value="${esc(fmtRate(entry[field]))}" aria-label="${esc(model + ' ' + (FIELD_LABELS[field] || field))}"
      data-rate="${esc(field)}" data-source="${esc(source)}" data-model="${esc(model)}"></td>`;
  }).join('');
  const actions = [
    `<button class="row-btn" type="button" data-lc="${esc(lcKey)}" aria-expanded="${expanded}">${entry.long_context_threshold != null ? 'Long ctx' : 'Long ctx…'}</button>`,
    isCustom
      ? `<button class="row-btn danger" type="button" data-remove="${esc(lcKey)}">Remove</button>`
      : (isModified ? `<button class="row-btn" type="button" data-reset="${esc(lcKey)}">Reset</button>` : ''),
  ].filter(Boolean).join('');
  const main = `<tr data-source="${esc(source)}" data-model="${esc(model)}">
    <td><span class="price-model"><code>${esc(model)}</code>${badge}</span></td>
    ${cells}
    <td><span class="row-actions">${actions}</span></td>
  </tr>`;
  if (!expanded) return main;
  const lcFields = LC_FIELDS.map(field => `<label class="lc-field">${esc(FIELD_LABELS[field] || field)}
    <input class="rate-input" type="number" min="0" step="any" value="${esc(fmtRate(entry[field]))}"
      data-rate="${esc(field)}" data-source="${esc(source)}" data-model="${esc(model)}"></label>`).join('');
  return main + `<tr class="lc-row"><td colspan="${RATE_FIELDS.length + 2}">
    <div class="lc-fields">${lcFields}
      <span class="lc-note">Crossing the threshold reprices the whole request at these rates. Clear the threshold to drop the tier. Whether a past turn crossed it is decided when that turn is scanned, so a threshold change only affects turns scanned from now on.</span>
    </div></td></tr>`;
}

function addModelHTML(source, label) {
  const fields = RATE_FIELDS.map(field =>
    `<label class="add-field">${esc(FIELD_LABELS[field] || field)}
      <input id="add-${esc(field)}-${esc(source)}" type="number" min="0" step="any" placeholder="0.00"></label>`).join('');
  return `<div class="add-model">
    <div class="add-model-title">Add a ${esc(label)} model</div>
    <div class="add-model-fields">
      <label class="add-field wide">Model name
        <input id="add-model-${esc(source)}" type="text" placeholder="e.g. claude-opus-6" autocomplete="off"></label>
      ${fields}
      <button type="button" data-add-model="${esc(source)}">Add model</button>
    </div>
  </div>`;
}

function setSettingsStatus(message, kind) {
  const el = document.getElementById('settings-status');
  if (!el) return;
  el.textContent = message;
  el.classList.toggle('is-error', kind === 'error');
  el.classList.toggle('is-ok', kind === 'ok');
}

function updateSettingsChrome() {
  const dirty = isSettingsDirty();
  const invalid = hasInvalidRates();
  const save = document.getElementById('settings-save');
  const discard = document.getElementById('settings-discard');
  if (save) save.disabled = settingsSaving || !dirty || invalid;
  if (discard) discard.disabled = settingsSaving || !dirty;
  const dot = document.getElementById('nav-settings-dot');
  if (dot) dot.hidden = !dirty;
  const status = document.getElementById('settings-status');
  if (status && !status.classList.contains('is-error') && !status.classList.contains('is-ok')) {
    status.textContent = dirty ? 'Unsaved changes.' : 'No changes yet.';
  }
  if (invalid) setSettingsStatus('Fix the highlighted rate before saving.', 'error');
}

// ── Change summary for the confirm dialog ──────────────────────────────────
function settingsChangeSummary() {
  const items = [];
  SOURCE_ORDER.forEach(source => {
    const label = (SETTINGS_SOURCE_META[source] || {}).label || SOURCE_LABELS[source] || source;
    const before = !!savedSettings.sources[source];
    const after = !!draftSettings.sources[source];
    if (before !== after) {
      items.push(after
        ? 'Show <strong>' + esc(label) + '</strong> in the dashboard again.'
        : 'Stop showing and scanning <strong>' + esc(label) + '</strong>.');
    }
  });
  SOURCE_ORDER.forEach(source => {
    const label = (SETTINGS_SOURCE_META[source] || {}).label || SOURCE_LABELS[source] || source;
    const before = overrideMap(source, savedSettings);
    const after = overrideMap(source, draftSettings);
    const builtin = builtinPricing(source);
    Object.keys(after).forEach(model => {
      if (entriesEqual(before[model], after[model])) return;
      if (builtin[model]) {
        items.push('Override <code>' + esc(model) + '</code> (' + esc(label) + ') at ' + rateSummary(after[model]) + '.');
      } else {
        items.push((before[model] ? 'Update' : 'Add') + ' <code>' + esc(model) + '</code> (' + esc(label) + ') at ' + rateSummary(after[model]) + '.');
      }
    });
    Object.keys(before).forEach(model => {
      if (after[model]) return;
      items.push(builtin[model]
        ? 'Reset <code>' + esc(model) + '</code> (' + esc(label) + ') to its built-in rate.'
        : 'Remove <code>' + esc(model) + '</code> (' + esc(label) + ').');
    });
  });
  return items;
}

function rateSummary(entry) {
  if (!entry) return 'no rates';
  return RATE_FIELDS.map(f => esc((FIELD_LABELS[f] || f).toLowerCase()) + ' $' + esc(fmtRate(entry[f]))).join(' / ');
}

// ── Confirm dialog ─────────────────────────────────────────────────────────
// A single promise-based dialog, used both for confirming a save and for
// warning about leaving with a draft. Native confirm() would block the page and
// cannot show the itemised diff.
let confirmResolver = null;

function openConfirm(options) {
  const modal = document.getElementById('confirm-modal');
  if (!modal) return Promise.resolve(window.confirm(options.body || 'Continue?'));
  document.getElementById('confirm-title').textContent = options.title || 'Are you sure?';
  document.getElementById('confirm-body').textContent = options.body || '';
  const list = document.getElementById('confirm-list');
  const items = options.items || [];
  list.innerHTML = items.map(item => '<li>' + item + '</li>').join('');
  list.hidden = items.length === 0;
  const accept = document.getElementById('confirm-accept');
  accept.textContent = options.confirmLabel || 'Save';
  modal.hidden = false;
  accept.focus();
  return new Promise(resolve => { confirmResolver = resolve; });
}

function closeConfirm(result) {
  const modal = document.getElementById('confirm-modal');
  if (modal) modal.hidden = true;
  const resolve = confirmResolver;
  confirmResolver = null;
  if (resolve) resolve(result);
}

// ── Save / discard ─────────────────────────────────────────────────────────
async function requestSettingsSave() {
  if (settingsSaving || !isSettingsDirty() || hasInvalidRates()) return;
  const items = settingsChangeSummary();
  const ok = await openConfirm({
    title: 'Save settings?',
    body: items.length === 1 ? 'One change will be written to disk and applied right away:'
                             : items.length + ' changes will be written to disk and applied right away:',
    items: items,
    confirmLabel: 'Save changes',
  });
  if (!ok) { setSettingsStatus('Save cancelled — your changes are still here.', null); return; }
  await saveSettings();
}

async function saveSettings() {
  settingsSaving = true;
  updateSettingsChrome();
  setSettingsStatus('Saving…', null);
  try {
    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(draftSettings),
    });
    const payload = await resp.json();
    if (!resp.ok) {
      setSettingsStatus(payload.error || 'Could not save settings.', 'error');
      return;
    }
    savedSettings = normalizeSettings(payload.settings);
    draftSettings = cloneSettings(savedSettings);
    // Re-price the dashboard in place: the effective table just changed, and a
    // stale PRICING would show costs the CLI no longer agrees with.
    if (payload.pricing) PRICING = payload.pricing;
    renderSettings();
    applyEnabledSources();
    if (rawData) applyFilter();
    setSettingsStatus('Saved. Prices and providers are live.', 'ok');
  } catch (e) {
    console.error(e);
    setSettingsStatus('Could not reach the dashboard server.', 'error');
  } finally {
    settingsSaving = false;
    updateSettingsChrome();
  }
}

async function discardSettings() {
  if (!isSettingsDirty()) return;
  const ok = await openConfirm({
    title: 'Discard changes?',
    body: 'Your edits will be thrown away and the saved settings restored.',
    items: settingsChangeSummary(),
    confirmLabel: 'Discard',
  });
  if (!ok) return;
  draftSettings = cloneSettings(savedSettings);
  renderSettings();
  setSettingsStatus('Changes discarded.', null);
}

// ── View routing ───────────────────────────────────────────────────────────
async function setView(view) {
  const target = view === 'settings' ? 'settings' : 'dashboard';
  if (target === currentView) return;
  if (currentView === 'settings' && isSettingsDirty()) {
    const ok = await openConfirm({
      title: 'Leave settings?',
      body: 'You have unsaved changes. Leaving keeps them in this tab, but nothing is written to disk.',
      items: [],
      confirmLabel: 'Leave',
    });
    if (!ok) return;
  }
  currentView = target;
  applyView();
  updateURL();
}

function applyView() {
  const settingsView = currentView === 'settings';
  const dash = document.getElementById('dashboard-panel');
  const panel = document.getElementById('settings-panel');
  if (dash) dash.hidden = settingsView;
  if (panel) panel.hidden = !settingsView;
  const nav = document.getElementById('nav-settings');
  if (nav) nav.setAttribute('aria-current', settingsView ? 'page' : 'false');
  if (settingsView) {
    renderSettings();
    window.scrollTo({ top: 0, behavior: 'auto' });
  }
}

function initSettings() {
  const pricingHost = document.getElementById('settings-pricing');
  const sourcesHost = document.getElementById('settings-sources');

  // Rate edits are handled without a re-render so the caret keeps its place;
  // the row's own chrome (badge, Reset button) is refreshed in place instead.
  pricingHost.addEventListener('input', event => {
    const target = event.target;
    if (target.dataset.priceFilter) {
      priceFilter[target.dataset.priceFilter] = String(target.value || '').trim().toLowerCase();
      renderSettingsPricing();
      const again = document.querySelector(`[data-price-filter="${target.dataset.priceFilter}"]`);
      if (again) { again.focus(); }
      updateSettingsChrome();
      return;
    }
    if (!target.dataset.rate) return;
    const { source, model, rate } = target.dataset;
    const ok = writeRate(source, model, rate, target.value);
    target.classList.toggle('invalid', !ok);
    if (ok) {
      const builtin = builtinPricing(source)[model];
      const override = overrideMap(source)[model];
      target.classList.toggle('dirty', !!override &&
        (!builtin || Number(builtin[rate]) !== Number(override[rate])));
      refreshRowChrome(source, model);
    }
    setSettingsStatus('', null);
    updateSettingsChrome();
  });

  pricingHost.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.addModel) { addModel(button.dataset.addModel); return; }
    const split = key => { const i = key.indexOf('::'); return [key.slice(0, i), key.slice(i + 2)]; };
    if (button.dataset.lc) { const [src, model] = split(button.dataset.lc); toggleLongContext(src, model); return; }
    if (button.dataset.reset) { const [src, model] = split(button.dataset.reset); resetModel(src, model); return; }
    if (button.dataset.remove) { const [src, model] = split(button.dataset.remove); removeModel(src, model); return; }
  });

  sourcesHost.addEventListener('change', event => {
    const source = event.target.dataset.sourceToggle;
    if (!source) return;
    setSettingsStatus('', null);
    toggleSourceEnabled(source, event.target.checked);
  });

  document.getElementById('confirm-cancel').addEventListener('click', () => closeConfirm(false));
  document.getElementById('confirm-accept').addEventListener('click', () => closeConfirm(true));
  document.getElementById('confirm-modal').addEventListener('click', event => {
    if (event.target.id === 'confirm-modal') closeConfirm(false);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && confirmResolver) closeConfirm(false);
  });

  const pathEl = document.getElementById('settings-path');
  if (pathEl && SETTINGS_BOOT.path) pathEl.textContent = SETTINGS_BOOT.path;

  // A reload would drop an unconfirmed draft, so warn the way the browser does
  // for any other unsaved form.
  window.addEventListener('beforeunload', event => {
    if (!isSettingsDirty()) return;
    event.preventDefault();
    event.returnValue = '';
  });

  applyView();
}

// Refresh only the badge and action buttons of one row, so typing in a rate
// never costs the input its focus.
function refreshRowChrome(source, model) {
  const row = document.querySelector(`#settings-pricing tr[data-source="${source}"][data-model="${model}"]`);
  if (!row) return;
  const builtin = builtinPricing(source)[model] || null;
  const override = overrideMap(source)[model] || null;
  const badgeHost = row.querySelector('.price-model');
  const existing = badgeHost.querySelector('.price-badge');
  if (existing) existing.remove();
  if (!builtin) badgeHost.insertAdjacentHTML('beforeend', '<span class="price-badge custom">Custom</span>');
  else if (override) badgeHost.insertAdjacentHTML('beforeend', '<span class="price-badge modified">Modified</span>');
  const actions = row.querySelector('.row-actions');
  const reset = actions.querySelector('[data-reset]');
  if (builtin && override && !reset) {
    actions.insertAdjacentHTML('beforeend',
      `<button class="row-btn" type="button" data-reset="${source}::${model}">Reset</button>`);
  } else if ((!override || !builtin) && reset) {
    reset.remove();
  }
}

initFooterMeta();
initSettings();
initSectionNav();
updateSourceTabs();
loadData();
scheduleAutoRefresh();
scheduleAutoRescan();
</script>
</body>
</html>
"""


def find_asset_file(filename):
    """Locate a bundled dashboard asset across both run contexts.

    - Installed alongside the Python files: the icon is at ``resources/icon.svg``.
    - Standalone repo (``python cli.py dashboard``): the icon is at
      ``resources/icon.svg`` next to this module.

    Returns the first existing path, or ``None`` so asset routes can 404
    gracefully.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "resources" / filename, here / "resources" / filename):
        if candidate.is_file():
            return candidate
    return None


def find_icon_file():
    return find_asset_file("icon.svg")


# Static files the page references by name. Everything else 404s, so the handler
# can never be talked into serving an arbitrary path.
ASSET_ROUTES = {
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/codex-icon.svg": ("codex-icon.svg", "image/svg+xml"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def _display_path(path):
    """Home-relative path for display: shorter, and it doesn't put the user's
    username into a screenshot or a shared bug report."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home())).replace(os.sep, "/")
    except ValueError:
        return str(path)


def settings_payload(current=None):
    """Everything the Settings page needs in one response.

    `builtin_pricing` is the un-overridden table so the page can show what a
    rate *was* and offer "Reset to default"; `pricing` is the effective table
    (built-ins + overrides) so the rest of the dashboard prices identically.
    """
    resolved = current if isinstance(current, dict) else settings.load()
    return {
        "settings": resolved,
        "defaults": settings.defaults(),
        "path": _display_path(settings.SETTINGS_PATH),
        "sources": {
            source: {"label": config["label"], "pricing_basis": config["pricing_basis"]}
            for source, config in SOURCE_CONFIG.items()
        },
        "builtin_pricing": BUILTIN_PRICING_BY_SOURCE,
        "pricing": pricing.pricing_by_source(),
        "rate_fields": list(settings.RATE_FIELDS),
        "long_context_fields": list(settings.LONG_CONTEXT_FIELDS),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        """Parse a JSON request body, or return None if it isn't usable."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _control_allowed(self):
        """Whether this request may drive the sign-in subprocess."""
        if not is_loopback_host(SERVE_HOST):
            return False, "Sign-in is only available when the dashboard is bound to localhost."
        supplied = self.headers.get(CONTROL_TOKEN_HEADER) or ""
        # Constant-time: the token is a secret and this endpoint is unauthenticated
        # otherwise, so don't leak its prefix through comparison timing.
        if not secrets.compare_digest(supplied, CONTROL_TOKEN):
            return False, "Reload the dashboard and try again."
        return True, ""

    def _load_settings(self):
        """Re-read settings (and re-apply price overrides) for this request.

        Reading the small JSON file per request keeps a save — or an edit made
        by the CLI or another browser tab — in effect immediately, with no
        restart and no stale in-process copy.
        """
        return settings.apply()

    def do_GET(self):
        active_settings = self._load_settings()
        # self.path includes the query string, but every URL the UI emits has
        # one (e.g. "/?range=all"); compare the bare path so bookmarkable
        # URLs don't fall through to 404.
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            # Inject runtime config (version) the page can't know at
            # author time. json.dumps produces a valid JS object literal for the
            # `window.APP_CONFIG = __APP_CONFIG_JSON__;` placeholder in the head.
            config = json.dumps({
                "version": VERSION,
                # Same-origin-only secret for the sign-in route; see CONTROL_TOKEN.
                "controlToken": CONTROL_TOKEN,
                # A sign-in button is pointless without the CLI that performs it
                # (the Docker image has no `claude` binary), and unsafe off
                # loopback — decide once, here, rather than in the browser.
                "canSignIn": quota.sign_in_available() and is_loopback_host(SERVE_HOST),
                # Effective prices, so the browser's calcCost matches the CLI's
                # even when the user has overridden a rate.
                "pricing": pricing.pricing_by_source(),
                "settings": settings_payload(active_settings),
            })
            html = HTML_TEMPLATE.replace("__APP_CONFIG_JSON__", config)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/data":
            # Pass DB_PATH explicitly: get_dashboard_data's default arg is frozen
            # to the original module global at def time, so a bare call would ignore
            # a monkey-patched dashboard.DB_PATH (same contract as /api/rescan). This
            # also keeps the dashboard reading the configured DB rather than a stale
            # path captured at import.
            query = parse_qs(urlparse(self.path).query)
            source = query.get("source", [SOURCE_CLAUDE])[0]
            force_refresh = query.get("refresh", ["0"])[0] == "1"
            data = get_dashboard_data(DB_PATH, source=source, quota_force_refresh=force_refresh)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/settings":
            self._send_json(200, settings_payload(active_settings))

        elif path == "/api/signin":
            allowed, reason = self._control_allowed()
            if not allowed:
                self._send_json(403, {"error": reason})
                return
            self._send_json(200, quota.sign_in_state())

        elif path in ASSET_ROUTES:
            filename, content_type = ASSET_ROUTES[path]
            asset = find_asset_file(filename)
            if asset is None:
                self.send_response(404)
                self.end_headers()
                return
            body = asset.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        active_settings = self._load_settings()
        path = urlparse(self.path).path
        if path == "/api/settings":
            payload = self._read_json_body()
            if payload is None:
                self._send_json(400, {"error": "Expected a JSON settings object."})
                return
            try:
                saved = settings.save(payload)
            except settings.SettingsError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError as exc:
                self._send_json(500, {"error": f"Could not write settings: {exc}"})
                return
            # Apply immediately so the very next /api/data response is priced
            # with the new overrides.
            settings.apply(saved)
            self._send_json(200, settings_payload(saved))

        elif path == "/api/signin":
            allowed, reason = self._control_allowed()
            if not allowed:
                self._send_json(403, {"error": reason})
                return
            if not quota.sign_in_available():
                self._send_json(501, {"error": quota.SIGN_IN_UNAVAILABLE_MESSAGE})
                return
            # 202: the browser half of the flow outlives this response.
            self._send_json(202, quota.start_sign_in())

        elif path == "/api/rescan":
            # Incremental scan: ingest new/changed JSONL without touching
            # existing rows. The DB is append-only and the only durable store
            # of history once Claude Code prunes old transcripts, so we must
            # never delete it here — scan() dedupes via the message_id index.
            # Pass DB_PATH / DEFAULT_PROJECTS_DIRS explicitly so tests that
            # patch the module globals are honored (scan's defaults are
            # frozen at def time and would otherwise target the real paths).
            import scanner
            db_path = DB_PATH
            result = scanner.scan(
                db_path=db_path,
                projects_dirs=scanner.DEFAULT_PROJECTS_DIRS,
                # A provider switched off in Settings is not walked at all, so
                # disabling Codex really does stop reading ~/.codex.
                source=settings.scan_source(active_settings),
                verbose=False,
            )
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None):
    settings.apply()
    global SERVE_HOST
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    # Remembered so the sign-in gate can refuse a non-loopback bind.
    SERVE_HOST = host
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
