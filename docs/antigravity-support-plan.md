# Antigravity support plan

Status: implemented

Prepared: 2026-09-01

Recommended source id: `antigravity`

## Decision

Yes, Antigravity support is feasible without adding a runtime dependency.
Python's standard library already provides the two primitives TokenScope needs:
`sqlite3` for the conversation databases and a small protobuf wire decoder for
the metadata BLOBs.

This is not another JSONL adapter. Antigravity writes per-conversation SQLite
databases whose `gen_metadata` and optional `steps` rows contain protobuf BLOBs.
The safest design is therefore a dedicated Antigravity adapter feeding
TokenScope's existing normalized `sessions` and `turns` tables.

The reference implementation reviewed for this plan is ccusage main at commit
[`21c7f68571f26f6bb65304d851c3941074f33b04`](https://github.com/ccusage/ccusage/tree/21c7f68571f26f6bb65304d851c3941074f33b04/rust/adapters/antigravity):

- [Adapter README](https://github.com/ccusage/ccusage/blob/21c7f68571f26f6bb65304d851c3941074f33b04/rust/adapters/antigravity/README.md)
- [Path discovery](https://github.com/ccusage/ccusage/blob/21c7f68571f26f6bb65304d851c3941074f33b04/rust/adapters/antigravity/src/paths.rs)
- [SQLite and protobuf parser](https://github.com/ccusage/ccusage/blob/21c7f68571f26f6bb65304d851c3941074f33b04/rust/adapters/antigravity/src/parser.rs)
- [Cross-database deduplication and tests](https://github.com/ccusage/ccusage/blob/21c7f68571f26f6bb65304d851c3941074f33b04/rust/adapters/antigravity/src/loader.rs)
- [User-facing data-source documentation](https://ccusage.com/guide/antigravity/)

Treat these as a verified behavioral reference, not a permanent schema
guarantee from Antigravity. The format is local and effectively private, so
parser revisioning and failure visibility are required.

## Goals

- Import local Antigravity token usage without network requests.
- Keep Antigravity isolated under `source = 'antigravity'`.
- Preserve input, cache-write, cache-read, output, and reasoning accounting.
- Normalize model aliases to stable pricing ids.
- Avoid double-counting usage repeated in `gen_metadata`, `steps`, retries, or
  copied conversation databases.
- Keep the scan read-only with respect to Antigravity files.
- Preserve TokenScope's durable-history behavior when source files disappear.
- Expose Antigravity everywhere Claude Code and Codex are provider choices:
  settings, scans, dashboard, costs, CLI help, Docker, and documentation.
- Keep the project stdlib-only.

## Non-goals for the first release

- Antigravity quota/remaining-plan limits. The referenced adapter exposes
  historical token usage, not an authoritative subscription quota contract.
- Reading prompts, responses, tool arguments, credentials, or other conversation
  content.
- Gemini CLI support. Antigravity must remain a separate source even though both
  products use directories below `~/.gemini`.
- Cloud synchronization or authenticated Google APIs.
- Treating API-equivalent cost estimates as actual Antigravity plan billing.
- A destructive command for purging retained history from deleted source files.

## Existing TokenScope contracts affected

| Area | Current contract | Required change |
|---|---|---|
| Source identity | `claude_code` and `codex` are repeated across modules | Add `antigravity`; preferably centralize ids/order in `sources.py` |
| Scanner | JSONL files, incremental by mtime and line count | Add SQLite discovery, read-only snapshots, protobuf parsing, and database signatures |
| Storage | Normalized `sessions`/`turns`; source-scoped uniqueness | Reuse normalized tables and add a small staging/provenance table for mutable Antigravity databases |
| Enabled providers | `settings.scan_source()` returns `all` or one source | Replace with a collection-capable contract; two enabled providers out of three must work |
| Pricing | One table per source | Add an Antigravity table and model normalization; unknown models remain `n/a` |
| Quota | Dashboard asks `quota.py` for every selected source | Add a `quota: false` capability and do not poll Antigravity |
| Dashboard | Two static tabs and several `selectedSource === 'codex'` branches | Make provider tabs/order and token semantics capability-driven |
| CLI | `--projects-dir`, `--codex-dir`, and one `--source` | Add `--antigravity-dir`; retain explicit one-source scans and support configured source sets |
| Docker | Mounts `~/.claude` and optional `~/.codex` read-only | Mount only the needed Antigravity roots read-only |
| Packaging | Flat `py-modules` list and explicit Docker `COPY` | Include new `sources.py` and `antigravity.py` modules |

Two existing details are especially important:

1. `settings.scan_source()` cannot represent `{claude_code, antigravity}` or
   `{codex, antigravity}`. Do not extend its current return-value trick.
2. `processed_files.lines` describes append-only text. An Antigravity database
   can update rows in place and may have a live `-wal` file, so line-based
   incremental parsing would be incorrect.

## Proposed architecture

```text
Antigravity roots
  -> discover canonical *.db paths
  -> open each changed DB read-only
  -> read gen_metadata / steps / trajectory metadata in one SQLite snapshot
  -> decode only required protobuf fields
  -> write normalized, provenance-bearing staging events
  -> deduplicate all retained Antigravity events by identity graph
  -> materialize source='antigravity' sessions + turns
  -> existing CLI/dashboard queries and per-turn cost calculation
```

### New top-level modules

`sources.py`

- Own `SOURCE_CLAUDE`, `SOURCE_CODEX`, `SOURCE_ANTIGRAVITY`, and
  `SOURCE_ORDER`.
- Remove source-id duplication from `scanner.py`, `pricing.py`, `settings.py`,
  `quota.py`, and `dashboard.py` while preserving compatibility imports where
  tests or external callers rely on them.
- Keep UI-specific labels and capabilities in `dashboard.SOURCE_CONFIG`.

`antigravity.py`

- Own default paths and `ANTIGRAVITY_DATA_DIR` parsing.
- Discover databases.
- Open and validate SQLite files.
- Decode the minimal protobuf wire format.
- Convert rows into normalized staging events.
- Normalize models and deduplicate events.
- Contain no dashboard, CLI, or global-database policy.

Keeping this logic outside `scanner.py` prevents a third large parser from
making the scan coordinator harder to reason about.

## Source discovery contract

Default roots, matching the current ccusage adapter:

```text
~/.gemini/antigravity/conversations/
~/.gemini/antigravity-cli/conversations/
~/.gemini/antigravity-ide/conversations/
~/.gemini/antigravity-backup/conversations/
~/.config/antigravity/conversations/
```

Configuration:

- `ANTIGRAVITY_DATA_DIR` accepts comma-separated paths.
- A configured path may be a data root containing `conversations/` or the
  conversation directory itself.
- `--antigravity-dir` uses the same comma-separated grammar and overrides the
  environment for that invocation.
- Recursively discover only `.db` files.
- Canonicalize paths where possible, deduplicate aliases/symlinks, and sort for
  deterministic session ownership when databases contain copied events.
- Silently skip missing default roots. A user-supplied path that exists but is
  unreadable should be reported in the scan result.

Do not treat all of `~/.gemini` as one logical source. In particular, do not
scan Gemini CLI JSON/JSONL data while implementing this feature.

## Read-only SQLite handling

Use a URI-mode connection so a missing path cannot be created accidentally:

```python
uri = Path(path).resolve().as_uri() + "?mode=ro"
conn = sqlite3.connect(uri, uri=True)
conn.execute("PRAGMA query_only = ON")
conn.execute("PRAGMA busy_timeout = 5000")
```

Implementation notes:

- Start one read transaction and read all required tables within that snapshot.
- Never issue DDL/DML against an Antigravity connection.
- Required schema: `gen_metadata(idx, data)`.
- Optional schema: `steps(idx, metadata)` and
  `trajectory_metadata_blob(data)`.
- If an optional table is absent, continue without it.
- If an optional table exists but has the wrong columns/types, report a parser
  error; do not silently pretend it was absent.
- Read `gen_metadata` and `steps` in ascending `idx` order.
- On a database-level failure, retain that database's last successfully staged
  events and leave its processed signature unchanged so the next scan retries.
- Include the path and table/row index in errors, but never dump BLOB contents.

### Change detection, including WAL

Add a `signature TEXT` column to `processed_files`, or introduce an equivalent
provider-file state table. For Antigravity, the signature should include:

- parser revision;
- database `st_mtime_ns` and size;
- `-wal` `st_mtime_ns` and size when present;
- optionally `-shm` metadata for diagnostics, though `-wal` is the meaningful
  content signal.

Take the signature before and after parsing. If it changed while reading, do not
commit the staged replacement; retry once or defer to the next scan. This avoids
marking a moving database as fully processed.

Always reparse the full changed database. `start_line` is not meaningful for
SQLite, and updated rows must be able to correct earlier normalized values.

## Minimal protobuf decoder

Do not add `protobuf` or require `protoc`. Implement the same small wire-level
reader used conceptually by ccusage:

- wire type `0`: unsigned varint;
- wire type `1`: skip 8-byte fixed64;
- wire type `2`: length-delimited bytes/message/string;
- wire type `5`: skip 4-byte fixed32;
- reject unsupported group wire types, zero field numbers, truncated values,
  lengths outside the BLOB, and varints longer than 10 bytes.

Accessor semantics matter:

- scalar varints and strings: last occurrence wins;
- singular embedded message: first occurrence;
- repeated embedded messages: preserve source order.

These rules must be unit-tested independently from SQLite parsing.

## Protobuf field map

### `gen_metadata.data`

1. Decode the root and read field `1` as the chat-model message.
2. Within the chat-model message:

| Field | Meaning |
|---:|---|
| `3` | numeric model id |
| `4` | primary `ModelUsage` |
| `9` | generation-info message; its field `4` is a timestamp message |
| `17` | repeated retry info; retry-info field `2` is `ModelUsage` |
| `19`, then `21` | model display/id string candidates |

### `steps.metadata`

| Field | Meaning |
|---:|---|
| `1` or `8` | timestamp message; prefer `8` |
| `9` | primary `ModelUsage` |
| `24` | model-info message |
| `28` | repeated retry info; nested field `2` is `ModelUsage` |

Within model-info:

| Field | Meaning |
|---:|---|
| `1` | numeric model id |
| `7` | API provider enum |
| `12`, fallback `8` | model name |

### `ModelUsage`

| Field | TokenScope destination |
|---:|---|
| `1` | numeric model id used for model resolution |
| `2` | fresh `input_tokens` |
| `3` | total output tokens, including reasoning |
| `4` | `cache_creation_tokens` |
| `5` | `cache_read_tokens` |
| `6` | API provider enum |
| `7` | message identity |
| `9` | reasoning token subset |
| `10` | visible output token subset |
| `11` | response identity |
| `12` | provider-assigned message identity |

Ignore zero-token records after considering all six token fields.

### Timestamp message

- Field `1`: Unix seconds, required and greater than zero.
- Field `2`: nanoseconds, clamped to `999_999_999`.
- Convert to UTC RFC 3339 with millisecond precision.

Timestamp precedence:

1. generation/step timestamp;
2. a higher-quality timestamp already associated with the same response,
   provider, or message identity;
3. first usable timestamp in `trajectory_metadata_blob` root field `2`;
4. database modification time;
5. Unix epoch only if every other source is unavailable.

Keep a timestamp rank with staging events so duplicate copies choose the best
timestamp deterministically. Equal-rank duplicates choose the earlier value.

## Token normalization invariant

Antigravity may expose total output, visible output, and reasoning in partially
redundant combinations. Normalize defensively:

```python
total_output = max(raw_total_output, visible_output + reasoning)
visible_output = max(visible_output, total_output - reasoning)
reasoning = max(reasoning, total_output - visible_output)
```

For TokenScope storage:

- `turns.input_tokens` = fresh input only;
- `turns.cache_creation_tokens` = cache writes;
- `turns.cache_read_tokens` = cache reads;
- `turns.output_tokens` = normalized total output, including reasoning;
- `turns.reasoning_output_tokens` = reasoning subset of output.

Do not add `reasoning_output_tokens` to `output_tokens` in costs or total-token
calculations. This matches TokenScope's existing Codex invariant and prevents
double billing. If a future UI needs visible-only output, derive it as
`max(output_tokens - reasoning_output_tokens, 0)` or add an explicitly named
column in a separate migration.

For Antigravity, input and cache buckets are independent. Therefore the generic
cost formula is correct; do not use the Codex branch that subtracts cached input
from `input_tokens`.

## Model resolution and normalization

Resolution order for each usage event:

1. `ModelUsage.model_id`;
2. step/generation numeric model id;
3. explicit step/generation model text;
4. most recently observed generation model for continuation rows;
5. last known generation model as a step fallback;
6. `gemini-internal-model` as an unpriced, visible fallback.

Port the upstream numeric-id and alias tests rather than relying on substring
guessing. The reviewed adapter currently includes, among others:

- numeric ids `246` -> `gemini-2.5-pro`, `312` -> `gemini-2.5-flash`,
  `313`/`329` -> `gemini-2.5-flash-thinking`;
- `281`/`282` -> `claude-4-sonnet`, `290`/`291` -> `claude-4-opus`;
- display names such as `Gemini 3 Pro` -> `gemini-3-pro`;
- internal aliases such as `gemini-3-flash-agent` -> the upstream canonical
  pricing id;
- placeholder ids that newer Antigravity versions use for named models.

Keep normalization in one function, strip parenthesized presentation suffixes,
and accept already canonical `gemini-*`, `claude-*`, and `gpt-*` ids. Unknown
values must remain explicit and unpriced; never silently map them to a nearby
family.

Numeric/placeholder aliases are the most schema-drift-prone part of the
integration. Put them behind `ANTIGRAVITY_PARSER_REVISION`, document their
source, and review upstream changes before releases.

## Deduplication and durable history

The same request may appear in generation metadata, step metadata, a retry, and
another copied database. Summing those copies would materially overstate usage.

Identity keys, in order of signal quality:

```text
response:<field 11>
provider:<field 12>
message:<field 7>
```

An event may carry several keys. Deduplication must therefore form connected
components, not merely choose one preferred key. If event A shares a response id
with B and B shares a provider id with C, all three belong to one component.

Merge duplicate components as follows:

- take the maximum of each token bucket, never the sum;
- recompute total output and total tokens after merging;
- prefer a real model over the unknown fallback;
- fill a missing provider enum from a duplicate;
- prefer timestamp and message id by their ranks;
- retain the union of every identity.

### Recommended staging table

Antigravity databases are mutable, while TokenScope also promises durable
history after source logs disappear. A small normalized staging table reconciles
those requirements:

```sql
CREATE TABLE IF NOT EXISTS source_events (
    source                  TEXT NOT NULL,
    origin_path             TEXT NOT NULL,
    origin_key              TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    timestamp               TEXT,
    timestamp_rank          INTEGER NOT NULL DEFAULT 0,
    model                   TEXT,
    provider_code           INTEGER,
    input_tokens            INTEGER NOT NULL DEFAULT 0,
    output_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens   INTEGER NOT NULL DEFAULT 0,
    reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
    message_id              TEXT,
    message_id_rank         INTEGER NOT NULL DEFAULT 0,
    identities_json         TEXT NOT NULL DEFAULT '[]',
    parser_revision         TEXT NOT NULL,
    PRIMARY KEY (source, origin_path, origin_key)
);
```

Use this table only for Antigravity initially; it need not force a Claude/Codex
migration.

`origin_key` is stable within a physical database and independent of token
values, for example:

```text
gen:<idx>:primary
gen:<idx>:retry:<ordinal>
step:<idx>:primary
step:<idx>:retry:<ordinal>
```

On a successfully parsed changed database:

1. begin one TokenScope database transaction;
2. delete its old staged rows by `(source, origin_path)`;
3. insert the newly parsed staged rows;
4. load all retained Antigravity staging events;
5. globally deduplicate them by the identity graph;
6. replace only `sessions` and `turns` where `source = 'antigravity'`;
7. update the processed signature and parser revision;
8. commit.

If a source database disappears, keep its staged rows. This preserves historical
usage just as TokenScope currently preserves Claude/Codex history after source
logs are pruned. If a present database becomes malformed or locked, retain its
last good staged rows and report the error.

For an identity-bearing component, build `source_record_id` from a stable hash
of its sorted identity union. For an event with no identities, hash
`origin_path + origin_key`. Set `message_id` to the highest-ranked raw identity
when available. Sorting paths and events before component construction keeps
session ownership deterministic.

This staging design costs one source-local rematerialization when an Antigravity
database changes, but it avoids the much worse failure modes of stale mutable
rows, copied-database duplicates, or accidental historical deletion.

## Parser revision and migration behavior

Add `ANTIGRAVITY_PARSER_REVISION` beside the Codex revision.

Bump it whenever any of these changes:

- protobuf field interpretation;
- model-id/alias normalization;
- output/reasoning normalization;
- identity extraction or deduplication;
- timestamp precedence;
- long-context prompt-token interpretation.

When the revision changes:

- reparse every currently discoverable Antigravity database;
- rebuild materialized Antigravity rows from all retained staging events;
- leave Claude Code and Codex rows untouched;
- retain staged history whose original database no longer exists, since it
  cannot be reconstructed.

Schema migrations must remain additive and idempotent through `init_db()`.

## Session mapping

- Use the database filename stem as the Antigravity conversation/session id,
  matching the reference adapter.
- If the stem is missing or unusable, use a deterministic hash of the canonical
  path rather than `unknown`, which would collapse many databases.
- `project_name`: `Antigravity` unless a verified metadata field is added later.
- `git_branch`: empty.
- `topic`: empty; do not read conversation text merely to manufacture one.
- Session primary model: reuse TokenScope's existing most-common-turn-model
  aggregation.
- One database may contain several models; price every turn by its own model.

## Pricing plan

Add `SOURCE_ANTIGRAVITY` to `PRICING_BY_SOURCE`.

- Seed only canonical model ids whose public rates have been verified.
- Keep source-specific entries even when a canonical id is also present under
  Claude Code. This preserves the settings page's per-provider override model.
- Reuse small rate constructors/data constants internally to avoid duplicated
  Claude or Gemini figures drifting, but continue resolving through
  `get_pricing(model, source='antigravity')`.
- Label the basis: `Underlying model API-equivalent estimate; not Antigravity plan billing`.
- Unknown/internal models return `None`, cost `$0`, and show `n/a` exactly as
  unknown Claude/Codex models do.
- Preserve exact-match then longest `known-key + '-'` prefix matching. Sort
  browser keys longest-first explicitly rather than relying on object insertion
  order.
- Antigravity's field `2` is fresh input, so use the generic independent-cache
  formula. Codex's inclusive-input special case must remain Codex-only.

If a priced model has a long-context tier, calculate its threshold input from
fresh input plus cache reads plus cache writes. Do not reuse Codex's inclusive
prompt count without a provider-aware helper.

The first implementation PR should separate parser/model normalization from the
actual price-table commit so rates can be reviewed independently.

## Settings and enabled-source migration

Change the settings contract from a scalar scan source to an ordered collection:

```python
def scan_sources(data=None):
    return tuple(enabled_sources(data))
```

Then let `scanner.scan()` accept either:

- an explicit scalar from CLI (`claude_code`, `codex`, `antigravity`, `all`); or
- an internal iterable/set of enabled sources from Settings.

Preserve `scan_source()` temporarily as a compatibility wrapper only if tests or
third-party imports require it. It must not be used by the dashboard once three
providers exist.

Settings migration:

- bump `SCHEMA_VERSION` to `2`;
- fresh settings: all three providers enabled;
- existing schema-v1 settings: preserve the two saved flags and add
  `antigravity: true`, matching the fresh-install default;
- show the new provider in Settings so the user can opt out;
- once saved as schema v2, normal strict validation applies;
- keep the invariant that at least one provider is enabled.

Automatic discovery is the selected product behavior and is called out in the
release notes. Keep the v1 migration explicit rather than letting the behavior
emerge accidentally from `defaults()`.

Tests must explicitly cover all seven non-empty enabled-source combinations.

## Dashboard plan

Add this provider metadata:

```python
SOURCE_ANTIGRAVITY: {
    "label": "Antigravity",
    "short_label": "Antigravity",
    "pricing_basis": "Underlying model API-equivalent estimate; not Antigravity plan billing",
    "capabilities": {
        "cache": True,
        "reasoning_tokens": True,
        "subagents": False,
        "quota": False,
        "input_includes_cache": False,
    },
}
```

Required UI refactors:

- Render source tabs from server-injected source metadata/order instead of two
  hard-coded buttons.
- Derive JavaScript `SOURCE_ORDER` and labels from `APP_CONFIG`; do not create a
  third duplicate list.
- Replace `selectedSource === 'codex'` token arithmetic with
  `input_includes_cache` capability checks.
- Continue showing reasoning for Antigravity, remembering it is a subset of
  output rather than an additional billed bucket.
- Hide the quota panel and refresh action when `quota` is false. Do not display
  a fabricated `5h` row or poll unrelated JSONL paths.
- Keep subagent sections hidden.
- Ensure URL persistence accepts `?source=antigravity` and falls back to the
  first enabled source if disabled.
- Keep the same shared charts/tables; do not clone an Antigravity dashboard.
- Make model-filter grouping neutral. Antigravity may contain Gemini, Claude,
  and GPT-family models, so labels such as `Anthropic`/`Other providers` must be
  based on model family or replaced with `Priced`/`Unpriced`.
- Add a restrained Antigravity accent. If an external brand asset is used,
  confirm its license first; otherwise use a TokenScope-owned neutral glyph.

`GET /api/data?source=antigravity` should return the same top-level shape as
other sources. `quota` may be `null` when capability-disabled, or an explicit
unavailable object, but the client/server contract and tests must choose one.

## Quota behavior

Do not route Antigravity through `_codex_events()` as a fallback. The current
two-way branch in `quota._read_file()` must become explicit per source so an
unknown provider cannot be interpreted as Codex.

Recommended first-release behavior:

- `SOURCE_CONFIG.capabilities.quota = false`;
- `resolve_quota()` returns `None` without scanning files or making a request;
- the dashboard hides the entire quota panel for Antigravity;
- no sign-in action is offered.

Add quota support later only if a stable local or official contract exposes
remaining limits. Historical token usage is not quota data.

## CLI plan

Examples after implementation:

```bash
tokenscope scan --source antigravity
tokenscope scan --source antigravity --antigravity-dir /path/to/root
ANTIGRAVITY_DATA_DIR=/one,/two tokenscope scan --source antigravity
tokenscope dashboard --antigravity-dir /path/to/root
```

Update `cmd_scan`, `cmd_dashboard`, startup background scan, and `/api/rescan`
to pass configured Antigravity roots explicitly, preserving the existing test
contract where module globals can be patched.

Change CLI copy from `Scan JSONL files` to `Scan local usage data`. Reject an
unknown `--source` before opening/writing the TokenScope database.

Read commands (`today`, `week`, `stats`) are already source-aware in their main
aggregations. Audit user-facing Claude/Codex-specific labels such as cached-input
notes so a three-source total is not misleading.

## Docker and packaging plan

Packaging:

- Add `sources` and `antigravity` to `pyproject.toml` `py-modules`.
- Add both files to Docker's explicit `COPY` list.

Docker runner mounts, only when present:

```bash
-v "$HOME/.gemini:/root/.gemini:ro"
-v "$HOME/.config/antigravity:/root/.config/antigravity:ro"
```

Do not mount all of `~/.config`. Continue storing TokenScope's database/settings
in the writable `/data` volume. Document that the Antigravity mounts are
read-only and optional.

Check Windows documentation separately. The ccusage reference lists the Unix
roots above; do not claim Windows path support until verified on a real install
or a reliable upstream fixture.

## Implementation phases

### Phase 0: fixture and schema spike

1. Obtain one local, user-owned Antigravity database and inspect only table
   names/columns/counts; do not print BLOBs or conversation content.
2. Confirm the reviewed field map against at least one current Antigravity
   version.
3. Create a synthetic test-database builder using `sqlite3` and tiny protobuf
   encoding helpers.
4. Record the Antigravity version(s) validated in test comments/documentation.

Exit condition: a synthetic and a local redacted validation produce matching
token buckets without reading prompt/response text.

### Phase 1: provider registry and settings contract

1. Add `sources.py` and migrate source constants/order.
2. Add schema-v2 settings migration.
3. Introduce `scan_sources()` and iterable scanner selection.
4. Test every non-empty enabled-source combination.

Exit condition: enabling any two providers scans exactly those two directories.

### Phase 2: Antigravity parser

1. Implement path discovery and read-only SQLite opening.
2. Implement the isolated protobuf decoder.
3. Parse generation, step, retry, model, provider, identity, and timestamp fields.
4. Normalize output/reasoning and model aliases.
5. Return provenance-bearing events without writing TokenScope's database.

Exit condition: parser tests cover all field paths and failures.

### Phase 3: incremental ingestion and materialization

1. Add processed signatures and staging schema.
2. Replace staging rows only after a complete successful file parse.
3. Implement identity-component deduplication and merge rules.
4. Materialize Antigravity turns/sessions in one transaction.
5. Add parser revision handling and scan-result counts/errors.

Exit condition: rescans, database updates, WAL changes, duplicates, retries, and
deleted source files all preserve correct totals.

### Phase 4: pricing

1. Add the source table and independently verified underlying-model rates.
2. Add aliases only through parser normalization, not fuzzy pricing fallbacks.
3. Verify Python and browser cost parity for input/cache/output/reasoning.
4. Verify unknown models remain `n/a` and user overrides work.

Exit condition: per-turn costs match hand-calculated fixture expectations.

### Phase 5: dashboard and quota capability

1. Add provider metadata and data-driven tabs/order.
2. Replace provider-name token arithmetic with capabilities.
3. Hide quota and subagent UI for Antigravity.
4. Add URL/settings/empty-state/export coverage.
5. Add or license an icon only after behavior is complete.

Exit condition: switching among all three providers keeps one shared renderer
and shows source-correct labels/totals.

### Phase 6: CLI, Docker, docs, and release

1. Add CLI/environment path options and help.
2. Add read-only Docker mounts and packaging entries.
3. Update README architecture/data-source/settings/pricing sections.
4. Add a CHANGELOG entry under the next `TBD` release.
5. Run the complete validation matrix.

Exit condition: source, installed package, and Docker workflows all discover the
same fixture data.

## Test matrix

### `tests/test_antigravity.py`

- default root discovery;
- environment/CLI override with parent and direct conversation directories;
- canonical-path deduplication and deterministic ordering;
- only `.db` files are opened;
- database opened read-only;
- missing required table reports the database path;
- absent optional tables succeed;
- present-but-invalid optional schema fails without replacing last good data;
- varint boundaries, duplicate scalar semantics, truncated fields, invalid wire
  types, invalid UTF-8, and oversized lengths;
- primary generation usage and continuation model inheritance;
- step usage, generation retries, and step retries;
- numeric model ids and every shipped alias;
- zero-token records ignored;
- fresh input/cache-write/cache-read remain separate;
- total output/reasoning normalization and no double count;
- timestamp precedence and nanosecond conversion;
- response/provider/message identity extraction;
- transitive deduplication and per-bucket max merge;
- no-id fallback identity;
- two databases with independent events;
- copied database/source records deduplicate globally.

### `tests/test_scanner.py`

- `source='antigravity'`, `source='all'`, and iterable source sets;
- unchanged database skipped;
- changed database fully reparsed;
- `-wal` signature change detected;
- same row updated in place without duplication;
- source database disappearing retains prior usage;
- parse failure retains last good materialization and retries later;
- parser revision rebuilds only Antigravity;
- Antigravity scan summaries appear in `by_source`;
- source-scoped session/message ids do not collide with Claude/Codex.

### Settings, pricing, dashboard, CLI, and packaging

- schema-v1 migration behavior and strict schema-v2 writes;
- all seven enabled-source combinations;
- Antigravity price overrides and reset behavior;
- longest-prefix parity in Python and JavaScript;
- API data source/capabilities/provider metadata;
- tab visibility, URL selection, reasoning/cache labels, and hidden quota;
- no Antigravity quota file walk or network request;
- Antigravity CSV rows preserve full session ids and source-correct costs;
- CLI parsing and explicit custom root propagation;
- startup/manual/automatic scans share the existing coordinator;
- Docker/`pyproject.toml` include every runtime module and required mount docs.

### Validation commands

```bash
python3 -m unittest tests.test_antigravity -v
python3 -m unittest tests.test_scanner tests.test_settings tests.test_pricing -v
python3 -m unittest tests.test_dashboard tests.test_cli tests.test_quota -v
python3 -m unittest discover -s tests -v
python3 -m py_compile scanner.py antigravity.py sources.py cli.py dashboard.py pricing.py quota.py settings.py
git diff --check
```

Do not run tests against the user's real `~/.claude/usage.db`. Use temporary
TokenScope and Antigravity databases throughout automated tests.

## Error and observability contract

Extend scan results with non-secret provider errors, for example:

```json
{
  "by_source": {
    "antigravity": {
      "new": 1,
      "updated": 0,
      "skipped": 3,
      "turns": 42,
      "sessions": 1,
      "errors": [
        {"path": "~/.gemini/antigravity/conversations/example.db", "message": "missing gen_metadata table"}
      ]
    }
  }
}
```

Rules:

- display home-relative paths where possible;
- never include BLOB bytes, prompts, responses, credentials, or SQL data values;
- one broken database must not erase last good usage;
- distinguish `no databases found` from `databases found but unreadable`;
- the dashboard rescan button may summarize an error count and log details to
  the server console; it must not claim success with an empty replacement.

## Main risks and mitigations

| Risk | Consequence | Mitigation |
|---|---|---|
| Private protobuf schema changes | Empty or misattributed usage | strict decoder, parser revision, synthetic fixtures, visible errors, upstream review |
| SQLite WAL changes missed | stale dashboard | include `-wal` in signatures and verify pre/post read |
| Same usage stored in several locations | inflated tokens/cost | identity-component deduplication and max-bucket merge |
| Database mutates after first scan | stale or duplicate rows | full per-database reparse into provenance staging |
| Source database is deleted | lost history | retain staging rows for missing origins |
| Reasoning counted twice | inflated tokens/cost | store output inclusive; reasoning as a subset and test parity |
| Model alias guessed incorrectly | wrong cost | explicit alias map; unknown is `n/a`; overrides remain available |
| Third provider breaks settings selection | skipped enabled provider | collection-based scan contract and seven-combination tests |
| Quota fallback interprets source as Codex | fabricated limit UI | explicit quota capability and exhaustive source dispatch |
| Packaging forgets new module/path | installed/Docker crash or empty data | packaging tests and optional read-only mounts |
| Real source contains private conversation data | privacy leak in logs/fixtures | select only metadata columns; never log BLOBs; synthetic checked-in fixtures |

## Acceptance criteria

The feature is complete only when all of the following are true:

- Antigravity can be enabled/disabled independently and a disabled source is not
  walked, queried, or polled.
- Any two-provider combination works, not only one or all three.
- Default and overridden roots discover the documented `.db` files.
- Antigravity databases are opened read-only and source content is never copied
  into logs or API responses.
- The fixture's fresh input, cache writes, cache reads, total output, and
  reasoning exactly match stored turns and session totals.
- Generation, step, retry, and copied-database representations count once.
- A second unchanged scan adds zero turns.
- Updating an existing SQLite row updates normalized usage without duplication.
- A malformed/locked database preserves the last good result and reports an
  actionable non-secret error.
- Removing a source database does not erase already imported history.
- Each turn uses its own normalized model and cost; unknown models show `n/a`.
- The dashboard has one shared renderer for Claude Code, Codex, and Antigravity.
- Antigravity shows cache/reasoning data but no fabricated quota or subagent UI.
- CLI, source install, and Docker agree on discovered sessions/tokens.
- Existing Claude Code and Codex regression suites remain green.
- Full unit tests, compilation, and `git diff --check` pass.
- README and CHANGELOG explain that Antigravity costs are underlying-model
  API-equivalent estimates, not subscription billing.

## Suggested commit boundaries

1. `refactor(sources): support arbitrary enabled provider sets`
2. `feat(scanner): decode Antigravity conversation metadata`
3. `feat(scanner): stage and deduplicate mutable Antigravity usage`
4. `feat(pricing): add verified Antigravity model estimates`
5. `feat(dashboard): add capability-driven Antigravity view`
6. `feat(cli): expose Antigravity paths and Docker mounts`
7. `docs: document Antigravity source and limitations`

Keep pricing and presentation follow-ups separate from the parser/storage commit.
That makes token-accounting review possible before model rates or visual details
can obscure the core correctness diff.
