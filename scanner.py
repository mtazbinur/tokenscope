"""
scanner.py - Scans local agent JSONL transcript files for TokenScope.
"""

import json
import os
import glob
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from pricing import is_long_context

# Single source of truth for the app version reported by the CLI (`--version`)
# and the dashboard footer. CHANGELOG.md is the canonical version reference, but
# the runtime version has to live here as a constant. Keep this in lockstep with
# the top CHANGELOG heading (see tests/test_version.py).
VERSION = "1.1.0"

SOURCE_CLAUDE = "claude_code"
SOURCE_CODEX = "codex"
# Bump this whenever Codex token normalization changes.  Existing transcript
# files are immutable, so mtime-based incremental scanning alone cannot repair
# rows written by an older parser.
CODEX_PARSER_REVISION = "4"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
XCODE_PROJECTS_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects"
CODEX_SESSIONS_DIR = Path(os.environ.get("CODEX_SESSIONS_DIR", Path.home() / ".codex" / "sessions"))
DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))
DEFAULT_PROJECTS_DIRS = [PROJECTS_DIR, XCODE_PROJECTS_DIR]

# Higher number = higher priority when choosing a session's primary model.
# Fable / Mythos are Anthropic's most capable class, so they outrank Opus.
MODEL_PRIORITY = {"fable": 5, "mythos": 5, "opus": 3, "sonnet": 2, "haiku": 1}


def _model_priority(model):
    """Return a priority score for a model name (higher = more capable)."""
    if not model:
        return 0
    m = model.lower()
    for keyword, priority in MODEL_PRIORITY.items():
        if keyword in m:
            return priority
    return 0


def get_db(db_path=DB_PATH):
    # Ensure the parent directory exists — on a fresh install or CI runner
    # ~/.claude may not yet exist, and sqlite3.connect needs the parent dir.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            source          TEXT NOT NULL DEFAULT 'claude_code',
            session_id      TEXT NOT NULL,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            total_reasoning_output  INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0,
            topic           TEXT,
            PRIMARY KEY (source, session_id)
        );

        CREATE TABLE IF NOT EXISTS turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            source                  TEXT NOT NULL DEFAULT 'claude_code',
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT,
            message_id              TEXT,
            source_record_id        TEXT,
            reasoning_output_tokens INTEGER DEFAULT 0,
            is_long_context         INTEGER DEFAULT 0,
            is_subagent             INTEGER DEFAULT 0,
            agent_id                TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );

        CREATE TABLE IF NOT EXISTS agents (
            agent_id              TEXT PRIMARY KEY,
            agent_type            TEXT,
            dispatched_in_session TEXT,
            completed_at          TEXT,
            status                TEXT,
            total_tokens          INTEGER,
            total_duration_ms     INTEGER,
            tool_use_count        INTEGER
        );

        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
        CREATE INDEX IF NOT EXISTS idx_agents_type ON agents(agent_type);
    """)
    # Migrate the original Claude-only sessions table from a single-column
    # primary key to (source, session_id), preserving all existing rows.
    _migrate_sessions_to_sources(conn)
    # Add provider-aware columns when upgrading an older DB.
    _ensure_column(conn, "turns", "source", "TEXT NOT NULL DEFAULT 'claude_code'")
    _ensure_column(conn, "turns", "source_record_id", "TEXT")
    _ensure_column(conn, "turns", "reasoning_output_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "turns", "is_long_context", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "total_reasoning_output", "INTEGER DEFAULT 0")
    # Add message_id column if upgrading from older schema
    try:
        conn.execute("SELECT message_id FROM turns LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE turns ADD COLUMN message_id TEXT")
    # Subagent attribution columns (added in a later schema version)
    _ensure_column(conn, "turns", "is_subagent", "INTEGER DEFAULT 0")
    _ensure_column(conn, "turns", "agent_id", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_subagent ON turns(is_subagent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_agent_id ON turns(agent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_source ON turns(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_source_session ON turns(source, session_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_source_record_id ON turns(source, source_record_id) WHERE source_record_id IS NOT NULL AND source_record_id != ''")
    # Session topic (from custom-title / ai-title records; added in a later
    # schema version). The one-time backfill of pre-existing sessions is driven
    # by scan() via the schema_meta 'topic_backfill_done' marker (not by the
    # column-add event), so it also covers DBs that gained the column from an
    # earlier build that predated the backfill.
    _ensure_column(conn, "sessions", "topic", "TEXT")
    # Conditional unique index: dedup message IDs within their provider only.
    # Keeping source in the key prevents a future Codex identifier from
    # colliding with a Claude identifier in the shared turns table.
    conn.execute("DROP INDEX IF EXISTS idx_turns_message_id")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id
        ON turns(source, message_id)
        WHERE message_id IS NOT NULL AND message_id != ''
    """)
    conn.commit()


def _migrate_sessions_to_sources(conn):
    """Upgrade a pre-provider sessions table without losing Claude history."""
    columns = {r["name"]: r["pk"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if not columns or ("source" in columns and columns.get("source") == 1):
        return

    # The first CREATE INDEX pass may already have attached this index to the
    # legacy table. Drop it before renaming so init_db can recreate it below.
    conn.execute("DROP INDEX IF EXISTS idx_sessions_first")
    conn.execute("ALTER TABLE sessions RENAME TO sessions_legacy")
    conn.execute("""
        CREATE TABLE sessions (
            source          TEXT NOT NULL DEFAULT 'claude_code',
            session_id      TEXT NOT NULL,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            total_reasoning_output  INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0,
            topic           TEXT,
            PRIMARY KEY (source, session_id)
        )
    """)
    topic_expr = "topic" if "topic" in columns else "NULL"
    conn.execute(f"""
        INSERT INTO sessions
            (source, session_id, project_name, first_timestamp, last_timestamp,
             git_branch, total_input_tokens, total_output_tokens,
             total_cache_read, total_cache_creation, total_reasoning_output,
             model, turn_count, topic)
        SELECT 'claude_code', session_id, project_name, first_timestamp, last_timestamp,
               git_branch, total_input_tokens, total_output_tokens,
               total_cache_read, total_cache_creation, 0, model, turn_count, {topic_expr}
        FROM sessions_legacy
    """)
    conn.execute("DROP TABLE sessions_legacy")


def _ensure_column(conn, table, column, decl):
    """Add a column to an existing table if it isn't already present.

    Returns True if the column was just added (an upgrade of an existing DB),
    False if it was already there (fresh DB or already-migrated).
    """
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True
    return False


def _meta_get(conn, key):
    """Read a value from the schema_meta key/value table (None if absent)."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn, key, value):
    """Upsert a value into the schema_meta key/value table."""
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        (key, value))


def _extract_title(record):
    """Extract a session title from a custom-title or ai-title record."""
    rtype = record.get("type")
    if rtype == "custom-title":
        return record.get("customTitle")
    if rtype == "ai-title":
        return record.get("aiTitle")
    return None


def _backfill_topics(conn, jsonl_files):
    """One-time backfill of topics for a DB created before topic support.

    Transcript files scanned before the topic column existed are already in
    processed_files, so an incremental scan skips them and never sees the
    custom-title / ai-title records they already contain. Re-read just those
    records (turns are left untouched, so token totals cannot drift) and set the
    topic for any session that doesn't have one yet. Runs once, gated by a flag
    in schema_meta (see scan()). Returns the number of sessions filled.
    """
    needing = {r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE source = ? "
        "AND (topic IS NULL OR topic = '')", (SOURCE_CLAUDE,))}
    if not needing:
        return 0

    titles = {}          # session_id -> chosen title
    has_custom = set()   # sessions whose topic came from a custom-title record
    for filepath in jsonl_files:
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Cheap prefilter: only title records carry the substring
                    # "title" (in their "custom-title" / "ai-title" type), so we
                    # skip JSON-parsing the ~99% of lines that are turns.
                    if "title" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    title = _extract_title(record)
                    if not title:
                        continue
                    sid = record.get("sessionId")
                    if sid not in needing:
                        continue
                    # custom-title wins; ai-title only if no custom-title seen.
                    if record.get("type") == "custom-title":
                        titles[sid] = title
                        has_custom.add(sid)
                    elif sid not in has_custom:
                        titles.setdefault(sid, title)
        except Exception as e:
            print(f"  Warning: error reading {filepath}: {e}")

    for sid, title in titles.items():
        conn.execute(
            "UPDATE sessions SET topic = ? WHERE session_id = ? "
            "AND source = ? AND (topic IS NULL OR topic = '')",
            (title, sid, SOURCE_CLAUDE))
    conn.commit()
    return len(titles)


def project_name_from_cwd(cwd):
    """Derive a friendly project name from cwd path."""
    if not cwd:
        return "unknown"
    # Normalize to forward slashes, take last 2 components
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def is_subagent_record(record, source_path=""):
    """True if a record belongs to a dispatched subagent (Task/Agent tool).

    Subagents are detected three ways: an explicit ``isSidechain`` flag, an
    ``agentId`` on the record (or its ``data`` wrapper), or a transcript path
    under a ``subagents`` directory (Claude Code writes one jsonl per subagent).
    """
    if record.get("isSidechain"):
        return True
    if record.get("agentId"):
        return True
    data = record.get("data")
    if isinstance(data, dict) and data.get("agentId"):
        return True
    sp = str(source_path).replace("\\", "/").lower()
    return "/subagents/" in sp


def record_agent_id(record):
    """Pull the subagent id off a record, if any (top-level or data wrapper)."""
    agent_id = record.get("agentId")
    if not agent_id:
        data = record.get("data")
        if isinstance(data, dict):
            agent_id = data.get("agentId")
    return agent_id


def extract_agent_dispatch(record):
    """Pull subagent identity from a parent's tool_result record.

    Claude Code writes a ``toolUseResult`` dict on the user-side record that
    closes out an Agent/Task tool invocation. It carries ``agentId`` (matching
    the subagent jsonl's records) and ``agentType`` (the human-readable type
    such as 'general-purpose' or 'Explore') plus aggregate stats.
    """
    if record.get("type") != "user":
        return None
    tur = record.get("toolUseResult")
    if not isinstance(tur, dict):
        return None
    agent_id = tur.get("agentId")
    agent_type = tur.get("agentType")
    if not agent_id or not agent_type:
        return None
    return {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "dispatched_in_session": record.get("sessionId"),
        "completed_at": record.get("timestamp", ""),
        "status": tur.get("status"),
        "total_tokens": tur.get("totalTokens"),
        "total_duration_ms": tur.get("totalDurationMs"),
        "tool_use_count": tur.get("totalToolUseCount"),
    }


def upsert_agents(conn, agents):
    """Insert or update agent dispatch metadata. Last write wins per agent_id."""
    if not agents:
        return
    conn.executemany("""
        INSERT INTO agents
            (agent_id, agent_type, dispatched_in_session, completed_at,
             status, total_tokens, total_duration_ms, tool_use_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            agent_type            = excluded.agent_type,
            dispatched_in_session = excluded.dispatched_in_session,
            completed_at          = excluded.completed_at,
            status                = excluded.status,
            total_tokens          = excluded.total_tokens,
            total_duration_ms     = excluded.total_duration_ms,
            tool_use_count        = excluded.tool_use_count
    """, [
        (a["agent_id"], a["agent_type"], a.get("dispatched_in_session"),
         a.get("completed_at"), a.get("status"),
         a.get("total_tokens"), a.get("total_duration_ms"), a.get("tool_use_count"))
        for a in agents
    ])


def parse_jsonl_file(filepath, source=SOURCE_CLAUDE, start_line=0,
                     replayed_usage_prefix=None):
    """Parse a JSONL file and return (session_metas, turns, agents, line_count).

    Deduplicates streaming events by message.id — Claude Code logs multiple
    JSONL records per API response, all sharing the same message.id. Only the
    last record per message_id is kept (it has the final usage tallies).
    """
    if source == SOURCE_CODEX:
        return parse_codex_jsonl_file(
            filepath, start_line=start_line,
            replayed_usage_prefix=replayed_usage_prefix,
        )

    seen_messages = {}  # message_id -> turn dict (dedup streaming records)
    turns_no_id = []    # turns without a message_id (kept as-is)
    session_meta = {}   # session_id -> dict
    agents = {}         # agent_id -> dispatch dict
    line_count = 0

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                if line_count <= start_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = record.get("type")
                if rtype not in ("assistant", "user", "custom-title", "ai-title"):
                    continue

                session_id = record.get("sessionId")
                if not session_id:
                    continue

                # Extract session title from title records
                title = _extract_title(record)
                if title:
                    if session_id not in session_meta:
                        session_meta[session_id] = {
                            "source": source,
                            "session_id": session_id,
                            "project_name": "unknown",
                            "first_timestamp": "",
                            "last_timestamp": "",
                            "git_branch": "",
                            "model": None,
                            "topic": None,
                        }
                    meta = session_meta[session_id]
                    # custom-title always wins; ai-title only if no custom-title set
                    if rtype == "custom-title":
                        meta["topic"] = title
                    elif rtype == "ai-title" and not meta.get("topic"):
                        meta["topic"] = title
                    continue

                if rtype == "user":
                    dispatch = extract_agent_dispatch(record)
                    if dispatch is not None:
                        agents[dispatch["agent_id"]] = dispatch

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                # Update session metadata from any record
                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "source": source,
                        "session_id": session_id,
                        "project_name": project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp": timestamp,
                        "git_branch": git_branch,
                        "model": None,
                        "topic": None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch
                    # A title record (custom-title / ai-title) carries no cwd, so
                    # a session whose title line precedes its first content line
                    # was seeded with "unknown". Repair it from the first record
                    # that does have a cwd; upsert_sessions never updates
                    # project_name, so leaving it here means "unknown" forever.
                    if cwd and meta["project_name"] == "unknown":
                        meta["project_name"] = project_name_from_cwd(cwd)

                if rtype == "assistant":
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")
                    message_id = msg.get("id", "")

                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

                    # Only record turns that have actual token usage
                    if input_tokens + output_tokens + cache_read + cache_creation == 0:
                        continue

                    # Extract tool name from content if present
                    tool_name = None
                    for item in msg.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_name = item.get("name")
                            break

                    if model:
                        session_meta[session_id]["model"] = model

                    turn = {
                        "source": source,
                        "session_id": session_id,
                        "timestamp": timestamp,
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_creation_tokens": cache_creation,
                        "tool_name": tool_name,
                        "cwd": cwd,
                        "message_id": message_id,
                        "source_record_id": message_id,
                        "reasoning_output_tokens": 0,
                        "is_subagent": 1 if is_subagent_record(record, filepath) else 0,
                        "agent_id": record_agent_id(record),
                    }

                    # Dedup: last record per message_id wins (final usage tallies)
                    if message_id:
                        seen_messages[message_id] = turn
                    else:
                        turns_no_id.append(turn)

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, list(agents.values()), line_count


def _codex_usage_values(usage):
    """Normalise one Codex usage snapshot without double-countable categories."""
    def token(name):
        value = usage.get(name, 0) or 0
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = token("input_tokens")
    cache_read = min(token("cached_input_tokens"), input_tokens)
    cache_creation = min(token("cache_write_input_tokens"), input_tokens - cache_read)
    return {
        "input_tokens": input_tokens,
        "output_tokens": token("output_tokens"),
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "reasoning_output_tokens": token("reasoning_output_tokens"),
        "total_tokens": token("total_tokens"),
    }


def _codex_usage_delta(current, previous):
    """Return the non-negative delta between Codex cumulative usage snapshots."""
    previous = previous or {}
    return {
        key: max(current.get(key, 0) - previous.get(key, 0), 0)
        for key in current
    }


def _codex_usage_key(usage):
    return tuple(usage.get(key, 0) for key in (
        "input_tokens", "cache_read_tokens", "cache_creation_tokens",
        "output_tokens", "reasoning_output_tokens", "total_tokens",
    ))


def _codex_turn_record_id(session_id, timestamp, model, usage, occurrence=0):
    """Stable identity for a logical usage event, including replay-safe usage.

    ``occurrence`` distinguishes repeated responses that are otherwise
    byte-identical (same session, timestamp, model and usage); without it
    ``INSERT OR IGNORE`` would silently drop the second one.
    """
    payload = "\x1f".join(str(part) for part in (
        session_id, timestamp or "", model or "", *_codex_usage_key(usage),
        occurrence,
    ))
    return "codex:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_codex_model(model, timestamp=""):
    """Resolve Codex's internal labels before persisting/billing a turn.

    Auto-review rollouts omit their underlying model in the token events.  The
    contemporaneous Codex/ccusage fallback for this log generation is GPT-5.5;
    without it those requests appear as an unpriced ``codex-auto-review`` row.
    """
    if (model or "").strip().lower() == "codex-auto-review":
        return "gpt-5.5"
    return model


def parse_codex_jsonl_file(filepath, start_line=0, replayed_usage_prefix=None):
    """Parse one Codex rollout JSONL file into the shared usage shape.

    Codex emits usage in token_count event messages. ``last_token_usage`` is
    the usage for the current response; ``total_token_usage`` is cumulative and
    must never be summed. Repeated cumulative snapshots are ignored, while a
    missing per-response snapshot falls back to the cumulative delta.
    """
    session_id = None
    cwd = ""
    model = ""
    rollout_id = None
    first_timestamp = ""
    last_timestamp = ""
    topic = None
    turns_by_id = {}
    previous_totals = None
    usage_occurrences = {}
    replay_index = 0
    line_count = 0

    def update_time(timestamp):
        nonlocal first_timestamp, last_timestamp
        if not timestamp:
            return
        if not first_timestamp or timestamp < first_timestamp:
            first_timestamp = timestamp
        if not last_timestamp or timestamp > last_timestamp:
            last_timestamp = timestamp

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                is_new_line = line_count > start_line
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = record.get("timestamp", "")
                update_time(timestamp)
                rtype = record.get("type")
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                if rtype == "session_meta":
                    # ``session_id`` is the logical Codex thread.  Some older
                    # rollout records expose only ``id``, which is a safe
                    # fallback rather than dropping the usage entirely.
                    # ``id`` identifies this rollout.  ``session_id`` is the
                    # logical parent thread and is shared by spawned rollouts;
                    # using it here would collapse distinct child sessions.
                    session_id = payload.get("id") or payload.get("session_id") or session_id
                    rollout_id = payload.get("id") or rollout_id or session_id
                    cwd = payload.get("cwd") or cwd
                    update_time(payload.get("timestamp", ""))
                    continue

                if rtype == "turn_context":
                    # Only a fallback: ``session_meta`` already chose the
                    # immutable rollout id, and ``turn_context.session_id`` is
                    # the logical parent thread shared by spawned rollouts —
                    # letting it win here would collapse children into the
                    # parent (the exact case ``session_meta`` guards against).
                    session_id = session_id or payload.get("session_id")
                    cwd = payload.get("cwd") or cwd
                    model = _resolve_codex_model(payload.get("model") or model, timestamp)
                    continue

                if rtype != "event_msg" or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                last_usage = info.get("last_token_usage")
                total_usage = info.get("total_token_usage")
                normalized_total = (
                    _codex_usage_values(total_usage)
                    if isinstance(total_usage, dict) else None
                )
                # Codex sometimes re-emits the same completed response.  The
                # cumulative snapshot is authoritative for deciding whether it
                # advanced.  Some record shapes omit `last_token_usage`; in
                # that case the cumulative delta is the only usable response
                # usage.
                cumulative_advanced = (
                    normalized_total is None or normalized_total != previous_totals
                )
                if isinstance(last_usage, dict) and cumulative_advanced:
                    usage = _codex_usage_values(last_usage)
                elif not isinstance(last_usage, dict) and normalized_total is not None:
                    usage = _codex_usage_delta(normalized_total, previous_totals)
                else:
                    usage = None
                if normalized_total is not None:
                    previous_totals = normalized_total
                # Historical lines are not re-inserted, but they still update
                # `previous_totals` above and the occurrence counter below, so an
                # appended event is compared with the real preceding cumulative
                # snapshot and numbered exactly as a full reparse would number it.
                if not isinstance(usage, dict):
                    continue
                if not session_id:
                    continue
                usage_key = _codex_usage_key(usage)
                if sum(usage_key[:-1]) == 0:
                    continue

                # A token_count event is one completed response.  A single
                # turn_context can precede many such events, so keying by the
                # context turn id silently discards all but its last response.
                #
                # Two responses can be identical in session/timestamp/model/usage
                # (short retries, tiny tool turns), and a bare content hash would
                # collapse them into one row.  Counting occurrences in file order
                # keeps each one addressable while staying stable across rescans:
                # the counter advances for historical lines too, so an
                # incremental scan numbers an appended event the same way a full
                # reparse would.
                occurrence = usage_occurrences.get(usage_key, 0)
                usage_occurrences[usage_key] = occurrence + 1
                record_id = _codex_turn_record_id(
                    session_id, timestamp, model, usage, occurrence)

                # Prefixes copied into a spawned thread are a replay of parent
                # usage, not child spending.  The scanner supplies the parent
                # prefix when its local rollout is available.
                if not is_new_line:
                    continue
                if replayed_usage_prefix is not None:
                    if (replay_index < len(replayed_usage_prefix)
                            and _codex_usage_key(usage) == replayed_usage_prefix[replay_index]):
                        replay_index += 1
                        continue
                    replayed_usage_prefix = None

                turns_by_id[record_id] = {
                    "source": SOURCE_CODEX,
                    "session_id": session_id,
                    "timestamp": timestamp,
                    "model": model,
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "cache_read_tokens": usage["cache_read_tokens"],
                    "cache_creation_tokens": usage["cache_creation_tokens"],
                    "reasoning_output_tokens": usage["reasoning_output_tokens"],
                    # Kept in-memory for replay-prefix matching.  It is not
                    # stored because Codex's total_tokens is a context-window
                    # metric, not an additive token category.
                    "codex_total_tokens": usage["total_tokens"],
                    "is_long_context": int(is_long_context(
                        model, usage["input_tokens"], source=SOURCE_CODEX)),
                    "tool_name": None,
                    "cwd": cwd,
                    "message_id": "",
                    "source_record_id": record_id,
                    "is_subagent": 0,
                    "agent_id": None,
                }
    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    if not session_id:
        return [], [], [], line_count

    meta = {
        "source": SOURCE_CODEX,
        "session_id": session_id,
        "project_name": project_name_from_cwd(cwd),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "git_branch": "",
        "model": None,
        "topic": topic,
    }
    turns = list(turns_by_id.values())
    for turn in turns:
        if turn["model"] and not meta["model"]:
            meta["model"] = turn["model"]
    return [meta], turns, [], line_count


def aggregate_sessions(session_metas, turns):
    """Aggregate turn data back into session-level stats."""
    from collections import defaultdict, Counter

    session_stats = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "total_reasoning_output": 0,
        "turn_count": 0,
        "model": None,
    })
    session_model_counts = defaultdict(Counter)

    for t in turns:
        key = (t.get("source", SOURCE_CLAUDE), t["session_id"])
        s = session_stats[key]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["total_reasoning_output"] += t.get("reasoning_output_tokens", 0)
        s["turn_count"] += 1
        if t["model"]:
            session_model_counts[key][t["model"]] += 1

    for key, counts in session_model_counts.items():
        if counts:
            session_stats[key]["model"] = counts.most_common(1)[0][0]

    # Merge into session_metas
    result = []
    for meta in session_metas:
        key = (meta.get("source", SOURCE_CLAUDE), meta["session_id"])
        stats = session_stats[key]
        result.append({**meta, **stats})
    return result


def upsert_sessions(conn, sessions):
    for s in sessions:
        source = s.get("source", SOURCE_CLAUDE)
        # Check if session exists
        existing = conn.execute(
            "SELECT total_input_tokens, total_output_tokens, total_cache_read, "
            "total_cache_creation, total_reasoning_output, turn_count FROM sessions "
            "WHERE source = ? AND session_id = ?",
            (source, s["session_id"])
        ).fetchone()

        # A session seen only via a title record (custom-title / ai-title carry a
        # sessionId but no timestamp) has no real content. Don't let it INSERT a
        # phantom, token-less row; if the session already exists it still falls
        # through to the UPDATE below and sets its topic.
        if existing is None and not s.get("first_timestamp"):
            continue

        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (source, session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, total_reasoning_output,
                     model, turn_count,
                     topic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                source, s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s.get("total_reasoning_output", 0),
                s["model"], s["turn_count"], s.get("topic")
            ))
        else:
            # Update: add new tokens on top of existing (since we only insert new turns)
            # Keep the highest-priority model (e.g. opus over haiku from subagents)
            existing_row = conn.execute(
                "SELECT model, topic FROM sessions WHERE source = ? AND session_id = ?",
                (source, s["session_id"])
            ).fetchone()
            existing_model = existing_row["model"]
            new_model = s["model"]
            if _model_priority(new_model) > _model_priority(existing_model):
                model_to_set = new_model
            else:
                model_to_set = existing_model

            # Update topic if the new scan found one and the existing is empty
            new_topic = s.get("topic")
            existing_topic = existing_row["topic"]
            topic_to_set = new_topic if new_topic else existing_topic

            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    total_reasoning_output = total_reasoning_output + ?,
                    turn_count = turn_count + ?,
                    model = ?,
                    topic = ?
                WHERE source = ? AND session_id = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s.get("total_reasoning_output", 0),
                s["turn_count"], model_to_set, topic_to_set,
                source, s["session_id"]
            ))


def insert_turns(conn, turns):
    conn.executemany("""
        INSERT OR IGNORE INTO turns
            (source, session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id,
             source_record_id, reasoning_output_tokens, is_long_context, is_subagent, agent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t.get("source", SOURCE_CLAUDE), t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t["tool_name"], t["cwd"], t.get("message_id", ""),
         t.get("source_record_id", ""), t.get("reasoning_output_tokens", 0),
         t.get("is_long_context", 0),
         t.get("is_subagent", 0), t.get("agent_id"))
        for t in turns
    ])


def _codex_rollout_metadata(filepath):
    """Read the tiny amount of metadata needed to identify spawned rollouts."""
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                source = payload.get("source")
                subagent = source.get("subagent") if isinstance(source, dict) else None
                spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                parent_id = payload.get("forked_from_id")
                if not parent_id and isinstance(spawn, dict):
                    parent_id = spawn.get("parent_thread_id")
                return {
                    "rollout_id": payload.get("id") or payload.get("session_id"),
                    "session_id": payload.get("session_id") or payload.get("id"),
                    "parent_id": parent_id,
                    "timestamp": record.get("timestamp") or payload.get("timestamp") or "",
                }
    except OSError:
        pass
    return None


def _codex_replay_prefixes(codex_paths, changed_paths):
    """Return parent usage prefixes for spawned rollouts that need scanning.

    Codex writes a child rollout with a replayed copy of its parent's leading
    usage. The child advertises its parent in ``source.subagent.thread_spawn``.
    We compare only against non-spawned rollouts for that parent thread and only
    events recorded before the child was created, leaving genuine child turns
    intact. Missing parent files simply yield no prefix rather than guessing.
    """
    metadata = {path: _codex_rollout_metadata(path) for path in codex_paths}
    root_paths_by_session = {}
    for path, meta in metadata.items():
        if meta and not meta.get("parent_id"):
            # Modern Codex uses the immutable rollout id in ``forked_from_id``;
            # older logs use a logical parent thread id inside thread_spawn.
            # Index both without letting a child be its own parent.
            for identifier in (meta.get("rollout_id"), meta.get("session_id")):
                if identifier:
                    root_paths_by_session.setdefault(identifier, []).append(path)

    cached_parent_turns = {}
    prefixes = {}
    for child_path in changed_paths:
        child = metadata.get(child_path)
        if not child or not child.get("parent_id"):
            continue
        parent_paths = root_paths_by_session.get(child["parent_id"], [])
        if not parent_paths:
            continue
        parent_turns = []
        for parent_path in parent_paths:
            if parent_path not in cached_parent_turns:
                _, parsed, _, _ = parse_codex_jsonl_file(parent_path)
                cached_parent_turns[parent_path] = parsed
            parent_turns.extend(cached_parent_turns[parent_path])
        cutoff = child.get("timestamp") or ""
        parent_turns.sort(key=lambda turn: (turn.get("timestamp") or "", turn.get("source_record_id") or ""))
        prefixes[child_path] = [
            _codex_usage_key({
                "input_tokens": turn["input_tokens"],
                "cache_read_tokens": turn["cache_read_tokens"],
                "cache_creation_tokens": turn["cache_creation_tokens"],
                "output_tokens": turn["output_tokens"],
                "reasoning_output_tokens": turn.get("reasoning_output_tokens", 0),
                "total_tokens": turn.get("codex_total_tokens", 0),
            })
            for turn in parent_turns
            if not cutoff or (turn.get("timestamp") or "") <= cutoff
        ]
    return prefixes


def scan(projects_dir=None, projects_dirs=None, db_path=DB_PATH, verbose=True,
         source=SOURCE_CLAUDE, codex_dir=None):
    """Scan Claude Code and/or Codex transcripts into the shared database."""
    conn = get_db(db_path)
    init_db(conn)

    source_filter = source or SOURCE_CLAUDE
    if source_filter not in ("all", SOURCE_CLAUDE, SOURCE_CODEX):
        raise ValueError(f"Unknown source: {source_filter}")

    files_to_scan = []
    claude_files = []
    if source_filter in ("all", SOURCE_CLAUDE):
        if projects_dirs:
            claude_dirs = [Path(d) for d in projects_dirs]
        elif projects_dir:
            claude_dirs = [Path(projects_dir)]
        else:
            claude_dirs = DEFAULT_PROJECTS_DIRS
        for directory in claude_dirs:
            if not directory.exists():
                continue
            if verbose:
                print(f"Scanning {directory} ...")
            found = glob.glob(str(directory / "**" / "*.jsonl"), recursive=True)
            claude_files.extend(found)
            files_to_scan.extend((path, SOURCE_CLAUDE) for path in found)

    if source_filter in ("all", SOURCE_CODEX):
        directory = Path(codex_dir) if codex_dir else CODEX_SESSIONS_DIR
        if directory.exists():
            if verbose:
                print(f"Scanning {directory} ...")
            found = glob.glob(str(directory / "**" / "*.jsonl"), recursive=True)
            files_to_scan.extend((path, SOURCE_CODEX) for path in found)

    files_to_scan.sort()
    claude_files.sort()

    # Token parsing is normally incremental by file mtime.  That deliberately
    # avoids rereading long histories, but it also means a parser correction
    # cannot change already-recorded immutable rollout files.  Rebuild only the
    # Codex slice once per parser revision; Claude rows and their file markers
    # remain untouched.
    codex_paths = [path for path, item_source in files_to_scan if item_source == SOURCE_CODEX]
    refresh_codex = bool(
        codex_paths
        and _meta_get(conn, "codex_parser_revision") != CODEX_PARSER_REVISION
    )
    if refresh_codex:
        conn.execute("DELETE FROM turns WHERE source = ?", (SOURCE_CODEX,))
        conn.execute("DELETE FROM sessions WHERE source = ?", (SOURCE_CODEX,))
        conn.executemany(
            "DELETE FROM processed_files WHERE path = ?",
            ((path,) for path in codex_paths),
        )
        conn.commit()
        if verbose:
            print("Refreshing Codex usage after parser update ...")

    # Build replay prefixes before incremental filtering. A changed child may
    # replay usage from an unchanged parent file, so the parent still needs to
    # be available for comparison.
    changed_codex_paths = []
    for path in codex_paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        row = conn.execute(
            "SELECT mtime FROM processed_files WHERE path = ?", (path,)
        ).fetchone()
        if not row or abs(row["mtime"] - mtime) >= 0.01:
            changed_codex_paths.append(path)
    codex_replay_prefixes = _codex_replay_prefixes(codex_paths, changed_codex_paths)

    if claude_files and _meta_get(conn, "topic_backfill_done") != "1":
        filled = _backfill_topics(conn, claude_files)
        _meta_set(conn, "topic_backfill_done", "1")
        conn.commit()
        if verbose and filled:
            print(f"Backfilled topic for {filled} existing session(s).")

    new_files = 0
    updated_files = 0
    skipped_files = 0
    total_turns = 0
    total_sessions = set()
    by_source = {
        SOURCE_CLAUDE: {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0},
        SOURCE_CODEX: {"new": 0, "updated": 0, "skipped": 0, "turns": 0, "sessions": 0},
    }

    for filepath, file_source in files_to_scan:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ?", (filepath,)
        ).fetchone()
        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            by_source[file_source]["skipped"] += 1
            continue

        is_new = row is None
        if verbose:
            print(f"  [{'NEW' if is_new else 'UPD'}] {filepath}")
        old_lines = 0 if is_new else row["lines"]
        session_metas, turns, agents, line_count = parse_jsonl_file(
            filepath, source=file_source, start_line=old_lines,
            replayed_usage_prefix=codex_replay_prefixes.get(filepath),
        )

        if not is_new and line_count <= old_lines:
            conn.execute("UPDATE processed_files SET mtime = ? WHERE path = ?", (mtime, filepath))
            conn.commit()
            skipped_files += 1
            by_source[file_source]["skipped"] += 1
            continue

        upsert_agents(conn, agents)
        if turns or session_metas:
            sessions = aggregate_sessions(session_metas, turns)
            upsert_sessions(conn, sessions)
            insert_turns(conn, turns)
            for session in sessions:
                total_sessions.add((session.get("source", file_source), session["session_id"]))
            total_turns += len(turns)

        if is_new:
            new_files += 1
            by_source[file_source]["new"] += 1
        else:
            updated_files += 1
            by_source[file_source]["updated"] += 1
        by_source[file_source]["turns"] += len(turns)
        by_source[file_source]["sessions"] += len(session_metas)

        conn.execute("""
            INSERT OR REPLACE INTO processed_files (path, mtime, lines)
            VALUES (?, ?, ?)
        """, (filepath, mtime, line_count))
        conn.commit()

    # Recompute session totals from actual turns in DB.
    # This ensures correctness when INSERT OR IGNORE skips duplicate turns
    # but upsert_sessions had already added their tokens additively.
    if new_files or updated_files:
        conn.execute("""
            UPDATE sessions SET
                total_input_tokens = COALESCE((SELECT SUM(input_tokens) FROM turns WHERE turns.source = sessions.source AND turns.session_id = sessions.session_id), 0),
                total_output_tokens = COALESCE((SELECT SUM(output_tokens) FROM turns WHERE turns.source = sessions.source AND turns.session_id = sessions.session_id), 0),
                total_cache_read = COALESCE((SELECT SUM(cache_read_tokens) FROM turns WHERE turns.source = sessions.source AND turns.session_id = sessions.session_id), 0),
                total_cache_creation = COALESCE((SELECT SUM(cache_creation_tokens) FROM turns WHERE turns.source = sessions.source AND turns.session_id = sessions.session_id), 0),
                total_reasoning_output = COALESCE((SELECT SUM(reasoning_output_tokens) FROM turns WHERE turns.source = sessions.source AND turns.session_id = sessions.session_id), 0),
                turn_count = COALESCE((SELECT COUNT(*) FROM turns WHERE turns.source = sessions.source AND turns.session_id = sessions.session_id), 0)
        """)
        conn.commit()

    if refresh_codex:
        _meta_set(conn, "codex_parser_revision", CODEX_PARSER_REVISION)
        conn.commit()

    if verbose:
        print(f"\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

    conn.close()
    result = {"new": new_files, "updated": updated_files, "skipped": skipped_files,
              "turns": total_turns, "sessions": len(total_sessions), "by_source": by_source}
    return result


if __name__ == "__main__":
    import sys
    projects_dir = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--projects-dir" and i + 1 < len(sys.argv[1:]):
            projects_dir = Path(sys.argv[i + 2])
            break
    scan(projects_dir=projects_dir)
