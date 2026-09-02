"""Read-only Antigravity conversation databases.

Antigravity's local format is a set of SQLite databases containing protobuf
metadata blobs.  This module deliberately returns plain dictionaries and does
not know about TokenScope's database or scan policy; scanner.py owns staging,
deduplication, and materialization.
"""

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sources import SOURCE_ANTIGRAVITY

ANTIGRAVITY_DATA_DIR = os.environ.get("ANTIGRAVITY_DATA_DIR", "")
ANTIGRAVITY_PARSER_REVISION = "2"
DEFAULT_ANTIGRAVITY_ROOTS = (
    Path.home() / ".gemini" / "antigravity" / "conversations",
    Path.home() / ".gemini" / "antigravity-cli" / "conversations",
    Path.home() / ".gemini" / "antigravity-ide" / "conversations",
    Path.home() / ".gemini" / "antigravity-backup" / "conversations",
    Path.home() / ".config" / "antigravity" / "conversations",
)


class AntigravityError(ValueError):
    """A non-secret, actionable source parser error."""


class ProtoError(AntigravityError):
    """Malformed protobuf wire data."""


class ProtoMessage:
    """Small protobuf wire representation preserving field occurrence order."""

    def __init__(self, fields):
        self.fields = tuple(fields)

    def values(self, number, wire_type=None):
        return [value for field, wire, value in self.fields
                if field == number and (wire_type is None or wire == wire_type)]

    def last(self, number, wire_type=None, default=None):
        values = self.values(number, wire_type)
        return values[-1] if values else default

    def first(self, number, wire_type=None, default=None):
        values = self.values(number, wire_type)
        return values[0] if values else default

    def messages(self, number):
        return [_message(value) for value in self.values(number, 2)]


def decode_message(blob):
    """Decode the supported protobuf wire types without external packages."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ProtoError("metadata is not a BLOB")
    data = bytes(blob)
    fields = []
    pos = 0
    size = len(data)

    def take(count):
        nonlocal pos
        if count < 0 or pos + count > size:
            raise ProtoError("truncated protobuf field")
        result = data[pos:pos + count]
        pos += count
        return result

    def varint():
        nonlocal pos
        value = 0
        for index in range(10):
            byte = take(1)[0]
            value |= (byte & 0x7f) << (7 * index)
            if not byte & 0x80:
                if index == 9 and byte > 1:
                    raise ProtoError("protobuf varint overflow")
                return value
        raise ProtoError("protobuf varint is longer than 10 bytes")

    while pos < size:
        key = varint()
        field_number = key >> 3
        wire_type = key & 7
        if field_number == 0:
            raise ProtoError("protobuf field number is zero")
        if wire_type == 0:
            value = varint()
        elif wire_type == 1:
            value = take(8)
        elif wire_type == 2:
            length = varint()
            if length > size - pos:
                raise ProtoError("protobuf length exceeds metadata BLOB")
            value = take(length)
        elif wire_type == 5:
            value = take(4)
        else:
            raise ProtoError("unsupported protobuf wire type")
        fields.append((field_number, wire_type, value))
    return ProtoMessage(fields)


def _message(value):
    if isinstance(value, ProtoMessage):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return decode_message(value)
    return None


def _text(value):
    if not isinstance(value, bytes):
        return ""
    try:
        return value.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ProtoError("metadata contains invalid UTF-8") from exc


def _string(message, *fields):
    for field in fields:
        value = message.last(field, 2)
        if value is not None:
            text = _text(value)
            if text:
                return text
    return ""


def _timestamp(message):
    message = _message(message)
    if message is None:
        return None
    seconds = message.last(1, 0)
    if not isinstance(seconds, int) or seconds <= 0:
        return None
    nanos = message.last(2, 0, 0)
    nanos = min(max(int(nanos or 0), 0), 999_999_999)
    try:
        value = datetime.fromtimestamp(seconds, timezone.utc).replace(
            microsecond=nanos // 1000)
    except (OverflowError, OSError, ValueError):
        return None
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _model_usage(message):
    if message is None:
        return None
    def integer(field):
        value = message.last(field, 0, 0)
        return max(int(value or 0), 0)
    return {
        "numeric_model_id": integer(1),
        "input_tokens": integer(2),
        "output_tokens": integer(3),
        "cache_creation_tokens": integer(4),
        "cache_read_tokens": integer(5),
        "provider_code": integer(6),
        "message_identity": _string(message, 7),
        "reasoning_output_tokens": integer(9),
        "visible_output_tokens": integer(10),
        "response_identity": _string(message, 11),
        "provider_identity": _string(message, 12),
    }


def _model_info(message):
    if message is None:
        return 0, 0, ""
    return (
        max(int(message.last(1, 0, 0) or 0), 0),
        max(int(message.last(7, 0, 0) or 0), 0),
        _string(message, 12, 8),
    )


def _workspace_path(uri):
    """Turn a ``file://`` workspace URI into a plain, decoded filesystem path."""
    if not uri:
        return ""
    parsed = urlsplit(uri)
    raw = parsed.path if parsed.scheme else uri
    return unquote(raw)


def _session_workspace(trajectory_root):
    """Workspace path + git branch from a decoded ``trajectory_metadata_blob`` root."""
    if trajectory_root is None:
        return "", ""
    workspace = _message(trajectory_root.first(1, 2))
    if workspace is None:
        return "", ""
    return _workspace_path(_string(workspace, 1, 2)), _string(workspace, 4)


def _truncate_title(text):
    text = " ".join(text.split())
    if len(text) > 200:
        text = text[:200].rstrip() + "…"
    return text


def _session_title(conn, steps_columns):
    """The first user prompt's text, used as a fallback session title.

    ``steps.idx == 0`` is the conversation's opening user turn; its
    ``step_payload`` carries the raw prompt text at field 19 -> field 2.
    """
    if "step_payload" not in steps_columns:
        return ""
    for _idx, blob in conn.execute(
            "SELECT idx, step_payload FROM steps WHERE step_payload IS NOT NULL "
            "ORDER BY idx LIMIT 5"):
        try:
            step = decode_message(blob)
        except ProtoError:
            continue
        prompt = _message(step.last(19, 2))
        if prompt is None:
            continue
        text = _string(prompt, 2)
        if text:
            return _truncate_title(text)
    return ""


_NUMERIC_MODELS = {
    246: "gemini-2.5-pro",
    312: "gemini-2.5-flash",
    313: "gemini-2.5-flash-thinking",
    329: "gemini-2.5-flash-thinking",
    281: "claude-4-sonnet",
    282: "claude-4-sonnet",
    290: "claude-4-opus",
    291: "claude-4-opus",
}
_MODEL_ALIASES = {
    # Antigravity has emitted these tier/agent aliases for the same public
    # model. Keep the translation explicit; unknown future aliases remain
    # visible and unpriced rather than being guessed into a family.
    "gemini-3-flash-a": "gemini-3-flash-preview",
    "gemini-3-flash-b": "gemini-3-flash-preview",
    "gemini-3-flash-c": "gemini-3-flash-preview",
    "gemini-3-flash-agent": "gemini-3-flash-preview",
    "model-placeholder-m7": "gemini-3-pro-low",
    "model-placeholder-m8": "gemini-3-pro-high",
    "model-placeholder-m18": "gemini-3-flash-preview",
    "model-placeholder-m12": "claude-opus-4-5",
    "model-placeholder-m26": "claude-opus-4-6",
    "gemini-3-flash": "gemini-3-flash",
    "gemini-3-pro": "gemini-3-pro",
    "gemini-2.5-pro": "gemini-2.5-pro",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "gemini-2.5-flash-thinking": "gemini-2.5-flash-thinking",
    "claude-4-sonnet": "claude-4-sonnet",
    "claude-4-opus": "claude-4-opus",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
}


# Model ids that don't name a real engine: no display/numeric id resolved
# ("gemini-internal-model"), or Antigravity's own "use whatever the client
# picked" routing tag ("gemini-default", numeric id 1020 as of this writing).
# Both get the same treatment everywhere a resolved model is needed — fall
# back to the session's last real model rather than surfacing a fake one.
PLACEHOLDER_MODELS = frozenset({"gemini-internal-model", "gemini-default"})


def normalize_model(value, numeric_id=0):
    """Resolve known Antigravity ids while keeping unknown values explicit."""
    if numeric_id and numeric_id in _NUMERIC_MODELS:
        return _NUMERIC_MODELS[numeric_id]
    raw = str(value or "").strip()
    if not raw:
        return "gemini-internal-model"
    raw = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip().lower()
    compact = re.sub(r"[^a-z0-9.+-]+", "-", raw).strip("-")
    display = {
        "gemini-3-pro": "gemini-3-pro",
        "gemini-3-flash": "gemini-3-flash",
        "gemini-2-5-pro": "gemini-2.5-pro",
        "gemini-2-5-flash": "gemini-2.5-flash",
        "gemini-2-5-flash-thinking": "gemini-2.5-flash-thinking",
        "claude-4-sonnet": "claude-4-sonnet",
        "claude-4-opus": "claude-4-opus",
    }
    compact = display.get(compact, compact)
    return _MODEL_ALIASES.get(compact, compact)


def _normalized_usage(raw):
    if not raw:
        return None
    total = max(raw["output_tokens"], 0)
    reasoning = max(raw["reasoning_output_tokens"], 0)
    visible = max(raw["visible_output_tokens"], 0)
    total = max(total, visible + reasoning)
    visible = max(visible, total - reasoning)
    reasoning = max(reasoning, total - visible)
    buckets = (raw["input_tokens"], raw["cache_creation_tokens"],
               raw["cache_read_tokens"], total, reasoning)
    if sum(buckets) == 0:
        return None
    return {
        "input_tokens": raw["input_tokens"],
        "cache_creation_tokens": raw["cache_creation_tokens"],
        "cache_read_tokens": raw["cache_read_tokens"],
        "output_tokens": total,
        "reasoning_output_tokens": min(reasoning, total),
        "provider_code": raw["provider_code"],
        "message_identity": raw["message_identity"],
        "response_identity": raw["response_identity"],
        "provider_identity": raw["provider_identity"],
        "numeric_model_id": raw["numeric_model_id"],
    }


def _identity_keys(usage):
    return tuple("%s:%s" % (kind, usage[kind + "_identity"])
                 for kind in ("response", "provider", "message")
                 if usage.get(kind + "_identity"))


def _make_event(origin_path, session_id, origin_key, usage, model, timestamp,
                timestamp_rank, fallback_timestamp):
    real_timestamp = timestamp
    timestamp = real_timestamp or fallback_timestamp or "1970-01-01T00:00:00.000Z"
    return {
        "source": SOURCE_ANTIGRAVITY,
        "origin_path": origin_path,
        "origin_key": origin_key,
        "session_id": session_id,
        "timestamp": timestamp,
        "timestamp_rank": timestamp_rank if real_timestamp else (3 if fallback_timestamp else 5),
        "model": model or "gemini-internal-model",
        "provider_code": usage.get("provider_code", 0),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_creation_tokens": usage["cache_creation_tokens"],
        "reasoning_output_tokens": usage["reasoning_output_tokens"],
        "identities": list(_identity_keys(usage)),
        "message_id": usage.get("message_identity", ""),
        "message_id_rank": 1 if usage.get("message_identity") else 5,
        "parser_revision": ANTIGRAVITY_PARSER_REVISION,
    }


def _table_columns(conn, path, table):
    try:
        rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    except sqlite3.DatabaseError as exc:
        raise AntigravityError("%s: cannot inspect %s" % (path, table)) from exc
    return {row[1]: (row[2] or "").upper() for row in rows}


def _require_schema(columns, path, table, required):
    if not set(required).issubset(columns):
        raise AntigravityError("%s: missing %s table or columns" % (path, table))
    for name, expected in required.items():
        declared = columns.get(name, "")
        if declared and expected not in declared:
            raise AntigravityError("%s: invalid %s.%s type" % (path, table, name))


def database_signature(path):
    """Return a deterministic signature including WAL changes."""
    path = Path(path)
    parts = [str(path.resolve())]
    # The WAL contains database content that may not have reached the main file,
    # so it is part of change detection.  The SHM file is only SQLite's mutable
    # coordination index: opening an otherwise unchanged live database can
    # rewrite it.  Including SHM here therefore makes the pre/post stability
    # check reject a successful read as a concurrent database change.
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stat = candidate.stat()
            parts.append("%s:%s:%s" % (candidate.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            parts.append(candidate.name + ":missing")
    return ANTIGRAVITY_PARSER_REVISION + ":" + hashlib.sha256(
        "|".join(parts).encode("utf-8")).hexdigest()


def discover_databases(roots=None, errors=None):
    """Find canonical, sorted Antigravity conversation databases."""
    configured = roots
    if isinstance(configured, (str, os.PathLike)):
        configured = [configured]
    if configured is None:
        configured_value = os.environ.get("ANTIGRAVITY_DATA_DIR", ANTIGRAVITY_DATA_DIR)
        configured = [part.strip() for part in configured_value.split(",") if part.strip()]
        if not configured:
            configured = list(DEFAULT_ANTIGRAVITY_ROOTS)
    found = {}
    for raw_root in configured:
        root = Path(raw_root).expanduser()
        conversation_dir = root / "conversations" if root.name != "conversations" else root
        if not conversation_dir.exists():
            continue
        if not os.access(conversation_dir, os.R_OK | os.X_OK):
            if errors is not None:
                errors.append((str(conversation_dir), "conversation directory is not readable"))
            continue
        try:
            for path in conversation_dir.rglob("*.db"):
                if path.is_file():
                    found[str(path.resolve())] = path.resolve()
        except OSError as exc:
            if errors is not None:
                errors.append((str(conversation_dir), "cannot discover databases"))
            continue
    return [found[key] for key in sorted(found)]


def _connect_readonly(path):
    path = Path(path).resolve()
    try:
        uri = path.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn
    except (OSError, sqlite3.Error) as exc:
        raise AntigravityError("%s: cannot open read-only database" % path) from exc


def parse_database(path):
    """Parse one database into provenance-bearing, normalized events."""
    path = Path(path).resolve()
    session_id = path.stem or "antigravity-" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    try:
        db_mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        db_timestamp = db_mtime.strftime("%Y-%m-%dT%H:%M:%S.") + f"{db_mtime.microsecond // 1000:03d}Z"
    except OSError:
        db_timestamp = "1970-01-01T00:00:00.000Z"
    conn = _connect_readonly(path)
    context_table = None
    context_index = None
    try:
        conn.execute("BEGIN")
        gen_columns = _table_columns(conn, path, "gen_metadata")
        _require_schema(gen_columns, path, "gen_metadata", {"idx": "INT", "data": "BLOB"})
        steps_columns = _table_columns(conn, path, "steps")
        if steps_columns:
            _require_schema(steps_columns, path, "steps", {"idx": "INT", "metadata": "BLOB"})
        trajectory_columns = _table_columns(conn, path, "trajectory_metadata_blob")
        if trajectory_columns:
            _require_schema(trajectory_columns, path, "trajectory_metadata_blob", {"data": "BLOB"})

        trajectory_timestamp = None
        project_name = ""
        git_branch = ""
        if trajectory_columns:
            context_table = "trajectory_metadata_blob"
            context_index = 0
            row = conn.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
            if row and row[0] is not None:
                trajectory_root = decode_message(row[0])
                trajectory_timestamp = _timestamp(_message(trajectory_root.first(2, 2)))
                project_name, git_branch = _session_workspace(trajectory_root)
        session_title = _session_title(conn, steps_columns)
        events = []
        last_model = ""
        for idx, blob in conn.execute("SELECT idx, data FROM gen_metadata ORDER BY idx"):
            context_table = "gen_metadata"
            context_index = idx
            root = decode_message(blob)
            chat = root.first(1, 2)
            chat = _message(chat)
            if chat is None:
                continue
            model_info = chat.first(9, 2)
            generation = _message(model_info)
            generation_timestamp = _timestamp(generation.first(4, 2)) if generation else None
            display_model = _string(chat, 19, 21)
            numeric_model = int(chat.last(3, 0, 0) or 0)
            if display_model or numeric_model:
                resolved = normalize_model(display_model, numeric_model)
                if resolved not in PLACEHOLDER_MODELS:
                    last_model = resolved
            primary = _normalized_usage(_model_usage(_message(chat.first(4, 2))))
            for ordinal, raw in enumerate([primary] + [
                _normalized_usage(_model_usage(_message(retry.last(2, 2))))
                for retry in chat.messages(17)
            ]):
                if raw is None:
                    continue
                model = normalize_model(display_model, raw["numeric_model_id"] or numeric_model)
                if model in PLACEHOLDER_MODELS and last_model:
                    model = last_model
                events.append(_make_event(
                    str(path), session_id, "gen:%s:%s" % (idx, "primary" if ordinal == 0 else "retry:%s" % (ordinal - 1)),
                    raw, model, generation_timestamp,
                    1 if generation_timestamp else (3 if trajectory_timestamp else 4),
                    trajectory_timestamp or db_timestamp))

        for idx, blob in conn.execute("SELECT idx, metadata FROM steps ORDER BY idx") if steps_columns else ():
            context_table = "steps"
            context_index = idx
            root = decode_message(blob)
            timestamp_message = _message(root.first(8, 2) or root.first(1, 2))
            step_timestamp = _timestamp(timestamp_message)
            info_id, provider, info_model = _model_info(_message(root.first(24, 2)))
            primary_raw = _model_usage(_message(root.first(9, 2)))
            usages = [primary_raw] + [_model_usage(_message(retry.last(2, 2))) for retry in root.messages(28)]
            for ordinal, raw in enumerate(usages):
                normalized = _normalized_usage(raw)
                if normalized is None:
                    continue
                normalized["provider_code"] = normalized["provider_code"] or provider
                numeric_model = normalized["numeric_model_id"] or info_id
                model = normalize_model(info_model, numeric_model)
                if model in PLACEHOLDER_MODELS and last_model:
                    model = last_model
                if model not in PLACEHOLDER_MODELS:
                    last_model = model
                events.append(_make_event(
                    str(path), session_id, "step:%s:%s" % (idx, "primary" if ordinal == 0 else "retry:%s" % (ordinal - 1)),
                    normalized, model, step_timestamp,
                    1 if step_timestamp else (3 if trajectory_timestamp else 4),
                    trajectory_timestamp or db_timestamp))
        for event in events:
            event["project_name"] = project_name
            event["git_branch"] = git_branch
            event["topic"] = session_title
        conn.commit()
        return events
    except ProtoError as exc:
        location = context_table or "metadata"
        if context_index is not None:
            location += " row %s" % context_index
        raise AntigravityError("%s: %s: %s" % (path, location, exc)) from exc
    except (sqlite3.DatabaseError, AntigravityError) as exc:
        if isinstance(exc, AntigravityError):
            raise
        raise AntigravityError("%s: cannot read metadata" % path) from exc
    finally:
        conn.close()


def deduplicate_events(events):
    """Merge copied/retried representations using a transitive identity graph."""
    events = list(events or [])
    if not events:
        return []
    parent = list(range(len(events)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    identities = {}
    for index, event in enumerate(events):
        for identity in event.get("identities", ()):
            if identity in identities:
                union(index, identities[identity])
            else:
                identities[identity] = index

    groups = {}
    for index, event in enumerate(events):
        groups.setdefault(find(index), []).append(event)

    merged = []
    for group in sorted(groups.values(), key=lambda values: (
            min(str(value.get("origin_path", "")) for value in values),
            min(str(value.get("origin_key", "")) for value in values))):
        ordered = sorted(group, key=lambda value: (
            str(value.get("origin_path", "")), str(value.get("origin_key", ""))))
        best_timestamp = min(
            ordered,
            key=lambda value: (value.get("timestamp_rank", 5), value.get("timestamp", "")),
        )
        real_models = [value.get("model") for value in ordered
                       if value.get("model") and value.get("model") not in PLACEHOLDER_MODELS]
        event = dict(ordered[0])
        event["origin_path"] = ordered[0].get("origin_path", "")
        event["origin_key"] = "component:" + hashlib.sha256(
            "|".join(sorted({identity for value in ordered
                              for identity in value.get("identities", ())})).encode("utf-8")
        ).hexdigest() if any(value.get("identities") for value in ordered) else ordered[0].get("origin_key", "")
        event["session_id"] = ordered[0].get("session_id") or session_id_from_path(event["origin_path"])
        event["timestamp"] = best_timestamp.get("timestamp")
        event["timestamp_rank"] = best_timestamp.get("timestamp_rank", 5)
        event["model"] = real_models[0] if real_models else "gemini-internal-model"
        event["provider_code"] = next((value.get("provider_code") for value in ordered
                                        if value.get("provider_code")), 0)
        for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_creation_tokens", "reasoning_output_tokens"):
            event[field] = max(value.get(field, 0) or 0 for value in ordered)
        event["output_tokens"] = max(
            event["output_tokens"],
            event["reasoning_output_tokens"],
        )
        event["reasoning_output_tokens"] = min(
            event["reasoning_output_tokens"], event["output_tokens"])
        event["identities"] = sorted({identity for value in ordered
                                       for identity in value.get("identities", ())})
        ranked_ids = [(1 if identity.startswith("response:") else
                       2 if identity.startswith("provider:") else 3, identity)
                      for identity in event["identities"]]
        if ranked_ids:
            event["message_id_rank"], best_id = min(ranked_ids)
            event["message_id"] = best_id.split(":", 1)[1]
        else:
            event["message_id"] = ""
            event["message_id_rank"] = 5
        merged.append(event)
    return merged


def session_id_from_path(path):
    path = Path(path)
    return path.stem or "antigravity-" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
