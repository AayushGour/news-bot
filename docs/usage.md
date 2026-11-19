# Usage

Day-to-day operation: getting work in, approving it, and fixing it when it
goes wrong.

## Getting work in

**Watched channel** — anything posted to a `CHANNEL_IDS` channel is ingested
automatically. On reconnect the listener backfills what it missed, so an
outage delays posts rather than losing them. The article a post links to is
its main source, as with a link you DM, so a story too new for other coverage
still gets made. A post that comes with a video is
read from one still frame of it (the image model cannot read video). To watch
another channel, see [Adding a channel](#adding-a-channel).

**DM the bot** — send a link, some text, or an image. An image can be a photo
or an image sent as a file (JPEG, PNG or WebP); other files (a PDF, a video)
are not read. Each photo of an album arrives as its own message, so each
becomes its own post. Same pipeline. Each DM
is read as one of three kinds of request:

| You send | It becomes |
|---|---|
| a story or claim (*"OpenAI shipped X"*) | **news**: verified against the web, then explained |
| a question about one thing (*"what is next concept modeling?"*, *"how does RAG work"*, *"X vs Y"*) or a bare link | **explainer**: that one subject explained |
| a request for a set (*"top 5 github repos for AI interview prep"*) | **list**: the items found and checked one by one |

A link you send is the main source: the page itself (a repo's README, a
paper's abstract) is read first and the web search only adds to it. One good
source is enough for an explainer or a linked page; news needs two.

Spell out an acronym if it is ambiguous. *"What is ncm?"* alone can come back
as the networking term; *"what is NCM (next concept modeling)"* or the link
cannot. The preview always shows what it understood before anything publishes.

**Every DM is its own post.** Send two messages back to back and you get two
posts. The one exception: a message long enough to have hit Telegram's length
limit (4096 characters, or 1024 for a caption) is joined with the next text
message sent within two minutes, and the bot says *"Got the rest of that —
researching it together."*

**Copies are caught.** A DM that repeats a post from the last 48 hours (the
same text, give or take case, spacing, punctuation and how links are written —
a channel post forwarded to the bot counts) is not started again: the bot names
the item it already has. Resending something that was dropped, failed or
rejected is a retry, and goes through. Channel reposts are dropped at triage.

**Dashboard** — `New post` on the index, for the same thing without Telegram.

**Commands** — type `/` in the chat for the menu:

| Command | Does |
|---|---|
| `/status` | every post by state: processing, pending, complete, rejected (dropped and failed count here too). Tap a tab or ◀️ ▶️ to page; tap a post to jump to it — the bot replies to that post's message, and tapping the quote scrolls the chat there. A channel post with no message in the chat yet opens the channel post instead |
| `/channels` | lists the channels being watched, by name and id |
| `/help` | what the bot does |
| `/post` | answers a question the bot asked: post with what it found |
| `/drop` | answers a question the bot asked: drop the item |

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
research, a request none of whose parts the research answered, or a DM whose
research came back thin (fewer usable sources than it needs). If only some
parts went unanswered (a side claim in a forwarded story, say), it does not
ask: it posts what the sources support and leaves the rest out, as `/post`
would. **Swipe-reply to the question message**
to answer it. That is how replies are routed when several items are waiting at
once; a bare message with two questions outstanding is ambiguous, so the reply
target disambiguates it.

Three commands, and only these — a question never offers prose, because a
genuine answer containing the words would be swallowed as a command:

| Command | Effect |
|---|---|
| `/post` | build a deck from what has already been gathered and send it for **approval** — it does not publish |
| `/skip` | same as `/post` (offered by the research gate before `/post` existed) |
| `/drop` | discard the item |

Anything else is treated as your answer and re-runs the research with it. A
link in your answer is read as a primary source, the same as a link in the
original DM.

While the bot is waiting on your **Regen** or **Caption** note, a slash command
(say `/post`) is not taken as the note: you get a short hint instead. `/skip`
still skips.

`/post` resumes *after* research rather than at the stage that parked the
item. Resuming where it parked re-runs the search, which is a fresh attempt
and can find less — item 116 was parked holding eight notes, re-searched,
came back with none, and died. If nothing has been gathered at all, `/post`
says so and leaves the item parked rather than building an empty deck.

## The dashboard

http://127.0.0.1:8770 — the `dashboard` container. On a bare host:
`./.venv/bin/python scripts/dashboard.py` (`--port 8779` for another port).

Loopback only, and deliberately so — approving publishes to a real account.
There is no login. All times on every page are UTC; the sidebar footer says so.

The CSS and JS live in `scripts/dashboard_static/` (`dashboard.css`,
`dashboard.js`). The dashboard reads both once at start-up and inlines them in
every page — there is no static route, so edit the files, then rebuild and
restart (`docker compose build && docker compose up -d dashboard`).

| Route | What it does |
|---|---|
| `/` and `/overview` | the Overview: health at a glance |
| `/items` | every item in a table, filtered by lane (`/?lane=attn` still works) |
| `/item/<id>` | one item: deck, caption, decision bar, timeline, chat, history |
| `/queue` | execution order, reorderable |
| `/logs` | pipeline log, newest first, with filters |

### Getting around

The left sidebar is on every page, in the same order: **Overview**, **Review**
(items waiting on you), **Running**, **Published**, **Stopped** (failed,
rejected, dropped), **All items**, **Run order**, **Logs**. Each lane has its
live count; the current page is highlighted. The `‹` button at the bottom
collapses the sidebar to an icon rail, and the choice is remembered. The
search box (press `/` to jump to it) takes text, or `#560` to go straight to
item 560. `New post` (Overview and Items) is the same as sending the bot a
message; a post too short to research is refused with a message and what you
typed stays in the box.

**On a phone** the sidebar becomes a menu button (top left) that opens a
drawer, and search is the magnifier. While the drawer is open focus is held
inside it (Tab and Shift+Tab wrap, the page behind it cannot be reached);
`Esc`, the close button or a tap outside closes it and puts focus back on the
menu button. Tables turn into stacked cards, and on an item the decision bar is
pinned to the bottom of the screen — keyboard focus is never left hidden behind
it.

### Overview

| Card | Means |
|---|---|
| Waiting on you | items in Review (ready to review, needs your answer, confirm change), and how old the oldest is |
| Failed | items currently failed, and how many *entered* failed in the last 24h |
| In progress | items the pipeline is working on, and which is next |
| Published (24h) | published in the rolling last 24 hours; the line under it is the rolling last 7 days |
| Publishing | `Clear`, or `Held: N waiting` with the time (UTC) it will retry |

The windows are rolling (24h, 7 days), not calendar days. The charts count UTC
days over the last 14: published, failures and new items per day, each with its
total. *Status mix* is the same four lanes as the sidebar; *Why items stopped*
lists, for the last 7 days, the commonest failure reasons in plain words and
the commonest drop reasons. *Waiting longest* is the five oldest items waiting
on you; *Recent activity* is the last twelve status changes. Cards link to the
matching list.

**Held** only means "an approved item has a retry time in the future". The
database does not record why, and the dashboard shows no Instagram quota
number because none is stored. The `of 20 this hour` figure is the publish
pace cap, not a quota read from Instagram.

### Items

Lane tabs (**All**, **Review**, **Running**, **Published**, **Stopped**), each
with its count, and a **Newest / Oldest** sort. 25 rows show; **Show all**
lists the rest. On wide screens (1280px and up) the column header stays
under the top bar as you scroll; on narrower ones the table scrolls sideways
inside its frame. The row button is **Review** (or **Answer** for an item that
needs your answer) and opens the item; **Drop** is beside it. Approve and
Reject are not on the list — you decide on the item page. A failed row shows
the reason in one plain sentence.

### Reviewing and approving

1. From Overview (*Waiting longest* or the **Waiting on you** card) or **Review**
   in the sidebar, press **Review** on an item.
2. At the top of the item page the decision bar holds **Approve & publish**,
   **Ask for changes**, **Reject**, and **Drop**. It is visible without
   scrolling at 1440×900 and on a phone.
3. Read the deck (an old item whose images have been cleared says *Images no
   longer available*; click a slide for the full-size viewer: ← → move, `Esc`
   closes and puts you back on the slide) and edit the caption if needed —
   **save caption**.
4. **Approve & publish** asks first: *Approve and publish this item? It will be
   posted to Instagram.* Cancel leaves the item untouched. Confirm and you get
   a notice, *Approved - publishing.*, and the item moves to Approved.
5. **Next waiting →**, top right, opens the oldest item still waiting.

**Reject** and **Drop** also ask before they act. An item that already went out
once shows the amber already-published banner in the bar.

If you approve an item that is no longer waiting (someone else did, or it
moved on) you get a page titled **Nothing was changed** with the item's
current status and links back — nothing is changed. A missing item is a
**Not found** page, a malformed request **That request couldn't be processed**.
All three keep their HTTP status code.

**Ask for changes** jumps to the conversation card and puts the cursor in the
note box: type what should change
and **send**. The note becomes a revision (the item shows *Rewriting*) and is
applied as a targeted edit. **regenerate** below it redoes the deck from the
same research, with an optional note. A plain message on any other item is
only recorded on the thread.

### When an item failed or is stuck

A failed item opens with a red card, *Failed while <stage>*, a plain-words
reason, and **Retry: <stage>**. Retry asks for confirmation (*Work from this
stage onward is cleared and run again*) and then shows *Retrying - the pipeline
will pick it up.* **Other stages** shows the timeline. The stage where it broke
is marked *failed here* in the timeline. The exact recorded error is on the
same page under **Error (as recorded)**, folded closed — raw provider and
exception text appears there and in `/logs`, nowhere else.

An item that needs your answer shows its question first, with the answer box
already focused. Type and **send**: *Answer sent - the pipeline is resuming.*

### Run order and logs

`/queue` lists items in execution order, one row each: position, `#id`, title
(two lines at most; only the id and the title are links), status, source, age.
The buttons are labelled per item
(*Move #577 to top*, *up*, *down*); a move shows *Run order updated.*

`/logs` shows the newest 500 lines. **Search logs** takes text or an item id,
**Level** is *All levels*, *Warning and above* or *Error and above*, and
**Lines to show** is a number (`0` = all, or use **Show all**). The heading says
how many lines match, for example *Showing 20 of 226 lines (filtered from
198,861)*. **Clear** resets the filters.

### Auto-refresh and drafts

Overview, Run order and an item that is still being worked on refresh
themselves about every 20 seconds. The top bar
says **Live · updated Ns ago**. The moment any box has text in it, or a
confirm or dialog is open, it switches to **Paused while you type** and the
page does not reload, so a half-written message or caption is never wiped.
Clear the box and it resumes. Pages with nothing in flight (the item lists, an item waiting for you) do not poll.
Without JavaScript the page still works and falls back to a plain timed
refresh.

On an item page:

- **Stage timeline** — the pipeline drawn as clickable stages. Green behind
  you, blue for where you are, hollow ahead. Click any stage to requeue there
  (you are asked to confirm; the text says what is cleared). `awaiting
  approval` and `published` render but are not clickable, because requeue
  rejects them.
- **Chat** — send a message to the item; the same conversation the bot has.
- **Already-published banner** — amber, on any item that has gone out before.
  It survives a requeue, which is the point: `ig_post_id` is cleared by a
  requeue, so without this an item that is already live looks untouched and a
  second approval would post a duplicate.

## Requeue

Send an item back to an earlier stage. Everything that stage and later stages
produce is cleared, so nothing carries stale inputs forward.

```bash
docker compose exec app python scripts/requeue.py 100 --to triaged
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

## Adding a channel

1. **Join the channel** with the Telegram account the worker reads through
   (the one that logged in during setup, not the bot). Live posts arrive only
   from channels that account has joined.
2. **Find its id.** Open the channel at web.telegram.org/k: the address ends in
   `#-1526709058`; insert `100` after the minus to get `-1001526709058`. Or
   forward one of its posts to @RawDataBot and read `forward_from_chat.id`.
3. **Add it to `.env`**, comma-separated, and restart:

   ```
   CHANNEL_IDS=-1001526709058,-100XXXXXXXXXX
   ```

   ```bash
   docker compose up -d app
   ```

On start the worker backfills up to `BACKFILL_LIMIT` (default 20) recent posts
from a channel it has never seen, and each one becomes an item for your review.
Set `BACKFILL_LIMIT=3` for that restart if you do not want twenty, and take the
line out again afterwards: the same limit caps catch-up after downtime.

Check: send `/channels` to the bot. A wrong id, or a channel the account has
not joined, shows "name unavailable" there and logs `backfill failed for <id>`.

## Priority and ordering

Items run in queue order, which you can change on `/queue` (▲▼ or *top*).
Useful when something is time-sensitive and the queue is deep.

## When something fails

Start with the item, not the logs:

```bash
docker compose exec -T app sqlite3 -readonly /app/state/app.db \
  "SELECT status, attempts, last_error, question FROM items WHERE id=100;"
```

The live database is on the `pipeline-state` volume, not in `data/`. Read it
from inside the container, read-only; for anything heavier, query a copy from
`data/backups/`. Never create `data/app.db` on the host.

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

Bing, Yahoo, Mojeek, Semantic Scholar, Wikipedia, Wikidata, Hacker News,
GitHub and Stack Overflow are enabled (`searxng/settings.yml`). Brave,
DuckDuckGo, Google CSE and Startpage CAPTCHA or get suspended under automated
load, so they stay off. After editing the file: `docker compose restart searxng`.

**`openrouter returning an unusable completion`**
The hosted model returned something that did not satisfy the schema. Three
attempts, then the local Ollama model. Frequent occurrences mean the model is
a poor fit — pin a different one rather than living with the retries.

**Item stuck in `awaiting_approval` forever**
That is the design. Nothing moves it but you.

## Health check

```bash
docker compose ps                           # app and dashboard "healthy"
docker compose exec -T app python -m pipeline.lease --check
cat data/tunnel-origin.txt                  # live media host
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8770/
tail -f data/pipeline.log                   # or: docker compose logs -f app
```

Two workers is a real failure mode — they contend for the Telegram session.
The worker lease (`data/worker.lease`) stops a second one starting, and a host
process refuses to open the database while the container holds it. Never run
the old launchd worker alongside the containers.

After a code change: `docker compose build && docker compose up -d app
dashboard`. `config/` and `templates/` are mounted, so edits there need only
`docker compose restart app dashboard`.

## Limits worth remembering

| Limit | Value | Imposed by |
|---|---|---|
| slides per carousel | 10 | Instagram |
| hashtags | 5 | project convention |
| posts / 24h | 50 | Instagram (carousel) |
| Telegram message | 4096 chars | Telegram |
| Telegram caption | 1024 chars | Telegram |
