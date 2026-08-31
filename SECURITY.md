# Security Policy

TokenScope reads local coding-assistant transcripts and, for live Claude usage limits,
uses the local Claude Code sign-in to contact Anthropic's usage endpoint. Security and
privacy reports are taken seriously.

## Supported versions

| Version | Supported |
|---|---|
| Latest release | Yes |
| `main` | Yes |
| Older releases | No |

## Reporting a vulnerability

Please do not disclose a suspected vulnerability in a public issue, discussion, pull
request, screenshot, or test fixture.

After the repository is public, use GitHub's **Security → Report a vulnerability** flow.
If that option is unavailable, contact the maintainer through a private contact method
listed on [@mtazbinur's GitHub profile](https://github.com/mtazbinur).

Include, where possible:

- the affected version or commit;
- the operating system and installation method;
- a concise description of the impact;
- reproducible steps using synthetic data;
- any suggested mitigation.

Do not send real transcript contents, access tokens, credential files, private database
contents, or other people's personal information. Redact secrets from logs and paths.

Reports involving credential exposure, unintended file access, unexpected remote access
to dashboard data, unsafe transcript parsing, or a way to bypass the documented provider
and path boundaries are in scope. Pricing inaccuracies, ordinary bugs, and feature
requests can use the public issue tracker unless publishing them would expose sensitive
information.

The maintainer will validate the report, coordinate a fix and disclosure when warranted,
and credit the reporter if requested and appropriate.
