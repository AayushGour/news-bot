# Telegram → Research → Instagram

Watches a Telegram channel, researches each post against the open web, writes
and renders an Instagram carousel, and publishes it **only after you approve it**.

You can also DM the bot a link, some text, or an image, ask it a question
("what is next concept modeling?"), or ask it for a list ("top 5 github repos
for AI interview prep") — and it runs the same pipeline. Every DM is its own
post.
Not happy with a preview? Press Regenerate and say what to change ("shorter
slide 3, swap the photo on slide 2"): it edits just that, checks the edit did
what you asked, and shows you a new preview with a list of the changes.

```
 Telegram channel ─┐
 DM to the bot   ──┼──▶ ingested ──▶ extracted ──▶ triaged ──▶ researched ──▶ synthesized
 Dashboard "new" ──┘                  (vision)     (score)     (SearXNG)       (brief)
                                                                                  │
                                                                                  ▼
  published ◀── publishing ◀── approved ◀── awaiting_approval ◀── rendered ◀── composed
   (Graph)      (containers)    (upload)       you decide           (PNG)     (slide JSON)
                                                  │    ▲
                                       Regenerate │    │ targeted edits, re-previewed
                                                  ▼    │
                                              revision_requested ⇄ revision_confirm
```

Everything runs on your machine in Docker: one worker container, the
dashboard, and SearXNG. Models run on local
Ollama or OpenRouter's free tier (per role, with local fallback), storage is
MinIO behind a Cloudflare tunnel or Cloudflare R2, and search is a self-hosted
SearXNG — nothing leaves the box except the search queries, the page and image
fetches, any hosted model calls, and the finished post.

## Documentation

| Doc | Read it when |
|---|---|
| **[Setup](docs/setup.md)** | first install — every credential, service, check, and the full configuration reference |
| **[Usage](docs/usage.md)** | running it day to day, and fixing what breaks |
| **[Architecture](docs/architecture.md)** | understanding *why* it is shaped this way; the state machine |
| **[Development](docs/development.md)** | changing the code, adding a stage or slide type, the team process |
| [Revision API](docs/revision-api.md) | the agentic Regenerate internals: request/confirm API, run record, rollback |
| [Retention](docs/retention.md) | what the cleanup job deletes, and when |
| [Themes](docs/themes.md) | slide themes and how one is picked |

## Quick start

```bash
cp .env.example .env                  # then fill it in — see docs/setup.md
docker volume create pipeline-state   # once; holds app.db and the Telegram session
docker compose up -d                  # builds the image; starts app, dashboard, searxng
docker compose logs -f app            # "running — watching ..." and "Start polling"
```

With `DRY_RUN=true` (the default) previews arrive in Telegram and nothing
publishes. That is the right way to start.

The operator dashboard is at http://127.0.0.1:8770 (loopback only). Ollama
stays on the host for the GPU. A bare-host run without Docker still works; see
[setup](docs/setup.md).

## How it behaves

- **Nothing publishes without you.** Approval is the only path to Instagram.
- **Nothing destructive happens without you either.** A Regenerate note that
  would remove a slide or rebuild the whole deck asks Yes/No first; a note it
  cannot understand changes nothing and asks what you meant.
- **Restarting loses nothing.** `items.status` is the state machine; there is
  no in-memory queue.
- **An outage never becomes a content decision.** Infrastructure failure is
  deferred without spending the item's retries, never recorded as "found
  nothing", and a config fault or an Instagram throttle alerts you once.
- **Text is shrunk, not cut.** Slides shrink their type before a sentence is
  cut, and searches page rather than drop queries. Links, repos and figures the
  research does not back are dropped and recorded, not published.
- **It posts at a human pace.** Publishes are at least three minutes apart, and
  a throttled account waits hours instead of hammering Meta.

## Status

A growing test suite — run `./.venv/bin/python -m pytest --collect-only -q`
for today's count rather than trusting a number pinned here. Most defects in
this codebase were found by running it; the tests exist to stop them coming
back. Coverage is checked by mutation, not by percentage; see
[development](docs/development.md#mutation-testing).

## Security

- `.env` and `.env.*` are gitignored. `secrets/telegram.session` is
  **account-equivalent** — treat it like a password.
- Every Telegram handler checks `OPERATOR_USER_ID`.
- The dashboard binds `127.0.0.1` only and has no login. Approving publishes to
  a real account.
- Every URL from a search result is fetched through one SSRF guard: public
  addresses only, checked again on every redirect and at connect time.
- Every log handler carries a redaction filter, because `httpx` logs full URLs
  and Meta's read endpoints take the access token as a query parameter.
