# Design — Mission control (web dashboard)  ·  rev 2 (admin-dashboard redesign)

Owner: ux-designer. Task T322. Scope: `scripts/dashboard.py` (+ two small static asset files, see "Architecture").
Single operator, loopback, no login. Not in scope: Telegram bot copy, slide renderer ("doodle" is the *deck* theme from
bddbc91; the dashboard was never doodle-styled).

User direction (their words): "make the entire UI look similar to a nice dashboard". Decisions from the user:
Telegram `/post` equivalent = "Not now" (not built); Approve placement not objected to -> approve from the item page behind
a confirm, list rows get "Review" (kept easy to reverse, see UX-3).

Evidence: audit of a private copy (`app-ux.db`, port 8771; never 8770; no mutating clicks), Playwright at 1440/390.
Screenshots (scratchpad `.../scratchpad/shots/`): `ux-index-1440.png` `ux-attn-1440.png` `ux-attn-390.png`
`ux-item567-1440.png` `ux-item567-390.png` `ux-item546-failed-1440.png` `ux-item560-needsinput-1440.png` `ux-queue-1440.png`.
Contrast figures are computed from real token values (WCAG luminance). Not seen live: published-warning and revision states
(no such rows in the copy) - specified from code. Redesigned look is a spec, not yet rendered; the tester verifies it visually.

## Findings (functional, still all valid; ranked)
H1 decision buried below source/thumbs/chat on item page; H2 no review flow through 247 waiting (landing is "all", prev/next
walks every id, approve gives no feedback); H3 approve one click from a 32px thumb row beside reject/drop, requeue dot clears
work in one click; H4 contrast failures (white-on-green 2.95, row actions at opacity .5 = 1.66, faint labels ~2.5, amber text
2.5, operator bubble 2.98); H5 failed/needs_input items have no path forward (tiny mono error, hollow timeline, no Retry);
M6 phone: page is 460px wide at 390 (header clips `find`/`logs`); M7 meta-refresh wipes drafts; M8 jargon/markdown noise
(`**`, `awaiting_approval`, `dm`, "1 entries"); M9 bare-text 409/400 pages; M10 25k-line unfiltered log; L11 a11y baseline
(skip link, h1, labels, state by colour only, <32px icon buttons). New with the redesign: **there is no overview** - the
operator cannot see health at a glance (how many waiting/failed, is publishing flowing, what failed lately).

## Architecture (decisions)
- **Keep server-side rendering, aiohttp, no framework, no CDN, no web fonts** (system stack).
- **Static assets are split out, but NOT served as routes:** `scripts/dashboard_static/dashboard.css` and
  `scripts/dashboard_static/dashboard.js` are read once at import (`Path(__file__).parent / "dashboard_static"`, so
  `importlib` loading from tests keeps working) and inlined into every page as `<style>`/`<script>`. Why: the CSS grows from 190
  to ~600 lines and an inline Python string is unmaintainable; inlining adds zero routes and no cache/offline issues. A
  missing file must fail loudly at startup. Per-package blocks in the CSS are fenced `/* ==== UX-n ==== */` so parallel
  branches merge cleanly.
- **Icons:** a small inline SVG sprite constant `ICONS` in dashboard.py (own simple 24x24 stroke paths, `<symbol id="i-...">`,
  emitted once in `page()`; used as `<svg class=ico aria-hidden=true><use href="#i-queue"/></svg>`). Needed: overview, inbox,
  play(running), check(published), alert(stopped), list(all), order, file-text(logs), search, menu, x, chevrons, plus, refresh,
  external. No icon-only controls without an `aria-label`.
- **Charts:** server-rendered inline SVG built by pure functions (`sparkline(values)`, `bars(values)`, `stacked_bar(parts)`)
  from existing DB aggregates; `role=img` + `aria-label` summarising the numbers + an adjacent visible number (charts are
  never the only carrier of a value); series colours >= 3:1 against the card (measured below).
- **New read-only routes** (GET, no params that mutate): `GET /overview` (alias of the new `/`). `GET /` renders Overview when
  the query string has none of `lane|q|all|thin`, otherwise it delegates to `index()` unchanged (so old links and tests that call
  `dash.index(FakeRequest(db, query={"lane": "attn"}))` keep working). `GET /items` is the same list handler as `index()`
  (alias, used by the new nav "All items"). No new mutating route; `POST /new`, `/item/{id}/{verb}`, `/queue/..` unchanged.
- **Overview aggregation lives in one function** `async def overview_stats(db, now) -> OverviewStats` (dataclass) so it is unit
  testable with a seeded temp DB; the handler only renders.

### `page()` new signature (explicit)
Existing callers pass `(title, body, counts, active="", query="", refresh=False)` positionally. Keep those six positional
parameters exactly, add keyword-only extras:
`page(title, body, counts, active="", query="", refresh=False, *, heading=None, subtitle=None, actions="", notice=None, extra_head="")`
- `title` -> `<title>` ("#567 - Ready to review - Mission control"); `heading` (default = title) -> the one `<h1>` in the top
  bar; `subtitle`/`actions` fill the top-bar page header; `notice` is a *code* looked up in `NOTICES`, never echoed.
- `active` values: `""` = All items (current behaviour for the list), lane keys `attn|work|good|bad`, `queue`, plus new
  `home` and `logs` (logs currently passes nothing and wrongly lights "all"). `counts` still drives sidebar badges.
- All existing callers keep working untouched; UX-1 updates them to pass `heading`/`active`.

### Test-compat contract (strings and structure tests rely on; do NOT change)
tests/test_dashboard_*.py assert on: card heading text `waiting on your answer` (CSS may uppercase it; source text stays),
`revisions · N` heading and the `...</details></div>` close of the revision card, `action='/item/{i}/revise_yes'`/`revise_no`,
`href='/item/{i}'>answer</a>` (attribute order: put `class` BEFORE `href`, nothing between `'>` and `answer`), the class name
`tl-node now`, `esc()` of `<script>` in chat/revision/notes (`&lt;script&gt;`), `rcopy.*` strings, `Item {i} has moved on`
(HTTPConflict text), `rcopy.REVISION_WAITING_SUMMARY`, and the module-level `error_text()`. `<h1>Item N</h1>` stays the item h1
text start. Tests that scrape the raw status word in badges must be updated in UX-1/UX-3 (badge keeps `data-status` and a `title`
with the raw status; visible text becomes the label). Every package lists the tests it touches.

## Visual direction
App-shell admin dashboard: calm neutral canvas, white (light) / deep-slate (dark) cards with soft elevation, indigo brand
accent for navigation and links, semantic green/amber/red/blue reserved for state. Dense but airy: 8px grid, 12-16px radii on
cards, 8px on controls, tabular numerals on every number, uppercase micro-labels only for section kickers. Motion: 120-160ms
opacity/transform on hover/press, 200ms slide for the mobile drawer; all disabled under `prefers-reduced-motion`.

### Layout (desktop >= 1024px)
```
+-----------+----------------------------------------------------------------------+
| (logo)    |  Overview                         [ search or #id        ] [* live ] |  top bar: h1, search, refresh state
| Mission   |  Last 24h · all times UTC                          [+ New post]       |
| control   +----------------------------------------------------------------------+
|           |  +-----------+ +-----------+ +-----------+ +-----------+ +----------+ |
| Overview  |  |Waiting on | |Failed     | |In progress| |Published  | |Publishing| |  KPI cards
| Review 247|  |you   247  | |  26  +5   | |   3       | |24h 9 7d 33| |Clear 3/20| |
| Running  3|  |oldest 4d  | |new in 24h | |next: #574 | |~~~spark~~~| |last 14m  | |
| Published |  +-----------+ +-----------+ +-----------+ +-----------+ +----------+ |
| Stopped 26|  +--------------------------------------+ +-------------------------+ |
| All items |  | Published per day (14d) bars + legend | | Status mix (stacked bar)| |
| Run order |  | Failures per day, Intake per day      | | Why items stopped (7d)  | |
| Logs      |  +--------------------------------------+ +-------------------------+ |
|           |  +--------------------------------------+ +-------------------------+ |
| [collapse]|  | Waiting longest (5): thumb #id title  | | Recent activity (12)     | |
| v time    |  |   age  [Review >]    [See all 247]    | | #561 triaged -> failed   | |
+-----------+  +--------------------------------------+ +-------------------------+ |
```
Sidebar 232px (collapsible to 64px icon rail via a button; state kept in `localStorage`, default expanded; rail items keep
`aria-label`+tooltip). Content max-width 1280px, 24px gutters.

Tablet 641-1023: sidebar hidden, replaced by the top bar with a menu button opening a left drawer. Phone <= 640: same top bar
(brand, search button, menu button); drawer is full-height 280px with a scrim, `Esc`/scrim/close button dismiss, focus trapped
inside while open and returned to the menu button on close; KPI cards 2 per row; tables become stacked cards; item decision
bar is fixed to the bottom. Without JS: the nav renders as a normal wrapped block above content and search is always visible
(progressive enhancement: `html.js` class is added by the script before paint, CSS collapses only under `.js`).

### Items table (`/items`, lanes)
```
 Waiting on you · 247                       [ All | Waiting | Running | Published | Stopped ]   sort: Oldest | Newest
+-----+-----+--------------------------------------------+------------------+-------+--------+-----------+
|     | #   | Title (2-line clamp)                       | Status           | Slides| Age    |           |   sticky thead, 40px
+-----+-----+--------------------------------------------+------------------+-------+--------+-----------+
| img | 567 | OpenAI says it will pay the full power bill| (o) Ready to rev.|   7   | 10h    | [Review >]|   row 64px, hover tint
| img | 560 | Fresh robots just walked themselves out... | (o) Needs answer |   -   | 33h    | [Answer >]|
| .. failed rows: error one-liner (plain reason) under title, red pill, [Retry...] link to item
+-----+-----+--------------------------------------------+------------------+-------+--------+-----------+
 Showing 25 of 247  [Show all]                                     (empty: illustration-free text + CTA)
```
Real `<table>` with `<th scope=col>`, sortable via links `?sort=age|new` (GET, server-side `ORDER BY`), status as a pill (dot +
label), the lane filter as a segmented control of links (each with count), `aria-current` on the active one. <=640px: each row
becomes a card (grid), th visually hidden but kept for AT.

### Item page (awaiting approval, >=1024)
```
< Waiting list                                              #566 <   > #568      Next waiting >
(h1) Item 567 · [Ready to review] [Channel post] [news] [10h ago]
+--[sticky decision bar]-----------------------------------------------------------------------+
| ! already published once (if any)   [ Approve & publish ] [ Ask for changes ] [ Reject ]  Drop v|
+----------------------------------------------------------------------------------------------+
+ Deck · 7 slides ---------------------------------+ Caption ------------------------+
|  [ current slide, ~480px wide, 4:5 ]  < 3 / 7 >    | textarea (editable)   [Save]    |
|  [ 1 ][ 2 ][ 3 ][ 4 ][ 5 ][ 6 ][ 7 ]  thumb strip | Source (3 lines) [Show all]     |
+----------------------------------------------------+ Details: theme · intent · IDs   |
+ Request changes: [textarea] [Send]   Regenerate: [note] [Regenerate]                  |
+ Conversation (bubbles)   + Revisions · N (details)  + Stage history   + Technical details +
```
Gallery = card grid of slide thumbs (auto-fill minmax(120px)) + large viewer; click opens `<dialog>` full-size.
Failed item: top card "Failed at <stage>" (flow C). needs_input: question card first, answer box autofocused.

## Design system

### Tokens (CSS custom properties; the existing names `--bg --raise --sink --line --ink --dim --faint --attn --work --good
--bad` are KEPT and re-valued so nothing hardcodes a second convention; new tokens added)
Spacing `--s1:4 --s2:8 --s3:12 --s4:16 --s5:24 --s6:32 --s7:48` (px). Radius `--r-s:8px` controls, `--r:12px` cards,
`--r-l:16px` dialogs, `99px` pills. Elevation: `--sh1:0 1px 2px rgb(16 24 40/.06)` (cards), `--sh2:0 8px 24px rgb(16 24 40/.12)`
(drawer, dialog, sticky bar); in dark mode shadows are replaced by 1px `--line` borders plus a faint top highlight.
Type (system stack `--sans`, `--mono`): `--fs-xs:12px` (floor for any text), `--fs-sm:13px`, `--fs-md:14px` (tables, controls),
`--fs-base:15px/1.55` (body), `--fs-lg:17px` card titles, `--fs-xl:22px` page h1, `--fs-kpi:30px/1.1` KPI numbers (weight
650, `font-variant-numeric:tabular-nums`). Letter-spacing -.01em on >=17px; .08em on uppercase kickers (12px, 600).
Layout: sidebar 232/64px, top bar 56px, table row 64px (thead 40px), control height 36px (44px at <=640px), icon 18px.
Z-index: bar 20, drawer 40, dialog 50, toast 60.

Colour roles - every text pair >= 4.5:1, UI boundaries >= 3:1 (computed).
| Token | Light | Dark | Notes (measured) |
|---|---|---|---|
| `--bg` canvas | #f4f6f8 | #0d1014 | |
| `--raise` card | #ffffff | #141920 | |
| `--sink` input/inset | #eceff3 | #0a0d11 | |
| `--side` sidebar | #ffffff | #0a0d11 | |
| `--line` hairline (decorative) | #dfe4ea | #1f2630 | decoration only, never a control edge |
| `--edge` control border | #78828e | #6b7788 | 3.6 on bg / 3.9 on card (light); 4.2 on bg (dark) |
| `--ink` | #11151b | #e8ecf2 | >=15:1 |
| `--dim` | #4b5563 | #9aa5b5 | 6.55 on sink / 7.08 on card (dark); sidebar 7.56 / 7.81 |
| `--faint` (text) | #5f6875 | #8793a3 | 5.64 white, 5.21 bg, 4.89 sink (light); 5.66-6.24 (dark). Old #6b7480 failed on bg/sink (4.37/4.11): do not use |
| `--brand` | #4338ca | #a5b4fc | links, active nav; 7.9 on white, 8.85 on dark card; white-on-brand 7.9 |
| `--brand-tint` | brand @9% on card | brand @14% on card | active nav bg; text on it 6.82 / 6.72 |
| `--good` (fill) | #1f7a4d | #5cc08e | primary button: white text 5.32 (light); dark text #0a0d11 on it 8.7 (dark) |
| `--bad` text | #b3342b | #ef8a82 | 6.1 / 7.25; **hover fill of danger button uses `--bad` with `--on-bad`: white in light (6.1), #0a0d11 in dark (8.0)**, never white on the dark red |
| `--attn` text | #9a5b00 | #f0a84a | 5.43 / 8.75; `--attn-dot` #e0932f for dots/borders only |
| `--work` | #2a6db0 | #7db4ec | 5.37 / 8.08; operator bubble bg white text 5.37 (light); dark: bg #2a6db0 white text 5.37 |
| `--focus` | #4338ca | #a5b4fc | 2px ring + 2px offset, >=3:1 against every surface |
Status pill = tinted bg (12-16% of its colour on the card) + darker text: good #166534 6.06, bad #9f2a22 6.18, attn #7a4500
6.85, work #1e4f8a 7.0, neutral #4b5563 6.51 (light); dark pills #7fd6a6 7.55, #f4a09a 6.67, #f6bd72 7.71, #9cc6f2 7.31, #aab4c2
6.64. Every pill also has a dot + text label. Chart series: published `--good`, failed `--bad`, intake `--work` - each >= 5:1 light
and >= 7:1 dark against the card; series are also distinguished by label/pattern in the legend (never colour alone).
Light/dark follow `prefers-color-scheme`; no toggle (an optional manual toggle can come later via `data-theme`, the existing
`:root[data-theme]` hook is preserved).

### Components (each: default / hover / focus-visible / active / disabled / busy / error where relevant)
- **App shell**: `<aside class=side>` (brand, `<nav aria-label="Main">` links with icon + label + count badge, collapse button,
  footer: "All times UTC · read-only data"), `<div class=top>` (menu button <=1023, `<h1>`, subtitle, search form, live/paused
  indicator, page `actions` slot), `<main id=main tabindex=-1>`. Active link: brand-tint bg, brand text, `aria-current=page`,
  3px left rail.
- **Card** `.card` (radius 12, `--sh1`, 1px `--line`, padding 16/20) with optional header row: `h2` (17px) + right-aligned action
  link. One card component for every panel; the old `.card>h2` caps kicker becomes `.kicker`.
- **KPI card** `.kpi`: label (13px dim) with icon, value (30px), delta/sub line (13px, text e.g. "+5 in 24h", never colour-only),
  optional sparkline right-aligned (SVG 96x28, `aria-hidden`; the number carries the meaning), whole card is a link to the
  filtered list when it has one (focus ring, hover lift 1px + `--sh2`).
- **Button** `.btn` primary(green)/secondary(outline `--edge`)/danger(outline red, fills on hover)/quiet(text), sizes 36/44.
  `[aria-disabled=true]`: 55% opacity + dashed border (still >= 3:1 boundary) - but disabled is never used for a main action
  that is merely unavailable; show a reason text instead. Busy: label -> "Working...", `pointer-events:none`.
- **Pill** `.pill[data-status]` (dot + label; `title` = raw status).
- **Table** `.tbl`: sticky thead (`position:sticky; top:var(--top-h)`), zebra none, row hover `--sink`, 1px bottom lines, `th scope=col`,
  numeric columns right-aligned tabular. Focus-within row outline for keyboard.
- **Segmented control** `.seg` (lane filter / sort): links with `aria-current`.
- **Empty state** `.empty`: icon (32px, dim), one-sentence title, one-sentence hint, primary CTA. Every list/table/chart card has one.
- **Notice/toast** `role=status` (success) / `role=alert` (error), top of content, auto-dismiss 6s only for success (JS), always
  dismissible, from `NOTICES` code map.
- **Slide gallery + viewer**, **decision bar**, **timeline** (done/now/todo/failed-here/off with icon + text + `aria-label`),
  **form field** (visible label, hint, error with `aria-describedby`, value preserved), **chat bubble**, **error page**,
  **disclosure** (`<details>` styled; chevron rotates): as in rev 1 spec, restyled with the tokens above.
- **Skeleton/loading**: pages are server-rendered, so no spinners; busy buttons and the "Rewriting..." banner cover waits. Charts
  with < 2 data points show "Not enough data yet" instead of an empty plot.

### Microcopy rules
Plain words, verb-first, sentence case. Status label map (single dict `STATUS_LABEL`; raw status stays in `data-status`/`title`):
awaiting_approval "Ready to review", needs_input "Needs your answer", revision_confirm "Confirm change", revision_requested
"Rewriting", ingested/extracted/triaged/researched/synthesized/composed/rendered "Working: <stage>", approved "Approved",
publishing "Publishing", published "Published", failed "Failed", rejected "Rejected", dropped "Dropped". Sources: dm "DM",
channel "Channel post". Nav labels: Overview, Review (= waiting on you), Running, Published, Stopped, All items, Run order, Logs.
Counts pluralised ("1 item", "247 items"). Publishing button reads "Approve & publish" and its confirm states what goes out and
whether it is a repeat. Errors: what happened, what was NOT changed, what to do next. Raw `last_error`/logs appear verbatim only
under "Technical details". Times relative with absolute UTC in `title`; the page states "all times UTC" once in the shell.

### Refresh policy (shell-level)
`<meta refresh>` removed. `dashboard.js` polls: every `REFRESH_S` (20s) it reloads only when the tab is visible AND no
`input/textarea` has a value or focus AND no `<dialog>`/drawer is open; the top-bar indicator shows "Live · updated 12s ago"
(live) or "Paused while you type" (paused). `<noscript>` meta refresh keeps old behaviour. Pages that have nothing in flight
don't poll. Overview polls (it has running items/KPIs).

## Flows (unchanged logic from rev 1, restated briefly, states included)
A **Review the pile**: Overview KPI/"Waiting longest" or sidebar Review -> table -> Review -> decision bar (approve with confirm / ask
for changes / reject / drop menu) -> toast "Approved #N - publishing" -> "Next waiting". Empty: "You're all caught up." 409:
styled "Nothing was changed - #N already moved on (now: X)" with Back / Next waiting.
B **Answer a stuck item** (needs_input): question card top, answer autofocus, toast "Sent - resuming at <stage>". (`/post`-style
"send what I have" is explicitly *not built* - user said "Not now".)
C **Understand and retry a failure**: "Failed at <stage>" card: plain reason (`explain_error`), primary "Retry from <stage>" (existing
requeue), "Other stages", "Technical details"; timeline shows "failed here".
D **New post**: compact form on Overview and Items ("+ New post" in top bar focuses it / opens it); Cmd/Ctrl+Enter; `TooThin`
shown inline with the text preserved.
E **Run order**: table with position, big move buttons, backing-off chip with the retry time. Empty: "Nothing queued - the pipeline
is idle." F **Logs**: filter by text/item/level, newest first, 500 lines default, "show all".
G **Overview**: read-only health snapshot; every KPI/list links to the filtered view; error state (a query raising) renders the
card as "Couldn't load this - reload", never a 500 page (each card's data fetched in try/except around the *read*, logged).

## KPI -> data source (read `src/pipeline/db.py`; all read-only, `db.conn.execute_fetchall`, timestamps are UTC ISO strings so
lexical `>=` comparison is correct - same as `published_since`)
| Card / visual | Value | Exact source |
|---|---|---|
| Waiting on you | count | `status_counts()` summed over LANES `attn` statuses (awaiting_approval, needs_input, revision_confirm) |
|  | oldest age | `SELECT MIN(status_updated_at) FROM items WHERE status IN (<attn statuses>)` -> `ago()` |
| Failed | current | `status_counts()["failed"]` |
|  | new in 24h | `SELECT COUNT(*) FROM events WHERE to_status='failed' AND at>=?` (`events(at,to_status)`; ~4.6k rows, fine unindexed) |
| In progress | count | `status_counts()` summed over `RUNNING` |
|  | next up | first of `db.queue_order(sorted(RUNNING))` (already used by `/queue`) |
| Published 24h / 7d | counts | `db.published_since(24)` / `db.published_since(168)` (`items.published_at`) |
|  | sparkline / bars (14d) | `SELECT substr(published_at,1,10) d, COUNT(*) FROM items WHERE published_at>=? GROUP BY d`; zero-fill the 14 UTC days in Python |
| Failures per day (14d) | bars | `SELECT substr(at,1,10) d, COUNT(*) FROM events WHERE to_status='failed' AND at>=? GROUP BY d` |
| Intake per day (14d) | bars | `SELECT substr(created_at,1,10) d, COUNT(*) FROM items WHERE created_at>=? GROUP BY d` |
| Publishing health | last published | `db.last_published_at()` -> `ago()` |
|  | last-hour pace | `db.published_since(1)` vs cap `3600 // IG_MIN_PUBLISH_GAP_S` (module constant in `pipeline.config`, = 20 per hour; import it, do not hardcode) |
|  | held state | `SELECT COUNT(*), MIN(next_attempt_at) FROM items WHERE status IN ('approved','publishing') AND next_attempt_at>?now` -> "Clear" if 0 else "Held: N waiting, retries at <time>"; first row's `last_error` truncated to 120 chars in `title`/details (escaped). Label is "Held", not "throttled": the DB cannot distinguish a throttle deferral from other retries; do not claim an IG quota number - there is none stored |
| Status mix | stacked bar | `status_counts()` grouped by lane (the same `LANES`) with legend text + counts |
| Why items stopped (7d) | top 5 reasons | `SELECT detail, COUNT(*) FROM events WHERE to_status='failed' AND at>=? GROUP BY detail ORDER BY 2 DESC LIMIT 5`, first line truncated to 90 chars, run through `explain_error` (UX-5; before that, raw escaped). Dropped: `db.dropped_reasons_since(168)` (existing, `items.triage_reason`) as a second list |
| Waiting longest | 5 rows | `SELECT id, raw_text, status, status_updated_at, rendered_paths FROM items WHERE status IN (<attn>) ORDER BY status_updated_at ASC LIMIT 5` |
| Recent activity | 12 rows | `SELECT item_id, from_status, to_status, at, detail FROM events ORDER BY id DESC LIMIT 12` |
Not available without new backend (so NOT shown): IG API quota remaining, time-of-day engagement, per-model cost. No new tables/columns.
"Today" is avoided in labels: windows are rolling ("last 24h", "last 7 days"), dates in charts are UTC days, stated in the card footer.

## Heuristic self-check (Nielsen)
1 status visibility: live/paused indicator, toasts, KPI health, busy buttons, "Rewriting" banner. 2 real-world language: label map,
plain reasons, "Held" not internals. 3 control: confirms, Esc/scrim closes, drafts never wiped, easy-to-reverse list Approve
(UX-3 note). 4 consistency: one card/pill/button/table/empty component, one token set. 5 error prevention: Approve apart from
Reject/Drop, confirm lists what is published/cleared, double-submit guard, already-published banner. 6 recognition: nav always
visible with counts, stage names/slide numbers shown. 7 efficiency: Overview -> Review -> Next waiting; `/`-to-search shortcut
(`/` focuses search when not typing), Cmd/Ctrl+Enter submit; `#id` jump; sort/filter links. 8 minimalism: details folded, one
primary action per row, charts limited to 3. 9 recovery: styled error pages, Retry-from-stage, raw detail one click away.
10 help: hints under forms, shell footer, docs path.

## Accessibility acceptance criteria (WCAG 2.2 AA; tester verifies at 1440 / 768 / 390, light and dark via emulated colour scheme;
devs can self-check the structural ones by reading the rendered HTML string and the CSS, no browser needed)
Perceivable (P)
- P1 Every text/background pair in the token table meets 4.5:1 and control edges/focus ring 3:1 (tester: axe contrast + spot checks:
  primary and danger buttons incl. dark hover, pills, KPI sub-lines, sidebar, table headers, operator bubble, chart legends).
- P2 No `opacity` < 1 used to de-emphasise text or primary actions (grep CSS: only on disabled controls and decorative elements).
- P3 State is never colour-only: pills/timeline/charts/age-stale all carry text or icon; each chart has `role=img` with an
  `aria-label` listing its numbers, plus a visible number elsewhere on the card.
- P4 Slide images `alt="slide N of M"`; decorative thumbs `alt=""`; icons `aria-hidden=true`; viewer image alt updates with slide.
- P5 Reflow: no horizontal page scroll at 320-1440px and at 200% zoom (tables become cards <=640px; the timeline and `<pre>`
  scroll inside their own container).
Operable (O)
- O1 All flows keyboard-only; visible `:focus-visible` ring on every link/button/input/summary/KPI card/table row link.
- O2 First Tab stop = "Skip to content" (visible on focus) -> `main#main`; focus order = visual order; drawer and `<dialog>` trap
  focus while open, `Esc` closes and returns focus to the trigger; no trap otherwise.
- O3 Sticky top bar, thead and decision bar never obscure the focused element (`scroll-margin-top`/`scroll-padding-top`, 2.4.11).
- O4 Targets >= 36px desktop, >= 44px at <=640px (AA floor 24px), including run-order buttons and the sidebar collapse button.
- O5 Approve, Drop, Requeue need an explicit confirm; Enter in a text field never triggers them; double submit blocked.
- O6 Auto-refresh never fires while a field has text/focus or an overlay is open; a visible "Paused while you type" state; noscript
  fallback. No motion when `prefers-reduced-motion`; no auto-playing animation > 5s.
Understandable (U)
- U1 Every field has a visible `<label>`; errors are text next to the field linked with `aria-describedby`; typed text survives.
- U2 Every 4xx the dashboard raises renders the styled error page (404 `/item/999999`, 409 approve on non-awaiting, 400 empty caption)
  with the original status code, what was not changed, and a link out.
- U3 Notices use `role=status`/`role=alert`; `?notice=` renders only mapped strings (`?notice=<b>x` renders nothing).
- U4 `<html lang=en>`, unique `<title>` per page, one `<h1>` per page (top bar), headings in order, consistent nav order on every page.
Robust (R)
- R1 Landmarks: `aside`+`nav aria-label=Main`, `header`/top bar, `main#main`; `aria-current=page` on the active nav link/segment;
  `aria-expanded` on drawer/search toggles; `<th scope=col>`; tables have a caption (visually hidden) or `aria-label`.
- R2 Valid HTML: unique ids, no nested forms (`form{display:contents}` wrappers preserved), icons via `<use>` resolve (sprite emitted once).
- R3 axe: 0 serious/critical violations on `/`, `/items`, `/queue`, `/logs`, item pages in 3 statuses, both themes.
- R4 No external requests (network log shows only 127.0.0.1); no new JS library; inline scripts <= ~150 lines total, vanilla.
Functional AC (all packages): no new mutating route; every POST keeps its guard (status check, confirm token, `HTTPConflict` when stale);
actions still go through `pipeline` functions; `esc()` on every interpolated value; the test-compat contract above holds; existing
tests green except the listed updates; new pure helpers (`overview_stats`, `sparkline`, `stacked_bar`, `plain_title`,
`explain_error`) have unit tests including empty-DB and hostile-text cases; ruff clean.

## Build packages
Dev environment note: devs have no browser (Python Playwright is not in the venv). Dev self-check = unit tests on rendered HTML
strings/pure functions + reading CSS; every visual/390px/contrast/keyboard AC is **verified by the tester with Playwright MCP**.
Order: UX-1 first (everything depends on it). Then UX-2, UX-3, UX-4, UX-7 can run in parallel (they touch different functions;
each adds CSS only inside its own fenced block). UX-5 after UX-4; UX-6 after UX-5 (same `detail()`/`action()`/`timeline_control()`).
All are size M unless stated; UX-1 is the largest.

**UX-1 - Foundation: tokens, static assets, app shell, components** (M+)  after: none
- Goal: the design system and shell every page uses; fixes H4/M6/M7/L11 globally.
- Touches: new `scripts/dashboard_static/dashboard.css` + `dashboard.js` (tokens, shell, all shared components, per-package fenced blocks,
  drawer/search toggle/sidebar collapse, refresh policy, busy/double-submit, confirm-on-submit helper reading `data-confirm`,
  `/` focuses search); `dashboard.py`: delete the inline `CSS`, add asset loader, `ICONS` sprite, `STATUS_LABEL`/`SOURCE_LABEL`/`NOTICES`
  constants, `plain_title()`, `page()` (new keyword-only signature above; shell markup; skip link; notice slot), `status_badge()` (pill),
  `thumb()`, `action()` (accepts `confirm=` -> `data-confirm`, `busy`), `ago()` unchanged; update the four existing `page(...)` call sites
  (index/queue/detail/logs) to pass `active`/`heading` (logs gets `active="logs"`).
- AC: P1, P2, P5 (shell), O1, O2 (skip link + drawer), O4, O6 (refresh), U4, R1, R2 (shell); sidebar nav present on every page with counts and
  `aria-current`; no `<meta refresh>` when JS runs (noscript fallback present); phone header has no overflow (reason: nav in drawer, search toggle);
  asset files missing -> startup error; light+dark tokens match the table; pills show labels with `data-status`.
- Tests to update: any asserting raw status text in HTML, and the `meta refresh` presence if tested; contract strings must still hold.

**UX-2 - Overview page** (M)  after: UX-1
- Goal: at-a-glance health (new capability).
- Touches: new `overview_stats()`, `sparkline()`, `bars()`, `stacked_bar()` (pure), `home()` handler for `GET /` (delegates to `index()` when
  `lane|q|all|thin` present) and `GET /overview`; `build()` routes; Overview cards per the KPI table; `/new` form on Overview.
- AC: every KPI traces to the KPI->data table (reviewer checks queries); empty DB renders all cards with zeros/empty states and no 500;
  hostile text in `raw_text`/`detail` is escaped; P3/P4 charts have `role=img` + `aria-label`; KPI cards link to their filtered lists; one card
  failing its read renders the "couldn't load" card; old `/?lane=` and `dash.index(...)` behaviour unchanged.

**UX-3 - Items table (board)** (M)  after: UX-1
- Goal: F2 (list side), H3, M8.
- Touches: `index()` only (+ `GET /items` alias route): table markup, segmented lane filter, `?sort=age|new`, pluralised crumb, empty states, "Review"
  /"Answer" primary action (`class` before `href`, text exactly `answer` for revision_confirm), Drop as quiet secondary, **Approve/Reject removed from
  rows** (decision on the item page), failed rows show a one-line plain reason, new-post form preserving text on `TooThin`.
- AC: O4, R1 (table semantics), U1; no approve/reject POST forms in the list; `href='/item/{i}'>answer</a>` still present; search/`#id` jump
  unchanged; stacked-card layout <=640px (tester, visual).
- Reversal path (easy to undo): row actions are produced by one function `row_actions(item, *, allow_approve=False)`; flipping the default
  to True restores Approve/Reject in rows (it then also gets UX-6's confirm). Tests that scraped list approve forms updated, flagged in PR.

**UX-4 - Item page layout, decision bar, slide gallery** (M)  after: UX-1
- Goal: H1.
- Touches: `detail()` (page header, card order, two-column layout, sticky decision bar, "Next waiting"/"waiting list" crumb using the same
  prev/next SQL pattern restricted to attn statuses, source clamp + Show all, folded Technical details with human titles "Research (3 sources)"),
  new `slide_gallery(item)` (grid + `<dialog>` viewer, no-JS fallback = plain links as today), `regen_form()`/`caption_form()`/`chat_html()` markup
  only (copy and form actions unchanged), `published_warning()` into the bar.
- AC: Approve/Ask for changes/Reject visible without scrolling at 1440x900 and 390x844 (tester); viewer slides >= 400px wide, arrows/Esc, focus
  return (O2); O3; P4; contract strings (`waiting on your answer`, `revisions · N`, `tl-node now`, `Item {id}`) intact; non-awaiting statuses
  render without the bar and without errors.

**UX-5 - Failure and stuck-item recovery** (M)  after: UX-4
- Goal: H5.
- Touches: `detail()` header card, `error_text()` (keep signature/behaviour; tests call it) + new `explain_error()` (ordered (regex, sentence) constants;
  unknown -> generic sentence, raw text always in Technical details), `timeline_control()` (failed-here/off states from `db.events_for`, labels,
  `aria-label`; keep `tl-node` classes), new `retry_card()` posting to existing `/item/{id}/requeue`, needs_input question card promoted + autofocus.
  Also used by Overview "Why items stopped" (UX-2 falls back to raw until this lands).
- AC: failed item shows stage + plain reason + Retry + verbatim `last_error` under Technical details; needs_input shows the question first;
  `revision_requested` keeps `rcopy.REVISION_WAITING_SUMMARY`; empty/None `last_error` handled; failed-here marker has text (P3).

**UX-6 - Action safety, feedback, error pages** (M)  after: UX-5
- Goal: H2 feedback, H3 confirms, M9.
- Touches: `action()`/`timeline_control()` confirm texts, `act()`/`say()`/`move()` final redirects (append whitelisted `notice=<code>` to the Referer path,
  fall back to Referer/`/`), `build()` middleware rendering the styled error page for `HTTPClientError` (status codes and `text` preserved; 5xx untouched), notice slot use.
- AC: O5, U2, U3; approve toast "Approved #N - publishing"; every verb shows a notice; arbitrary `?notice=` renders nothing; HTTP codes asserted by existing
  tests unchanged (409/400/404, `Item {i} has moved on`); no new mutating route; security note in PR (reviewer security pass: user input/redirects).

**UX-7 - Run order and logs** (S)  after: UX-1 (parallel with UX-2/3/4)
- Goal: M10, L11 (queue), F12.
- Touches: `queue()` (table, >=36/44px move buttons, `aria-label`s "Move #574 down", backing-off chip with `next_attempt_at`, empty state), `logs()`
  (GET `q`, `level`, `n` default 500, count line "Showing 500 of N lines", level prefix styling), shared line-filter helper reused by `item_log()`.
- AC: O4; labelled filter input; filter returns only matching lines, newest first, capped at 500 by default; `move()` untouched; empty states.

## Docs to change
`docs/usage.md` "## The dashboard": new route table (`/` Overview, `/overview`, `/items`, `/queue`, `/item/<id>`, `/logs`), the app shell and nav labels
(the old "needs attention · working · good · bad" wording becomes Review/Running/Published/Stopped), what each Overview card means and its source
window (rolling 24h/7d, UTC days; "Publishing health" is "Held", not an IG quota), the decision bar, gallery, Retry card, confirm prompts and toasts,
Approve now lives on the item page, log filters, the refresh pause ("Paused while you type"), and the two static files in `scripts/dashboard_static/`.
Architect/PM: note the new assets dir and the `/` behaviour change in project records.

## Decisions and open items
1. `/` becomes the Overview; the list is `/items` and `/?lane=...` still works (delegation, no redirect, so tests/bookmarks are safe).
2. CSS/JS split into two static files inlined at import (no static route). Reversible: concatenate back into constants.
3. Publishing "throttle state" is shown as "Held" derived from `next_attempt_at`; no IG quota number exists in the data and none is invented.
4. Approve moved to the item page behind a confirm (user did not object); reversal flag in UX-3.
5. `/post` equivalent: not built ("Not now").
6. Attn order stays newest-first on the list with an explicit Oldest sort control; Overview shows "Waiting longest".
7. Dark theme and the new shell/charts are specified, not yet rendered; the tester verifies visually (and may adjust tints within the contrast floor).
