# Usage

Day-to-day operation: getting work in, approving it, and fixing it when it
goes wrong.

## Getting work in

**Watched channel** — anything posted to a `CHANNEL_IDS` channel is ingested
automatically. On reconnect the listener backfills what it missed, so an
outage delays posts rather than losing them.

**DM the bot** — send a link, some text, or an image. Same pipeline. A DM can
also carry a request rather than a story: *"top 5 github repos for AI interview
preparation"* routes to the enumeration path instead of the news path.

**Dashboard** — `New post` on the index, for the same thing without Telegram.

## Approving

When a deck is ready you get a Telegram preview with the slides and caption:

| Action | Effect |
|---|---|
| **Approve** | uploads, builds containers, publishes |
| **Reject** | terminal — the item stops |
| **Regen** | recompose with a note you supply |
| **Caption** | rewrite just the caption |

Approval is the only thing that publishes. Nothing reaches Instagram without
it.

### When the pipeline asks you something

An item can park in `needs_input` with a question — a subject it could not
research, or a clause nothing answered. **Swipe-reply to the question message**
to answer it. That is how replies are routed when several items are waiting at
once; a bare message with two questions outstanding is ambiguous, so the reply
target disambiguates it.

`/drop` discards the item. *"post what you have"* continues with partial
research.

## The dashboard

```bash
./.venv/bin/python scripts/dashboard.py     # http://127.0.0.1:8770
```

Loopback only, and deliberately so — approving publishes to a real account.

| Route | What it does |
|---|---|
| `/` | all items in lanes: needs attention · working · good · bad |
| `/queue` | execution order, reorderable |
| `/item/<id>` | slides, research, caption, timeline, log, chat |
| `/logs` | pipeline log, newest first, rotation-aware |

On an item page:

- **Stage timeline** — the pipeline drawn as clickable stages. Green behind
  you, blue for where you are, hollow ahead. Click any stage to requeue there;
  the hover text says what that clears. `awaiting approval` and `published`
  render but are not clickable, because requeue rejects them.
- **Chat** — send a message to the item; the same conversation the bot has.
- **Already-published banner** — amber, on any item that has gone out before.
  It survives a requeue, which is the point: `ig_post_id` is cleared by a
  requeue, so without this an item that is already live looks untouched and a
  second approval would post a duplicate.

## Requeue

Send an item back to an earlier stage. Everything that stage and later stages
produce is cleared, so nothing carries stale inputs forward.

```bash
./.venv/bin/python scripts/requeue.py 100 --to triaged
```

Or click the stage on the dashboard timeline.

Requeue targets are the nine worker-owned stages. Choosing the right one:

| You want to change | Requeue to |
|---|---|
| the whole research | `triaged` |
| just the writing | `researched` |
| just the slide layout | `synthesized` |
| just the images | `composed` |
| republish the same deck | `publishing` |

A requeue resets the attempt counter — it is a fresh attempt, not the
continuation of a failing one.

## Priority and ordering

Items run in queue order, which you can change on `/queue` (▲▼ or *top*).
Useful when something is time-sensitive and the queue is deep.

## When something fails

Start with the item, not the logs:

```bash
./.venv/bin/python - <<'PY'
import sqlite3, json
d = sqlite3.connect("data/app.db"); d.row_factory = sqlite3.Row
r = d.execute("SELECT status, attempts, last_error, question FROM items WHERE id=100").fetchone()
print(dict(r))
PY
```

`/item/100` on the dashboard shows the same thing plus the transition timeline,
which is usually the fastest way to see *what* moved it and *when*.

### Failures you will actually hit

**`Only photo or video can be accepted as media type`**
Meta could not fetch the image URL. The message names a media type but the
fault is the host. Check the tunnel:

```bash
cat data/tunnel-origin.txt
curl -sI "$(cat data/tunnel-origin.txt)/slides/items/100/item100_slide_01.png" | head -1
```

You should not see this any more: the preflight catches an unreachable host
before Meta does and names it, stale URLs are re-addressed to the live host at
publish time, and a media outage defers without spending an attempt. If it does
appear, the media host is genuinely down — check MinIO, not the pipeline.

**`found nothing worth posting (0 candidates)`**
Either the subject genuinely has nothing, or a search engine is suspended.
SearXNG suspends an engine that 403s for 180 seconds and keeps answering 200
with an empty list. Check:

```bash
curl -s 'http://localhost:8080/search?q=test&format=json&engines=github' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["results"]), d.get("unresponsive_engines"))'
```

**`openrouter returning an unusable completion`**
The hosted model returned something that did not satisfy the schema. Three
attempts, then the local Ollama model. Frequent occurrences mean the model is
a poor fit — pin a different one rather than living with the retries.

**Item stuck in `awaiting_approval` forever**
That is the design. Nothing moves it but you.

## Health check

```bash
launchctl list | grep newspipeline          # pid, last exit code
pgrep -fc "python -m pipeline"              # must be exactly 1
cat data/tunnel-origin.txt                  # live media host
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8770/
tail -f data/pipeline.log
```

Two instances is a real failure mode — they contend for the Telegram session.
If `pgrep` returns more than 1, unload the service and kill the strays.

## Limits worth remembering

| Limit | Value | Imposed by |
|---|---|---|
| slides per carousel | 10 | Instagram |
| hashtags | 5 | project convention |
| posts / 24h | 50 | Instagram (carousel) |
| Telegram message | 4096 chars | Telegram |
| Telegram caption | 1024 chars | Telegram |
