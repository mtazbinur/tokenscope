# Contributing to TokenScope

Thanks for helping improve TokenScope. Bug reports, focused fixes, tests, documentation,
and well-scoped feature proposals are welcome.

For security vulnerabilities, do not open a public issue. Follow
[SECURITY.md](SECURITY.md) instead.

## Before you start

- Search existing issues and pull requests to avoid duplicating work.
- Open an issue before a large feature, dependency, schema change, or behavior change.
- Keep pull requests focused. Unrelated fixes should be separate changes.
- Never include real transcripts, OAuth credentials, settings files, databases, or other
  user data in an issue, test fixture, screenshot, or pull request.

## Development setup

TokenScope supports Python 3.8+ and has no third-party runtime dependencies.

```bash
git clone https://github.com/mtazbinur/tokenscope.git
cd tokenscope
python3 cli.py --version
python3 -m unittest discover -s tests -v
```

On Windows, use `python` in place of `python3`.

Run the dashboard from the checkout with:

```bash
python3 cli.py dashboard
```

Tests use temporary databases and must not read or modify the user's real
`~/.claude/usage.db`.

## Making a change

1. Fork the repository and create a descriptive branch.
2. Add focused tests for behavior changes and regressions.
3. Run the relevant test file while iterating, then run the full suite.
4. Update the README or CHANGELOG when user-visible behavior changes.
5. Open a pull request against `main` unless the issue requests another target.

Useful focused commands:

```bash
python3 -m unittest tests.test_scanner -v
python3 -m unittest tests.test_dashboard -v
python3 -m unittest tests.test_pricing -v
```

## Project conventions

- Keep the runtime standard-library-only unless a dependency has been discussed first.
- Preserve streaming deduplication by `message.id`; only the final record for a streamed
  response carries the complete usage totals.
- Preserve the final session-total reconciliation from the `turns` table after a scan.
- Calculate cost per turn before aggregating because one session can span several models.
- Resolve model pricing by exact match, then longest matching model prefix.
- Treat unknown models as unpriced instead of silently assigning a family rate.
- Keep settings reads forgiving and settings writes strict and atomic.
- Keep dashboard asset routes explicit; do not turn them into a general file server.
- Use synthetic, obviously non-secret data in tests and screenshots.

More detailed implementation notes for coding agents and maintainers are in
[AGENTS.md](AGENTS.md).

## Pull request checklist

- [ ] The change is focused and explained clearly.
- [ ] Relevant regression coverage was added or updated.
- [ ] `python3 -m unittest discover -s tests -v` passes.
- [ ] Documentation and the CHANGELOG were updated when needed.
- [ ] No real usage logs, credentials, personal paths, or generated local files are included.

TokenScope preserves contributor authorship when merging accepted work. By contributing,
you agree that your contribution is licensed under the repository's [MIT License](LICENSE).
