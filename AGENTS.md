# AGENTS.md

Guidance for any coding agent (Codex, Claude Code, etc.) working on this repository.

> **Naming note.** This project *analyzes* Claude Code's local usage logs, so "Claude Code" below always refers to that product (the source of the JSONL data) — not to the agent reading this file. The agent working on the codebase is referred to as "the coding agent" or just "you".

## Project shape

Flat top-level Python modules, stdlib only, no `pip install` step. Python 3.8+.

- [scanner.py](scanner.py) — parses Claude Code JSONL transcripts into a SQLite DB at `~/.claude/usage.db`. Also holds `VERSION`, the single source of truth.
- [cli.py](cli.py) — terminal commands (`scan` / `today` / `week` / `stats` / `dashboard`).
- [dashboard.py](dashboard.py) — single-file `http.server` serving an embedded HTML/JS SPA on `localhost:8080`.
- [pricing.py](pricing.py) — the one price table, plus the user-override layer.
- [quota.py](quota.py) — provider plan-limit snapshots for the sidebar panel.
- [settings.py](settings.py) — the user's enabled providers and price overrides, on disk.

Use `python` on Windows, `python3` on macOS/Linux. Both work the same.

## Common commands

```
python cli.py scan                  # incremental scan (fast on re-run)
python cli.py today                 # today's usage by model
python cli.py week                  # last 7 days, per-day + by-model
python cli.py stats                 # all-time stats
python cli.py dashboard                          # scan + open http://localhost:8080
python cli.py dashboard --host 0.0.0.0 --port 9000
python cli.py scan --projects-dir PATH           # scan a custom transcripts dir
# or via env vars:
HOST=0.0.0.0 PORT=9000 python cli.py dashboard

python -m unittest discover -s tests -v             # full test suite (CI runs this)
python -m unittest tests.test_scanner -v            # one file
python -m unittest tests.test_scanner.TestProjectNameFromCwd.test_windows_path  # one test
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the suite on Python 3.9 / 3.11 / 3.12 against `main` and PRs.

## Architecture

### Data flow

```
~/.claude/projects/**/*.jsonl   →   scanner.parse_jsonl_file()
~/Library/.../Xcode/...                  ↓
                              aggregate_sessions() → upsert_sessions() + insert_turns()
                                         ↓
                              ~/.claude/usage.db (SQLite)
                                         ↓
                  cli.py queries   ←──────────→   dashboard.py /api/data
```

By default the scanner walks both `~/.claude/projects/` and the Xcode coding-assistant directory; missing dirs are silently skipped. Override with `--projects-dir`.

### SQLite schema (created/migrated in [scanner.py](scanner.py) `init_db`)

- **`turns`** — one row per assistant API response. The source of truth for tokens and per-model attribution.
- **`sessions`** — aggregated per session (denormalized totals + chosen primary model).
- **`processed_files`** — incremental-scan tracking: `(path, mtime, lines)`. A file is skipped if its mtime matches; if it grew, only lines past the stored `lines` count are processed.

A conditional unique index on `turns.message_id` (where non-empty) lets `INSERT OR IGNORE` cheaply dedupe replays across rescans.

### Non-obvious invariants

These three things will bite you if you don't know them:

1. **Streaming dedupe by `message.id`.** Claude Code writes multiple JSONL records per API response — only the *last* one for a given `message.id` has the final usage tallies. `parse_jsonl_file` keeps the last record per `message_id` in a dict; earlier records are discarded. Don't sum across records of the same `message_id`.

2. **Session totals are recomputed from `turns` at the end of `scan()`.** During an incremental scan `upsert_sessions` adds tokens additively, but `insert_turns` uses `INSERT OR IGNORE` against the `message_id` unique index — so if a turn is a duplicate, session totals would drift. The final `UPDATE sessions ... (SELECT SUM ... FROM turns)` block reconciles this. Preserve it if you refactor scan logic.

3. **Session primary model priority is opus > sonnet > haiku** (`_model_priority` in [scanner.py](scanner.py)). This prevents a subagent's haiku turn from overwriting the session's opus model when an existing session is updated. Per-turn model is always honored in the `turns` table; only the session-level summary uses the priority.

### Cost calculation

Costs are computed **per turn** (each turn knows its own model), then summed. This is true in both the CLI ([cli.py](cli.py) `calc_cost`) and the dashboard JS ([dashboard.py](dashboard.py) `calcCost` inside the embedded HTML). Aggregating tokens first and applying a single price is wrong for sessions that span multiple models.

Pricing lives in **one** place: [pricing.py](pricing.py) `PRICING_BY_SOURCE`. The dashboard no longer keeps a copy — `do_GET` injects the same dict into the page as `window.APP_CONFIG.pricing`, so the browser and the CLI cannot drift. Add a model in `pricing.py` and both surfaces pick it up.

**Read prices through `get_pricing` / `pricing_by_source()`, never `PRICING_BY_SOURCE` directly.** The Settings page lets users override a rate or add a model, and `pricing.set_overrides` layers those on top of the shipped table. `BUILTIN_PRICING_BY_SOURCE` is the un-overridden table and exists only so the Settings page can show a default and offer "Reset". An override replaces a built-in entry wholesale rather than merging field by field, so a partial override can't leave a stale rate behind. `settings.apply()` is what installs the layer — the CLI calls it once in `main()`, the dashboard calls it per request.

`get_pricing` / `getPricing` resolve in two tiers: exact match → **longest** matching `key + "-"` prefix (handles date-suffixed IDs like `claude-opus-4-7-20260215`). Longest-first matters: `gpt-5.4-mini` must not be priced as `gpt-5.4`, and `claude-opus-4-1-20250805` must not be priced as `claude-opus-4`. Models that match nothing return `None` and are billed at $0 (shown as `n/a`) — intentional, so local/3rd-party models (gemma, glm, etc.) aren't charged at Sonnet rates. The flip side is that a *real* model missing from the table silently costs nothing, so keep retired Claude ids and older `gpt-5.x` ids in the table rather than pruning them.

Codex families with a `long_context_threshold` (272K prompt tokens) reprice the **whole** request at `long_*` rates once crossed. The flag is computed per turn at scan time (`turns.is_long_context`), every aggregate groups by it, and `pricing.long_context_price` / the JS `longContextPrice` apply the overlay. Changing a threshold or a `long_*` rate means bumping `scanner.CODEX_PARSER_REVISION` so stored flags are recomputed.

### Settings

[settings.py](settings.py) owns `~/.claude/tokenscope-settings.json` (override with `TOKENSCOPE_SETTINGS`; the Docker image points it at the writable `/data` volume because `~/.claude` is mounted read-only). Two keys: `sources` (which providers are active) and `pricing_overrides` (`{source: {model: rates}}`).

- **Reads are forgiving, writes are strict.** `normalize(raw, strict=False)` drops junk so a hand-edited or truncated file degrades to defaults instead of taking the dashboard down; `strict=True` (only the `POST /api/settings` path) raises `SettingsError`, whose message goes straight to the user. Don't "helpfully" make the write path lenient — silently dropping a rate the user typed is worse than rejecting it.
- `save()` writes to a temp file in the same directory and `os.replace`s it, so a crash mid-write can't truncate the settings file.
- At least one provider must stay enabled. Enforced in `normalize` (strict), in the browser before the click lands, and defended in `enabled_sources()` — a settings file with none falls back to all.
- A disabled provider must be genuinely inert, not just hidden: `settings.scan_source()` translates the enabled set into `scanner.scan`'s `source` argument, so its log directory is never walked and its quota never polled. That's the whole point of the toggle.
- Changing a long-context **threshold** does not re-flag stored turns (`turns.is_long_context` is computed at scan time and gated on `CODEX_PARSER_REVISION`). The Settings UI says so; keep that caveat if you touch it.

### Dashboard server

`http.server.BaseHTTPRequestHandler`-based:
- `GET /api/data` → JSON snapshot from `get_dashboard_data()`. Returns *all* history; client-side filters by date range and model.
- `GET /api/settings` → current settings, the built-in price table, and field lists (see `settings_payload`).
- `POST /api/settings` → validate + save + `settings.apply()`, then return the same payload. `400` carries the `SettingsError` message.
- `POST /api/rescan` → incremental scan. Passes `db_path` and `projects_dirs` explicitly so tests that monkey-patch the module globals work — scan's default arg values are frozen at def time, so don't switch to bare defaults.
- `GET /icon.svg` / `/codex-icon.svg` / `/favicon.svg` → the only static files served, via the `ASSET_ROUTES` table. Keep it a fixed table so the handler can't be talked into serving an arbitrary path.

`do_GET` / `do_POST` call `settings.apply()` first, so a save (or a CLI-side edit) takes effect without a restart.

The entire UI lives in `HTML_TEMPLATE` as a raw string. Chart.js is loaded from CDN. `window.APP_CONFIG` carries the version, the *effective* price table, and the settings payload, so the first paint already knows which providers to show.

Two views share the shell: `#dashboard-panel` and `#settings-panel`, toggled by `setView()` and reflected in `?view=settings`. The settings page is a draft editor — every control writes `draftSettings`, `isSettingsDirty()` compares a key-sorted serialization against `savedSettings`, and nothing reaches disk until the confirm dialog is accepted. Rate keystrokes deliberately skip the full re-render (`refreshRowChrome`) so the caret keeps its place.

Client-side UI state (collapsed sections) is kept in **`localStorage`**, which is keyed by the page's origin.

## Testing notes

- `tests/test_scanner.py` and `tests/test_dashboard.py` use `tempfile.NamedTemporaryFile` for an isolated DB; never touch the user's real `~/.claude/usage.db`.
- The `/api/rescan` test patches `dashboard.DB_PATH` and `scanner.DEFAULT_PROJECTS_DIRS` — keep that contract intact (see commit 8ae2664).
- On Windows, `~/.claude/` may not exist on a fresh checkout. `get_db` creates the parent dir (`mkdir(parents=True, exist_ok=True)`) — don't remove that or `sqlite3.connect` will fail in CI / fresh installs (commit b5d1e15).

## Respecting contributors

When merging community PRs, **preserve the original author's commit so they get GitHub contributor credit**. In practice:

- `git fetch origin pull/<N>/head:pr-<N>` → `git merge --no-ff pr-<N>` keeps the author commit verbatim inside the merge bubble (don't squash, don't rebase-flatten).
- For a partial merge — when only one hunk of a PR is wanted — use `git cherry-pick <commit-sha>` against the specific upstream commit so authorship is preserved. If the diff isn't a clean single commit, fall back to applying the hunk manually + adding a `Co-Authored-By: Name <email>` trailer.
- Improvements that the bot/maintainer makes _on top_ of a contributor's work go in **separate follow-up commits**, not amendments to the contributor's commit.
- When closing duplicate PRs (multiple authors fixed the same bug independently), thank each one and explain that landing the earliest version isn't a quality judgment.

This applies to all agents working on this repo, not just Claude Code.

## Versioning and releases

[SemVer](https://semver.org/). **`CHANGELOG.md` is the canonical version reference**; tags are a projection of it, created automatically.

The release flow:
1. While work accumulates on `DEV`, the `## vX.Y.Z — TBD` heading at the top of `CHANGELOG.md` collects bullets. (For automated triage runs, see the routine note below.)
2. When the maintainer is ready to release, they finalize the heading (`TBD` → today's date), bump `scanner.VERSION` to match the CHANGELOG version, run the version tests, merge `DEV → main` with `merge --no-ff` (so the release boundary is visible in `git log main`), and push `main`.
3. [`.github/workflows/tag-on-merge.yml`](.github/workflows/tag-on-merge.yml) fires on the push, sees the new `## vX.Y.Z` heading in the CHANGELOG diff, and:
   - creates a lightweight tag at the merge commit (**no `git tag` step for the maintainer**), then
   - publishes a **GitHub Release** for that tag using the matching CHANGELOG section as the release notes.

So every release is both a tag and a GitHub Release. The workflow is intentionally limited to the Python dashboard.

The workflow is idempotent: if the tag already exists (someone tagged manually before the workflow caught up) the tag step is a no-op, and if the Release already exists the release step is a no-op. It also no-ops entirely on pushes that don't add a new version heading (typo fixes, docs-only edits, etc.).

Existing tags `v1.0.0`, `v1.1.0`, `v1.1.1` are lightweight and were created by hand before the workflow existed. `v1.1.2` was the first tag created by the workflow. The workflow only *adds* missing tags; it never reconciles existing ones. Don't bother re-tagging the legacy ones.

### CHANGELOG conventions

The workflow trusts the CHANGELOG, so the format matters. Every new release entry on `DEV` follows this exact shape:

```
## vX.Y.Z — TBD

### <Area>

- One bullet per change, past tense, with a PR/issue link and `thanks @author` where the change came from a contributor (#73, thanks @thomasleveil)
```

Format rules the workflow relies on:

| Field | Required form | Why |
|---|---|---|
| Heading | `## vX.Y.Z` (exactly two `#`, the `v` prefix, three numeric components — strict semver) | The workflow regex `^## v[0-9]+\.[0-9]+\.[0-9]+([[:space:]]|$)` won't match anything else. `v1.1`, `v1.1.0-rc1`, `V1.1.0` are all silently ignored. |
| Separator | ` — ` (em-dash with surrounding spaces) | Cosmetic but consistent. The workflow ignores everything after the version. |
| Date | `TBD` while accumulating on `DEV`; replace with `YYYY-MM-DD` *at the moment of merging to `main`* | The workflow doesn't enforce dates — but a `TBD` heading that ships to main means the release looks unfinished forever. |
| Subsections | `### Dashboard`, `### Scanner`, `### Packaging`, `### Project / docs` — pick the smallest set that fits | Keeps the CHANGELOG scannable. |
| Bullets | Past tense, link the PR/issue with `#N`, credit external contributors with `thanks @login` | Lets readers (and future maintainers tracing history) find the source quickly. |

**The TBD → date rule is the only step a human must remember at release time.** If you forget, the workflow still tags correctly, but the CHANGELOG entry on main reads `## v1.1.3 — TBD` forever. Fix-up commit can correct it, but it'll feel sloppy.

Patch (`Z` increments) is the default for any release. Bump minor (`Y`) when a non-breaking user-visible feature lands (e.g. Today range button shipping alone would have been a minor in a different world). Bump major (`X`) only on breaking changes — there have been none and likely won't be soon. There's no automation around picking the right bump; the maintainer (or `/triage`) decides when writing the CHANGELOG heading on `DEV`.

## Weekly triage routine

The repo has a self-contained slash command at [.claude/commands/triage.md](.claude/commands/triage.md) that automates the weekly PR/issue cleanup we used to ship v1.1.0: classify open items with Codex, merge no-brainers to DEV preserving authorship, run tests, close duplicates / scope-violations with friendly messages, bump CHANGELOG by patch, push DEV. **The routine never pushes to `main`** — release decisions stay with the maintainer.

Register the Windows Task Scheduler entry with [scripts/setup-weekly-triage.ps1](scripts/setup-weekly-triage.ps1). Logs go to `logs/triage-*.log`.

If you're working on this repo and want to invoke the routine ad-hoc, just type `/triage` in Claude Code. Hard safety rails (test-passing gates, no security-sensitive auto-merges, no scope-changing merges, Codex sign-off required on closures) live inside `triage.md`.
