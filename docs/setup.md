# Setup

From nothing to a first published post. Budget an hour, most of it waiting on
Meta's app screens. Every variable is listed in the
[configuration reference](#configuration-reference) at the end.

## 1. Prerequisites

| Thing | Why | Check |
|---|---|---|
| Python 3.13 | the app | `python3 --version` |
| `uv` | dependency install (and the code brain, for development) | `uv --version` |
| Docker | SearXNG, and MinIO if you host media locally | `docker ps` |
| Ollama | local models (the default provider, and the fallback for OpenRouter) | `curl localhost:11434/api/tags` |
| `cloudflared` | public media host for local MinIO | `cloudflared --version` |
| Chromium | slide rendering | installed by Playwright below |

```bash
brew install uv ollama cloudflared
uv sync
./.venv/bin/python -m playwright install chromium
```

Plain `uv sync` is enough to run the pipeline (Pillow and jsonschema are runtime
dependencies). Add `--extra dev` only to run the tests; see
[development](development.md).

Ollama runs on the **host**, not in Docker — Docker Desktop on macOS cannot
reach the GPU. Pull the default local models (`src/pipeline/config.py`):

```bash
ollama pull qwen3:4b-instruct     # cheap   (MODEL_CHEAP)
ollama pull qwen3.5:9b            # good    (MODEL_GOOD; also the verifier's default)
ollama pull qwen2.5vl:7b          # vision  (MODEL_VISION)
```

If you choose different models, `MODEL_VISION` must actually accept images: a
text-only model there fails every extract with `image input is not supported`.
`llama3.2-vision` no longer loads on current Ollama.

## 2. Configuration

```bash
cp .env.example .env
```

`.env` holds live credentials. It is gitignored, along with `.env.*` — a
timestamped backup once sat outside that pattern and was staged for a push to
a public repo. `.env` is read **once at startup**: an edit changes nothing
until the process restarts.

### Telegram — reading the channel

Reading a channel needs a **user** account (Telethon), not a bot. Bots cannot
read arbitrary channels.

1. Go to https://my.telegram.org → API development tools.
2. Create an app; copy `api_id` and `api_hash`.

```
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
CHANNEL_IDS=-1001234567890
```

First run prompts for your phone number and a login code, then writes
`secrets/telegram.session` (`TELEGRAM_SESSION_PATH` moves it). **That file is
account-equivalent** — it is gitignored and should be `chmod 600`.

### Telegram — the approval bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Copy the token.
3. Message your new bot once so it can DM you.
4. Get your numeric user id from [@userinfobot](https://t.me/userinfobot).

```
TELEGRAM_BOT_TOKEN=...
OPERATOR_USER_ID=123456789
```

`OPERATOR_USER_ID` is the authorisation boundary — every handler checks it, and
a tap or message from anyone else changes nothing and gets no reply. Approving
publishes to a real account, so this is not cosmetic.

### Models

Local only (the default):

```
LLM_PROVIDER=ollama
```

Hosted, with local fallback:

```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL_CHEAP=nvidia/nemotron-3.5-super-vl:free
OPENROUTER_MODEL_GOOD=nvidia/nemotron-3-ultra-550b-a55b:free
OPENROUTER_MODEL_VISION=google/gemma-4-31b-it:free
FALLBACK_TO_LOCAL=true
```

Mixed — any one role can run elsewhere, e.g. the `good` model local while
`cheap` and `vision` stay hosted:

```
LLM_PROVIDER=openrouter
LLM_PROVIDER_GOOD=ollama
```

Startup refuses three mistakes for every role that resolves to OpenRouter: no
`OPENROUTER_API_KEY`, a paid slug (anything not ending `:free`), and an
auto-router slug (`openrouter/free`, `openrouter/auto`). `openrouter/free` is
a router, not a model: it re-picks per call, so quality is not reproducible.

The three default slugs are the ones live-tested against this pipeline's own
schemas, not picked off advertised capabilities (`.claude/project-context.md`
D9/D11 has the matrix):

| Role | Slug | Tested against | Result |
|---|---|---|---|
| `good` | `nvidia/nemotron-3-ultra-550b-a55b:free` | real `SLIDES_SCHEMA` | 3/3 conforming |
| `cheap` | `nvidia/nemotron-3.5-super-vl:free` | `NOTE_SCHEMA`, 40k-char payload; banana-enum enforcement | 3/3 and 3/3 (T231) |
| `vision` | `google/gemma-4-31b-it:free` | structured-output probe | advertises support, **ignores it** |

Before you change any of them:

- **A slug's `supported_parameters` metadata is not trustworthy.**
  `google/gemma-4-31b-it:free` reports `structured_outputs: true` and then
  answered `"2 + 2 = 4"` against a schema whose enum was `["banana"]`. Probe
  enforcement yourself. `vision` still ships with it because it is the only
  vision-capable free slug tested so far; `src/pipeline/stages/extract.py`
  checks the verdict shape in code, which is what actually enforces the
  contract on that path.
- **A toy-schema probe does not predict large-schema reliability.**
  `nvidia/nemotron-3-super-120b-a12b:free` passed a trivial enum test and
  fails 2/3 on the real `SLIDES_SCHEMA`. Re-probe each role against the schema
  *that role actually sends*, at a realistic payload size.
- **Free slugs move to paid.** The previous `cheap` default did on 2026-10-30
  (T231). The startup check catches it; a backup that measured well is
  `dots-studio/dots-3-note-preview:free` (enforcement not yet probed).

`cheap` is not a minor role: it writes the research notes and runs every
quality gate. See [architecture](architecture.md#models-three-roles-plus-a-local-verifier).

The **advisory verifier** (`VERIFY`, default on) runs on local Ollama whatever
the provider settings say, using `MODEL_VERIFY` (default: whatever
`MODEL_GOOD` resolves to). It writes `items.verification` and gates nothing.
It is slow on a small local model (minutes per deck under load); `VERIFY=false`
turns it off with no other effect.

### Search

```bash
docker compose up -d searxng      # uses ./searxng/settings.yml
```

```
SEARXNG_URL=http://localhost:8080
```

`searxng/settings.yml` enables `bing` explicitly and allows the JSON format.
Do not remove either. brave, duckduckgo, google cse and startpage are all
persistently blocked under automated querying, and mojeek returns zero results
**without reporting itself unresponsive**; with bing removed,
`categories=general` returns nothing. A SearXNG that refuses JSON (403) is a
`ConfigFault`: items defer and you get one alert.

Verify:

```bash
curl -s 'http://localhost:8080/search?q=anthropic&format=json&categories=general' \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["results"]), "results")'
```

Zero results means the general engines are down — fix that before blaming the
pipeline.

### Media storage

Instagram fetches slides from **its own servers**, so the media URL must be
publicly resolvable. A `localhost` or `127.0.0.1` base is refused at upload.

**Cloudflare R2** (public URL out of the box, no tunnel):

```
R2_ACCOUNT_ID=...
R2_ACCESS_KEY=...
R2_SECRET_KEY=...
R2_BUCKET=slides
R2_PUBLIC_BASE=https://<your public bucket url>
```

**Local MinIO** (or any S3-compatible store) behind a tunnel:

```bash
docker run -d --name pipeline-minio -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=... -e MINIO_ROOT_PASSWORD=... \
  quay.io/minio/minio server /data --console-address ":9001"
```

Create a bucket named `slides` with public read (Instagram fetches anonymously).

```
S3_ENDPOINT=http://localhost:9000
R2_ACCOUNT_ID=minio                  # any non-empty value; see below
R2_BUCKET=slides
R2_ACCESS_KEY=...
R2_SECRET_KEY=...
MANAGE_TUNNEL=true
R2_PUBLIC_BASE=https://placeholder.invalid/slides
```

`R2_ACCOUNT_ID` is only used to build an R2 endpoint when `S3_ENDPOINT` is
empty, but startup requires it to be set whenever `DRY_RUN=false`, MinIO
included. The same goes for `R2_PUBLIC_BASE`, which is why the placeholder.

With `MANAGE_TUNNEL=true` the pipeline runs a Cloudflare quick tunnel itself
and rewrites media URLs to the live hostname, so `R2_PUBLIC_BASE` is only a
fallback. Without it, `R2_PUBLIC_BASE` is used as-is.

### A named tunnel (optional, but permanent)

A quick tunnel's hostname lasts roughly a day. The pipeline survives a
rotation without losing a post, but a named tunnel removes the rotation
entirely and costs nothing on a domain you already own:

```bash
cloudflared tunnel login                      # opens a browser; pick your domain
cloudflared tunnel create news-media          # writes ~/.cloudflared/<uuid>.json
cloudflared tunnel route dns news-media media.example.com
cloudflared tunnel run --url http://localhost:9000 news-media
```

Then pin it and stop managing one:

```
R2_PUBLIC_BASE=https://media.example.com/slides
MANAGE_TUNNEL=false
```

### Instagram

Needs an Instagram **Business or Creator** account. This uses the *Instagram
API with Instagram Login* (`graph.instagram.com`), so no Facebook Page is
needed.

1. https://developers.facebook.com → create an app → **Business** type.
2. Add the **Instagram** product → *API setup with Instagram login*.
3. Generate a token with `instagram_business_content_publish` and
   `instagram_business_basic`.
4. Copy the **Instagram user id** shown there (a `17841…` number) — not the
   Meta App ID from the dashboard URL, and not the App Secret.

```
IG_USER_ID=17841...
IG_ACCESS_TOKEN=IGAA...
```

Verify without printing anything secret:

```bash
./.venv/bin/python scripts/check_instagram.py
```

It checks the token resolves, that `IG_USER_ID` names the same **account** (an
account has more than one valid id, and both are accepted), and that the
publishing scope is present.

Tokens expire after 60 days. The running process checks every 12 hours,
refreshes from day 50 (recorded in `data/ig_token.json`), and messages you if
a refresh fails.

## 3. Preflight

```bash
./.venv/bin/python scripts/preflight.py
./.venv/bin/python scripts/preflight.py --send   # also DMs you a test message
```

Checks imports, config, live services and credentials before a first real run,
printing only whether secrets work, never their values. Fix what it reports; it
is cheaper than debugging a half-configured pipeline.

## 4. First run — dry

```
DRY_RUN=true
```

Previews arrive in Telegram; nothing is uploaded or published. Approving a
dry-run item takes it to `published` with a fake `DRYRUN-<id>` post id, so the
whole path is exercised. Start with a small `BACKFILL_LIMIT` (2–3): every
backfilled message costs a full pipeline pass and lands a preview in your chat.

```bash
./.venv/bin/python -m pipeline
```

You should see, within a couple of seconds:

```
DRY_RUN is on — previews will arrive, nothing will publish
telethon...: Connection to ... complete!
pipeline: running — watching [-1001234567890]
aiogram.dispatcher: Start polling
```

The operator dashboard is a separate process, bound to loopback:

```bash
./.venv/bin/python scripts/dashboard.py     # http://127.0.0.1:8770
```

It has no login — approving there publishes — so keep `DASHBOARD_HOST` at
`127.0.0.1` on a bare host.

## 5. Install as a service (macOS)

```bash
./deploy/install-launchd.sh
```

It checks for `.env`, `secrets/telegram.session` and `.venv`, runs preflight,
rewrites the paths in `deploy/com.aayush.newspipeline.plist` to this checkout,
and loads it as a launchd agent. Re-run it after editing the plist.

Two things in that plist are load-bearing:

- **`ProcessType` must be `Interactive`.** launchd's default throttles CPU and
  I/O, and `Background` is the *most* throttled tier, not a neutral one. Left
  on `Background` the job got about 1% of a core and took over seven minutes to
  import its dependencies, sitting past every log line looking hung.
- **stdout goes to `data/stderr.log`, not `data/pipeline.log`.** The app owns
  and rotates its own log; a launchd-redirected file cannot be rotated.

macOS blocks **newly added** background items until you approve them in
**System Settings → General → Login Items & Extensions**. A job that has not
been approved exits `78 / EX_CONFIG` with no output at all.

**Containers instead:** see [Running in Docker](#running-in-docker) below.

### Running in Docker

`docker compose up -d` builds the image and starts SearXNG, the worker (`app`)
and the dashboard. Ollama and MinIO stay on the host and are reached at
`host.docker.internal` (`OLLAMA_HOST`, `S3_ENDPOINT` and `TUNNEL_TARGET` are
overridden in `docker-compose.yml`; your `.env` stays valid for a bare-host run).

- **State lives in a named volume**, `pipeline-state`, mounted at `/app/state`
  (`app.db`, `telegram.session`, `ig_token.json`). A live SQLite WAL database
  must never sit on a macOS bind mount (D298). Create it once, outside compose:
  `docker volume create pipeline-state`. It is declared `external`, so
  `docker compose down -v` cannot delete it.
- `./data` is a bind mount (read-write for `app`, read-only for the dashboard):
  logs, hourly DB backups in `data/backups/`, the worker lease.
  `./secrets`, `./config` and `./templates` are mounted read-only and override
  the copies baked into the image.
- The image bundles `cloudflared`, `sqlite3`, and the open fonts Inter, Gelasio
  and JetBrains Mono, which stand in for the Apple fonts the themes name, so
  slides look slightly different from host-rendered ones.
- `TZ` defaults to `Asia/Kolkata`; set `TZ` in the shell or `.env` to change it.
- The dashboard is published on `127.0.0.1:8770` only. Do not change it to a
  bare `8770:8770`.
- Ops inside the stack: `docker compose exec app sqlite3 /app/state/app.db ...`
  or `docker compose exec app python scripts/requeue.py ...`.
- **Never run the host worker and the containers at the same time.** Moving an
  existing install from launchd to Docker (stop the host worker, copy the DB and
  session into the volume, rollback) is a one-off procedure: follow
  [the cutover runbook](design/docker-everything.md#4-cutover-runbook-t353-devops-needs-the-users-own-go).

## 6. Going live

```
DRY_RUN=false
```

Startup now also requires `R2_ACCOUNT_ID`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`,
`R2_BUCKET`, `R2_PUBLIC_BASE`, `IG_USER_ID` and `IG_ACCESS_TOKEN`. Restart:

```bash
launchctl unload ~/Library/LaunchAgents/com.aayush.newspipeline.plist
launchctl load   ~/Library/LaunchAgents/com.aayush.newspipeline.plist
```

Approve one post and watch `data/pipeline.log` for `published item N as …`.
Publishes are spaced at least 180 s apart; a throttled account defers for
hours and alerts you once (see [architecture](architecture.md#publishing)).

## Configuration reference

Environment variables are read once at startup by `Settings.load()` in
`src/pipeline/config.py`, and required variables are enforced by
`validate_for_run()`. **Default** is what you get with the variable unset.
Booleans accept `1/true/yes/on`; anything else is false.

### Required

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | — | user-account API app, for reading the channel |
| `TELEGRAM_BOT_TOKEN` | — | the approval bot; also accepts your DMs |
| `OPERATOR_USER_ID` | — | the only account allowed to talk to the bot or press buttons |
| `CHANNEL_IDS` | — | comma-separated channel ids to watch |

### Optional

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_SESSION_PATH` | `secrets/telegram.session` | Telethon session file |

### Models

| Variable | Default | Notes / switching it off |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` or `openrouter`, for every role without an override |
| `LLM_PROVIDER_CHEAP` / `_GOOD` / `_VISION` | unset = `LLM_PROVIDER` | per-role override, same two values |
| `FALLBACK_TO_LOCAL` | `true` | after 3 failed OpenRouter attempts, run that call on local Ollama. `false`: the last OpenRouter error is raised as-is — a rate limit, outage or wrong slug defers the item without spending an attempt, but an unusable completion (`BadCompletion`) spends one |
| `OLLAMA_HOST` | `http://localhost:11434` | from a container: `http://host.docker.internal:11434` |
| `MODEL_CHEAP` / `MODEL_GOOD` / `MODEL_VISION` | `qwen3:4b-instruct` / `qwen3.5:9b` / `qwen2.5vl:7b` | local models |
| `MODEL_CHEAP_FALLBACK` / `_GOOD_FALLBACK` / `_VISION_FALLBACK` | unset | a second local model tried when the first fails or is missing. Unset: the chain stops at the first |
| `NUM_CTX_CHEAP` / `NUM_CTX_GOOD` / `NUM_CTX_VISION` | `8192` / `16384` / `8192` | **must** be pinned: unpinned, Ollama loads the full context and spills to CPU. Not sent to OpenRouter |
| `OPENROUTER_API_KEY` | unset | required if any role resolves to `openrouter` |
| `OPENROUTER_MODEL_CHEAP` / `_GOOD` / `_VISION` | see the table above | `:free` slugs only; no auto-routers |
| `VERIFY` | `true` | advisory pre-render verifier. `false`: compose calls nothing and writes `verification = null` |
| `MODEL_VERIFY` / `NUM_CTX_VERIFY` | inherit `MODEL_GOOD` / `NUM_CTX_GOOD` | always local, never routed to OpenRouter |

### Research and content

| Variable | Default | Notes |
|---|---|---|
| `SEARXNG_URL` | `http://localhost:8080` | |
| `SEARXNG_CATEGORIES` | `general,it,news` | categories for news searches; repo searches use the github engine |
| `TRIAGE_THRESHOLD` | `6` | posts scoring below it are dropped |
| `RESEARCH_CONCURRENCY` | `3` | parallel researchers per item |
| `RESEARCH_NOTE_ROLE` | `cheap` | `cheap` or `good`; which model writes research notes |
| `BACKFILL_LIMIT` | `20` | missed channel messages replayed on each (re)connect |
| `SOURCE_CREDIT` | empty | handle credited at the end of every caption |
| `HANDLE` | empty | handle printed on every slide; empty keeps the theme file's |
| `THEME` | `signal` | a name from `config/themes/`, `rotate` or `random`. Set but empty: the composer's pick. See [themes](themes.md) |
| `FEW_SHOT_EXAMPLES` | `list` | worked example decks: `off`, `list` (enumerations only) or `all` |

### Agentic Regenerate

| Variable | Default | Notes / switching it off |
|---|---|---|
| `REVISE_ENABLED` | `true` | `false` is the rollback switch: a Regenerate note goes straight to a full recompose (`regen_note`), as before the feature. Restart to apply. Items already parked in `revision_requested` / `revision_confirm`: see [revision-api](revision-api.md#rollback) |
| `REVISE_ENGINES_BLOCKED_MAX_S` | `1800` | how long a revision waits on suspended search engines before reporting its search-backed edits as not done and running the rest. `0`: give up on the first deferral |

### Media and Instagram

| Variable | Default | Notes |
|---|---|---|
| `S3_ENDPOINT` | empty = Cloudflare R2 | any S3-compatible endpoint (MinIO, B2, S3) |
| `S3_REGION` | empty = `auto` | |
| `R2_ACCOUNT_ID` | — | builds the R2 endpoint; required (any value) to publish, even with `S3_ENDPOINT` |
| `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET` | — | required to publish |
| `R2_PUBLIC_BASE` | — | public URL of the bucket; required to publish. With `MANAGE_TUNNEL` it is only the fallback |
| `MANAGE_TUNNEL` | `false` | run a Cloudflare quick tunnel in-process and address media at its live hostname. `false`: `R2_PUBLIC_BASE` is used as-is |
| `IG_USER_ID`, `IG_ACCESS_TOKEN` | — | required to publish |

### Runtime, retention and dashboard

| Variable | Default | Notes |
|---|---|---|
| `DRY_RUN` | `true` | previews only; approved items get a `DRYRUN-<id>` record. `false` makes the publish variables mandatory |
| `DB_PATH` | `data/app.db` | SQLite file; `ig_token.json` is kept next to it |
| `DASHBOARD_HOST` | `127.0.0.1` | dashboard bind address. Only a container's internal bind may change it; never `0.0.0.0` on a bare host |
| `RETENTION_ENABLED` | `true` | the worker's periodic file cleanup. `false`: the job does nothing (`scripts/cleanup.py` still works by hand) |
| `RETENTION_DRY_RUN` | `false` | `true`: log and report what would be deleted, change nothing |
| `AWAITING_APPROVAL_MAX_DAYS` | `120` | auto-reject unapproved decks older than this. `0` disables |
| `CLEANUP_INTERVAL_S` | `21600` (6 h) | floor 60 s; first run about 60 s after boot |
| `TERMINAL_MEDIA_GRACE_DAYS` | `1` | published/rejected/dropped items keep files this long |
| `FAILED_MEDIA_GRACE_DAYS` | `30` | failed items keep files this long (a requeue re-reads them) |
| `CLEANUP_PUBLISHED_MEDIA` | `true` | `false`: published items keep their local files |
| `CLEANUP_MINIO` | `true` | `false`: bucket objects under `items/<id>/` are never deleted |

Retention details: [retention](retention.md).

Not configurable from the environment (code constants): retry budget
(`max_attempts` = 3), worker poll interval (5 s), media directory
(`data/media`), log location (`data/pipeline.log`), Instagram pacing and
throttle backoffs (`IG_*` in `src/pipeline/config.py`).
