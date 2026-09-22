# Coding standards — telegram-automation

Owner: architect (seed) · senior-dev refines. Grep this before building; match it.

## Non-negotiables (every project, every agent, regardless of stack)
- **DRY** — no copy-pasted logic. Extract to a shared function/module the *second* a real duplicate appears (not preemptively).
- **No magic strings/numbers** — every literal used more than once, or that carries meaning (status codes, keys, routes, error messages, thresholds), lives in one `constants` module. Nothing else hardcodes it inline.
- **Config in one place** — all env vars read through a single config module (e.g. `config.py` / `config.ts`); rest of the codebase imports from it. Never scatter raw `process.env.*` / `os.environ.*` calls through business logic.
- **Consistency** — one way to do a thing per project (naming, error handling, file layout). New code matches existing patterns; don't introduce a second convention.
- **Reusability + separation of concerns** — one module = one responsibility. Business logic separate from I/O/framework glue. Prefer composition over duplication.
- **Lint clean, enforced not optional** — formatter + linter run and pass before handoff (pre-commit or CI gate, not manual discipline). Zero new lint errors on touched files.

## Stack

- Language + version: Python, `requires-python = ">=3.13"`. `from __future__ import annotations` at the top of every module.
- Framework(s): asyncio throughout. Telethon (channel listener) + aiogram (operator bot) + aiohttp (loopback dashboard) + httpx (all outbound HTTP) + Jinja2 (slide templates) + Playwright/Chromium (slide rendering, in its own subprocess) + aiosqlite (state).
- Formatter / linter: `uvx ruff check src tests scripts` and `uvx ruff format --check`. **Known defect — do not treat a ruff count as reproducible yet:** ruff is not a pinned dev dependency and there is no `[tool.ruff]` section, so `uvx ruff` resolves whatever build is newest at invocation. Three agents got three different finding counts on the same tree (D33). **T79 pins the version and adds an explicit `[tool.ruff] select` list; until it lands, report the ruff version alongside any finding count.**
- Test framework + how to run: pytest, `asyncio_mode = "auto"`, `pythonpath = ["src"]`.
  ```
  uv sync --extra dev            # REQUIRED — a missing hypothesis/jsonschema is a
                                 # collection error and 0 tests run, not a skip (T91)
  ./.venv/bin/python -m pytest -q
  ./.venv/bin/python -m pytest tests/stages/test_compose.py -q   # one file
  ```
  `tests/stages/test_render.py` drives real Chromium and is slow. 5 async tests are a known latent race under full-suite load (D24, T89) — a flake there is not automatically your regression, but say so out loud rather than re-running until green. **Do not add a sixth browser-driving test to the suite** (D44): layout facts are established by `scripts/measure_slide_capacity.py` and its pasted table, not by a suite assertion.
- Naming conventions: `snake_case` functions/modules, `UPPER_SNAKE` module constants, `_leading_underscore` for module-private. Stage entry points are verbs (`compose`, `render`, `research`); pure helpers are nouns or predicates (`usable_images`, `gap_slides`, `code_preserved`).
- Error handling pattern: the taxonomy in `src/pipeline/errors.py` **is** the control flow, and `worker._run_one` dispatches on it. Raise the right one; never raise a bare `Exception` to mean something specific.
  - `Retryable` — this item, transient. Spends an attempt.
  - `Retryforever` — infrastructure is down. Does **not** spend an attempt (a provider 404 is ours, not the item's — T54).
  - `Terminal` — will never succeed; notify the operator.
  - `Recompose(slide_index, reason)` — rendered output did not fit; routes back to `SYNTHESIZED` (not `COMPOSED`, which would loop) with a `regen_note`.
  - `NeedsInput` — the stage cannot do its job honestly. Parks the item and asks the operator. Reserve it for genuine ambiguity; it costs a human.
- Folder / module layout:
  ```
  src/pipeline/            stage-independent core: worker, db, models, llm, config,
                           errors, budget, integrity, quality, conversation, coverage
  src/pipeline/stages/     one file per pipeline stage (compose, render, research, ...)
  templates/               base.html.j2 + slides/<type>.html.j2, one per slide type
  config/themes/*.json     11 theme files -> 5 CSS styles (signal, newsprint,
                           blockprint, aurora, sketchbook)
  scripts/                 operator + eval tooling (eval_compose, preflight, mutate, smoke)
  tests/, tests/stages/    mirrors the source layout
  ```
- Commit style: `type: what changed, in the voice of the defect` — e.g. `fix: a provider's 404 is infrastructure, not a bad request from us`. Present tense, lowercase after the colon, states the *rule now enforced* rather than the file touched.

## House rules this codebase has earned (follow these, they are not preferences)

1. **Constants live once, in the module that owns the concept, and are imported — never re-typed.**
   There is deliberately no `constants.py`: `compose.py` owns `MAX_SLIDES`, `errors.py` owns the
   taxonomy, `budget.py` owns `CHARS_PER_TOKEN`. That satisfies the non-negotiable above *only if
   nobody re-types the value.* Re-typed literals are bugs waiting to happen and two exist today —
   `MAX_LINKS = 10` vs the inline `slide["links"][:10]`, and `MAX_SOURCE_URLS = 4` vs the inline
   `slide["urls"][:4]`, both in `compose.py`. Fix the one you touch.
2. **A constant that encodes a limit must name where the limit comes from.**
   `MAX_SLIDES = 10  # Instagram carousel hard maximum` is good: a reader can tell it cannot move.
   A bare `[:4]` is not — nobody could tell whether it was a layout fact or a guess, and it took a
   user-visible defect to find out it was a guess. If you cannot cite the source of a limit,
   measure it (see rule 4) or do not write it. **Say which kind of limit it is.** A *platform*
   limit cannot move (`MAX_SLIDES`). A *measured layout* limit moves when the CSS moves. An
   *editorial* limit is a judgement call and may be changed by whoever owns the product — deriving
   one from a layout fact, as `enumerate_items.MAX_ITEMS` did, disguises a choice as a constraint.
3. **One registry, many readers — never a second copy of a contract.**
   `REQUIRED_BODY_FIELDS` / `SLIDE_TYPE_FIELDS` are the single source of truth for the slide
   contract; the JSON schema, `normalise_slides` and the tests all derive from them. The contract
   has nonetheless drifted into three places (schema, SYSTEM prompt prose, Jinja templates) and
   that drift has shipped bugs (D21). Derive, do not restate. If you must restate, add a test that
   asserts the two copies agree.
4. **Guessed numbers get measured, and the measurement is the evidence.**
   `CHARS_PER_TOKEN` was lowered 3→2 on reasoning, not data, and left zero margin (D28). A number
   that governs behaviour is either cited from a platform limit or produced by a re-runnable
   measurement script whose output is pasted into the task log. "It looked about right" is not a
   source.
   **A measurement is only evidence if the configuration it was taken in is recorded with it.**
   A layout capacity depends on the fill text, on which sibling fields were present, on the theme,
   and on whether the renderer's shrink pass had run. "12 bullets fit" is not a fact; "12 bullets
   of 72 chars each fit under a 45-char headline with no `sub`, pre-shrink, minimum across all 11
   theme files, fill text `<generator>`" is. The script prints that header above its table and the
   constant's comment cites script + date + configuration. Corollary: **capacity is a surface, not
   a number** — the same field holds more short items than long ones, so measure
   `(type, field, chars-per-item) -> count` and read the row you need.
   Layout numbers go **stale** when `templates/base.html.j2`, a slide template or a theme file
   changes. Touching any of those means re-running `scripts/measure_slide_capacity.py` and
   re-pasting the table, in the same task. Nothing enforces this mechanically — the suite may not
   grow a sixth browser test (D44) — so it is a standard, and the reviewer checks it.
5. **Schema is a request, not a guarantee — on every path.**
   No provider on this tier reliably enforces a JSON schema: OpenRouter returned field names
   nowhere in the schema despite `strict: true`, and Ollama ignores a top-level `oneOf` outright
   (D18, D27). Send the schema *and* enforce the same contract in code. `normalise_slides`' body
   check is load-bearing on the primary path as well as the fallback — do not remove it on the
   theory the schema covers it.
6. **No model ever judges another model's output.** Every metric in `pipeline/quality.py` and
   `scripts/eval_compose.py` is string/regex/set arithmetic. A judge shares the blind spots of the
   thing it grades and returns a number that feels like evidence without being any. `quality.py`
   importing only `re` and `unicodedata` is how this is enforced structurally — keep it that way.
7. **Bytes may be dropped, never without a trace (D5) — and that now cuts both ways.**
   *Input side:* a cap derived from a real model context limit is legitimate; one that hides what
   it discarded is not. Fair-share across sources, an explicit `[... N chars omitted ...]` marker,
   and `kept + omitted == original` per source.
   *Output side (T101):* the same rule applies to anything that trims what the model produced. A
   silent `[:4]` that contradicts the headline rendered above it is the output-side form of the
   same defect. Every trim logs what it dropped, and no deck may assert a count it does not
   deliver.
   *And the count the deck asserts is the count it delivers (D43).* Where the material exists,
   the answer to "too many items for the layout" is to carry more per slide, not to quietly show
   fewer. Dropping is the last resort, and it is still logged with `kept + dropped == received`.
8. **A structural mistake with one right answer is repaired in code, not re-asked of the model.**
   `drop_duplicate_ctas`, `ensure_closing_slide`, `ensure_links_slide`, `_restore_repo_facts` are
   the pattern. A model call to fix something deterministic costs free-tier quota (account-wide
   20 RPM, ~25 calls per item — D8) and still might not comply. Ask again only when the fix
   genuinely requires judgement. **Plan the shape before you ask for it:** when the deck's shape
   is arithmetic (how many item slides, how many per slide), compute it first, generate the prompt
   from the plan, and reconcile the answer against the same plan. One planner, two readers.
9. **Free models only.** `config.py` refuses to start on a paid slug (T28) and rejects auto-router
   slugs (T69, D3/D13). Wall-clock and quota are spendable; dollars are not. Never add a
   dependency on a paid tier, and never narrow the `openrouter/` namespace ban to a slug list.
10. **One live rep proves nothing.** These models are non-deterministic and the providers are
    inconsistent. A single observation has produced two wrong conclusions already (D9, D27). Any
    claim about provider or model behaviour needs several reps before it goes in
    `project-context.md`.

## Tests

- Devs write the tests, then **try to break them** — mutate the code under test (flip a condition,
  shift a boundary, return a constant), rerun, and the test MUST go red. `scripts/mutate.py` runs
  24 anchor-checked mutations for this. Revert every mutation and rerun green before handoff.
- A green suite is not a handoff. Report what you tried to break and what it exposed.
- Property-based tests via `hypothesis` for invariants over generated input (the budget packer's
  `kept + omitted == original` is the model to copy).
- Schema assertions use real `jsonschema`, never a hand-rolled `_schema_allows` helper —
  hand-rolled schema logic is blind by construction and missed a live bug (D19).
- **Layout facts are not tested, they are measured** (D44). The suite may not grow another
  Chromium-driving test; `tests/stages/test_render.py` is already one of five known
  flaky-under-load tests. What the suite *does* assert about layout is structural and browserless:
  that every list-shaped field has a capacity entry, that no code path trims below a planned
  count, that the prompt's stated numbers equal the registry's.

## Security musts
Input validation, no hardcoded secrets, authz on every endpoint. Specific to this repo: the
dashboard is **loopback-only**; access tokens are redacted at every log handler
(`logredact.py`, T63) — any new handler or new log sink must go through it; and the fair-share
packer is a security property, not only a quality one (it bounds a hostile page to
`1/docs_per_query` of the corpus budget — D29).
