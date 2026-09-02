FROM python:3.12-slim

WORKDIR /app

# Every runtime module: dashboard.py imports pricing, quota and settings.
COPY scanner.py cli.py dashboard.py pricing.py quota.py settings.py sources.py antigravity.py ./
COPY resources ./resources

ENV HOST=0.0.0.0
ENV PORT=8080
ENV CLAUDE_USAGE_DB=/data/usage.db
# ~/.claude is mounted read-only, so settings live in the writable data volume.
ENV TOKENSCOPE_SETTINGS=/data/tokenscope-settings.json

EXPOSE 8080

CMD ["python3", "cli.py", "dashboard", "--no-browser"]
