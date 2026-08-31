# Telegram → Research → Instagram

Watches a Telegram channel, researches each post against the open web, writes
and renders an Instagram carousel, and publishes it **after you approve it in
Telegram**.

You can also DM the bot a link, some text, or an image and it runs the same
pipeline on that.

```
channel post ─┐
              ├─▶ triage ─▶ extract ─▶ research ─▶ synthesize ─▶ compose
your DM     ──┘                (SearXNG,           (brief)      (slide JSON)
                            relevance-gated)                         │
                                                                     ▼
   Instagram ◀── publish ◀── R2 ◀── ✅ your approval ◀── preview ◀── render
                                     (Telegram)                    (PNG)
```

Everything runs locally. Models run on Ollama; no inference cost.

---

## What you have to set up by hand

The pipeline is built and tested, but five credentials cannot be created for
you. Until they exist, the process will not start.

| What | Where | Notes |
|---|---|---|
| `TELEGRAM_API_ID` / `_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools | Your account's API credentials |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` | Delivers approvals *and* accepts your DMs |
| `OPERATOR_USER_ID` | [@userinfobot](https://t.me/userinfobot) | The **only** account allowed to DM or press buttons |
| R2 keys | Cloudflare → R2 → create bucket, enable public access | Instagram fetches images by URL |
| `IG_USER_ID` / `IG_ACCESS_TOKEN` | Meta app → *Instagram API with Instagram Login* | No Facebook Page needed |

Steps 1–3 are enough to run everything up to a preview arriving in Telegram.
R2 and Instagram are only needed to actually publish.

---

## Setup

```bash
cp .env.example .env          # then fill it in
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/playwright install chromium

docker compose up -d searxng  # research backend
ollama serve                  # must run on the HOST, not in Docker
```

Pull the models:

```bash
ollama pull qwen3:4b-instruct   # triage, research, relevance gate
ollama pull qwen3.5:9b          # synthesis, slide copy
ollama pull llama3.2-vision     # image OCR  (see "Known gaps")
```

### Generate the Telegram session

Telethon needs an interactive login once. Run this yourself — it asks for your
phone number and the code Telegram sends you:

```bash
./.venv/bin/python -c "
from telethon import TelegramClient
import os
c = TelegramClient('secrets/telegram.session',
                   int(os.environ['TELEGRAM_API_ID']),
                   os.environ['TELEGRAM_API_HASH'])
c.start()
print('session created')
"
chmod 600 secrets/telegram.session
```

> `secrets/telegram.session` is **account-equivalent**. Anyone holding it has
> full access to your Telegram. It is gitignored; keep it that way.

### Run

```bash
./.venv/bin/python -m pipeline
```

`DRY_RUN=true` (the default) runs everything up to and including the approval
preview, and logs what it *would* post instead of posting. Leave it on until
previews look right. Turning it off without R2 and Instagram configured is
refused at startup.

---

## How it behaves

- **Triage** drops chatter before the expensive stages. Your DMs skip it.
- **Research** fans out 4–5 queries, each judged for relevance before its text
  is used. See "Why the relevance gate exists" below.
- **Regenerate** reuses the stored brief, so it costs one model call (~80s)
  rather than a full re-research (~4.5 min).
- **Nothing publishes without you.** `awaiting_approval` is terminal for the
  worker; only a button press moves an item forward.
- **Crash-safe.** State lives in `items.status`. Kill it mid-research and it
  resumes where it stopped. Missed messages are backfilled on boot.

Roughly **4.5 minutes** per item from arrival to preview, on an M4 with the
models warm.

---

## Why the relevance gate exists

An early prototype researched a story about Cursor, the AI editor. The search
returned mouse-cursor download sites, and the synthesis cited
`custom-cursor.com` as the source for a statement by Cursor's leadership.

Two defences now sit in `stages/research.py`: the query planner must emit
disambiguating context (enforced in code, not just asked for in the prompt),
and every fetched page is judged on whether it is actually about the subject
before its text can be used. The preview also lists the source domains, so you
can see where the research went before approving.

`tests/stages/test_research.py` pins this with the real domains that failed.

---

## Tests

```bash
./.venv/bin/python -m pytest -q     # ~170 tests, a few seconds
```

No GPU, no network, no accounts. Model calls go through a fake; the renderer
tests drive real Chromium.

---

## Known gaps

- **Vision is unvalidated.** The prototype was text-only. `llama3.2-vision` is
  weak at OCR; if image extraction disappoints, swap
  `MODEL_VISION=qwen2.5vl:7b`. Nothing downstream depends on it — extraction
  failures are survivable.
- **The theme is a placeholder.** `config/theme.json` and `templates/` are
  deliberately plain. They are decoupled from the pipeline; edit freely.
- **Local models do not transfer to free cloud hosting.** The 4.5 min/post
  figure is an M4 GPU number. Oracle's Always Free ARM tier has no GPU, so the
  same models would take 25–45 minutes. When you migrate, either keep inference
  at home and host only the orchestrator, or point `llm.py` at a hosted API —
  it is a provider abstraction for exactly that reason.

---

## Layout

```
src/pipeline/
  config.py db.py models.py llm.py worker.py errors.py digest.py
  stages/      triage extract research synthesize compose render
  intake/      channel (Telethon)   bot_intake (your DMs)
  approval/    auth (operator gate) bot (preview + buttons)
  publish/     media_host (R2)  instagram  tokens (60-day refresh)
docs/superpowers/specs/    design document
docs/superpowers/plans/    implementation plan
poc/                       throwaway prototype; kept for its fixtures
```
