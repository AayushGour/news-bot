# Agentic regeneration — the edit agent (T291 design, 2026-11-07)

Owner: architect. Status: plan, not built. Decisions D248-D261 (project-context.md, "Agentic regeneration").
User decision U3: an EDIT AGENT that reads the operator's note and the current deck, plans tool actions,
applies only those, and checks the note was satisfied before re-previewing.

## 1. What is wrong today (verified in code)

| Pain (user-confirmed) | Cause |
|---|---|
| (a) note ignored or half-applied, nothing checks | `compose.py:710` appends `REVISION REQUESTED — apply this: <note>` to a fresh one-shot `llm.good` call. The only post-checks are clause coverage (`item.clauses`, empty for channel items) and `check_deck`. Nothing compares the result with the note. |
| (b) cannot fix images or facts | Regen goes `AWAITING_APPROVAL -> SYNTHESIZED -> compose`. Compose reuses the same brief, research and `extracted.image_descriptions`. There is no search and no image pick. |
| (c) changes too much | The prior `slides`/`caption`/`theme` never reach the prompt, so every regen is a new deck at temperature 0.6. The render-overflow path (`worker.py:122-135`) has the same problem: one slide that does not fit causes a full recompose. |

Entry points that write a regen today. All three must move to the new path:
`approval/bot.py:279` (Telegram), `scripts/dashboard.py:866` (operator message on an awaiting item), `scripts/dashboard.py:924` (regen verb).

## 2. Shape of the solution

```
operator note ──► request_revision() ──► status REVISION_REQUESTED  (+ revision_request JSON)
                                                │  worker registry
                                                ▼
                                   stages/revise.py  revise(item)
            ┌──────────── fallback ladder (D254) ──────────────┐
            │ no slides / /skip / plan says global / planner    │──► compose() with PREVIOUS DECK (T292)
            │ invalid twice / REVISE_ENABLED=false              │
            └───────────────────────────────────────────────────┘
                     │ else
                     ▼
     round r (r ≤ MAX_ROUNDS=2):
       PLAN    1 llm.good call → flat JSON plan → validate in code against ACTION_SPECS
       EXECUTE fetch → image → text → structural  (only the targeted slides)
       CHECK   untouched slides identical (code), per-action outcomes (code),
               explicit note references (code), note-satisfaction judge (1 call, tri-state)
       if every ask satisfied, or nothing retryable is left → stop; else replan with feedback
                     │
                     ▼
     write `revision` run record, clear revision_request
     _next = COMPOSED (slides/theme changed) | RENDERED (caption-only or no change)
                     │
                     ▼
     render → send_preview, which shows a "Changes" block (did X / could not do Y because Z)
```

It is a bounded plan-execute-check loop with one closed action vocabulary, not an open tool-calling loop.
The local good model is a 4B Qwen distill with 16k context and is weak at tool calling (D249). It gets
one schema-constrained call that returns a whole plan. Code validates the plan and runs it. Steps that
have one correct answer are deterministic (rule 8).

## 3. Action vocabulary — one registry (`revision/model.py: ACTION_SPECS`)

`ACTION_SPECS` is the only contract. The planner schema, the validator, the executor dispatch, the
prompt's action list and the preview's plain-English labels are all derived from it (rule 3).

| op | args (required) | kind | executor | model calls |
|---|---|---|---|---|
| `edit_slide` | slide, instruction | text | one single-slide `llm.good` call, same type arm schema (`compose._slide_variant`) | 1 (+1 retry on integrity reject) |
| `add_slide` | slide (= insert after), instruction | text | same as edit_slide, model also picks the type from a content-type list (not hook or closing types) | 1 |
| `remove_slide` | slide | structural | deterministic | 0 |
| `reorder` | order (list of slide numbers) | structural | deterministic. Hook stays first and closing types stay last, enforced in code | 0 |
| `set_theme` | theme ∈ `render.available_themes()` | structural | deterministic, writes `theme_pin` (D256) | 0 |
| `remove_image` | slide | image | deterministic | 0 |
| `replace_image` | slide, query | image | `search.image_search` + `download_image` → `media/researched/<id>/`, T223 gate when built | 0 (+1 with gate) |
| `research` | query | fetch | `research._research_one` reused as-is (gate, D232 provenance once T265 lands). The note is appended with `origin:"revision"` | ~2-5 |
| `fix_fact` | slide, query | fetch+text | expands in code to `research(query)` then `edit_slide(slide, "correct the claim using the new note")` with the new note given as priority material | research + 1 |
| `rewrite_caption` | instruction | text | one `llm.good` call → `compose.build_caption` (hashtags/credit re-applied) | 1 |
| `full_recompose` | instruction | escape | compose() with prior deck (T292). Allowed only as the sole action | compose's own |

Kinds run in the order fetch -> image -> text -> structural (reorder last); `full_recompose` is `global` and runs alone. `remove_image` and `replace_image` are kind `image` (D267, T295), not structural/fetch as first drafted, so image changes land before text edits. The planner schema has no `after` field (T293: two slide-number fields were confused; D263).

Limits. These are editorial, not platform, limits and are named as such (rule 2): `MAX_ACTIONS=6`,
`MAX_RESEARCH_ACTIONS=2`, `MAX_ROUNDS=2`, `REVISION_RUNS_KEPT=10`.

Code-owned slides (`follow`, `sources`, `links`, i.e. `compose.CLOSING_TYPES`) are never targets for
edit, add-after-last or remove. They are rebuilt by `ensure_closing_slide` / `ensure_links_slide`, and
only after a structural op. Slide indices given to the planner are 1-based, as the operator reads them.
Internally every slide gets a stable id equal to its original index, so a `remove_slide` cannot shift
the target of a later `edit_slide` in the same plan. Execution order is fetch → image → text →
structural, with reorder applied last.

Conflicts are resolved in code and every drop is logged (rule 7):
- edit and remove on the same slide → the edit is dropped.
- two edits on the same slide → their instructions are merged into one call.
- a theme in `TEXT_ONLY_THEMES` while the deck has a photo slide → applied (D242: an explicit pin is
  honoured) and the report warns that images will not show.

## 4. Planner (`revision/plan.py`)

**Input** is compact and budgeted with `budget.pack` against the good role. Fetched or research text is
wrapped as delimited data. The planner never sees page text; it sees claims only.
```
OPERATOR NOTE: <note>
DECK (8 slides):
[1] hook — "headline" | sub: … | image: researched photo "title…"
[2] point — "headline" | bullets: a; b; c
…  (fields flattened and capped per slide; code-owned slides marked [fixed])
CAPTION: <first 300 chars>
THEME: signal   AVAILABLE THEMES: <render.available_themes()>
RESEARCH CLAIMS: [1] <claim> (confidence) …
ACTIONS: <one line per ACTION_SPECS entry>
FEEDBACK (round 2 only): <unsatisfied asks + failed actions + reason codes>
```
**Output** is a flat schema with no `oneOf`, because Ollama does not bind a top-level `oneOf` (rule 5, D18/D27):
```json
{"asks": ["<distinct thing the note wants>", "..."],          // ≤4
 "global": false,                                              // true → fallback
 "actions": [{"op": "<enum from ACTION_SPECS>", "ask": 0,
              "slide": 3, "after": 0, "order": [],
              "instruction": "", "query": "", "theme": ""}]}   // ≤ MAX_ACTIONS
```
**Validation** (pure, code): unknown op; required arg missing or out of range; target is a code-owned
slide; theme not available; ask index out of range; more than `MAX_RESEARCH_ACTIONS` research actions.
An invalid action is dropped and logged. Checked against the note itself (rule 16 spirit): every slide
number and theme name the note mentions explicitly (`note_references()`, regex) must be targeted by
some action. If one is missing, that ask is pre-marked unsatisfied, which drives a replan. On zero
valid actions the planner retries once with the validation errors. If the retry also has no valid
actions, a `PlanFailed` triggers the fallback (D254).

Temperature is 0.2: this is a plan, not prose. T293 (spike) decides one schema versus a two-step
asks-then-actions call before this is built, and decides GO/STOP on a model planner at all.

## 5. Executor guarantees (diff-preserving, D251/D252)

- Before execution, `fingerprint(slide)` = sha256 of canonical JSON (sorted keys) for every slide.
- After execution, every slide whose stable id was not targeted must have an identical fingerprint. A
  violation is a bug. That slide is restored from the original and the error is logged. It never ships
  as a "change".
- An edited slide keeps its `image`/`image_mode`. The model never sees or returns images. Image actions
  alone change them.
- Single-slide output goes through `compose.normalise_slides([slide], None)` (verified safe for one
  non-hook slide; hook edits keep `type:"hook"` and pass through the same function), then through the
  same integrity gates as compose: `integrity.check_deck(deck, research, caption)`, `slide_backed`, and
  `figure_backed` on every number in the edited text against `material_text(item)` including new
  revision notes. A failure gets one retry naming the problem. If the retry fails too, the original
  slide is restored and the outcome is `rejected_integrity`.
- Output identical to input → outcome `unchanged`. This counts as not applied.
- Deck invariants after structural ops: hook first, ≥ floor (`MIN_SLIDES` model-written), ≤ `MAX_SLIDES`,
  `drop_duplicate_ctas` + `ensure_closing_slide` re-run. If an op would break an invariant it is refused
  (`refused_invariant`), never silently clamped.

Outcome codes (closed set, in `revision/model.py`, mapped to operator copy by T303): `applied`,
`unchanged`, `rejected_integrity`, `not_found` (search or image came back empty), `refused_invariant`,
`invalid`, `unavailable` (a checker's own fault). Infrastructure faults are not outcomes; they propagate.

## 6. Check step (`revision/check.py`, D253)

1. **Code:** untouched invariant (§5); per-action outcome; explicit note references covered by an
   `applied` action; for `set_theme`, the pin equals the request; for `replace_image`, the path changed.
2. **Judge (1 call):** the asks against BEFORE/AFTER text of the changed slides, the caption diff, and
   one-line descriptions of image and theme changes. `coverage.unaddressed`'s core is refactored to return
   `None` on the check's own fault. `unaddressed()` keeps its current behaviour exactly (unchanged-path
   test). The revise check calls the strict core, so "could not check" is recorded as `unchecked` and is
   never read as satisfied. Rule 6 scope: this judges work against a human's request, gates nothing beyond
   one replan, and is not a metric.
3. **Status computed in code** (rule 15 style): `satisfied` (every ask covered and an applied action
   cites it) / `partial` / `unsatisfied` / `unchecked`.
4. **Replan** only when a retryable reason remains (`unchanged`, `rejected_integrity` once, an uncovered
   ask with no `not_found` behind it) and `round < MAX_ROUNDS`. Otherwise stop and report honestly. An
   unsatisfied note never escalates to a full recompose, because that is pain (c).

Never fail-open (rule 17): `Retryforever`/`ConfigFault` from any call propagate. The item is deferred
in `REVISION_REQUESTED` with its request intact, and the worker retries it. The stage never raises
`NeedsInput`: the operator is already in the loop, so the honest report is the answer.

## 7. Data model (data-modeling skill, D255/D256)

Current: `items` has `regen_note TEXT`, `theme TEXT`, `verification TEXT`, `integrity TEXT`
(`NULLABLE_JSON_COLUMNS`), `events(from_status,to_status,at,detail)`. Migrations are `_migrate()`'s
ADD COLUMN list.

| Column | Type | Writer | Cleared by |
|---|---|---|---|
| `revision_request` | TEXT NULL, nullable JSON | `request_revision()` only (bot, dashboard, worker overflow) | revise on completion; requeue `_ALWAYS_CLEARED` |
| `revision` | TEXT NULL, nullable JSON | revise stage only | requeue to SYNTHESIZED or earlier |
| `theme_pin` | TEXT NULL | revise `set_theme` | requeue to SYNTHESIZED or earlier |

Each column has one writer, so there is no read-modify-write between processes. Shapes:
```
revision_request = {"origin": "operator"|"skip"|"overflow", "note": str|null,
                    "slide": int|null, "reason": str|null, "surface": "telegram"|"dashboard"|"worker", "at": iso}
revision = {"v": 1, "runs": [ {"origin", "note", "at", "rounds", "fallback": bool,
            "asks": [..], "actions": [{"op", "slide", "ask", "outcome", "reason"}],
            "check": "satisfied|partial|unsatisfied|unchecked", "unsatisfied": [..],
            "changed": [slide numbers], "caption_changed": bool, "theme": str|null} ]}   // ≤ REVISION_RUNS_KEPT, oldest dropped + logged
```
`regen_note` stays. It is the input to the legacy and fallback compose path. With REVISE on,
`request_revision()` writes the note into `revision_request.note`, and the fallback sets
`item.regen_note` in memory before calling `compose()` (which then clears the column as it does today).

New status: `Status.REVISION_REQUESTED = "revision_requested"`. It is operator-entered and maps to the
next stage, following the `APPROVED` precedent. It is not in `WORKER_HALTS` and not a requeue target. The
`__main__.py:144` registry-coverage check needs the registry entry in the same task. The dashboard's
status list (`dashboard.py:79`) is updated.

Render precedence (`render.py:171-175`) becomes `theme_pin` > settings named/rotate/random > `item.theme`.
An operator's per-item choice beats the global default. Coordinate with T283 (items.theme vs rendered theme).

There is deliberately no history table and no prior-deck snapshot. Undo was not requested (open
question Q1), and `events.detail` carries a one-line summary per run for the timeline.

**Rollback (breaking for a downgrade):** older code's `Status` enum lacks `revision_requested`, so
loading such a row would raise. Before downgrading, move those items back with
two statements: `UPDATE items SET status='awaiting_approval', revision_request=NULL WHERE status='revision_confirm'`
(never an unconfirmed rebuild; the operator re-asks), then
`UPDATE items SET status='synthesized', regen_note=json_extract(revision_request,'$.note') WHERE status='revision_requested'`
(documented in the T294 log). The new columns are additive, so old code ignores them.

Concurrency: the bot path changes from `transition` to `transition_if(expected=AWAITING_APPROVAL)`, so a
dashboard approve and a Telegram regen cannot both win. This fixes an existing race at `bot.py:279`.

## 8. Interactions

- **Recompose / overflow (D258, T304):** `worker.py:122` calls `request_revision(origin="overflow", slide=i, reason=r)`
  → `REVISION_REQUESTED`. Revise builds the plan in code, `[edit_slide(i, "shorten to fit; keep every fact")]`,
  with 0 planner calls and no judge (the check is the next render). It is bounded by T272's
  `recomposes` counter (ceiling → NeedsInput, D236). The operator's earlier run stays in `revision.runs`,
  so the preview still shows it.
- **requeue.STAGE_OUTPUTS:** the SYNTHESIZED entry adds `revision: None, theme_pin: None`, and
  `_ALWAYS_CLEARED` adds `revision_request: None`.
- **Retention:** new images land in `media/researched/<id>/`, which D228 already purges. The 120-day clock
  restarts on re-entry to awaiting_approval, as today. No change.
- **Advisory verifier:** the never-raise verify hook moves out of `compose.py:814-828` into one shared
  `verify.run_advisory(item, slides, caption, llm, settings)`. Compose and revise both call it and always
  write the `verification` key (D209).
- **Integrity column (D238):** revise leaves `items.integrity` as written by compose. Its own rejections
  live in `revision.runs[].actions[].outcome`. T266/T273 may later merge them.
- **approved_hash (D237):** computed at approval, so it is unaffected.
- **Dashboard:** the regen form label becomes "revise · targeted edits", and the detail page renders
  `revision.runs` (escaped, length-capped).

## 9. Operator UX (Telegram, spec finalised by T303)

- REGEN_PROMPT lists what is possible: "slide 3 shorter", "swap the image on slide 2 for <subject>",
  "check the number on slide 4", "theme doodle", "rewrite the caption more casual", or /skip for a fresh
  version.
- Immediate ack: "Revising item N — planning edits; new preview shortly."
- Re-preview: the caption message gains a **Changes** block above the stats, length-capped, with no
  exception text (rule 15):
  ```
  Revised per your note: "make slide 3 shorter and use a real photo of the chip"
  ✓ Slide 3 — shortened
  ✗ Slide 1 image — no usable photo found for "Blackwell B300 die"; kept the old one
  Note check: 1 of 2 asks done · slides 1-2, 4-8 unchanged
  ```
  Fallback runs say "I couldn't plan targeted edits, so I rebuilt the deck with your note." A `/skip`
  run says "Fresh version (no note)."

## 10. Quota and latency (rule 9)

Typical revision: planner 1 (local good) + 1 per edited slide (local good) + judge 1 (cheap), so 3-5
calls. A research action adds about 2-5 (relevance gates + note). The worst case is bounded at about
26 (2 rounds × (1 + 6 + 1) + 2 × 5). Today's regen costs 1-4. Regens are operator-driven, so the
increase is negligible against D8's 1000/day. Latency is dominated by local 4B calls and SearXNG
fetches. The ack message covers it.

## 11. Security (hard trigger on T299, T302)

- The operator note comes from an authenticated operator and is still wrapped as delimited data.
- Web text (new notes, fetched pages) reaches only executor calls, as delimited and packed data.
- The planner sees the note, the deck and claims only.
- Every action is code-validated against `ACTION_SPECS`. Injected text cannot pick an op outside the
  registry, target a code-owned slide, or name an unavailable theme.
- Images follow the T218 rules (byte and pixel guards, re-encode) and T227 (SSRF) once built.
- Exception text never reaches the preview or `revision`.
