# Revision API reference (T301)

For T302 (bot, dashboard, copy) and anyone calling the revise stage. Design: `docs/design/agentic-regen.md`;
copy: `docs/design/revision-copy.md`. Code: `src/pipeline/revision/request.py` (the only module
operator surfaces call), `src/pipeline/stages/revise.py` (the stage), `src/pipeline/revision/record.py`
(the run record).

## State machine

```
AWAITING_APPROVAL --request_revision()--> REVISION_REQUESTED --revise stage--> COMPOSED   (slides / theme / images changed)
                                                |                              RENDERED   (caption-only or nothing changed; existing PNGs re-previewed)
                                                |                              AWAITING_APPROVAL (nothing grounded: "what do you mean?"; old preview stays valid)
                                                |                              SYNTHESIZED (REVISE_ENABLED=false: legacy regen, note in regen_note)
                                                |  plan holds remove_slide / full_recompose
                                                v
                                         REVISION_CONFIRM   (in WORKER_HALTS: only confirm_revision() moves it)
                          confirm_revision(yes)  -> REVISION_REQUESTED (the STORED plan runs, no re-planning)
                          confirm_revision(no)   -> REVISION_REQUESTED (destructive actions dropped, the rest runs)
                                                 or AWAITING_APPROVAL  (nothing else was planned; no new preview)
```

`COMPOSED` and `RENDERED` continue as usual (`render` then `send_preview`). Infrastructure faults
(`Retryforever`, `ConfigFault`, `EnginesBlocked`) leave the item in `REVISION_REQUESTED` with its request
intact and back off without spending an attempt.

## Writing a request

```python
from pipeline.revision.request import request_revision

ok = await request_revision(
    db, item,                      # item = the caller's SNAPSHOT (status + status_updated_at are the guard)
    origin="operator",             # "operator" | "skip" | "overflow"
    note="slide 3 shorter",        # operator's RAW words; blank + operator => treated as "skip"; skip => note None
    slide=None, reason=None,       # overflow only: slide number, renderer reason
    surface="telegram",            # "telegram" | "dashboard" | "worker"
)
```

Returns `False` and writes nothing if someone moved the item first (`transition_if`), so a dashboard
approve and a Telegram regen cannot both win. Which statuses may request a revision is the caller's
policy. Passing a snapshot in `REVISION_CONFIRM` supersedes that pending confirm (its old button then
answers `STALE`/`NOT_WAITING`).

`items.revision_request` payload (`new_request()` builds it):

| key | writer | meaning |
|---|---|---|
| `origin`, `note`, `slide`, `reason`, `surface`, `at` | `request_revision` | the request. `note` stored up to `MAX_STORED_NOTE_CHARS` (2x the parse cap, so a long dashboard note stays visibly clipped) |
| `notified` | stage | the deferral message was sent (one per request) |
| `blocked_since` | stage | first `EnginesBlocked` deferral (start of the search-give-up clock) |
| `pending_confirm` | stage / `confirm_revision` | `{token, plan, answer, asked_at}`; `answer` is `None` until the operator taps; `plan` = `{asks, actions[], dropped[], unaddressed[], calls, plan_failed}` |

Only the stage clears `revision_request` (atomically with the next status).

## Answering a confirm

```python
from pipeline.revision.request import confirm_revision, ConfirmResult

result = await confirm_revision(db, item_id, yes=True, token=token_from_callback_data)  # token REQUIRED: missing/different = STALE
```

One `transition_if` on (status, status_updated_at): idempotent, two simultaneous taps have exactly one
winner. Nothing destructive runs inside it; Yes re-queues and the stage executes the stored plan.

| result | meaning | copy (revision-copy.md §3.3-3.4) |
|---|---|---|
| `YES` | will run, including the destructive actions | `Removing {what}…` / `Rebuilding the deck…` |
| `NO_CONTINUE` | destructive actions dropped, the rest runs | `Kept {what}. Applying the rest…` |
| `NO_NOTHING_LEFT` | nothing else planned; item back at the gate, a `declined` run recorded, **no new preview** | `Kept {what}. Item {id} is unchanged…` |
| `ALREADY_ANSWERED` | second tap, run in progress | `Already answered — working on it.` |
| `STALE` | token belongs to a replaced request / newer confirm | `That question is out of date…` |
| `NOT_WAITING` | no longer waiting (approved, rejected, changed elsewhere, corrupt plan) | `That's no longer needed — nothing was changed.` |
| `NO_ITEM` | item gone | `That item no longer exists.` |

## Notices (what the operator must be told)

`build_stage_registry(..., revise_notify=None)` binds the async `notify(item, Notice)` (`stages/revise.py`)
into `revise`; `__main__.main()` passes `approval.bot.make_revision_notifier(bot, db, settings, pending)` (T302). It is called AFTER the DB transition committed (worker
`_after` hook; a failure is logged by exception type and never fails the stage):

| `Notice.kind` | when | payload |
|---|---|---|
| `CONFIRM` | plan holds a destructive action; item is now `REVISION_CONFIRM` | `token` (for callback data), `held` (Actions to list), `rest` (Actions also running) |
| `ASK_OPERATOR` | nothing grounded; item is back at `AWAITING_APPROVAL` | none: read `item.revision.runs[-1]` (`asks`, `dropped`, `ask_operator: true`) from the DB, re-arm the reply slot |
| `DEFERRED` | first infrastructure deferral of a request | none (sent once, flag `notified`) |

If a `CONFIRM` notice cannot be delivered the item waits in `REVISION_CONFIRM`: the dashboard "waiting on you"
lane shows an `answer` link to the item page, which renders the pending plan with the same Yes/No (and the same
token), so the question is always answerable from the browser.

## The run record (`items.revision`)

`{"v": 1, "runs": [run, ...]}`, at most `REVISION_RUNS_KEPT` (10), oldest dropped and logged. The preview is
built from the LAST run. Only counts, slide numbers, closed codes and the operator's/planner's own words
(`note`, `asks`, `unaddressed`: sanitise and cap them in copy). Never exception text.

```
origin, surface, note, at, rounds, fallback, cause, plan_failed, ask_operator, clipped,
asks: [str],                         # indexed by actions[].ask and unsatisfied[]
actions: [{op, slide, ask, outcome, reason}],   # outcome = OutcomeCode (incl. "declined"); reason = closed code or ""
dropped: [{op, slide, outcome, reason}],        # planner drops (Reason codes)
unaddressed: [str],                  # planner asks that got no action: phrase softly (D281)
deck: str | null,                  # sha256 of the deck the run left behind (T302): a later preview of a different deck hides the run
check: "satisfied|partial|unsatisfied|unchecked" | null,   # null: no check ran (fallback, ask_operator, all declined)
check_reasons: [CheckReason], unsatisfied: [ask index], missed_slides: [int], missed_themes: [str],
changed: [new-deck numbers], added: [new-deck numbers], removed: [OLD numbers], reordered: bool,
caption_changed: bool, theme: str | null          # theme = the new pin when it changed
```

`cause` (fallback runs only): `skip` | `no_slides` | `plan_failed` | `rebuild` | `overflow`.

Closed `Outcome.reason` codes new in T301 (copy must map them): `search_blocked` (search engines blocked
past the cap; the action was not attempted, outcome `not_found`), `model_unusable` (the model's reply was
unusable twice; kept as is, outcome `unchanged`), `global_op`. T299's fetch codes
(`no_results`, `not_answered`, `source_unverified`, `no_candidates`, `no_usable_image`, `same_image`,
`image_unchecked`) and T297's structural codes (`min_slides`, `max_slides`, `hook_slide`, `bodyless`,
`cta_dropped`, `closing_clamp`, `already_removed`, `text_only_theme_with_photo`) also reach the record.

## Configuration

| env | default | meaning |
|---|---|---|
| `REVISE_ENABLED` | `true` | `false`: REVISION_REQUESTED goes straight to SYNTHESIZED with `regen_note` (the pre-agentic regen) |
| `REVISE_ENGINES_BLOCKED_MAX_S` | `1800` | how long a revision keeps retrying while search engines are suspended before research / fix_fact / replace_image are reported not done and the rest runs. `0` = give up on the first deferral |

## Rollback

Same SQL as D255 (`UPDATE items SET status='synthesized', regen_note=json_extract(revision_request,'$.note')
WHERE status IN ('revision_requested','revision_confirm')`), or set `REVISE_ENABLED=false` (no downgrade needed).


## Operator surfaces (T302)

Spec: `docs/design/revision-copy.md`. All wording lives in `src/pipeline/revision/copy.py` (pure; no I/O).

### `revision/copy.py`

| call | returns |
|---|---|
| `render_changes(run, slides=deck, themes=[...], budget=1200)` | the Changes block (plain text, "" when the run has nothing to say) |
| `fit_changes(run, ..., budget=)` | `ChangesBlock(text, standalone)`: `standalone=True` when it cannot share a message with the caption (send it first, on its own) |
| `preview_budget(len(caption+stats), limit=4096)` | characters the block may use |
| `run_summary(run, slides=None)` | one run for a history list (dashboard) |
| `regen_prompt(item_id, themes)` / `REGEN_TOAST` / `ACK_NOTE` / `ACK_SKIP` / `DEFERRED` / `ITEM_MOVED` | chat messages and toasts |
| `confirm_view(held, rest, slides=, note=)` / `confirm_view_for(item)` | `ConfirmView(text, yes, no, what, rebuild)` for the Yes/No |
| `confirm_answered_text(result, view, item_id)` / `CONFIRM_TOASTS[ConfirmResult]` | the edit and the toast for each `ConfirmResult` |
| `ask_cause(dropped)` -> `AskCause`; `render_ask_operator(item_id, dropped, slides, themes)` | the "what do you mean?" message. Never takes or echoes the note |
| `resolve(code, op, reason)` | the `Row(mark, text)` for one action outcome |

Closed inputs only: `OutcomeCode`, `Reason` (new: `Reason.MAX_ASKS`, the ask cap; `MAX_ACTIONS` is now only the
action cap, D278), op, check status, and the closed `Outcome.reason` codes. Free text (`note`, `asks`,
`unaddressed`, headlines) is sanitised and capped (`clean`, `quote`). A new `OutcomeCode` / `Reason` / `Op` /
`ConfirmResult` / executor `REASON_*` constant without a sentence fails `tests/revision/test_copy.py`.
Numbers in the block are the new deck's (`Numbering`); a reorder keeps the note's numbers and says so.
The block's order and squeeze steps are the spec's section 5.9; deviations: an action's search query is not in
the run record, so `not_found` lines do not quote it (the check line quotes the ask instead).

### Bot (`approval/bot.py`)

- Regenerate (REVISE_ENABLED): the prompt is a chat message (`regen_prompt`), the toast is `REGEN_TOAST`. The reply is
  `request_revision(db, item, origin=operator|skip, note=, surface="telegram")`; allowed from `AWAITING_APPROVAL`
  and `REVISION_CONFIRM` (a newer note supersedes the open question and edits its message to "Replaced by your newer
  note."). A lost race answers `ITEM_MOVED` and `handle_pending_reply` returns `MOVED` (truthy, so the text is not
  ingested as a new request). REVISE_ENABLED=false: the pre-agentic prompt/toast and `SYNTHESIZED` + `regen_note`.
- Confirm buttons: `rvc:y|n:{item id}:{token}` (`confirm_callback_data`, <= 64 bytes, checked at build time;
  `parse_confirm_callback` is strict and ignores anything else, including a missing token). The handler calls
  `confirm_revision` and maps `ConfirmResult` to a toast and an edit of the question (buttons removed).
- Every handler is registered through `operator_only`; a non-operator tap changes nothing and gets no reply.
- `build_preview(item, limit=4096, with_changes=True)` -> `Preview(text, standalone)`; `build_preview_text` is its
  `.text`. The latest run (for the item's CURRENT deck: `record.latest_run`) is shown on top; the caption/stats part is
  the unchanged legacy text. An item with no current run previews exactly as before. A caption edit re-sends without the block.

### Dashboard (`scripts/dashboard.py`)

- `POST /item/{id}/regen` and `/say`: `request_revision(..., surface="dashboard")` when REVISE_ENABLED and the item is
  `awaiting_approval | revision_confirm | failed | rejected`; the old guards (no brief, published) stay; an item in
  `revision_requested` is refused (409); other statuses keep the legacy recompose.
- `POST /item/{id}/revise_yes|revise_no` with form field `token`: `confirm_revision`; anything but YES / NO is a 409 carrying the toast sentence.
- Item page: "waiting on your answer" card (pending plan + Yes/No), "revisions" card (every run, escaped, same wording as Telegram).
  `items.last_error` is replaced by a closed sentence while the item is in `revision_requested` (it holds `str(exc)`, rule 15).

### Changes after the T302 tester's first pass
- `request_revision` records the tokens of confirms it replaces in `revision_request.superseded` (max 5, cleared with
  the request); `confirm_revision` answers `STALE` for those while the newer request is alive (D1).
- `model.theme_request_drop` makes a note that asks for a theme the rules cannot set emit `BAD_THEME` (unknown name) or
  `THEME_NOT_NAMED`, so `ask_cause` reaches WHICH_THEME (D2). `ground` reports a negated rules-only op as `NEGATED`,
  not `RULES_ONLY` (D3).
- D289: a planner failure with no rules actions no longer falls back to a whole-deck rebuild; the run is an
  ask-the-operator run with `plan_failed: true` ("I couldn't work out what to change — nothing was touched."). `/skip`
  and overflow still compose. `cause: "plan_failed"` remains only in older records.
