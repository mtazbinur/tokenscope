"""Canonical TokenScope provider identifiers.

Keep provider ids in one small dependency-free module.  User-facing labels and
capabilities belong to the dashboard; these constants are shared by storage,
settings, pricing, and the scanner.
"""

SOURCE_CLAUDE = "claude_code"
SOURCE_CODEX = "codex"
SOURCE_ANTIGRAVITY = "antigravity"

SOURCE_ORDER = (SOURCE_CLAUDE, SOURCE_CODEX, SOURCE_ANTIGRAVITY)
KNOWN_SOURCES = SOURCE_ORDER


def normalize_sources(value):
    """Return known source ids in canonical order, rejecting unknown values."""
    if isinstance(value, str):
        value = (value,)
    try:
        requested = set(value)
    except TypeError:
        return ()
    return tuple(source for source in SOURCE_ORDER if source in requested)
