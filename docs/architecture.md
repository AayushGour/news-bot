# Architecture

## What this is

One long-lived Python process that turns Telegram posts into published
Instagram carousels, with a human approval gate in the middle. It is a
**queue with a state machine**, not a request/response service: every unit of
work is a row in SQLite, and every stage is a function that moves one row
forward.

There is no message broker, no external scheduler, and no second service. That
is deliberate — see [Why one process](#why-one-process).

## The flow

```
 Telegram channel ─┐
                   ├──▶ ingested ──▶ extracted ──▶ triaged ──▶ researched
 DM to the bot   ──┘                  (vision)     (score)     (SearXNG)
 Dashboard "new" ──┘                                                │
                                                                    ▼
                                                              synthesized
                                                                (brief)
                                                                    │
                                                                    ▼
  published ◀── publishing ◀── approved ◀── awaiting_approval ◀── rendered ◀── composed
   (Graph)      (containers)    (upload)     (you decide)          (PNG)     (slide JSON)
```

Every arrow is one stage function. Stages never call each other; they return
field updates and the worker writes them, so any stage can be re-run in
isolation.

## The state machine is the schema

`items.status` **is** the state machine. There is no separate queue table, no
job registry, and no in-memory state that matters. Restarting the process
loses nothing.

| Status | Meaning | Next stage |
|---|---|---|
| `ingested` | raw text/media captured | extract |
| `extracted` | images described by a vision model | triage |
| `triaged` | scored for researchability | research *or* enumerate |
| `researched` | notes gathered and relevance-gated | synthesize |
| `synthesized` | a written brief exists | compose |
| `composed` | slide JSON exists | render |
| `rendered` | PNGs on disk | send preview |
| `awaiting_approval` | **waiting on you** | — |
| `approved` | you said yes | upload to storage |
| `publishing` | media uploaded, containers next | publish |
| `published` | live on Instagram | — |
| `needs_input` | the pipeline asked you a question | — |
| `rejected` / `dropped` / `failed` | terminal | — |

`WORKER_HALTS` is the set the worker will not touch: the three terminal states
plus `awaiting_approval`, `needs_input`, and `published`. Anything else has a
stage registered for it, and `missing_statuses()` raises at startup if one
does not — a status with no stage would strand items silently.

### Extraction runs before triage

Counter-intuitive, and load-bearing. Many channel posts are an image with no
caption. Triaging on body text alone dropped those unread and reported it as a
quiet channel. The cost is one vision call per post *ahead of* the filter,
which is the price of not being blind.

## The worker

Two loops over the same registry, split by latency class:

```python
FAST_STAGES = frozenset({Status.APPROVED, Status.PUBLISHING})

run_slow(interval=5s)   # everything else
run_fast(interval=2s)   # approve → publish
```

They were one loop until a 983-second compose blocked every approval behind it.
`tick()` claims a batch and gathers it, so one slow item stalls the batch;
splitting the stages that a human is waiting on into their own loop bounds that.

### Failure taxonomy

`errors.py` defines how a stage fails, and the worker acts on the type — not
on a string match:

| Exception | Worker behaviour |
|---|---|
| `Survivable` | log, continue with partial results |
| `Retryable` | retry, counting against `max_attempts` (default 3) |
| `Retryforever` | defer **without** spending an attempt — it is infrastructure, not the item |
| `RateLimited` (⊂ `Retryforever`) | same, plus backoff |
| `BadCompletion` (⊂ `Retryable`) | the model returned something unusable |
| `Terminal` | stop permanently, notify |
| `Recompose` | go back to compose |
| `NeedsInput` | ask the operator, park in `needs_input` |

The distinction that matters most: **an outage must not spend the item's retry
budget**. A dead SearXNG is down for every item, so charging one item three
attempts for it turns an infrastructure blip into permanent content loss.

## Models: three roles, not three models

Every call asks for a *role* — `cheap`, `good`, or `vision` — and the client
resolves it. Each role has an OpenRouter model and a local Ollama fallback.

```
llm.cheap(...)   triage · relevance gate · research notes · query expansion · coverage
llm.good(...)    synthesize · compose · enumerate
llm.vision(...)  extract
```

`cheap` does far more than its name suggests: it writes the research notes that
become the deck's facts, and it operates every quality gate. A weak `cheap`
model does not produce visibly broken output — it produces undifferentiated
output, which is worse, because every downstream threshold then passes
everything.

The client degrades in a ladder: OpenRouter with a JSON schema → `json_object`
with the schema in the prompt (for models that reject `json_schema`) → local
Ollama. Three attempts at OpenRouter, then local. A 4xx that will fail
identically is excluded from retry.

## Research

```
plan_queries ──▶ expand_queries ──▶ search_many ──▶ fetch ──▶ relevance gate ──▶ notes
  (entity +        (4 → ~16)        (SearXNG)     (trafilatura)   (llm.cheap)
   clauses)
```

Two catalogues, chosen by intent:

- **news** — `categories=general,it,news`, deduped by domain (five pages from
  one publisher is one source).
- **repos** — `engines=github`, deduped by path (twenty github.com results are
  twenty different answers, not one source repeated).

Single-engine searches are **serialised and paced** (`SINGLE_ENGINE_INTERVAL_S`).
GitHub's unauthenticated search API allows roughly ten requests a minute;
sixteen expanded queries fired four at a time earned a 403, and SearXNG
suspends a 403ing engine for 180 seconds while continuing to answer 200 with an
empty result list. Pacing costs wall-clock and buys correctness. No query is
dropped.

`search_many` keeps partial results when an outage starts mid-batch, and raises
only when nothing came back at all — a batch that found 107 repositories across
fifteen queries used to report none because the sixteenth tripped a cooldown.

## Composition and rendering

`compose` emits slide JSON against a schema of 15 slide types (`hook`, `point`,
`repo`, `facts`, `kpi`, `chart`, `code`, `flow`, `compare`, `quote`, `photo`,
`links`, `takeaway`, `sources`, `follow`). `normalise_slides` then enforces what
a JSON schema cannot: ordering, uniqueness, and that **every slide has body
content** — a headline alone renders as text on an empty 1080×1350 field.

Rendering is Jinja2 → HTML → Chromium screenshot, one PNG per slide, with an
automatic shrink pass (`data-shrink`) when content overflows. That shrink pass
is why text is never truncated to fit: the slide gets smaller type instead of a
cut-off sentence.

## Publishing

```
approved ──▶ upload to MinIO ──▶ resolve public base ──▶ child containers
                                                              │
   published ◀── media_publish ◀── wait for FINISHED ◀── carousel container
```

Three things here are non-obvious and each cost a real failure:

1. **Containers build asynchronously.** Publishing one second after creating
   the carousel returns `Media ID is not available`. `_await_container` polls
   `status_code` until `FINISHED`.
2. **`ig_post_id` is the double-post guard**, so a requeue must clear it — which
   makes it useless as a record of what went out. `publish_log` is that record,
   append-only, cleared by no stage.
3. **Media URLs must be publicly fetchable.** Meta reports an unreachable host
   as `Only photo or video can be accepted as media type` — a message about
   file formats for what is a DNS problem. A preflight GETs one slide first and
   fails with the host named.

### The media tunnel

Instagram fetches slides from the public internet, so local MinIO needs a public
face. A Cloudflare quick tunnel provides one, but **regenerates its hostname on
every start**. Pinning it in `R2_PUBLIC_BASE` meant the config was stale from
the next restart onward.

So the pipeline owns it: `publish/tunnel.py` supervises `cloudflared`, writes
the live origin to `data/tunnel-origin.txt` (write-then-rename, so an upload
cannot read half a hostname), and uploads resolve the base **per upload**.
`R2_PUBLIC_BASE` remains the fallback, so a named tunnel or a hosted bucket
needs no special case.

## Why one process

The tunnel supervisor was first written as its own launchd job. It exited
`78 / EX_CONFIG` with no output on every attempt — and so did a trivial
`/bin/echo` agent. macOS blocks **newly added** background items until a human
approves them in System Settings, so a second service fails silently until
someone thinks to check.

Running in-process also removes the state where the tunnel is up and the
pipeline is not, or the reverse.

## Storage and logs

- `data/app.db` — SQLite (WAL). `items`, `events`, `messages`.
- `data/media/<id>/` — rendered PNGs.
- `data/pipeline.log` — rotating, 8 MB × 5. The app owns this file; launchd's
  stdout goes to `data/stderr.log` because a launchd-redirected file cannot be
  rotated (moving it leaves launchd writing to the old inode).
- Every log handler carries a **redaction filter**. `httpx` logs full request
  URLs at INFO and Meta's read endpoints take the access token as a query
  parameter, which wrote a live token into the log in cleartext.

## Design rules this codebase follows

- **The prompt is a hint; the code is a guarantee.** Anything that must hold is
  enforced after the model returns, not asked for in the system prompt.
- **An outage is not a result.** Infrastructure failure must never be recorded
  as a finding about the content.
- **Never truncate content to fit.** Shrink the type, widen the window, page
  the query — but do not silently drop what was gathered.
- **Evidence over assertion.** A stage reports what it verified, not what it
  attempted.
