# Telegram → Research → Instagram

Watches a Telegram channel, researches each post against the open web, writes
and renders an Instagram carousel, and publishes it **only after you approve it**.

You can also DM the bot a link, some text, or an image — or ask it for a list
("top 5 github repos for AI interview prep") — and it runs the same pipeline.

```
 Telegram channel ─┐
                   ├──▶ ingested ──▶ extracted ──▶ triaged ──▶ researched
 DM to the bot   ──┘                  (vision)     (score)     (SearXNG)
 Dashboard "new" ──┘                                                │
                                                                    ▼
                                                              synthesized
                                                                    │
                                                                    ▼
  published ◀── publishing ◀── approved ◀── awaiting_approval ◀── rendered ◀── composed
   (Graph)      (containers)    (upload)     ↑ you decide          (PNG)     (slide JSON)
```

Everything runs on your machine. Models run through OpenRouter with a local
Ollama fallback, storage is MinIO behind a Cloudflare tunnel, and search is a
self-hosted SearXNG — nothing leaves the box except the search queries, the
model calls, and the finished post.

## Documentation

| Doc | Read it when |
|---|---|
| **[Setup](docs/setup.md)** | first install — every credential, service and check |
| **[Usage](docs/usage.md)** | running it day to day, and fixing what breaks |
| **[Architecture](docs/architecture.md)** | understanding *why* it is shaped this way |
| **[Development](docs/development.md)** | changing the code, adding a stage or slide type |

## Quick start

```bash
uv sync
./.venv/bin/python -m playwright install chromium
cp .env.example .env          # then fill it in — see docs/setup.md
./.venv/bin/python scripts/preflight.py
./.venv/bin/python -m pipeline
```

With `DRY_RUN=true` previews arrive in Telegram and nothing publishes. That is
the right way to start.

Operator dashboard, on loopback only:

```bash
./.venv/bin/python scripts/dashboard.py     # http://127.0.0.1:8770
```

## How it behaves

- **Nothing publishes without you.** Approval is the only path to Instagram.
- **Restarting loses nothing.** `items.status` is the state machine; there is
  no in-memory queue.
- **An outage never becomes a content decision.** Infrastructure failure is
  deferred, not recorded as "found nothing".
- **Nothing is truncated to fit.** Slides shrink their type rather than cut a
  sentence; searches page rather than drop queries.

## Status

599 tests. The interesting ones are not the count — most defects in this
codebase were found by running it, and the tests exist to stop them coming
back. Coverage is checked by mutation, not by percentage; see
[development](docs/development.md#mutation-testing).

## Security

- `.env` and `.env.*` are gitignored. `secrets/telegram.session` is
  **account-equivalent** — treat it like a password.
- Every log handler carries a redaction filter, because `httpx` logs full URLs
  and Meta's read endpoints take the access token as a query parameter.
- The dashboard binds `127.0.0.1` only. Approving publishes to a real account.
- Every Telegram handler checks `OPERATOR_USER_ID`.
