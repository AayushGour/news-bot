# Revision copy — Telegram wording for agentic regeneration (T303)

Owner: ux-designer. Status: spec, not built. Inputs: `docs/design/agentic-regen.md` §9, D248-D269, U3-U6,
`src/pipeline/revision/model.py` (OutcomeCode, Reason, ACTION_SPECS), `src/pipeline/approval/bot.py`,
`src/pipeline/limits.py`.

Reader: one operator, on a phone, glancing. So: one fact per line, verbs first, no jargon ("ask", "grounded",
"op", "outcome code" never appear), no exception text (rule 15). Honest over reassuring: a line never says
more than we know.

## 0. Findings the build must act on (read first)

1. **REGEN_PROMPT cannot stay a toast.** `handle_callback` sends the prompt through `_say(answer, ...)` =
   `callback.answer(text)`. Telegram caps an `answerCallbackQuery` text at **200 characters**. Today's prompt
   is about 170 chars, so it fits. The new prompt (about 440 chars) would be rejected by Telegram. It must be a
   normal chat message (`callback.message.answer(...)` or `bot.send_message`), and the toast becomes a short
   pointer (`REGEN_TOAST`, section 1). `handle_callback` needs a send function (or the bot) for this.
2. **Planner `global: true` is also a whole-deck rebuild** but U6/D269 only confirms the `full_recompose`
   action. Recommendation: fallback-to-compose caused by a planner `global` verdict should also ask the
   Yes/No (same prompt as section 3, recompose variant), because a rebuild with no undo (U4) is exactly what
   the confirm protects. `/skip` is an explicit command and needs no confirm. Planner-failure fallbacks
   (invalid twice, flag off, no slides) need none: the operator asked for a change, not a rebuild. Architect to
   decide; copy for both is in section 3.
3. **The deferral message needs a one-shot flag** (`notified`) in `revision_request`, or the worker retry loop
   would send it every attempt.
4. **Ask-operator needs a cause.** Add a pure helper (suggested `ask_cause(plan_result, note) -> AskCause`,
   enum `VAGUE | NEGATED | WHICH_SLIDE | EXPLICIT_NEEDED | WHICH_THEME`) derived from the drop reasons, so the
   message picks the right first line (section 4). No model call.
5. All copy below lives in one pure module (suggested `revision/copy.py`: constants + `render_*` functions, no
   I/O), unit-tested for length caps and for "every OutcomeCode and every Reason has a mapping" (a missing key
   fails the test, like `ACTION_SPECS`). No wording is typed twice in the bot, dashboard or worker.

## 1. Conventions

- **Plain text, no `parse_mode`** (matches every current bot message). Nothing needs escaping; model- and
  web-derived text is shown as inert text.
- **Emoji.** Existing bot copy uses emoji only on buttons (`✅ ✏️ ❌ 🔄`) and prose is plain with em dashes.
  Keep that. Message prose has no emoji. The Changes block uses three plain glyphs, never colour alone:
  `✓` done, `✗` not done, `?` could not verify. Every `✗` and `?` line also says so in words ("couldn't",
  "not", "kept the old one"), so meaning survives a screen reader or a font without the glyph.
  New buttons: `✅` for yes. The "no" button uses `↩️`, deliberately not `❌`, because `❌ Reject` already means
  "discard this item" on the preview keyboard.
- **Slide naming.** "Slide N" plus, where a number could mislead, the headline in straight quotes.
  Numbers in the Changes block are the **new deck's** numbers (what the album shows). A removed slide is
  "Slide 4 (was)". When the run added, removed or reordered, add the line `Slide numbers below are the new
  ones.` and show the headline on `✗` lines.
- **Quoted text** (operator note, ask text, query, headline) is: whitespace collapsed, control characters
  stripped, cut at the cap with `…`, wrapped in straight double quotes.
- **Caps** (editorial constants, house rule 2, in the copy module):

| Constant | Value | Applies to |
|---|---|---|
| `TELEGRAM_TEXT_LIMIT` / `TELEGRAM_CAPTION_LIMIT` | 4096 / 1024 | imported from `limits.py`, never retyped |
| `TOAST_MAX` | 200 | `answerCallbackQuery` text (platform limit; add to `limits.py` as `TELEGRAM_CALLBACK_ANSWER_LIMIT`) |
| `CHANGES_MAX_CHARS` | 1200 | whole Changes block |
| `CHANGE_LINE_MAX` | 100 | one line, truncated with `…` |
| `NOTE_QUOTE_MAX` | 80 | operator note in the header |
| `HEADLINE_QUOTE_MAX` | 24 | headline in a slide label |
| `QUERY_QUOTE_MAX` | 40 | search query in a line |
| `ASK_QUOTE_MAX` | 60 | unsatisfied "ask" text |
| `MAX_NOT_DONE_LINES` | 6 | `✗`/`?` lines shown, then `+N more not done` |
| `SLIDE_LIST_MAX_LINES` | 10 | slide list in the ask-operator message (MAX_SLIDES) |

## 2. REGEN_PROMPT and ack

### 2.1 REGEN_PROMPT (chat message, not a toast)

Teaches by example. Names a slide by number or by what it shows. The remove / swap / theme line is written
with the exact explicit phrasings the rules parser needs (U6), so an operator who copies it gets a hit.

```
REGEN_PROMPT  (format: id, themes)
Revising item {id}. Tell me what to change. Examples:
• "slide 3 shorter"
• "the chart slide is too wordy"
• "swap the photo on slide 2 for a clearer shot of the chip"
• "is the 40% on slide 5 right?"
• "make the caption more casual"
• "remove slide 4" · "swap slides 2 and 5" · "use the {theme} theme"
Only what you name changes. Removing slides or starting over asks you to confirm first.
/skip = fresh full version (every slide rewritten).
```
- `{theme}` is the first of `render.available_themes()`. The full list goes on its own line only if it fits:
  `Themes: signal, riso, doodle` (omit the line if `available_themes()` is empty).
- "Starting over" is the whole-deck phrasing ("start over", "from scratch"); `/skip` is the no-note path (U5).
- Length: about 440 characters, 9 lines, one phone screen. Cap 1000 (test).
- `DISPLACED` suffix stays as-is, appended after the block (it is part of the same message now, so no 200 limit).

```
REGEN_TOAST  (the callback toast, <= TOAST_MAX)
Reply with what to change, or /skip.
```
If `displaced`: the toast stays the same; the displaced note goes in the chat message.

`CAPTION_PROMPT` is unchanged. The empty-reply and "looks like a new request" messages are unchanged.

### 2.2 Ack (sent immediately after the note, before any planning)

```
ACK_NOTE:   Got it — revising item {id}. New preview shortly.
ACK_SKIP:   Making a fresh version of item {id} — new preview shortly.
```
Replaces `Regenerating item {id} — new preview shortly.` Same length class as today. It promises nothing about
what will change (the plan may ask a question or a confirm first). If a confirm or question follows it is a
separate message; the ack is never edited.

## 3. Confirmation (mandatory for every action `needs_confirmation()` returns)

Sent after the plan is made and before anything runs, as a new message with an inline keyboard. The item
waits (nothing is changed). Everything else in the plan is held, not run, until the answer, so "the rest
proceeds" is a clean statement.

### 3.1 Prompt

Remove variant (one or more `remove_slide`):

```
CONFIRM_REMOVE
Remove {what}? There is no undo — the old version isn't kept.
{slide list, one per line: "Slide 4 "Why it matters""}
{if other actions: "Also doing: {short list}."}
```
Recompose variant (`full_recompose`, or a planner `global` verdict if the architect adopts finding 2):

```
CONFIRM_REBUILD
Rebuild the whole deck from scratch? Every slide gets new text. There is no undo.
{if note: "Your note: "{note, 80}""}
```
- `{what}` = `slide 4` / `slides 2, 3` / `3 slides` (more than 3 slides: count, list stays).
- "Also doing:" lists the held non-destructive actions by their short labels from the table in section 5
  ("slide 2 reworded, caption rewritten"), max 3, then `+N more`. Recompose is always alone (validator), so it
  never has this line.
- Rules can fire on a question or a conditional ("should I remove slide 3?"), so the prompt states the action
  as a question and the safe button is equally prominent. Nothing is pre-selected.

### 3.2 Buttons (one row, two buttons, equal weight)

| Variant | Yes | No |
|---|---|---|
| remove, 1 slide | `✅ Yes, remove` | `↩️ No, keep it` |
| remove, N slides | `✅ Yes, remove {N}` | `↩️ No, keep them` |
| rebuild | `✅ Yes, rebuild` | `↩️ No, keep deck` |

Each label is at most 20 characters so two fit one row on a narrow phone. Callback data carries the item id
and a short token of the request (suggested `rvc:y:{id}:{tok}` / `rvc:n:{id}:{tok}`, under the 64-byte limit).
`parse_callback` must accept the new prefix. The token is what makes a stale button detectable.

### 3.3 What No does

"No" drops exactly the destructive actions. The rest of the plan runs. If nothing is left the item returns to
the awaiting-approval state with its old preview valid.

The confirm message is **edited** (buttons removed, so a second tap is impossible) to the result line:

| Answer | Edited message text | Next |
|---|---|---|
| Yes (remove) | `Removing {what}…` | run continues, new preview |
| Yes (rebuild) | `Rebuilding the deck…` | run continues, new preview |
| No, other actions remain | `Kept {what}. Applying the rest…` | run the rest, new preview whose Changes block has `– {what}: kept (you said no)` |
| No, nothing else | `Kept {what}. Item {id} is unchanged — the preview above still stands.` | back to awaiting approval; no new preview |

### 3.4 Stale, expired, double tap (toasts, <= 200 chars; the buttons never throw)

| Situation | Toast |
|---|---|
| Second tap on an answered confirm | `Already answered.` |
| Answered, run in progress (status revision_requested) | `Already answered — working on it.` |
| Token no longer matches (newer note replaced this request) | `That question is out of date — I'm using your newer note.` |
| Item no longer waiting on a confirm (approved, rejected, or dashboard changed it) | `That's no longer needed — nothing was changed.` |
| Item gone | `That item no longer exists.` (existing text) |

When a newer note supersedes an open confirm, edit the old confirm message to
`Replaced by your newer note.` and remove its buttons. The existing generic toast
`Already handled — that item is '{status}'.` is not reused for confirms: it leaks a raw status name.

## 4. Ask the operator (nothing grounded)

Sent when the plan has zero grounded actions (vague, off-topic, injected text, negated, ambiguous). The
operator is the right person to ask (the stage never raises NeedsInput; this is a reply, not an error).
Nothing was changed. The pending reply slot for this item is re-armed, so the next message is the rephrased
note, and `/skip` still means a fresh version. The old preview and its keyboard stay valid.

Structure: **what happened (cause line) → what exists (slide list) → how to say it (2 examples) → exit**.

```
ASK_OPERATOR
{cause line}
Slides in item {id}:
1 "{headline, 24}"
2 "…"
7–8 added automatically (can't be edited)
Try e.g. {example 1} or {example 2}.
Reply with a new note, or /skip for a fresh full version.
```
- Slide list: one line per body slide (`N "headline"`), closing slides collapsed into the single
  `A–B added automatically (can't be edited)` line. At most `SLIDE_LIST_MAX_LINES`; about 250 characters.
- Examples are built from the real deck (first body slide number, and a slide type that exists, e.g.
  "the chart slide"), so they work if pasted. Never generic placeholders.
- Injection and off-topic notes get the `VAGUE` line. We never say "injection", never echo the text, never
  explain a refusal. Neutral beats clever.

| Cause | Cause line |
|---|---|
| `VAGUE` (no concrete change, off-topic, hostile) | `I couldn't tell what to change — nothing was touched.` |
| `NEGATED` ("don't remove slide 2", "I don't like the caption…") | `You said not to, so I left it alone. If you do want a change, say it directly.` |
| `WHICH_SLIDE` (ambiguous "it"/"that one", headline word shared by several slides, type word matching several) | `Which slide do you mean? I couldn't pin it to one.` |
| `EXPLICIT_NEEDED` (model proposed remove/swap/reorder/theme/rebuild but the note didn't say so plainly, `RULES_ONLY`) | `Removing, swapping and restyling need exact wording, so I didn't guess.` Examples: `"remove slide 4"`, `"swap slides 2 and 5"`. |
| `WHICH_THEME` (`THEME_NOT_NAMED`, `BAD_THEME`) | `Which theme? Available: {themes, max 100 chars}.` Example: `"use the {theme} theme"`. |

## 5. Changes block in the new preview

### 5.1 Layout and order

The preview is one text message (4096 limit), sent as a reply to the album with the existing keyboard. The
Changes block goes **first**: it answers "did it do what I asked", and a long caption below it would otherwise
push it off-screen. It is still above the stats line, as design §9 says.

```
{header}                          ← state in the words: revised / partly / couldn't / not verified
{clipped line}                    ← only if the note was cut
{renumber line}                   ← only if slides were added/removed/reordered
✓ lines                           ← applied, by slide number, then non-slide (theme, caption, research)
✗ / ? lines                       ← not done / unverified, by slide number
{note check line}                 ← the verdict, always last of the block's lines
{untouched line}
— — —
{caption}                         ← existing, never truncated by us
— — —
{stats / sources}                 ← existing
```
No blank line inside the block; one blank line before the first `— — —`.

### 5.2 Header (carries the verdict, so a glance is enough)

| Check status | Header |
|---|---|
| `satisfied` | `Revised per your note: "{note}"` |
| `partial` | `Partly revised — "{note}"` |
| `unsatisfied` | `Couldn't do what you asked — "{note}"` |
| `unchecked` | `Revised, not verified — "{note}"` |
| fallback (section 5.6) | `Rebuilt the whole deck with your note: "{note}"` |
| origin `overflow` (T304) | `Slide {n} didn't fit; shortened it automatically.` |
| no note (`/skip`) | `Fresh version (no note).` |

### 5.3 Note-check line (status computed in code, D253; judge wording never leaks)

"Unchecked" must never sound like done. It uses `?`, never `✓`, and tells the operator to look.

| Status | Line |
|---|---|
| `satisfied` | `Checked against your note: all {n} done.` (n = 1: `Checked against your note: done.`) |
| `partial` | `Checked against your note: {k} of {n} done. Not done: "{ask}"; "{ask}".` |
| `unsatisfied` | `Checked against your note: not done. Missing: "{ask}".` |
| `unchecked` | `? Couldn't check this against your note — read the slides before approving.` |

- "Not done / Missing" lists the unsatisfied ask texts (<= `ASK_QUOTE_MAX` each, max 3, then `+N more`).
  Ask text is model-written, so it is cut and sanitised like any quote.
- In `unchecked`, the `✓` action lines still say what was applied (true), but the header says "not verified"
  and this line is the last thing read before the buttons.

### 5.4 Per-outcome lines (every `OutcomeCode`)

`{S}` = `Slide {n}`. `{Sq}` = `Slide {n} "{headline}"` (used when the numbers moved). The verb after the dash
is code-owned fixed text, never model text. Marker is chosen by outcome, not by the line.

| OutcomeCode | Op | Line |
|---|---|---|
| `applied` | edit_slide | `✓ {S} — reworded` |
| | add_slide | `✓ New slide after {n} — added` |
| | remove_slide | `✓ Slide {n} (was) — removed` |
| | reorder | `✓ Slide order — changed` |
| | set_theme | `✓ Theme — now {name}` (text-only theme and the deck has a photo slide: add `; photos won't show on this theme`) |
| | remove_image | `✓ {S} image — removed` |
| | replace_image | `✓ {S} image — replaced` |
| | research | `✓ Research — new findings added` |
| | fix_fact | `✓ {S} — figure checked and corrected` |
| | rewrite_caption | `✓ Caption — rewritten` |
| | full_recompose | `✓ Whole deck — rebuilt` |
| `unchanged` | any slide op | `✗ {S} — came out the same; kept as is` |
| | rewrite_caption | `✗ Caption — came out the same; kept as is` |
| | fix_fact | `✗ {S} — no change made` |
| `not_found` | replace_image | `✗ {S} image — no usable photo found for "{query}"; kept the old one` |
| | research | `✗ Research — nothing useful found for "{query}"` |
| | fix_fact | `✗ {S} — couldn't find a source to check it; left as is` |
| `rejected_integrity` | edit_slide | `✗ {S} — new wording had claims your sources don't back; kept the old text` |
| | add_slide | `✗ New slide after {n} — had claims your sources don't back; not added` |
| | fix_fact | `✗ {S} — corrected figure wasn't backed by a source; left as is` |
| | rewrite_caption | `✗ Caption — new version had unbacked claims; kept the old one` |
| | replace_image | `✗ {S} image — the new photo failed the relevance check; kept the old one` |
| `refused_invariant` | remove_slide | `✗ {S} — not removed: the deck needs at least {MIN_SLIDES} slides` |
| | add_slide | `✗ New slide after {n} — not added: {MAX_SLIDES} slides is the most` |
| | reorder | `✗ Slide order — unchanged: the first slide stays first, closing slides stay last` |
| | other | `✗ {S} — couldn't be done without breaking the deck` |
| `unavailable` | any | `? {S} — changed, but I couldn't check it; please read it` (the slide-less ones: `? Caption — …`) |
| `deferred` | any | never in the preview; standalone message, section 5.8 |
| `dropped_invalid` / `dropped_ungrounded` / `dropped_conflict` | any | by `Reason`, next table |

`{MIN_SLIDES}` and `{MAX_SLIDES}` are read from `deck_shape` / compose, never typed.
Queries are the planner's/rules' search text, shown to help the operator reword; they are the only
web-adjacent text in the block and are quoted and capped like any other.

### 5.5 Per-Reason lines (every `Reason`, for the three `dropped_*` codes)

Principle: show a drop only when the **operator asked for something and it did not happen**. A drop of
something the operator never asked for (the model over-reached) is hidden: showing "I almost did X" is noise
and teaches nothing. Hidden drops are still logged (rule 7). If they leave zero actions, section 4 speaks.

| Reason | Shown? | Line / treatment |
|---|---|---|
| `malformed`, `unknown_op`, `missing_arg` | one aggregated line | `✗ Part of your note couldn't be turned into a clear edit — skipped.` (once, however many) |
| `out_of_range` | yes | `✗ Slide {n} doesn't exist — the deck has {N} slides.` |
| `closing_slide` | yes | `✗ Slide {n} is added automatically and can't be edited.` |
| `no_image` | yes | `✗ {S} has no image to remove.` |
| `bad_theme` | yes | `✗ No theme called "{name}". Available: {themes, 100}.` |
| `bad_order` | aggregated with above | `✗ Couldn't work out the new order; unchanged. Try "swap slides 2 and 5".` |
| `noop_reorder` | yes, neutral | `– Slides were already in that order.` |
| `duplicate` | hidden | (asked twice, done once) |
| `target_removed` | yes, neutral | `– {S} edit skipped: that slide was removed.` |
| `research_cap` | yes | `✗ Only {MAX_RESEARCH_ACTIONS} lookups per revision; skipped "{query}". Send it as a new note.` |
| `recompose_not_alone` | yes, neutral | `– Full rebuild not run: your note also asked for specific edits, so I did only those.` |
| `max_actions` | yes | `✗ Too many changes in one note — did the first {MAX_ACTIONS}. Send the rest as a new note.` |
| `no_slide_reference`, `no_trigger`, `negated`, `rules_only`, `scoped_note`, `theme_not_named` | hidden here | model over-reach or unclear note. Not an operator-asked failure. If they leave nothing, section 4 uses them to choose the cause line. If the note had other asks that stay unmet, the note-check line lists them. |

`–` is a neutral marker for "nothing needed / skipped by design"; it never counts toward "done" and is listed
after `✗` lines.

### 5.6 Fallback to a full compose (D254)

No per-action lines (nothing targeted ran). Replace the block body with:

```
Rebuilt the whole deck with your note: "{note}"      (or: Fresh version (no note).)
{cause line}
Every slide is new — compare before approving.
{note-check line, if the run has a check; else: ? Not checked against your note — read it through.}
```
| Cause | Cause line |
|---|---|
| planner could not plan twice, flag off, no slides on the item | `I couldn't plan targeted edits, so I rebuilt it.` |
| planner verdict `global` / rebuild confirmed | `Your note changes the whole deck, so I rebuilt it.` |
| `/skip` | `You asked for a fresh version.` |

No `Unchanged:` line (nothing is unchanged).

### 5.7 Clipped note, untouched line

- Clipped (`is_clipped`, dashboard notes only; Telegram text cannot exceed the cap): second line under the
  header: `Your note was longer than {MAX_NOTE_CHARS} characters; I only read the start.` The check line is then
  forced to the `unchecked` wording (the model code already refuses `satisfied` for a clipped note).
- Untouched: `Unchanged: slides 1, 4–8` (consecutive runs collapsed with an en dash; slide numbers in the new
  deck). When every slide changed, omit the line. When no slide changed (caption or theme only): `Slides
  unchanged.` When nothing at all changed: `Nothing changed.`

### 5.8 Deferral ("waiting on the model — will retry")

Not part of the preview (there is no new preview yet). One standalone chat message, sent **once** per request
the first time the planner times out or the model is unavailable (Retryforever, no attempt spent):

```
DEFERRED
The model is busy, so item {id} is waiting. I'll retry on my own — your note is kept, no need to resend.
```
If it is still deferred after the usual stall window, no second message (the dashboard shows status). No
exception text, no "timeout" or "Ollama" wording.

### 5.9 Truncation algorithm (total <= 4096; Changes <= 1200)

Never cut mid-line. Never truncate the caption (it is what gets published, and a clipped caption would
mislead). Drop the Changes block's own detail first, in this order, until it fits:

1. Budget = `min(CHANGES_MAX_CHARS, TELEGRAM_TEXT_LIMIT - len(caption part) - len(stats part) - 40)`.
2. Truncate each line to `CHANGE_LINE_MAX` with `…`.
3. Collapse all `✓` lines into one: `✓ {k} changes applied: slides 2, 3, 5; caption`.
4. Keep `✗`/`?` lines up to `MAX_NOT_DONE_LINES`, then `+N more not done`.
5. Drop the untouched line.
6. Last resort (budget < about 200): header + note-check line only.
7. If even that does not fit (caption alone near the limit), send the Changes block as its own message
   before the keyboard message. The caption message is never altered.
Header, note-check line and `✗`/`?` lines are never dropped in steps 1-5: they are what the operator needs.

`TELEGRAM_CAPTION_LIMIT` (1024) applies only if a future change sends the preview as a photo caption; today
the preview is a text message, so the 4096 limit governs. The copy module takes the limit as a parameter and
the test covers both values.

### 5.10 No exception text

No message in this spec interpolates `str(exc)`, a class name, a stack frame, a URL, a file path, a model
name or a raw status/enum value. Only: counts, slide numbers, headlines, quoted note/ask/query/theme text
(sanitised, capped) and the fixed strings above. `Outcome.reason` / `Dropped.detail` are logs, never copy.

## 6. Mock-ups

Slide set used (8 slides): 1 hook "Blackwell B300 is here", 2 point "Memory is the bottleneck", 3 photo "The
die up close", 4 point "Pricing leaks", 5 kpi "40% faster inference", 6 takeaway "What to watch", 7 sources, 8 follow.

### M1. Applied (satisfied)

Note: `slide 3 shorter and a better photo of the chip on slide 1`

```
Revised per your note: "slide 3 shorter and a better photo of the chip on slide 1"
✓ Slide 1 image — replaced
✓ Slide 3 — reworded
Checked against your note: all 2 done.
Unchanged: slides 2, 4–8
— — —
Blackwell B300 is out and the memory story is the real news. Here's what changed…
#nvidia #blackwell #ai
— — —
triage 8/10 · 6 research notes · 8 slides
sources: nvidia.com, anandtech.com, servethehome.com
[✅ Approve] [🔄 Regenerate]
[✏️ Caption] [❌ Reject]
```

### M2. Partial

Note: `shorten slide 3, check the 40% on slide 5, make the caption casual`

```
Partly revised — "shorten slide 3, check the 40% on slide 5, make the caption casual"
✓ Slide 3 — reworded
✓ Caption — rewritten
✗ Slide 5 — corrected figure wasn't backed by a source; left as is
Checked against your note: 2 of 3 done. Not done: "verify the figure on slide 5".
Unchanged: slides 1–2, 4–8
— — —
(caption)
— — —
triage 8/10 · 6 research notes · 8 slides
sources: nvidia.com, anandtech.com
[buttons as above]
```

### M3. Not found (image), nothing else asked

Note: `swap the photo on slide 3 for the B300 die shot`

```
Couldn't do what you asked — "swap the photo on slide 3 for the B300 die shot"
✗ Slide 3 image — no usable photo found for "B300 die shot"; kept the old one
Checked against your note: not done. Missing: "a B300 die photo on slide 3".
Slides unchanged.
— — —
(caption)
— — —
triage 8/10 · 6 research notes · 8 slides
sources: nvidia.com, anandtech.com
[buttons as above]
```

### M4. Confirm prompt (remove + another edit)

Note: `remove slide 4 and tighten slide 2`

```
Remove slide 4? There is no undo — the old version isn't kept.
Slide 4 "Pricing leaks"
Also doing: slide 2 reworded.
[✅ Yes, remove] [↩️ No, keep it]
```
After "No": the message is edited to `Kept slide 4. Applying the rest…` and the new preview's block contains
`– Slide 4: kept (you said no)` above the check line.

Rebuild variant:
```
Rebuild the whole deck from scratch? Every slide gets new text. There is no undo.
Your note: "start over, this angle isn't working"
[✅ Yes, rebuild] [↩️ No, keep deck]
```

### M5. Ask the operator (vague note)

Note: `make it better`

```
I couldn't tell what to change — nothing was touched.
Slides in item 12:
1 "Blackwell B300 is here"
2 "Memory is the bottleneck"
3 "The die up close"
4 "Pricing leaks"
5 "40% faster inference"
6 "What to watch"
7–8 added automatically (can't be edited)
Try e.g. "slide 2 shorter" or "the kpi slide: check the number".
Reply with a new note, or /skip for a fresh full version.
```
`NEGATED` variant swaps only line 1: `You said not to, so I left it alone. If you do want a change, say it directly.`

### M6. Fallback to full compose

Note: `the whole thing is too technical` (planner verdict global, confirmed) — failure variant shown:

```
Rebuilt the whole deck with your note: "the whole thing is too technical"
I couldn't plan targeted edits, so I rebuilt it.
Every slide is new — compare before approving.
? Not checked against your note — read it through.
— — —
(caption)
— — —
triage 8/10 · 6 research notes · 8 slides
sources: nvidia.com, anandtech.com
[buttons as above]
```

## 7. Acceptance criteria (tester)

AC-1 REGEN_PROMPT is delivered as a chat message and the callback toast is at most 200 characters. The prompt
contains at least 5 examples, includes the exact phrasings "remove slide N", "swap slides N and M" and "use
the X theme", names `/skip` as "fresh full version", and is under 1000 characters.
AC-2 Ack text differs for a note and for `/skip`; neither names an outcome.
AC-3 For every item in `needs_confirmation(plan)`, no executor step runs before Yes. Yes runs all. No drops
exactly those actions and runs the rest; with nothing left the item is awaiting approval with no new preview.
AC-4 Confirm buttons: tapped twice, the second tap yields a toast and changes nothing; a button from a
superseded request yields the "out of date" toast; none raise or show raw status text. Buttons are removed
from the answered message.
AC-5 The confirm prompt states "no undo", lists each destructive slide by number and headline, and the two
buttons are the same size with distinct labels. Neither uses `❌`.
AC-6 Zero grounded actions sends the ask-operator message with the cause line matching the drop reasons, the
real slide list (number + headline, closing slides collapsed), and examples that exist in this deck. Nothing
changes in the item, the reply slot is re-armed, an injected/off-topic note's text is not echoed.
AC-7 Changes block: a unit test enumerates `OutcomeCode` x op and `Reason`; a missing mapping fails. Each
`applied` shows `✓`; `unchanged`, `not_found`, `rejected_integrity`, `refused_invariant` show `✗` with a
plain reason; `unavailable` shows `?`; `deferred` never appears in a preview.
AC-8 A run with check `unchecked` never contains "done", "all done" or a bare `✓`-only verdict; the header
says "not verified" and the check line starts with `?`. A clipped note is always `unchecked`.
AC-9 Fallback previews say "Rebuilt the whole deck", state the cause, omit the `Unchanged:` line, and show
either a real check line or "Not checked against your note".
AC-10 Length: for captions of 0, 1, 1024, 2200 and 3500 characters with up to 6 ✗ lines, the preview is at
most 4096 characters, the caption is byte-identical to the item's caption, no line is cut mid-word, and
header + check line + ✗ lines survive. The same function tested with limit 1024 returns at most 1024.
AC-11 Sanitisation: notes, headlines, queries and ask text containing newlines, control characters, 500
characters of text or `<b>`/Markdown characters render as one capped line of plain text.
AC-12 No preview, toast or message in this spec ever contains an exception message, class name, path, URL,
model name or raw enum value (grep test over all rendered strings for a fault-injected run).
AC-13 Deferral: exactly one message per request across N retries, with no exception text.
AC-14 Accessibility: every `✗`/`?` line states its meaning in words; no meaning is carried by the glyph or
by emoji alone; button labels read sensibly as plain text ("Yes, remove", "No, keep it").

## 8. Heuristics self-check (Nielsen)

1 Status: ack, deferral message, header verdict, "Removing…" edit. 2 Real-world language: "photo", "kept the
old one", no "ask/op/grounded". 3 Control: Yes/No before destruction, `/skip`, ask-operator re-arms the
reply. 4 Consistency: one glyph set, one slide naming rule, same buttons idiom as the preview. 5 Prevention:
mandatory confirm, "no undo" stated, explicit wording required for removal. 6 Recognition: examples in the
prompt, real slide list in the question, theme list on demand. 7 Efficiency: terse lines, `/skip`, rules
phrasings pass without the model. 8 Minimalism: hidden over-reach drops, collapsed `✓` lines when tight.
9 Errors: each `✗` says what and what was kept, plus how to retry. 10 Help: the prompt is the help; examples
are copy-pasteable.
