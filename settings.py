"""
settings.py - User-editable settings: which providers to show, and price overrides.

Persisted as one small JSON file next to the usage database
(``~/.claude/tokenscope-settings.json``, override with ``TOKENSCOPE_SETTINGS``)
so the CLI and the dashboard read the same source of truth.  Stdlib only, like
the rest of the project.

Two things live here:

- ``sources`` — which providers are active.  A disabled provider is hidden from
  the dashboard *and* skipped by ``scan``, so nothing about it is read, polled,
  or stored.  All known providers are on by default for new settings files.
- ``pricing_overrides`` — per-source ``{model: rates}`` entries layered over
  ``pricing.PRICING_BY_SOURCE``.  This is how a model that ships later than the
  release gets a price without a code change, and how a user corrects a rate we
  got wrong.  Overrides are keyed exactly like the built-in table, so the same
  exact-match-then-longest-prefix resolution applies to them.

Reads are deliberately forgiving (a corrupt or half-hand-edited file degrades to
  defaults rather than taking the dashboard down); writes coming from the settings
page are strict, so bad input is rejected with a message instead of silently
dropped.
"""

import json
import os
import tempfile
from pathlib import Path
from sources import (SOURCE_CLAUDE, SOURCE_CODEX, SOURCE_ANTIGRAVITY,
                     SOURCE_ORDER, KNOWN_SOURCES)

SETTINGS_PATH = Path(os.environ.get(
    "TOKENSCOPE_SETTINGS", Path.home() / ".claude" / "tokenscope-settings.json"))

SCHEMA_VERSION = 2

# Every price entry needs these four; they mirror pricing.py's entry shape.
RATE_FIELDS = ("input", "output", "cache_read", "cache_write")
# Optional long-context tier.  ``long_context_threshold`` is a token count, the
# rest are USD/MTok.  Supplying the threshold without the rates is allowed —
# pricing.long_context_price falls back to the short-context rate per field.
LONG_CONTEXT_FIELDS = ("long_context_threshold", "long_input", "long_output",
                       "long_cache_read", "long_cache_write")

# Model ids in the wild are lowercase alphanumerics plus these separators
# (``claude-opus-4-1-20250805``, ``gpt-5.6-sol``, ``openai/gpt-5``).  Anything
# else — whitespace especially — is a typo, not a model.
_ID_EXTRA_CHARS = set("-._:/")


class SettingsError(ValueError):
    """A rejected settings payload. The message is shown to the user verbatim."""


def defaults():
    """A fresh settings dict: every provider on, no price overrides."""
    return {
        "schema_version": SCHEMA_VERSION,
        "sources": {source: True for source in KNOWN_SOURCES},
        "pricing_overrides": {source: {} for source in KNOWN_SOURCES},
    }


def _normalize_model_id(raw, strict):
    if not isinstance(raw, str):
        if strict:
            raise SettingsError("Model names must be text.")
        return None
    model = raw.strip().lower()
    if not model:
        if strict:
            raise SettingsError("Model name cannot be empty.")
        return None
    bad = [ch for ch in model if not (ch.isalnum() or ch in _ID_EXTRA_CHARS)]
    if bad:
        if strict:
            raise SettingsError(
                f"Model name {raw!r} contains characters that never appear in a "
                f"model id ({''.join(sorted(set(bad)))}). Use letters, digits, "
                "and - . _ : / only."
            )
        return None
    return model


def _normalize_rate(value, field, model, strict, required):
    if value is None or value == "":
        if required and strict:
            raise SettingsError(f"{model}: {field.replace('_', ' ')} is required.")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        if strict:
            raise SettingsError(f"{model}: {field.replace('_', ' ')} must be a number.")
        return None
    # NaN fails its own equality check; it would poison every downstream sum.
    if number != number or number < 0:
        if strict:
            raise SettingsError(
                f"{model}: {field.replace('_', ' ')} must be zero or greater.")
        return None
    return number


def _normalize_price_entry(model, raw, strict):
    if not isinstance(raw, dict):
        if strict:
            raise SettingsError(f"{model}: price must be an object of rates.")
        return None
    entry = {}
    for field in RATE_FIELDS:
        number = _normalize_rate(raw.get(field), field, model, strict, required=True)
        if number is None:
            return None
        entry[field] = number
    for field in LONG_CONTEXT_FIELDS:
        number = _normalize_rate(raw.get(field), field, model, strict, required=False)
        if number is not None:
            entry[field] = number
    # A long_* rate with no threshold can never apply, and a threshold is what
    # pricing.long_context_price keys off — so drop a partial tier rather than
    # storing something that silently does nothing.
    if "long_context_threshold" not in entry:
        for field in LONG_CONTEXT_FIELDS:
            entry.pop(field, None)
    return entry


def normalize(raw, strict=False):
    """Coerce an arbitrary payload into a full settings dict.

    ``strict=True`` (settings-page writes) raises ``SettingsError`` on anything
    unusable.  ``strict=False`` (disk reads) drops it and keeps going.
    """
    result = defaults()
    if not isinstance(raw, dict):
        if strict:
            raise SettingsError("Settings must be a JSON object.")
        return result

    try:
        # Browser writes are current-schema payloads even if a third-party
        # caller omitted the version field. Versionless disk data is legacy
        # schema v1 and receives the explicit upgrade behavior below.
        raw_schema_version = int(raw.get(
            "schema_version", SCHEMA_VERSION if strict else 1))
    except (TypeError, ValueError):
        raw_schema_version = 1
    # Version 1 only knew about Claude Code and Codex.  Antigravity follows the
    # product default on upgrade, so existing users receive the same enabled
    # source set as a fresh install.  Ignore an Antigravity flag stamped with a
    # pre-Antigravity schema version rather than treating it as authoritative.

    sources = raw.get("sources")
    if isinstance(sources, dict):
        for source in KNOWN_SOURCES:
            if raw_schema_version < SCHEMA_VERSION and source == SOURCE_ANTIGRAVITY:
                continue
            if source in sources:
                result["sources"][source] = bool(sources[source])
    elif sources is not None and strict:
        raise SettingsError("`sources` must be an object of provider flags.")
    # A dashboard with no providers has nothing to show and no way back except
    # editing JSON by hand, so refuse the write instead of bricking the page.
    if not any(result["sources"].values()):
        if strict:
            raise SettingsError("Keep at least one provider enabled.")
        result["sources"] = defaults()["sources"]

    overrides = raw.get("pricing_overrides")
    if isinstance(overrides, dict):
        for source, models in overrides.items():
            if raw_schema_version < SCHEMA_VERSION and source == SOURCE_ANTIGRAVITY:
                continue
            if source not in KNOWN_SOURCES:
                if strict:
                    raise SettingsError(f"Unknown provider: {source}")
                continue
            if not isinstance(models, dict):
                if strict:
                    raise SettingsError(f"{source}: price overrides must be an object.")
                continue
            for raw_model, raw_entry in models.items():
                model = _normalize_model_id(raw_model, strict)
                if model is None:
                    continue
                entry = _normalize_price_entry(model, raw_entry, strict)
                if entry is None:
                    continue
                result["pricing_overrides"][source][model] = entry
    elif overrides is not None and strict:
        raise SettingsError("`pricing_overrides` must be an object keyed by provider.")

    return result


def load(path=None):
    """Read settings from disk, falling back to defaults on anything unreadable."""
    target = Path(path) if path else SETTINGS_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults()
    return normalize(raw, strict=False)


def save(data, path=None, strict=True):
    """Validate and write settings, returning the normalized dict that was stored.

    The write goes to a temp file in the same directory and is then renamed, so
    a crash mid-write can't leave a truncated settings file behind.
    """
    normalized = normalize(data, strict=strict)
    target = Path(path) if path else SETTINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent), prefix=target.name + ".",
        suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(body)
        os.replace(handle.name, str(target))
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return normalized


def apply(data=None):
    """Push the stored price overrides into the pricing module and return settings.

    Called by the CLI entry point and by the dashboard on each request, so both
    surfaces price a turn identically and a Settings-page save takes effect
    without a restart.  Imported locally to keep settings.py dependency-free
    (pricing.py documents the same layering from its side).
    """
    import pricing

    resolved = data if isinstance(data, dict) else load()
    pricing.set_overrides(resolved.get("pricing_overrides"))
    return resolved


def enabled_sources(data=None):
    """The provider ids the user wants active, in canonical order."""
    resolved = data if isinstance(data, dict) else load()
    flags = resolved.get("sources") or {}
    active = [source for source in KNOWN_SOURCES if flags.get(source, True)]
    return active or list(KNOWN_SOURCES)


def scan_sources(data=None):
    """Return all enabled providers in canonical order for scanner.scan."""
    return tuple(enabled_sources(data))


def scan_source(data=None):
    """Compatibility wrapper for callers that still expect a scalar.

    New code should use :func:`scan_sources`; a tuple is returned when a
    partial multi-provider set is enabled so no source is silently skipped.
    """
    active = enabled_sources(data)
    if len(active) == len(KNOWN_SOURCES):
        return "all"
    if len(active) == 1:
        return active[0]
    return tuple(active)
