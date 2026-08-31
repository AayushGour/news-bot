# Telegram → Research → Instagram Content Pipeline

**Date:** 2026-08-31
**Status:** Design approved, ready for implementation planning
**Validated by:** PoC spike, 2026-08-31 (see Appendix A)

---

## 1. Purpose

An always-on service that turns tech/AI news into Instagram carousel posts without
manual writing or design work.

A message arrives in a Telegram channel. The system researches it against the open
web, synthesises what it finds into a factual brief, writes carousel slides from
that brief, renders them as images, and sends them to the operator for approval.
On approval it publishes to Instagram.

The operator can also feed the system directly by sending text, links, or images to
the same bot that delivers approvals.

**Core value:** the research step. The source channel posts a paragraph; the system
publishes a researched carousel with context the original post did not contain.
The PoC confirmed this works — see Appendix A.

## 2. Scope

### In scope

- Ingest from one or more Telegram channels the operator can read
- Ingest from operator DMs (text, URLs, images, forwarded messages)
- LLM triage to reject noise before expensive work
- Vision extraction from attached images
- URL fetch and article extraction
- Parallel web research via self-hosted SearXNG
- Synthesis into a sourced factual brief
- Slide composition (3–10 slides, model-chosen count)
- HTML/CSS → PNG rendering
- Human approval via Telegram, with regenerate and caption editing
- Publishing to Instagram as a carousel

### Out of scope

- Auto-posting without human approval. The approval gate is mandatory and
  structural, not a configurable flag. Appendix A explains why.
- Video or Reels. Static carousels only.
- Instagram Stories.
- Multi-account publishing.
- Analytics or engagement tracking.
- A web UI. Telegram is the only operator interface.

## 3. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Image generation | HTML/CSS template → headless Chromium screenshot | Pixel-perfect text; diffusion models garble informative slides |
| Instagram API | Instagram API with Instagram Login | Account is Business without a linked Facebook Page |
| Telegram read | Telethon user session | Source channel is not operator-owned |
| Telegram approval | Separate BotFather bot | User accounts cannot send inline keyboards |
| Models | Local, via Ollama | Zero marginal cost; PoC proved viability |
| Architecture | Single async process, SQLite state machine | Crash-safe and resumable without a broker |
| Triage | LLM gate before research | Noisy channel would otherwise flood the queue |
| Post timing | Publish immediately on approve | No scheduler needed |
| Stale previews | Never expire | Operator acts on their own schedule |
| Hosting | Local machine first | Cloud migration deferred; see §12 |

### Model assignment

| Role | Model | `num_ctx` |
|---|---|---|
| Cheap — triage, query planning, relevance gate, note summaries | `qwen3:4b-instruct` | 8192 |
| Good — synthesis, slide composition | `qwen3.5:9b` | 16384 |
| Vision — image description and OCR | `llama3.2-vision` | 8192 |

`num_ctx` **must** be pinned per request. Unpinned, Ollama 0.33 loads models at
their full declared context (262144 for `qwen3:4b-instruct`), claims ~43 GB, and
spills to CPU. Pinned, the same model is 3.9 GB and fully GPU-resident. This is a
correctness requirement, not a tuning preference.

## 4. Architecture

Single Python process, one asyncio event loop, three concurrent components sharing
one SQLite database:

```
┌─ Telethon listener ─┐
│  channel messages   │──┐
└─────────────────────┘  │
┌─ aiogram bot ───────┐  │      ┌──────────────┐
│  operator DMs       │──┼─────▶│  SQLite      │
│  approval callbacks │  │      │  items table │
└─────────────────────┘  │      └──────┬───────┘
┌─ worker loop ───────┐  │             │
│  advances stages    │◀─┴─────────────┘
└─────────────────────┘
```

The `status` column on `items` is the state machine. There is no separate queue,
broker, or scheduler. The worker polls for rows needing their next stage, runs one
stage, and commits the result with the new status in a single transaction.

Every stage is a pure function `Item → dict of new fields`. No stage imports
another. This is what makes them independently testable and keeps `worker.py`
small.

### State machine

```
                    ┌──────────────▶ dropped        (triage rejected)
                    │
ingested ──▶ triaged ──▶ extracted ──▶ researched ──▶ synthesized ──▶ composed
                                                                          │
                                                                          ▼
  published ◀── publishing ◀── approved ◀── awaiting_approval ◀────── rendered
                                   ▲              │
                                   │              ├──▶ rejected     (operator)
                                   └──────────────┘  (regenerate → composed)

  any stage ──▶ failed            (retries exhausted; operator notified)
```

`awaiting_approval` is **terminal for the worker**. Only a bot callback advances
it. The human gate is enforced by the state machine rather than by a check any
future code change could forget.

### Why this shape

- **Crash safety.** Kill the process mid-research; on restart it resumes at
  `extracted`. No lost work, no duplicate posts.
- **Catch-up.** Telethon delivers only live events. On boot the listener queries
  `MAX(source_msg_id)` per channel and backfills anything missed while down. The
  uniqueness constraint makes replay safe.
- **Cheap regeneration.** `brief` is stored separately from `slides`, so
  regenerating slides costs one model call (~78s) rather than a full re-research
  (~4.5 min).

## 5. Module layout

```
telegram-automation/
├── docker-compose.yml            # searxng + app (Ollama stays on host)
├── .env.example                  # every variable, documented
├── .gitignore
├── config/theme.json             # brand tokens; placeholder initially
├── data/                         # gitignored
│   ├── app.db                    # SQLite, WAL mode
│   └── media/                    # rendered PNGs pending upload
├── secrets/telegram.session      # gitignored, chmod 600
├── templates/
│   ├── base.html.j2
│   └── slides/{hook,point,facts,takeaway,sources}.html.j2
├── src/pipeline/
│   ├── __main__.py               # boots listener + bot + worker
│   ├── config.py                 # env → typed settings, fail fast
│   ├── db.py                     # schema, migrations, row helpers
│   ├── models.py                 # Item, Slide, ResearchNote
│   ├── llm.py                    # cheap() / good() / vision() over Ollama
│   ├── worker.py                 # stage dispatch loop
│   ├── intake/
│   │   ├── channel.py            # Telethon listener + backfill
│   │   └── bot_intake.py         # operator DM handler
│   ├── stages/
│   │   ├── triage.py
│   │   ├── extract.py            # vision + URL article extraction
│   │   ├── research.py           # plan → search → relevance gate → notes
│   │   ├── synthesize.py
│   │   ├── compose.py
│   │   └── render.py             # Jinja2 → Playwright, subprocess
│   ├── publish/
│   │   ├── media_host.py         # PNG → R2 → public URL
│   │   └── instagram.py          # containers → carousel → publish
│   └── approval/
│       └── bot.py                # preview, keyboard, callbacks
└── tests/
```

## 6. Data model

```sql
CREATE TABLE items (
  id                INTEGER PRIMARY KEY,
  source            TEXT NOT NULL,        -- 'channel' | 'dm'
  source_chat_id    INTEGER,
  source_msg_id     INTEGER,
  created_at        TEXT NOT NULL,

  raw_text          TEXT,
  raw_media_paths   TEXT,                 -- json array

  status            TEXT NOT NULL,
  status_updated_at TEXT NOT NULL,
  attempts          INTEGER DEFAULT 0,
  next_attempt_at   TEXT,
  last_error        TEXT,

  triage_score      INTEGER,
  triage_reason     TEXT,
  extracted         TEXT,                 -- json
  research          TEXT,                 -- json: notes[]
  brief             TEXT,
  slides            TEXT,                 -- json
  caption           TEXT,
  regen_note        TEXT,

  rendered_paths    TEXT,                 -- json
  media_urls        TEXT,                 -- json

  approval_msg_id   INTEGER,
  ig_child_ids      TEXT,                 -- json; persisted for retry safety
  ig_carousel_id    TEXT,
  ig_post_id        TEXT,
  published_at      TEXT,

  UNIQUE(source_chat_id, source_msg_id)
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  from_status TEXT, to_status TEXT,
  at TEXT NOT NULL, detail TEXT
);

CREATE INDEX idx_items_status ON items(status, next_attempt_at);
```

`UNIQUE(source_chat_id, source_msg_id)` is the idempotency guarantee. Backfill and
restart replay cannot create duplicates. DM-sourced items use the DM's message id,
so the same constraint applies.

## 7. Pipeline stages

### 7.1 `triage.py`

Cheap model. Structured output `{score: 0-10, reason, topic}`, prompt anchored on
tech/AI criteria: does this contain a concrete, researchable claim, or is it
chatter, promotion, or a repost?

- Below `TRIAGE_THRESHOLD` (default 6) → `dropped`, reason recorded, surfaced in
  the daily digest.
- **DM items bypass triage** with score forced to 10. If the operator sent it, they
  want it. This bypass is why DM authorisation (§8.1) is a hard requirement rather
  than a nicety — an unauthenticated DM would be a direct, untriaged path into the
  publishing pipeline.
- Near-duplicate check against recent items before scoring — news channels repost
  the same story multiple times.

### 7.2 `extract.py`

- **Images:** vision model returns both a description and OCR'd text. Media
  downloaded via Telethon.

  **Unvalidated.** The PoC deliberately excluded vision — the sample message was
  text-only and vision was not the risky part. `llama3.2-vision` is the only vision
  model currently installed and is known to be weak at OCR. Step 3 of §13 should
  begin by testing it against real channel screenshots; if OCR quality is
  insufficient, `qwen2.5vl:7b` (~6 GB) is the intended replacement. Everything
  downstream is unaffected either way, since `extract.py` failures are survivable
  (§10).
- **URLs:** regex-extracted, fetched with httpx (timeout, size cap, browser UA),
  main text via `trafilatura`, capped at ~6k chars each.

Failures here never fail the item. A paywalled link records `{url, error}` and the
pipeline continues with whatever else it has.

### 7.3 `research.py`

```
input text + extracted
   │
   ▼ query planner (cheap model)
4–6 search queries, each targeting a different facet:
  what happened · technical background · who is involved ·
  prior comparable events · criticism and consequences
   │
   │  Queries MUST carry entity disambiguation. See §7.3.1.
   ▼ asyncio.gather over queries
per query:
   SearXNG /search?format=json  →  top 6 results
   →  domain-dedupe, keep 3
   →  fetch + extract each
   →  RELEVANCE GATE (cheap model): is this document actually about
      the entity in question? Reject before it enters the corpus.
   →  cheap model → ResearchNote{claim, detail, confidence, sources}
   │
   ▼
notes[]   — individual researcher failure is survivable
            ≥2 successful notes required to advance, else `failed`
```

#### 7.3.1 Entity disambiguation and the relevance gate

The PoC failed here in a way that would have published a fabricated attribution.
Searching "Cursor" returned mouse-cursor sites — `custom-cursor.com`,
`rw-designer.com`, Stack Overflow threads about SQL and CSS cursors. The synthesis
then cited `custom-cursor.com`, a cursor-graphics download site, as the source for
a statement by Cursor's leadership.

Two independent mitigations, because either alone can fail:

1. **Query planner disambiguation.** The planner is given the entity context and
   must include disambiguating terms in every query (e.g. "Anysphere", "AI coding
   editor"), never the bare ambiguous token.
2. **Relevance gate.** Every fetched document is scored by the cheap model for
   whether it concerns the actual subject before it may enter the corpus.
   Rejections are logged with the domain so recurring offenders are visible.

A static domain blocklist backs both up, seeded from the PoC's observed failures.

### 7.4 `synthesize.py`

Good model. All notes plus the original message → a factual brief.

- Only claims the notes support. Unsupported material is dropped.
- Contradictions between sources are stated explicitly, not silently resolved.
- Every fact carries its source URL inline.
- 250 words maximum.

Source attribution here is what makes the sources slide honest and lets the
operator spot-check claims at approval time.

### 7.5 `compose.py`

Good model, structured output against a JSON schema. Brief → slides, caption,
hashtags. The model chooses the slide count, clamped to 3–10 (Instagram's carousel
maximum is 10).

Slide types:

| Type | Content | Usage |
|---|---|---|
| `hook` | headline, sub | exactly one, first |
| `point` | headline, up to 4 bullets, optional stat | one or more |
| `facts` | headline, up to 4 label/value rows | optional |
| `takeaway` | headline, sub | exactly one, near last |
| `sources` | headline, up to 4 URLs | exactly one, last |

`facts` is deliberately named for what the model actually produces. The PoC used
the name `compare`, and the model correctly ignored the comparison semantics and
emitted a label/value table — the name was wrong, not the output. A genuine
`versus` type can be added later if A-vs-B slides are wanted.

Per-field character limits are stated in the prompt but treated as **hints, not
guarantees**. The PoC produced a 243-character field against a stated 110-character
limit. Enforcement is the renderer's job — see §7.6.

### 7.6 `render.py`

Jinja2 template per slide type, tokens from `config/theme.json`. Playwright
Chromium at 1080×1350, one screenshot per slide, browser reused across slides
within an item.

**Runs in a subprocess.** Chromium OOMs are routine and must not take the Telethon
connection down with them.

**Overflow guard — the real enforcement mechanism.** After layout, every text box
is checked via `scrollHeight > clientHeight`. On overflow: step the font down one
notch and re-measure; if it still overflows, return the item to `composed` with a
note naming the offending slide. Deterministic, costs no tokens, and does not
depend on the model respecting a limit it demonstrably ignores.

## 8. Approval flow

Telegram album media groups cannot carry inline keyboards, so the preview is two
messages:

```
[album: slide_01.png … slide_N.png]
[text message, replying to the album]
   caption preview + hashtags
   triage score · N/M researchers succeeded · source domains used
   [✅ Approve]  [🔄 Regenerate]
   [✏️ Caption]  [❌ Reject]
```

Listing the **source domains in the preview** is deliberate: it is the operator's
last chance to catch a relevance failure that got past §7.3.1.

| Action | Transition | Cost |
|---|---|---|
| Approve | → `approved`; worker publishes immediately | — |
| Reject | → `rejected` (terminal) | — |
| Regenerate | prompts for optional note → `composed` with `regen_note` | ~78s, re-composes from stored `brief` |
| Caption | prompts for replacement text; re-previews caption only | no re-render |

Items with no response **never expire**. They remain in `awaiting_approval`
indefinitely and are counted in the daily digest.

### 8.1 Authorisation

Telegram bot usernames are discoverable, and anyone can start a conversation with
one. Both operator entry points must therefore authenticate:

- **DM intake** accepts messages only from `OPERATOR_USER_ID`. Anything else is
  ignored silently — no reply, no error, no acknowledgement that the bot is live.
  Without this check, any stranger who finds the bot can inject content that
  bypasses triage (§7.1) and reaches the approval queue.
- **Approval callbacks** verify `callback_query.from_user.id == OPERATOR_USER_ID`
  before acting. Without this check, a stranger who obtains a callback payload
  could approve a post to the operator's Instagram account.

Both checks are enforced in a single decorator applied to every handler, so adding
a new handler cannot accidentally omit it. This is the only authorisation boundary
in the system and it protects the publishing path, so it is covered by explicit
tests (§11) rather than assumed.

## 9. Publishing

Instagram API with Instagram Login. No Facebook Page involved.

```
rendered PNGs
   ▼ media_host.py
R2 upload, key items/{id}/slide_{n}.png  →  public URLs
   ▼ instagram.py
POST /{ig-user-id}/media  ×N   (image_url, is_carousel_item=true)  → child ids
   ▼
POST /{ig-user-id}/media  (media_type=CAROUSEL, children=[…], caption)  → carousel id
   ▼
POST /{ig-user-id}/media_publish  (creation_id)  → ig_post_id
```

Media must be at a publicly reachable URL because Meta's servers fetch it;
`localhost` cannot work. Cloudflare R2 free tier (10 GB, no egress fees) serves
this and remains correct after any later migration.

### Retry safety

Double-posting is the significant hazard. If `media_publish` succeeds but the
response is lost, a naive retry publishes twice.

- `ig_child_ids` and `ig_carousel_id` are persisted as they are created and
  **reused** on retry rather than recreated.
- Before retrying `media_publish`, query recent media for a post matching the
  carousel container.
- `ig_post_id` is written the moment it returns, before any other work.

### Token lifecycle

Instagram long-lived tokens expire after 60 days. A background job refreshes at day
50 and **notifies the operator by DM if the refresh fails**. Without this,
publishing stops silently and the only symptom is that nothing happens.

Rate limit: 100 published posts per rolling 24 hours, tracked in the database.

## 10. Error handling

Failures are not uniform, and treating them uniformly wastes retry budget on
errors that will never succeed:

| Class | Examples | Policy |
|---|---|---|
| **Survivable** | one researcher fails, one URL 404s, image extract fails | Continue with what remains. Advance the item. |
| **Retryable** | Ollama unreachable, schema parse failure, R2 upload, Instagram 5xx | Exponential backoff via `next_attempt_at`, max 3 attempts, then `failed` + operator DM |
| **Terminal** | Instagram 4xx, expired token, rate limit exceeded | No retry. Immediate `failed` + operator DM. |

Ollama being unreachable is retryable but should not consume the attempt budget —
the service being down is not the item's fault. It backs off and retries
indefinitely until Ollama returns.

### Observability

- `events` table is a complete audit trail of every transition.
- Daily digest DM: ingested, dropped (with reasons), published, failed,
  awaiting approval.
- Structured logs to file.

## 11. Testing

Stage functions are pure `Item → dict`, which makes the strategy straightforward:

- **Per-stage unit tests** against recorded fixtures.
- **Fake `llm()` seam** returning canned responses, so the suite runs without a GPU
  and in CI-appropriate time. This is the single most important test affordance in
  the design.
- **Recorded SearXNG and HTTP fixtures.** The PoC's `run.json` is the seed corpus —
  the one artefact worth carrying over from the throwaway.
- **Golden-image tests** for the renderer: known slide JSON → PNG, compared against
  a committed reference with tolerance.
- **Relevance gate regression test** using the PoC's actual poisoned domains
  (`custom-cursor.com`, `rw-designer.com`, the Stack Overflow cursor threads) as
  negative cases. This bug is now permanently pinned by a test.
- **Authorisation tests** (§8.1): a DM from a non-operator user id creates no item;
  a callback from a non-operator user id does not transition state. These guard the
  only path from a stranger to the operator's Instagram account.
- **Integration test** driving the full pipeline against fakes, asserting the state
  machine reaches `awaiting_approval`.
- **`DRY_RUN` mode** logs Instagram API calls instead of making them. No test ever
  posts to the live account.

## 12. Configuration, secrets, deployment

```
CHANNEL_IDS=-1001526709058
TELEGRAM_API_ID=            TELEGRAM_API_HASH=
TELEGRAM_BOT_TOKEN=         OPERATOR_USER_ID=
OLLAMA_HOST=http://localhost:11434
MODEL_CHEAP=qwen3:4b-instruct     NUM_CTX_CHEAP=8192
MODEL_GOOD=qwen3.5:9b             NUM_CTX_GOOD=16384
MODEL_VISION=llama3.2-vision      NUM_CTX_VISION=8192
SEARXNG_URL=http://localhost:8080
TRIAGE_THRESHOLD=6
R2_ACCOUNT_ID=  R2_ACCESS_KEY=  R2_SECRET_KEY=  R2_BUCKET=  R2_PUBLIC_BASE=
IG_USER_ID=     IG_ACCESS_TOKEN=
DRY_RUN=true
```

### Secrets

`secrets/telegram.session` is **account-equivalent** — anyone holding it has full
access to the operator's Telegram account. It is generated locally, chmod 600, and
never committed. `.env` is gitignored; `.env.example` is committed with every
variable documented and no values. `.gitignore` exists from the first commit.

### Deployment

Docker Compose runs SearXNG and the application. **Ollama runs on the host, not in
Compose** — Docker Desktop on macOS cannot reach the GPU. The app container reaches
it at `host.docker.internal:11434`.

### Known risk: local models do not transfer to free cloud hosting

The measured ~4.5 min/post is an M4 GPU figure. Oracle's Always Free ARM instance
has no GPU, so the same models would run on CPU — realistically 5–10× slower, or
25–45 minutes per post.

This is a genuine conflict between two of the requirements ("free cloud hosting"
and "local models via Ollama") that does not resolve itself. When migration comes,
the options are:

1. Keep inference on the local machine, host only the orchestrator in the cloud.
2. Switch the cloud deployment to hosted model APIs (~$5–20/mo, the originally
   chosen budget).
3. Accept 25–45 min per post — viable, since approval is asynchronous anyway.

No decision is required now. The design must not pretend the choice transfers, and
`llm.py` is therefore an abstraction over providers rather than hardwired to
Ollama, so option 2 stays a configuration change rather than a rewrite.

## 13. Implementation order

Vertical slices, each independently runnable:

1. Skeleton: config, db, models, worker loop, `.gitignore`, `DRY_RUN`
2. Intake: Telethon listener + backfill, DM handler with the §8.1 authorisation
   decorator → `ingested` rows
3. Triage + extract
4. Research with disambiguation and relevance gate
5. Synthesis + compose
6. Render + overflow guard + golden tests
7. Approval bot: preview, four callbacks
8. Publishing: R2, Instagram, retry safety, token refresh
9. Daily digest, operator alerts

Steps 1–6 are testable end-to-end with no Instagram account involvement at all.

## 14. Open questions

- Brand theme (colours, fonts, logo) is a placeholder. Deferred by decision; the
  pipeline works without it and the visual layer swaps independently.
- Whether feed variety needs a knob. The PoC produced an identical 5-slide
  structure across all 5 runs at temperature 0.6 — good for brand consistency,
  potentially monotonous over many posts. Revisit after ~20 real posts.
- Republishing someone else's channel content: captions credit the source channel.
  Output is transformed research rather than reproduction, but the operator should
  confirm this is acceptable for the specific channel.

---

## Appendix A — PoC findings

A throwaway spike (`poc/`) ran the research → synthesis → slides → render path
end-to-end on local models, using a real message from the target channel about
OpenAI restricting Cursor's model access.

### Measurements

| Stage | Cold | Warm |
|---|---|---|
| Query planning (4b) | 8.7s | — |
| 5 researchers (4b) | 130.7s | — |
| Synthesis (9b) | 90.8s | ~45s |
| Slide composition (9b) | 132.5s | 72–83s |
| Render 6 PNGs | 8.3s | — |
| **Total** | **371s** | **~270s** |

Schema adherence: **5/5 valid** across five composition runs, zero parse failures,
zero over-length headlines. Render overflow: none.

### What it validated

- Ollama's JSON-schema structured output is reliable on both model tiers. This was
  the principal risk to the local-model approach.
- The research step adds genuine value. The source message never mentioned SpaceX,
  Elon Musk, or the OpenAI Startup Fund seed round; research surfaced all three as
  the actual causal context.
- The overflow guard works and is cheap.

### What it broke, and what changed as a result

1. **Source-pool poisoning by keyword collision** → §7.3.1 relevance gate and query
   disambiguation. This is the most serious finding; without a fix the system would
   have published a fabricated attribution.
2. **`compare` slide type misnamed** → renamed `facts` in §7.5.
3. **Character limits ignored by the model** → §7.6 makes the overflow guard the
   enforcement mechanism rather than the prompt.
4. **Unpinned `num_ctx` causes 43 GB allocation and CPU spill** → §3 pins it per
   model role.

### A note on verification

The research surfaced claims about a SpaceX acquisition of Cursor that could not be
verified at design time — they postdate the assistant's knowledge cutoff, and the
same run demonstrably ingested irrelevant sources. Whether those claims were sound
reporting or collision noise was not determinable from inside the pipeline.

This is the concrete argument for the approval gate in §2 being mandatory rather
than optional. A system that researches faster than it can verify needs a human
between research and publication.

### Disposition

`poc/` is throwaway. The only artefact carried forward is `poc/out/run.json` as
seed test fixtures (§11). The SearXNG container configuration is reused directly.
