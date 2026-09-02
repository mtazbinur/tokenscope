# Changelog

All notable changes to TokenScope, newest first. Versions follow [SemVer](https://semver.org/),
and this file is the canonical version reference.

TokenScope's history starts at v1.0.0 — the entries of the project it grew out of
are not carried over here.

## v1.2.1 — TBD

### Antigravity

- Added optional local Antigravity conversation-database ingestion with read-only SQLite access, strict stdlib protobuf decoding, WAL-aware incremental signatures, source-local staging, and transitive deduplication across retries and copied databases.
- Added Antigravity provider settings, capability-driven dashboard tabs, underlying-model API-equivalent estimates, CLI path selection, and optional read-only Docker mounts. Antigravity quota limits are intentionally not fabricated.
- Enabled Antigravity by default for both fresh settings and schema-v1 upgrades, while preserving an explicit opt-out that prevents future scans from walking its directories.
- Excluded SQLite's mutable `-shm` coordination file from content signatures, preventing live conversation databases from being rejected as changed merely because the read itself refreshed shared-memory state.
- Added explicit API-equivalent pricing for the current Antigravity model ids observed in local databases (`gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.5-flash`, and Claude Sonnet 4.6); private placeholders such as `gemini-default` remain intentionally unpriced.
- Defaulted the model filter to **All models** so a newly observed or private model cannot hide otherwise valid usage merely because its price is still unknown.
- Extracted each session's workspace path, git branch, and a title (from its first prompt) from the conversation database's `trajectory_metadata_blob` and opening `steps` row, replacing the flat `Antigravity` / empty-branch / "Untitled" placeholders shown in Recent Sessions and the Cost by Project(+Branch) tables. Bumped `ANTIGRAVITY_PARSER_REVISION` to `2` so existing installs backfill this on their next scan.
- Stopped `gemini-default` (Antigravity's own "use whatever the client picked" routing tag) from surfacing as a distinct fake model — it now falls back to the session's last real model, the same treatment already given to unresolved internal ids.

### Dashboard

- Replaced the misleading five-minute SQLite-only refresh plus 30-minute rescan with one five-minute usage update that incrementally scans enabled providers and then reloads costs. Startup, manual, and automatic scans now share a process-wide coordinator, so concurrent dashboard tabs reuse the active scan instead of overlapping filesystem walks and SQLite writes.
- Made each provider's Model pricing table in Settings collapsible, so a long Codex or Antigravity model list no longer forces you to scroll past it to reach the next section.

## v1.2.0 — 2026-09-01

### Dashboard

- Added a **Sign in to Claude Code** button to the usage panel. It runs `claude auth login` on the server and watches for the renewed credential, so a expired sign-in no longer means a trip to the terminal. The route is gated on a loopback bind and a same-origin control token, and is hidden where the Claude Code CLI isn't installed.
- Fixed the usage panel's freshness label (`Local · 1:…`) being permanently truncated. The heading row carried the title, the timestamp and the refresh button in ~200px of sidebar, so the ellipsis always fired; the timestamp now sits on its own line and the title fits on one.
- Auth messages state what happened and let the surface supply the remedy, instead of hardcoding "run `claude auth login`" next to a button that does it.

### Scanner

- Fixed a signed-in user being reported as signed out. A locally-expired `expiresAt` is now verified against `/api/oauth/profile` before the panel claims the sign-in expired — the CLI and desktop app renew in-process and don't always write the new token back to the store TokenScope reads.
- `_refresh_credentials_via_cli` judges a refresh by whether the stored credential actually moved forward, not by the CLI's `loggedIn` field, which reports success for an exchange that never happened.
- A probe that fails (timeout, 429, 5xx) now reports "could not confirm" and keeps the last reading, instead of showing an expired-sign-in error and a sign-in button.
- Both credential stores (Keychain and `~/.claude/.credentials.json`) are read, and the freshest wins; a broken store no longer hides a working one.

## v1.1.0 — 2026-08-31

### Settings

- Added a **Settings page** (gear in the sidebar, or `?view=settings`). Two things are configurable, both stored in `~/.claude/tokenscope-settings.json` and read by the dashboard *and* the `tokenscope` CLI, so the two can never disagree.
- **Providers** can be switched off individually. A disabled provider is dropped from the sidebar, its usage limits are never polled, and `scan` never walks its log directory — so turning Codex off really does stop reading `~/.codex`. At least one provider has to stay on; the UI refuses the click that would leave none.
- **Model pricing** is editable per provider: correct a built-in rate, or add a model that shipped after this release so it stops costing $0 / showing as `n/a`. Overrides resolve exactly like built-ins (exact match, then longest prefix), so `claude-opus-6` covers `claude-opus-6-20260101`. An override that matches the built-in rate is dropped rather than stored, and every built-in row offers **Reset**.
- Long-context tiers (threshold plus `long_*` rates) are editable behind a per-model expander, with the honest caveat in place: whether a stored turn crossed a threshold is decided when that turn is scanned, so a threshold change only affects turns scanned afterwards.
- **Nothing is written until it is confirmed.** Every control edits a draft; Save opens a dialog that itemises exactly what will change, the sidebar carries an unsaved-changes dot, and leaving the page or reloading with a draft in flight warns first. A save applies immediately — no restart — and the dashboard re-prices in place.
- Added `settings.py`: tolerant reads (a corrupt file degrades to defaults rather than taking the dashboard down), strict validated writes, and an atomic temp-file rename so a crash mid-write can't truncate the file.

### Pricing

- Added every model that local logs still carry but the table had lost: retired Claude ids (Opus 4.1 / Opus 4 / Sonnet 4 / Haiku 3.5 and the Claude 3.x family) and the older OpenAI families (`gpt-5.4` and its mini/nano/pro tiers, `gpt-5.3-codex`, `gpt-5.2`, `gpt-5.1`, `gpt-5` and its mini/nano tiers). These were resolving to `None` and being billed at $0 / shown as `n/a` — `gpt-5.3-codex` alone accounted for hundreds of turns in a normal history.
- Resolved model prefixes longest-first. `gpt-5.4-mini` was matching `gpt-5.4` (3.3x its real price) and `claude-opus-4-1-...` was matching `claude-opus-4`, depending only on dict order.
- Gave every OpenAI family with a documented long-context tier (`gpt-5.6-sol` / `-terra` / `-luna`, `gpt-5.5`, `gpt-5.4`) its 272K threshold and `long_*` rates; only `gpt-5.5` had them before, so long Sol/Terra/Luna requests were billed at short-context rates.
- Derived cache rates from the documented multipliers (Claude: 1.25x input write, 0.1x input read) instead of transcribing them per model.

### Dashboard

- Added a non-destructive automatic rescan every 30 minutes, while keeping the existing five-minute SQLite-only refresh for ranges that include today. Manual and automatic rescans share an in-flight guard so they cannot overlap in one dashboard tab.
- Mirrored the long-context tier in the browser's `calcCost`, so a client-priced row can no longer come out cheaper than the same row priced server-side.
- Fixed Top Subagent Dispatches falling in the wrong day: `start_date` was a raw UTC slice compared against local range bounds — the same bug #151 fixed for sessions, still present here.
- Fixed the hourly chart mixing timezone frames. The server now sends the UTC *and* local hour and day per row (parsed with `strftime`, not sliced, so a timestamp with a numeric offset no longer buckets as UTC), and the client reads both from one frame. Half-hour zones like UTC+5:30 also stop being rounded.
- Fixed "Last 7 / 30 / 90 Days" spanning one day too many; the range is now inclusive of today, matching `python cli.py week`.
- Guarded the injected-config read so a missing `window.APP_CONFIG` can't take the whole page down.

### Scanner

- Fixed a session's project showing as "unknown" forever when its title record (`custom-title` / `ai-title`) preceded its first content record: the title record carries no `cwd`, and neither the parse loop nor `upsert_sessions` ever repaired the placeholder.
- Stopped `turn_context` relabelling a Codex rollout with its parent thread id, which collapsed spawned child rollouts into the parent session — the exact case the `session_meta` handling already guarded against.
- Stopped dropping genuinely repeated Codex responses. Two responses identical in session, timestamp, model and usage produced the same record id, so `INSERT OR IGNORE` silently discarded the second; ids now carry an occurrence index that stays stable across incremental rescans. `CODEX_PARSER_REVISION` bumped to 4 so existing Codex rows are rebuilt.

### Quota panel

- Dropped windows that have already reset from a *stale* reading too. The disk cache lives 24h — longer than a 5h window — so the panel could keep showing "3% left" long after the limit refilled, despite v1.0.0 claiming to fix exactly that.
- Kept offering "Retry sign-in" when the refresh failed because the user is signed out. A cached reading was short-circuiting the check, so a signed-out user saw stale percentages and no way to act on them.
- Re-poll instead of serving an in-memory reading whose windows have all reset inside the 60s cache TTL.

### CLI

- `stats` daily average now buckets by local day and uses a real 30-day window. It was grouping by the UTC date and lexically comparing an ISO `T`-separated timestamp against `datetime('now')`'s space-separated format — both the buckets and the cutoff drifted for anyone off UTC.
- Closed the DB connection on `today`'s no-usage early return.

### Project / docs

- Documented that pricing now lives only in [pricing.py](pricing.py) (the dashboard receives it via `window.APP_CONFIG.pricing`), that prefix resolution is longest-first, and that changing a long-context threshold requires a `CODEX_PARSER_REVISION` bump.

### Packaging

- Fixed the Docker image missing `pricing.py` and `quota.py` — `dashboard.py` imports both, so the container could not have started. `settings.py` is copied too, and `TOKENSCOPE_SETTINGS` points at the writable `/data` volume because `~/.claude` is mounted read-only.
- Added `settings` to the `py-modules` list so `uv tool install` / `pipx install` ships it.

### Project / docs

- Replaced the application mark with a purpose-drawn TokenScope logo — a scope ring sighted on a rising bar chart — as a full-colour `resources/favicon.svg` (now served as the browser tab icon) and a matching monochrome `resources/icon.svg` for the sidebar's CSS mask. The old 160KB raster-traced mark is gone.
- Rewrote the README around this project: current feature set, a Settings section, and fresh screenshots taken from this build. The inherited screenshots were of a different product and have been deleted.
- Added public contribution and security-reporting guides, corrected the installation instructions, and documented the dashboard's network behavior.

## v1.0.0 — 2026-08-31

Initial release.

### Added

- **Scanner** (`scanner.py`) — parses Claude Code JSONL transcripts (`~/.claude/projects/`, plus the Xcode coding-assistant directory) and Codex rollout sessions (`~/.codex/sessions/`) into a SQLite database at `~/.claude/usage.db`. Incremental by file mtime and line count, with streaming dedupe by `message.id` so a partially-written response is never double-counted.
- **Dashboard** (`dashboard.py`) — a single-file `http.server` app serving an embedded SPA: daily token and cost charts, hourly distribution, per-model / per-project / per-branch / per-session tables, subagent dispatches, a date-range picker with bookmarkable URLs, model filters, CSV export, and collapsible sections remembered across reloads. Claude Code and Codex each get their own tab so models and pricing are never mixed.
- **Quota panel** — reads the provider's own local usage signal and shows the remaining percentage per limit window, keeping the last good reading on disk (24h) and backing off between failed polls rather than blanking.
- **CLI** (`cli.py`) — `scan`, `today`, `week`, `stats`, and `dashboard`, with `--host` / `--port` and `HOST` / `PORT` support.
- **Pricing** (`pricing.py`) — one table for both providers, cache rates derived from the documented multipliers, and OpenAI long-context tiers. Costs are computed per turn (each turn knows its own model) and then summed, so a session spanning several models is priced correctly. An unknown model returns no price and is billed at $0 rather than inheriting a neighbour's rate.
- **Packaging** — `pyproject.toml` for `uv tool install` / `pipx`, and a Dockerfile plus `scripts/run-docker.sh` that mounts the log directories read-only.
