"""Provider-specific API-equivalent token pricing.

Rates are USD per million tokens.  Codex subscription usage is not an API
invoice; these entries are deliberately presented as API-equivalent estimates.
Unknown models return ``None`` rather than inheriting a neighbouring model's
price.

The table here is the shipped baseline.  ``set_overrides`` layers the user's
Settings-page edits (stored by settings.py) on top, which is how a model
released after this version gets a price without a code change.  Read prices
through ``get_pricing`` / ``pricing_by_source`` so the overrides always apply;
``BUILTIN_PRICING_BY_SOURCE`` is the un-overridden table, for showing defaults.

Sources:
- Claude: https://platform.claude.com/docs/en/about-claude/pricing (cache write
  = 1.25x base input, cache read = 0.1x base input, per the prompt-caching
  multiplier table).  Retired models keep the rates that page still lists.
- Codex/OpenAI: https://developers.openai.com/api/docs/pricing.  Families with
  a ``long_context_threshold`` bill the *whole* request at 2x input / 1.5x
  output once the prompt crosses 272K tokens.
"""

from sources import SOURCE_CLAUDE, SOURCE_CODEX, SOURCE_ANTIGRAVITY

# 272K prompt tokens is the documented cliff for every OpenAI family that has
# one; crossing it reprices the entire request, not just the excess.
OPENAI_LONG_CONTEXT_THRESHOLD = 272_000


def _openai(input_rate, output_rate, long_context=True):
    """Build an OpenAI entry from its two published rates.

    Cached input is 10% of input.  Cache writes keep this project's existing
    125%-of-input convention: Codex reports ``cache_write_input_tokens`` as a
    subset of prompt input, and ``calc_cost`` charges those tokens at the write
    rate *instead of* the input rate rather than on top of it.
    """
    entry = {
        "input": input_rate,
        "output": output_rate,
        "cache_read": round(input_rate * 0.1, 6),
        "cache_write": round(input_rate * 1.25, 6),
    }
    if long_context:
        entry.update({
            "long_context_threshold": OPENAI_LONG_CONTEXT_THRESHOLD,
            "long_input": input_rate * 2,
            "long_output": output_rate * 1.5,
            "long_cache_read": round(input_rate * 0.2, 6),
            "long_cache_write": round(input_rate * 2.5, 6),
        })
    return entry


def _claude(input_rate, output_rate):
    """Build a Claude entry from its base input/output rates.

    Cache write is 1.25x base input (5-minute TTL, the only one Claude Code
    uses) and a cache hit is 0.1x base input.
    """
    return {
        "input": input_rate,
        "output": output_rate,
        "cache_read": round(input_rate * 0.1, 6),
        "cache_write": round(input_rate * 1.25, 6),
    }


def _gemini(input_rate, output_rate, cache_read=None, cache_write=None,
            long_context=None):
    """Build a conservative API-equivalent Gemini entry.

    Antigravity's counters are independent buckets, unlike Codex's inclusive
    prompt counter.  Cache-write rates are intentionally explicit so this
    source never inherits Codex's replacement arithmetic.
    """
    entry = {
        "input": input_rate,
        "output": output_rate,
        "cache_read": input_rate if cache_read is None else cache_read,
        "cache_write": input_rate if cache_write is None else cache_write,
    }
    if long_context:
        entry.update({
            "long_context_threshold": long_context[0],
            "long_input": long_context[1],
            "long_output": long_context[2],
            "long_cache_read": long_context[3],
            "long_cache_write": long_context[4],
        })
    return entry


PRICING_BY_SOURCE = {
    SOURCE_CLAUDE: {
        "claude-fable-5": _claude(10.00, 50.00),
        "claude-mythos-5": _claude(10.00, 50.00),
        "claude-opus-5": _claude(5.00, 25.00),
        "claude-opus-4-8": _claude(5.00, 25.00),
        "claude-opus-4-7": _claude(5.00, 25.00),
        "claude-opus-4-6": _claude(5.00, 25.00),
        "claude-opus-4-5": _claude(5.00, 25.00),
        # Retired, still priced so historical transcripts cost something.
        "claude-opus-4-1": _claude(15.00, 75.00),
        "claude-opus-4": _claude(15.00, 75.00),
        "claude-sonnet-5": _claude(2.00, 10.00),
        "claude-sonnet-4-7": _claude(3.00, 15.00),
        "claude-sonnet-4-6": _claude(3.00, 15.00),
        "claude-sonnet-4-5": _claude(3.00, 15.00),
        "claude-sonnet-4": _claude(3.00, 15.00),
        "claude-haiku-4-7": _claude(1.00, 5.00),
        "claude-haiku-4-6": _claude(1.00, 5.00),
        "claude-haiku-4-5": _claude(1.00, 5.00),
        # Claude 3.x ids ("claude-3-5-sonnet-20241022") put the family after the
        # generation, so they need their own keys rather than a prefix match.
        # Rates are the historical list prices for these retired models; the
        # current pricing page only still carries Haiku 3.5.
        "claude-3-7-sonnet": _claude(3.00, 15.00),
        "claude-3-5-sonnet": _claude(3.00, 15.00),
        "claude-3-5-haiku": _claude(0.80, 4.00),
        "claude-haiku-3-5": _claude(0.80, 4.00),
        "claude-3-opus": _claude(15.00, 75.00),
        "claude-3-sonnet": _claude(3.00, 15.00),
        "claude-3-haiku": _claude(0.25, 1.25),
    },
    # OpenAI API list prices.  Codex's local logs are subscription telemetry,
    # so the resulting value is an estimate, never actual plan billing.
    SOURCE_CODEX: {
        "gpt-5.6-sol": _openai(4.00, 20.00),
        "gpt-5.6-terra": _openai(2.00, 12.00),
        "gpt-5.6-luna": _openai(0.20, 1.20),
        "gpt-5.5-pro": _openai(30.00, 180.00),
        "gpt-5.5": _openai(5.00, 30.00),
        "gpt-5.4-pro": _openai(30.00, 180.00),
        "gpt-5.4-mini": _openai(0.75, 4.50, long_context=False),
        "gpt-5.4-nano": _openai(0.20, 1.25, long_context=False),
        "gpt-5.4": _openai(2.50, 15.00),
        "gpt-5.3-codex": _openai(1.75, 14.00, long_context=False),
        "gpt-5.2": _openai(1.75, 14.00, long_context=False),
        "gpt-5.1": _openai(1.25, 10.00, long_context=False),
        "gpt-5-codex": _openai(1.25, 10.00, long_context=False),
        "gpt-5-mini": _openai(0.25, 2.00, long_context=False),
        "gpt-5-nano": _openai(0.05, 0.40, long_context=False),
        "gpt-5": _openai(1.25, 10.00, long_context=False),
    },
    # Underlying Google/Anthropic API-equivalent estimates only.  These are
    # not Antigravity plan charges; unknown/private model ids stay unpriced.
    SOURCE_ANTIGRAVITY: {
        "gemini-2.5-pro": _gemini(
            1.25, 10.00, cache_read=0.125, cache_write=1.25,
            long_context=(200_000, 2.50, 15.00, 0.25, 2.50)),
        "gemini-2.5-flash": _gemini(0.30, 2.50, cache_read=0.03, cache_write=0.30),
        "gemini-2.5-flash-thinking": _gemini(0.30, 2.50, cache_read=0.03, cache_write=0.30),
        "gemini-3-flash-preview": _gemini(0.50, 3.00, cache_read=0.05, cache_write=0.50),
        # Introductory standard rates through 2026-12-31. Keep these explicit
        # rather than letting newer Flash ids inherit an older family price.
        "gemini-3.7-flash": _gemini(0.75, 3.75, cache_read=0.075, cache_write=0.75),
        "gemini-3.6-flash": _gemini(0.75, 3.75, cache_read=0.075, cache_write=0.75),
        "gemini-3.5-flash": _gemini(1.50, 9.00, cache_read=0.15, cache_write=1.50),
        "gemini-3-pro": _gemini(
            2.00, 12.00, cache_read=0.20, cache_write=2.00,
            long_context=(200_000, 4.00, 18.00, 0.40, 4.00)),
        "gemini-3-pro-low": _gemini(2.00, 12.00, cache_read=0.20, cache_write=2.00),
        "gemini-3-pro-high": _gemini(2.00, 12.00, cache_read=0.20, cache_write=2.00),
        "claude-4-sonnet": _claude(3.00, 15.00),
        "claude-4-opus": _claude(15.00, 75.00),
        "claude-opus-4-5": _claude(5.00, 25.00),
        "claude-opus-4-6": _claude(5.00, 25.00),
        "claude-sonnet-4-6": _claude(3.00, 15.00),
    },
}

# The table above is the shipped baseline.  Users can correct a rate or add a
# model the release doesn't know about from the dashboard's Settings page; those
# overrides are layered on top by ``set_overrides`` (see settings.py).
BUILTIN_PRICING_BY_SOURCE = PRICING_BY_SOURCE

# Compatibility export for callers that historically imported Claude pricing.
PRICING = PRICING_BY_SOURCE[SOURCE_CLAUDE]

_OVERRIDES = {}
# Merged table + its prefix keys, rebuilt lazily whenever overrides change.
# Every price lookup goes through here, so it must stay cheap.
_MERGED = None
_MERGED_PREFIX_KEYS = None


def _prefix_keys(table):
    """Longest key first, so "gpt-5.4-mini" can't be swallowed by "gpt-5.4" and
    "claude-opus-4-1-20250805" can't be swallowed by "claude-opus-4"."""
    return {
        source: sorted(prices, key=len, reverse=True)
        for source, prices in table.items()
    }


def set_overrides(overrides):
    """Layer user price overrides over the built-in table.

    ``overrides`` is ``{source: {model: rates}}`` as stored by settings.py.  An
    override replaces a built-in entry wholesale rather than merging field by
    field, so a partially-filled override can't leave a stale rate behind.
    Passing a falsy value clears back to the shipped table.
    """
    global _OVERRIDES, _MERGED, _MERGED_PREFIX_KEYS
    cleaned = {}
    for source, models in (overrides or {}).items():
        if source not in BUILTIN_PRICING_BY_SOURCE or not isinstance(models, dict):
            continue
        for model, entry in models.items():
            if not isinstance(entry, dict):
                continue
            if not all(field in entry for field in ("input", "output", "cache_read", "cache_write")):
                continue
            cleaned.setdefault(source, {})[str(model).strip().lower()] = dict(entry)
    _OVERRIDES = cleaned
    _MERGED = None
    _MERGED_PREFIX_KEYS = None


def get_overrides():
    """The override layer currently in effect (a copy; mutating it does nothing)."""
    return {source: {model: dict(entry) for model, entry in models.items()}
            for source, models in _OVERRIDES.items()}


def pricing_by_source():
    """The effective price table: built-ins with user overrides applied."""
    global _MERGED, _MERGED_PREFIX_KEYS
    if _MERGED is None:
        if not _OVERRIDES:
            _MERGED = BUILTIN_PRICING_BY_SOURCE
        else:
            _MERGED = {
                source: dict(prices, **_OVERRIDES.get(source, {}))
                for source, prices in BUILTIN_PRICING_BY_SOURCE.items()
            }
        _MERGED_PREFIX_KEYS = _prefix_keys(_MERGED)
    return _MERGED


def _merged_prefix_keys():
    pricing_by_source()
    return _MERGED_PREFIX_KEYS


def get_pricing(model, source=SOURCE_CLAUDE):
    """Return the exact known model price for a source, allowing suffixes."""
    if not model:
        return None
    prices = pricing_by_source().get(source, {})
    normalized = model.strip().lower()
    if normalized in prices:
        return prices[normalized]
    for key in _merged_prefix_keys().get(source, ()):
        if normalized.startswith(key + "-"):
            return prices[key]
    return None


def is_long_context(model, prompt_tokens, source=SOURCE_CLAUDE):
    """Whether one request crosses this model's documented long-context tier."""
    price = get_pricing(model, source=source)
    threshold = price.get("long_context_threshold") if price else None
    return bool(threshold is not None and (prompt_tokens or 0) > threshold)


def long_context_price(price):
    """Overlay a price entry's long-context tier, if it has one."""
    if not price or price.get("long_context_threshold") is None:
        return price
    return {
        **price,
        "input": price.get("long_input", price["input"]),
        "output": price.get("long_output", price["output"]),
        "cache_read": price.get("long_cache_read", price["cache_read"]),
        "cache_write": price.get("long_cache_write", price["cache_write"]),
    }


def calc_cost(model, inp, out, cache_read, cache_creation, source=SOURCE_CLAUDE,
              long_context=False):
    """Return an API-equivalent cost from normalized token fields.

    Codex records include cached input in ``input_tokens``.  Charge only the
    non-cached part at the input rate and the cached part at the cached-input
    rate, preventing double billing.  A reported Codex cache write is a
    subset of non-cached prompt input, so its write rate replaces (rather than
    supplements) the ordinary input rate for those tokens.
    """
    price = get_pricing(model, source=source)
    if not price:
        return 0.0
    if long_context:
        price = long_context_price(price)
    if source == SOURCE_CODEX:
        non_cached_input = max((inp or 0) - (cache_read or 0), 0)
        cache_writes = min(cache_creation or 0, non_cached_input)
        ordinary_input = non_cached_input - cache_writes
        return (ordinary_input * price["input"] + (out or 0) * price["output"]
                + (cache_read or 0) * price["cache_read"]
                + cache_writes * price["cache_write"]) / 1_000_000
    return ((inp or 0) * price["input"] + (out or 0) * price["output"]
            + (cache_read or 0) * price["cache_read"]
            + (cache_creation or 0) * price["cache_write"]) / 1_000_000
