# Setup

From nothing to a first published post. Budget an hour, most of it waiting on
Meta's app review screens.

## 1. Prerequisites

| Thing | Why | Check |
|---|---|---|
| Python 3.13 | the app | `python3 --version` |
| `uv` | dependency install | `uv --version` |
| Docker | MinIO + SearXNG | `docker ps` |
| Ollama | local model fallback | `curl localhost:11434/api/tags` |
| `cloudflared` | public media host | `cloudflared --version` |
| Chromium | slide rendering | installed by Playwright below |

```bash
brew install uv ollama cloudflared
uv sync
./.venv/bin/python -m playwright install chromium
```

Pull the local fallback models — these run when the hosted provider fails:

```bash
ollama pull qwen3:4b-instruct     # cheap
ollama pull qwen3.5:latest        # good
ollama pull qwen2.5vl:7b          # vision
```

## 2. Configuration

```bash
cp .env.example .env
```

`.env` holds live credentials. It is gitignored, along with `.env.*` — a
timestamped backup once sat outside that pattern and was staged for a push to
a public repo.

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
`secrets/telegram.session`. **That file is account-equivalent** — it is
gitignored and should be `chmod 600`.

### Telegram — the approval bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Copy the token.
3. Message your new bot once so it can DM you.
4. Get your numeric user id from [@userinfobot](https://t.me/userinfobot).

```
TELEGRAM_BOT_TOKEN=...
OPERATOR_USER_ID=123456789
```

`OPERATOR_USER_ID` is the authorisation boundary — every handler checks it.
Approving publishes to a real account, so this is not cosmetic.

### Models

```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL_CHEAP=nex-agi/nex-n2.5-mini:free
OPENROUTER_MODEL_GOOD=nex-agi/nex-n2.5-pro:free
OPENROUTER_MODEL_VISION=google/gemma-4-31b-it:free
FALLBACK_TO_LOCAL=true
```

Two things worth knowing before you pick models:

- **Every stage passes a JSON schema.** A model that does not support
  structured output 400s on every call. The client degrades to `json_object`
  and then to a prompt-embedded schema, but verify before committing:
  `inclusionai/ling-3.0-flash-vl:free` answers
  `"model features structured outputs not support"` to both.
- **`openrouter/free` is a router, not a model.** It re-picks per call — a
  content-safety classifier one moment, a code model the next — so quality is
  not reproducible and retries are frequent. Pin a real model.

`cheap` is not a minor role: it writes the research notes and runs every
quality gate. See [architecture](architecture.md#models-three-roles-not-three-models).

### Search

```bash
docker run -d --name pipeline-searxng -p 8080:8080 \
  -v "$PWD/searxng:/etc/searxng" searxng/searxng:latest
```

```
SEARXNG_URL=http://localhost:8080
```

`searxng/settings.yml` enables `bing` explicitly. Do not remove it: brave,
duckduckgo, google cse and startpage are all persistently blocked under
automated querying, and mojeek returns zero results **without reporting itself
unresponsive**. With bing removed, `categories=general` returns nothing and the
pipeline researches news stories against MDN and Docker Hub.

Verify:

```bash
curl -s 'http://localhost:8080/search?q=anthropic&format=json&categories=general' \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["results"]), "results")'
```

Zero results means the general engines are down — fix that before blaming the
pipeline.

### Media storage

```bash
docker run -d --name pipeline-minio -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=... -e MINIO_ROOT_PASSWORD=... \
  quay.io/minio/minio server /data --console-address ":9001"
```

Create a bucket named `slides` with public read (Instagram fetches anonymously).

```
S3_ENDPOINT=http://localhost:9000
R2_BUCKET=slides
R2_ACCESS_KEY=...
R2_SECRET_KEY=...
MANAGE_TUNNEL=true
R2_PUBLIC_BASE=https://placeholder.invalid/slides
```

With `MANAGE_TUNNEL=true` the pipeline runs `cloudflared` itself and rewrites
media URLs to the live hostname, so `R2_PUBLIC_BASE` is only a fallback. If you
have a **named** tunnel or a real bucket, set `R2_PUBLIC_BASE` to it and leave
`MANAGE_TUNNEL` unset.

### A named tunnel (optional, but permanent)

A quick tunnel's hostname lasts roughly a day. The pipeline now survives a
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
# MANAGE_TUNNEL unset
```

`cloudflared tunnel login` is interactive and needs a Cloudflare account with a
domain; nothing else in this setup does.

### Instagram

Needs a **Business or Creator** account linked to a Facebook Page.

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

## 3. Preflight

```bash
./.venv/bin/python scripts/preflight.py
```

Checks every dependency and credential before a first real run. Fix what it
reports; it is cheaper than debugging a half-configured pipeline.

## 4. First run — dry

```
DRY_RUN=true
```

Previews arrive in Telegram; nothing publishes. Run it in the foreground:

```bash
./.venv/bin/python -m pipeline
```

You should see, within a couple of seconds:

```
telethon...: Connection to ... complete!
pipeline: running — watching [-1001234567890]
aiogram.dispatcher: Start polling
```

## 5. Install as a service

```bash
cp deploy/com.aayush.newspipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.aayush.newspipeline.plist
```

Edit the paths in the plist to match your checkout first.

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

## 6. Going live

```
DRY_RUN=false
```

Restart the service. `.env` is read once at startup — editing it changes
nothing until you do.

```bash
launchctl unload ~/Library/LaunchAgents/com.aayush.newspipeline.plist
launchctl load   ~/Library/LaunchAgents/com.aayush.newspipeline.plist
```

Approve one post and watch `data/pipeline.log` for `published item N as …`.
