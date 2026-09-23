# Project context — <project>

The single source of truth for WHAT + WHY. business-analyst seeds it; architect adds design. Decisions are appended by the pen-holder (spawner/architect/PM) — a **spawned** worker returns decisions in its handoff instead of editing this file (single-writer, like the board).

## Goal
<problem + why it matters>

## Users
<who uses this>

## User stories
- As <user>, I want <action>, so <benefit>.
  - AC: <testable>
  - AC: <testable>

## Business rules
<rules, edge cases>

## Constraints
<deadlines, tech limits, must-use / must-avoid>

## Assumptions / open questions
<flagged by business-analyst when the client didn't specify>

## Out of scope
<explicit non-goals>

---

## Architecture  (architect)
- Stack: <...>
- Modules: <module → responsibility>
- Data model: <entities, relationships>
- APIs: <endpoint → purpose → in/out>

## Team  (architect — from the team-formation self-review)
- Core roles in play: <which of the 10 this project uses>
- Specialists added: <name → why the 10 didn't cover it → when added>  (none if standard team suffices)

## Decisions  (append-only; only if it constrains future work)
### D1 — <title>  (<date>, <agent>)
Why: <reason>. Alt: <rejected> because <...>. Impact: <what it locks>. Files: <paths>

## Post-quality programme  (2026-09-22, orchestrator + architect)

**Trigger:** user report — "the quality of posts produced by the free models is very bad,
improve it without adding any cost." Hard constraint: free OpenRouter slugs + local Ollama only;
`config.py` refuses to start on a paid slug (T28). Wall-clock and free-tier quota are spendable,
dollars are not.

### The substance funnel (why tuning the compose prompt was the wrong lever)

```
full articles, 20k-45k chars, uncapped (T60)
  -> relevance gate    cheap, 1200-char excerpt   <- 209 facets lost every source
  -> research note     cheap, claim + detail      <- 45k chars becomes 2 sentences
  -> brief             good, 250 words max
  -> deck              good, 3-10 slides
```

The composer's SYSTEM prompt already demands specificity ("Numbers beat adjectives", "never state
a fact that is not in the brief"). It cannot be specific about facts the brief does not carry.

Evidence, `data/pipeline.log`, 2026-09-13 -> 2026-09-22:
```
791  openrouter returning an unusable completion on cheap
221  openrouter rate-limited on cheap
140  cheap calls exhausted 3 attempts -> fell back to local qwen3:4b-instruct
209  "researcher N: every source rejected as irrelevant"
```

### Decisions

- **D2 — Quality work is gated on measurement, and measurement needs TWO harnesses.**
  `load_golden()` (`scripts/eval_compose.py`) rebuilds items at `Status.SYNTHESIZED` from a frozen
  snapshot already containing `research` and `brief`, then calls `compose()` directly. The existing
  eval therefore **cannot see any research-stage change**. Hence T73. Alt rejected: research
  metrics inside the existing harness — it starts after research, there is nothing to measure.

- **D3 — A model slug names one model; auto-routers are banned.** `openrouter/free` selects a free
  model at random per call, so the context window is unknowable and every A/B is noise. Alt
  rejected: keeping it for resilience — resilience is the 3-attempt ladder plus local fallback.

- **D4 — Research fixes ship as ONE measured bundle; compose fixes as single-variable arms.**
  T70+T71+T72 are mutually reinforcing and individually below noise. Bisect only on disappointment.
  Eval cost: 1 shared baseline + 1 research arm + 2 compose arms = 4 runs, not 8.

- **D5 — Bytes may be dropped, never without a trace.** THE hard requirement for T70 and anything
  that trims model input. Reconciles T60 ("removed every content cap") with T61 (context overflow):
  a cap derived from a real model context limit is legitimate; one that hides what it discarded is
  not. Concretely: fair-share across sources, an explicit `[... N chars omitted ...]` marker, a
  per-source manifest, and the invariant `kept + omitted == original` for every source.
  Alt rejected: one-call-per-source-then-merge — 3x the calls; held in reserve.

- **D6 — The 21% golden-set failure was a stale baseline, not a live regression.** Of 5 failures in
  `golden_current.json`, ids 34/9/31 are provider-404s — exactly T54, whose fix landed in `82e2928`
  AFTER that eval ran. Only ids 3 and 33 were real. `enumerations_ok` went 1/4 -> 3/3 over the same
  interval, so it was never a pure regression. `prompt_fingerprint()` hashes only the compose
  prompt + examples, not the model slug or temperature — which is why two materially different runs
  share the fingerprint `7678671d34f9+fewshot:list`.

- **D8 — Free-tier quota is account-wide; 20 RPM is the real ceiling.** One account-level pool: 20
  requests/min, 1000/day (the >=$10-lifetime-credit tier this account is on), spent by any call to
  any `:free` slug. Splitting across slugs buys **no** extra quota, so routing the note from `cheap`
  to `good` (T71) is quota-neutral. One item already costs ~25 calls (~15 relevance + ~5 notes +
  plan + triage + synthesize + compose), i.e. over a minute of throughput floor before best-of-N.

### Team
Core 10, no specialist. Model *selection and routing* plus eval-harness work, not model
development. Every metric is mechanical: no model judges another model's output, because a judge
shares the blind spots and returns a number that feels like evidence without being any.

### Corrections and additions after live provider testing (2026-09-22, orchestrator)

Three hypotheses were tested against the live OpenRouter API. Recording what held and what did
not, because two of them were wrong and the wrong ones cost a re-plan.

- **D3a stands, and is the durable lesson.** `GET /api/v1/models` `supported_parameters` is
  unreliable. `google/gemma-4-31b-it:free` advertises `structured_outputs: true` and then
  returned `2 + 2 = 4` against a schema whose enum was `["banana"]` — real content, real
  violation. Probe enforcement, never trust the metadata. This is what T82 is for.

- **D7 was RIGHT about the symptom and WRONG about the cause.** The hypothesis was that
  `MODEL_GOOD` ignores `response_format`, so the deck was being built with no structural
  enforcement. Live testing refuted it:
  ```
  banana test (enum contradicting truth):  finish_reason=error, content=None, 3/3 reproducible
  realistic nested schema:                 conforms 3/3
  real SLIDES_SCHEMA, 3 reps:              3/3 parse OK
  ```
  The `finish_reason: error` only appears on an *impossible* constraint (single-value enum that
  contradicts the truthful answer), which never occurs in production. The incumbent model is
  not broken.

  **The actual cause is the schema.** `SLIDES_SCHEMA` sets `required: ["type","headline"]` with
  every type's optional fields legal on every other type, so a `compare` carrying `series` and
  no `rows` is fully schema-VALID. `normalise_slides` then enforces the real contract via
  `required_any` and silently discards the slide. The model never violated anything; the schema
  permits the garbage and the code throws it away. Reproduced live: first of three reps against
  the real schema emitted `compare_missing_rows=1`.

  Consequence: T78 (discriminated union per slide type) promoted to P0 with no model dependency;
  T80 demoted to P3 (an A/B, not a fix); T81 demoted to P2 and now depends on T78's outcome.

- **D9 — Candidate slugs, live-tested against the REAL `SLIDES_SCHEMA`, 2026-09-22.**
  ```
  nvidia/nemotron-3-ultra-550b-a55b:free   3/3 OK   incumbent good; 1 rep emitted compare w/o rows
  nvidia/nemotron-3-super-120b-a12b:free   1/3 OK   2x NO_CHOICES - WORSE than incumbent
  dots-studio/dots-3-note-preview:free     2/2 OK   clean; 512000 ctx
  ```
  Note the trap this nearly caused: `nemotron-3-super-120b` passed the trivial banana test
  (ENFORCED) and was pinned as the new `cheap` default by T69 on that basis — but it fails 2 of 3
  on a large real schema. **A toy-schema probe does not predict large-schema reliability.** T82's
  preflight probe must use a realistically-sized schema, not the banana schema, or it will bless
  a model that cannot do the actual job. Flagged for T82 and for re-checking T69's pin.

- **D10 — Parallel devs sharing one working tree is a data-loss hazard.**
  Two senior-devs spawned in parallel each ran `git checkout -b`; branches do NOT isolate an
  uncommitted working tree. One agent's checkout reverted the orchestrator's writes to the
  shared board file mid-session, and each dev could see the other's uncommitted files.
  CLAUDE.md's size-L rule says "branch-per-task", which is necessary but NOT sufficient.
  Corrected practice: spawn parallel code tasks with worktree isolation (`isolation: "worktree"`
  on the Agent tool), so each dev gets a physically separate checkout. Applied from T78 onward.

- **D11 — The substance bottleneck is the FALLBACK path, not the primary one.**
  Measured 2026-09-22 against the real `NOTE_SCHEMA` at a 40,274-char payload (three full
  articles — exactly what `docs_per_query=3` uncapped produces):
  ```
  nvidia/nemotron-3-super-120b-a12b:free   3/3 OK   correct fact extracted
  dots-studio/dots-3-note-preview:free     3/3 OK   correct fact extracted
  nex-agi/nex-n2.5-mini:free               3/3 OK   correct fact extracted
  nvidia/nemotron-3-ultra-550b-a55b:free   3/3 OK   correct fact extracted
  ```
  40k chars is ~10k tokens against 262144-token windows, so **OpenRouter never truncated
  anything** and the hosted models extract the right facts reliably. The original "uncapped
  article text overflows the context" framing is therefore wrong for the primary path.

  What remains true: `num_ctx_cheap=8192` cannot hold 10k tokens, Ollama drops the overflow
  silently, and the log shows 140 fallbacks to local qwen3:4b in 9 days. So the chain was:
  `openrouter/free` auto-router returns garbage (791 unusable completions) -> 3 attempts burned
  -> fall back to a 4B local model -> feed it a corpus it cannot hold -> generic note -> thin
  brief -> dull deck.

  Consequence: **T69 (pinning `cheap`) is the highest-value single fix in the programme**, because
  it removes the trigger rather than mitigating the symptom. T70 narrows to making the fallback
  correct when it does fire, and still closes T61. It should NOT constrain the primary path,
  which testing proves is healthy.

  This also means the expected quality gain is mostly *reliability* — fewer items silently
  researched by a 4B model on a third of the sources — rather than a better ceiling on a good day.
  Worth stating plainly so the eval is read correctly: average substance should rise while the
  best-case deck barely moves.

- **D12 — T69's pin is validated for its actual tier.** `nemotron-3-super-120b` failed 2/3 on the
  large `SLIDES_SCHEMA` but scored 3/3 on `NOTE_SCHEMA` at 40k chars. The `cheap` tier never sees
  `SLIDES_SCHEMA` (that is `good`/compose only), so the pin stands. The general rule from D9 is
  unchanged and still matters for T82: probe each slug against the schema *its own tier actually
  uses*, at a realistic payload size — not a toy schema, and not another tier's schema.

### Review findings, T84 (2026-09-22, reviewer)

- **D13 — The `openrouter/` namespace ban is deliberately over-broad. Do not narrow it to a slug
  list.** T69's in-code comment justified the ban with "no model is actually published under this
  org", which is **factually wrong**: OpenRouter ships genuine single third-party models under its
  own namespace as cloaked/stealth releases — `openrouter/quasar-alpha`, `openrouter/optimus-alpha`,
  `openrouter/horizon-alpha`/`-beta`, `openrouter/sonoma-dusk-alpha`/`-sky-alpha`. They are free and
  are not routers. The **behaviour is still correct**: a cloaked model is a deliberately
  unidentified upstream, withdrawn without notice, which is exactly the irreproducibility the guard
  exists to prevent. But the justification must be fixed, or a future dev who knows about
  optimus-alpha will read the comment as mistaken and "fix" the check by narrowing it to an
  enumerated slug list, reopening the hole.

- **D14 — `grounding_ratio` is trustworthy for RANKING, not for any absolute number.**
  The shipped implementation builds 3-grams over a joined blob, so every adjacent fragment pair
  invents two 3-grams present nowhere in the deck. On `golden_current`: **798/2533 = 31.5% of deck
  3-grams are join artifacts**; a deck copied verbatim from its brief scores **0.53, not 1.0**.
  Corrected per-fragment, grounding is **0.1453 vs the reported 0.0992** (+46.5% relative).
  Spearman rho between broken and corrected across the 24 golden decks is **0.962** (only 3/24
  decks move >=3 ranks), so best-of-N selection and arm-vs-arm A/B would mostly reach the same
  verdict — but the absolute figure is wrong and the headline claim "~88-90% of deck text is
  untraceable" is overstated. Corrected it is **~85%**, which is still damning, so the programme's
  direction survives. **No acceptance criterion may cite 0.099/0.125.** Fix is T68 bounce 1.

  Two caveats that survive the fix, both load-bearing later:
  - The denominator is deck-only, so items with longer briefs have more chance to match. Valid
    within a fixed golden set; **not valid across item sets** — which directly constrains T73's
    research-stage eval.
  - The artifact rate varies with deck shape, partly double-counting `bare_bullet_ratio` in the
    composite.

- **D15 — Record a metric-set version with every baseline.** Composites are comparable only within
  one `FILLER_PHRASES` generation. The stub tuple is currently *included* at a constant 1.0, adding
  a flat +11.1 to every composite and compressing the remaining spread by x0.889. Ranking is
  immune to a constant offset, but when T74 populates the list every previously recorded composite
  silently becomes incomparable — **and T77 re-baselines straight across that boundary.** Likewise
  the documented weights are never the effective weights: `code_preserved` is `None` on 100% of
  both golden sets, so every run to date really used grounding **0.333**, not the documented 0.30.

- **D16 — The guard does not run where it is most needed (T85).** `validate_for_run()` is invoked
  only from `src/pipeline/__main__.py:233` and `scripts/preflight.py:81`. `scripts/eval_compose.py`,
  `smoke.py`, `build_golden.py` and `dashboard.py` all construct `Settings` and make model calls
  **without it** — so the eval harness, whose reproducibility T69 exists to protect, never runs the
  check. Pre-existing (equally true of T28's paid-model check), so not a T69 regression, but it
  means T69 does not yet deliver its full purpose.

- **D17 — Headline comparisons must split `doc` from `raw` rows.** `golden_current` mixes 19
  validated `doc` decks with 5 unvalidated `raw` completions; `golden_listonly` is 24 `doc` / 0
  `raw`. Like-for-like on `doc` only: **0.1087 vs 0.1255**, not 0.0992 vs 0.1255 — roughly a third
  of the apparent gap between those runs is composition, not quality.

### T78 review findings (2026-09-22, reviewer, verified with real jsonschema)

- **D18 — Ollama does NOT honour a top-level `oneOf`.** Measured 2026-09-22: the full
  `SLIDES_SCHEMA` posted as Ollama's `format` to `qwen3:4b-instruct` accepted a bodyless
  `compare`, a `compare` carrying `series` with no `rows` (the exact target bug), a bodyless
  `point`, and a `point` carrying `tiles`+`stars`. A **single arm sent alone does bind** —
  `additionalProperties: false` is enforced — so the hole is specifically the top-level union.

  Consequence: with `LLM_PROVIDER=openrouter` and `MODEL_GOOD_FALLBACK` set to a local GGUF,
  compose runs **unenforced** on every OpenRouter failure. `normalise_slides`' body check is
  **load-bearing on the local path and must never be removed.** T82's enforcement probe must
  therefore cover the Ollama/local path, not only OpenRouter slugs — and should test both `oneOf`
  and sibling-`anyOf`, the two least portable constructs (OpenAI strict mode supports `anyOf` but
  not `oneOf`).

- **D19 — Hand-rolled schema assertions cannot validate a schema.** T78's `_schema_allows` helper
  reimplements JSON Schema semantics rather than mirroring the builder, so it is not a tautology —
  but it is structurally blind to value types, to `oneOf` exclusivity, and to slides our own code
  generates. It therefore **could not find B1 by construction**: `SLIDE_TYPE_FIELDS["links"]`
  omitted `"sub"` while `ensure_links_slide` emits exactly that field, making our own generated
  slide invalid against the schema we send. Real `jsonschema` found it in minutes.
  `jsonschema>=4.23` added as a dev dep (T88); it is a prerequisite for T81.

- **D20 — Schema serialization tripled: 2101 -> 6553 chars.** The `json_object` degradation path
  (`_schema_prompt` does `json.dumps(schema)`) therefore carries the literal 15-arm union — the
  per-type requirement survives a downgrade intact, just unenforced. Cost is **~+1113 tokens per
  compose attempt** on that path, which is a real input to T70/T87's context budgeting.

- **D21 — The contract is now described in three places** and has already drifted: the schema
  (`SLIDE_TYPE_FIELDS`), the `SYSTEM` prompt's per-type prose, and the Jinja templates. The `sub`
  field on `facts`/`links` appears in the templates but in neither the prompt nor the schema.
  Worth a test asserting every field the templates read exists in its type's `SLIDE_TYPE_FIELDS`.

### T68 corrected baselines and metric rules (2026-09-22, verified independently by reviewer)

- **D22 — The corrected T68 baselines.** Reproduced to the digit by a from-scratch
  reimplementation that never imports `pipeline.quality`:
  ```
  golden_current  19 doc / 5 raw   overall 0.145 / 39.570   doc 0.161 / 37.588   raw 0.084 / 47.098
  golden_listonly 24 doc / 0 raw   overall 0.182 / 42.542
  ```
  `grounding_ratio` history, for the record:
  `0.0992` (joined blob, WRONG) -> `0.1453` (per-fragment) -> `0.1447` (+ apostrophe fold, shipped).
  The join fix alone lands exactly on the value predicted at bounce 0; the apostrophe fold is worth
  -0.0006 on real data (principled, near-inert here). **Every figure previously reported — including
  "~88-90% of deck text untraceable" — is superseded. The real figure is ~82-85%.**

- **D23 — A composite is meaningful ONLY with its active weight set named.** Today: 5 terms,
  total weight 0.80. The moment T74 populates `FILLER_PHRASES` it becomes 6 terms / 0.90 and every
  clean deck jumps **+6.4 to +6.7** (`42.54 -> 48.93`, `39.57 -> 46.28`) with **zero quality
  change**. T77 re-baselines straight across that boundary.
  **Rule: pin acceptance criteria on `grounding_ratio` and raw per-metric values, which are stable
  across the boundary. Never on a bare composite.** Quote a composite only together with its
  weight set.

- **D24 — The 5 async test flakes are a pre-existing latent race, not a regression (T89).**
  `tests/test_main.py:162,185` sleep 0.2s/0.15s then assert `client.connects >= 3` / `>= 2` against
  `LISTENER_RETRY_S = 0.01`; `test_render.py` drives real Chromium. Unreproducible idle (735/735)
  and under 3 concurrent runs plus 8x hammering at load 34-50, but real by construction. Scoped to
  the tests, not to production code.

- **D25 — `pipeline.quality` has exactly one importer** (`scripts/eval_compose.py`), so there is no
  pipeline runtime coupling until T75 wires best-of-N. Keep it that way: the no-model-judge
  invariant is enforced structurally — the module imports only `re` and `unicodedata`.

- **D26 — Latent bug carried to T74 (filed as T93).** `filler_count(doc, phrases=FILLER_PHRASES)`
  binds the default **at import**. After a runtime reassignment of `quality.FILLER_PHRASES`,
  `score_substance` reports `filler_count=0` from the stale default while `substance_composite`
  begins including the term and credits it a flat `1.0` — measured `12.5 -> 22.22` on one deck.
  That is precisely the failure option A exists to prevent. T74's in-place-edit instruction is the
  only thing keeping it latent.

### D27 — No provider on this tier reliably enforces a JSON schema (2026-09-22)

Supersedes the bounce-0 claim that OpenRouter enforced `additionalProperties: false` server-side.
That rested on a **single** observation (returned `compare` keys were exactly
`[headline, left_title, right_title, rows, type]`). Re-run live against the real
`OPENROUTER_MODEL_GOOD` (`nvidia/nemotron-3-ultra-550b-a55b:free`, routed via Nvidia) with
`strict: true`, the same request returned **HTTP 200 with field names nowhere in the schema**:
`subheadline`, `background`, `valueA`/`valueB` objects in place of `rows` 2-tuples, and `items`
in place of `urls`.

Enforcement is therefore **inconsistent** — not reliably present, not reliably absent. Combined
with D18 (Ollama ignores a top-level `oneOf` outright), the conclusion for this whole programme:

> **Schema is a request, not a guarantee — on the primary path as well as the fallback.
> Nothing may depend on the provider honouring it.**

Consequences:
- **T81 promoted to P0.** Validating the completion against the schema it was sent is the only
  mechanism that catches this class at all. `parse_json` currently checks only that the text
  *parses*, so well-formed JSON of the wrong shape flows straight through the retry ladder and
  fails later as a missing key — or silently, as a dropped slide in `normalise_slides`.
- **T82 widens again**: probe enforcement per slug AND per provider AND across reps. A single rep
  is what produced the wrong conclusion twice now (see also D9: `nemotron-3-super-120b` passed a
  toy probe and failed 2/3 on a real schema).
- **`normalise_slides`' body check is load-bearing on every path**, not only the local one.
- Open question for the reviewer: given inconsistent enforcement, is the per-type schema worth its
  cost (2101 -> 6553 chars, ~+1113 tokens per degraded attempt, per D20)? It may be that
  `normalise_slides` + T81 do the real work and the schema is belt rather than braces.

**Methodological lesson, stated plainly because it has now cost two wrong conclusions:** a single
live rep against a free-tier model proves nothing. These models are non-deterministic and the
providers are inconsistent. Any claim about provider behaviour needs several reps before it goes
in this file.

### T70 re-review findings (2026-09-22)

- **D28 — `CHARS_PER_TOKEN` is an assumption, not a measurement.** Safety holds iff actual
  chars/token >= `CHARS_PER_TOKEN`. At the shipped value of 2, the default local path budgets
  `(8192 - 2048) * 2 = 12,288` chars = 6,144 real tokens, plus `RESERVED_TOKENS` 2048 =
  **exactly `num_ctx` 8192**. There is **zero margin in the constant itself** — all slack lives in
  `RESERVED_TOKENS` being generous. CJK on byte-level BPE runs below 2 and can still overflow;
  English prose runs ~4 and leaves ~37% of the window unused. The value was lowered 3 -> 2 on
  reasoning, not data, and two attempts to measure it live were blocked by a local Ollama
  saturated by this session's own concurrent agent worktrees.

  **Resolution path is already in hand (T95):** Ollama's non-streaming `/api/chat` response
  carries `prompt_eval_count`, which `llm.py:348` currently discards after reading
  `["message"]["content"]`. Comparing it to `num_ctx` turns the constant into a self-reporting
  measured invariant with no tokenizer dependency. Fold into T87, which already opens `llm.py`.

- **D29 — Fair-share packing is a SECURITY property, not only a quality one.** Combined with
  `dedupe_by_domain`, it bounds a hostile page to `1/docs_per_query` of the corpus budget, so a
  single adversarial source cannot starve honest ones out of the model's context.

- **D30 — A mutant that survives the whole suite: nothing asserts the packer USES its share.**
  `content_budget = (share - marker_room) // 2` silently halves every truncated source and passes
  all 725 tests. Verified replacement (500 examples, kills the mutant, no false positive):
  ```python
  assert sum(len(p.text) for p in packed) >= min(budget, total_clean) - len(packed) * _MARKER_RESERVE
  ```
  Handed to tester (T94) rather than burning a second bounce. Notable because `CHARS_PER_TOKEN`
  3 -> 2 just halved the budget deliberately — a second, accidental halving would be invisible.

- **D31 — Merge-order collision, known in advance.** T78's branch and T70's branch both rewrite the
  same `dev = [...]` line of `pyproject.toml` (T78 adds `jsonschema>=4.23`, T70 adds `hypothesis`).
  Whichever lands second **will** conflict; resolution is a trivial union of the two lists. T68's
  merge touched `config.py`/`quality.py`/`eval_compose.py` only — no overlap.

- **D32 — A third unmarked trim exists, scope it deliberately.** `research.py:469` does
  `body[:RELEVANCE_EXCERPT_CHARS]` (1200 chars) feeding the relevance gate. It is a deliberate
  excerpt for a yes/no verdict with no overflow risk, but D5 says "anything that trims model
  input", so T90 should consciously rule it in or out rather than leaving it unexamined.

- **D33 — Lint is not reproducible across sessions.** `ruff` is not a pinned dev dependency and
  there is no `[tool.ruff]` config anywhere in the repo, so `uvx ruff` resolves whatever build is
  latest at invocation. That is why three agents reported three different finding counts on the
  same tree. `coding-standards.md` still carries an unfilled `<cmd>` placeholder for the linter
  while CLAUDE.md lists lint-clean as a non-negotiable. Fold into T79: pin a version AND add an
  explicit `[tool.ruff] select` list.

### D43 — USER DECISION: deliver the requested count (2026-09-22)

Asked directly whether a request for "top 12 Rust tools" should yield a deck of 7 honestly
labelled 7. **The user rejected that.** Verbatim:

> "deliver whatever number, if the number is more than 7, then split it into pages (1/2) ext,
> or giving bullet points"

So **the requested count must actually be delivered.** Two acceptable mechanisms were named, with
no choice between them:

1. **Split across posts** — a 1/2, 2/2 series.
2. **Densify — "giving bullet points"** — stop assuming one rich `repo` card per item; carry
   several items per slide, so 12 fit in one carousel.

This supersedes the premise behind **D37**. Deriving `enumerate_items.MAX_ITEMS` from `MAX_SLIDES`
was justified by "we only ever show 7"; if the count is delivered by densifying, the enumeration
budget must derive from the **densified** capacity, not the one-card-per-item capacity — and
`MAX_ITEMS = 8` may not need to move at all.

Orchestrator's read, for the architect to push against rather than accept: mechanism 2 is far
cheaper and contained entirely in compose (a density decision), while mechanism 1 touches the
publish path, per-post approval, item/DB modelling and Instagram sequencing — a genuine feature.
Natural shape: **<= capacity -> one rich card per item** (today's behaviour, the better-looking
deck); **> capacity -> densify** so the full count fits; splitting reserved for counts too large
to densify. Precedent that dense listing works: the existing `links` slide already carries up to
10 URLs on one slide.

### D44 — USER DECISION: capacity measurement is script-only

`scripts/measure_slide_capacity.py` is re-runnable and its table is pasted as task evidence, but
**no new real-browser test enters the suite.** `tests/stages/test_render.py` already drives
Chromium and is one of the five known flaky-under-load tests (T89, D24). T102's acceptance
criterion drops the "renders without overflowing through the real path" suite assertion and keeps
the script-produced evidence table instead.

### T103 redesign decisions (2026-09-22, architect)

- **D45 — `enumerate_items.MAX_ITEMS` is an EDITORIAL cap, not a layout-derived one. Replaces D37.**
  Densify removes layout as the binding constraint, so deriving the enumeration budget from
  `MAX_SLIDES` derives a product choice from a platform fact. What actually binds it: research
  yield (`_extract_things` reads <= 6 documents in ONE call — asking 40 things of 6 pages produces
  filler) and editorial judgement. Default **12**. Invariant `MAX_ITEMS <= dense_ceiling()`
  enforced by test. The current comment on `MAX_ITEMS = 8` is wrong twice — it forgot the links
  slide, and it derives an editorial number from a layout fact.
  **The clamp is also silent today:** `max(3, min(count, MAX_ITEMS))` means an operator asking for
  20 gets 12 and is told nothing — the same promised-a-count-delivered-another defect one stage
  upstream. `plan` must carry `count_requested` and log `requested N, carrying M` at WARNING.

- **D46 — Densify first; split is parked (T106).** `<= rich_capacity` -> one `repo` card per item;
  above it -> several items per slide; splitting reserved for a ceiling the clamp prevents
  reaching. Arithmetic: a 12-item request at ~6 items/slide needs `1 hook + 2 item + 2 links +
  1 follow = 6` slides — it fits with four to spare, and the dense ceiling sits near **n ~ 30**.
  *Rejected:* split-first — it touches `publish/instagram.py`, `publish/tunnel.py`, `models.py`,
  `db.py` (T66's publish_log guarantee breaks the moment one item has three posts), the approval
  flow, the dashboard, and caption/hashtag rules (Instagram counts hashtags per post). L-sized.
  And it still must answer "how many on part 1", so it adds a question rather than removing one.
  **The user's "(1/2)" instinct is honoured INSIDE the carousel**: each dense slide is headlined
  with its range ("Rust CLI tools 1-4 of 12"), and `base.html.j2` already renders
  `{{ loop.index }} / {{ slides|length }}` in every footer.

- **D47 — The dense container is `point` + `bullets`. No new slide type.** A new type costs 3
  registry entries, a template, CSS, prompt prose and **11 theme files**
  (`test_kickers_cover_every_slide_type_the_deck_can_emit` binds `sketchbook.json` to
  `SLIDE_TYPES`). `point` costs none of it and is literally what the user asked for
  ("giving bullet points"). Per item the bullet carries **name - one-line summary · language ·
  stars**; the per-item URL is carried by the existing numbered `links` slide in the same rank
  order, so bullet *k* maps to link *k*. `_restore_repo_facts`' discipline extends to bullets —
  the model writes only the summary, code composes the fact string, so facts cannot be
  hallucinated and rendered length stays predictable.
  *Accepted tradeoff, on the record:* dense slides render under the `point` kicker, which every
  theme sets to "THE DETAIL" — slightly off for a roundup. Not worth 11 theme edits this pass.

- **D48 — One planner, two readers.** The deck's shape is arithmetic, so it is computed before the
  model is asked: `list_deck_plan(n)` generates the DECK SHAPE prompt **and** is what
  `reconcile_list_items` repairs against. Two copies of that arithmetic — one in prompt prose, one
  in code — is exactly how `compose.py:482` ("end on sources") and `:502` ("follow last") came to
  contradict each other (D38).

- **D49 — The honest ceiling is enforced at INTAKE, not in compose.** `MAX_ITEMS <=
  dense_ceiling()` makes the over-ceiling branch unreachable on the normal path, so "I can fit N"
  belongs at classification — before research is paid for — not as a `NeedsInput` in compose. A
  defensive branch remains (deliver the ceiling, log kept/dropped, headline reconciled),
  unreachable in production and unit-tested directly. **This is not "deliver 7 and say 7"** — it
  is a floor on a branch the clamp prevents.

- **D50 — The composer is NEVER given the enumeration's notes. Verified. (T105, P0.)**
  `compose()` builds `parts` from `item.brief`, the images list, and `source_urls[:8]`.
  `item.research` appears in `compose.py` only at :435 (harvest URLs), :565/:579 (`check_deck`,
  post-hoc), :592 (`_restore_repo_facts`, post-hoc) and :593 (`ensure_links_slide`, post-hoc) —
  **never appended to `parts`, never in the prompt.** Meanwhile `LIST_SYSTEM` instructs the
  synthesizer "Do not describe the items one by one — each gets its own slide", so the brief
  carries no item data either. Yet the DECK SHAPE block tells the model to "Copy `owner`, `name`,
  `url`, `stars` and `language` from the note VERBATIM".
  So the model reverse-engineers owner/name from the GitHub URLs in `AVAILABLE SOURCE URLS` and
  **invents the rest**; `_restore_repo_facts` rescues only the cases where its guessed `name`
  happens to match. **Today's rich list deck is partly invention**, and densify is impossible
  without fixing it. Note `source_urls[:8]` also means only 8 URLs appear for a 12-note list.

- **D51 — Capacity is a SURFACE, not a number.** `(type, field, chars-per-item) -> count`, measured
  pre-shrink, minimum across 11 theme files, with the configuration (fill text, per-item lengths,
  sibling fields present) printed alongside — a capacity without its configuration is not
  reproducible. One script run yields the rich cap, the dense cap and the links cap. Honest limits
  to state rather than paper over: the fill is fixed lowercase English, so caps and wide glyphs run
  wider; and the numbers go stale when `base.html.j2`, a slide template or a theme file changes.
  `render.py`'s shrink-then-`Recompose` remains the real enforcement — the registry only stops us
  *promising* what we cannot fit.

- **D52 — Raising the item count adds ZERO model calls.** Verified: `_extract_things` is one
  `llm.good` over <= 6 documents regardless of `count`; the repos catalogue builds notes from
  search results with no model call at all. Per-item cost is HTTP (search, logo fetch), not quota.
  D8's 20 RPM / 1000-per-day ceiling does not constrain this change.

### D53/D54 — USER DECISIONS on the delivered-count redesign (2026-09-22)

- **D53 — `MAX_ITEMS = 15`** (asked directly; user chose 15 over the recommended 12). Editorial,
  per D45 — layout does not bind it (dense ceiling ~30); research yield does. Note the honest
  caveat that came with the question: `_extract_things` reads at most 6 documents in ONE call, so
  15 things share those 6 sources and the thinner entries are a real risk. If T104's metrics later
  show low grounding on high-`n` lists, that is the number to revisit, not the layout.

- **D54 — Deck density is "minimum that fits", and the number ADAPTS to n.** The user chose the
  spread shape (richer copy per item, more slides) over the compressed one. But *spread* is a
  principle, not a constant:
  ```
  n=12: 1 hook + 6 item slides x 2 + 2 links + 1 follow = 10   fits exactly
  n=15: 1 hook + 8 item slides x 2 + 2 links + 1 follow = 12   OVERFLOWS MAX_SLIDES=10
        -> planner raises density to 3/slide: 1 + 5 + 2 + 1 = 9
  ```
  So `list_deck_plan(n)` must compute the **lowest** per-slide density that satisfies
  `1 + ceil(n/per_slide) + links_slides + 1 <= MAX_SLIDES`, never a hard-coded 2. With D53's
  `MAX_ITEMS = 15`, densities of 2 and 3 are both reachable in production, so both need tests.

### T105 review findings (2026-09-22, reviewer)

- **D55 — D50 is confirmed closed in the composer prompt**, by independent reproduction from real
  `enumerate_items._note(repos=True)` output rather than a fixture: a 15-note list prompt carries
  every note's `owner`/`name`/`url`/`stars`/`language`/`detail` verbatim, `stars=0` is preserved
  distinctly from a missing count, and `language=""` is omitted rather than manufactured.
  `AVAILABLE SOURCE URLS` is gated off the list path; the per-note `url` travels uncapped, bounded
  by `budget.pack()`. Worst measured case — 15 x 64,000-char spam READMEs — packs to <= budget with
  **all 15 URLs surviving** (the header sits in the head 70%, so facts survive and prose is cut).

- **D56 — `prompt_fingerprint()` is unchanged by T105, and that is itself a finding.**
  `compose.SYSTEM` hashes to `6bff7bf2db7d` on both sides, so T77 needs no re-baseline for T105.
  But T105 is a **material** change to the list prompt that the fingerprint structurally cannot
  see — it hashes `SYSTEM` + examples only (D6), not `parts`. This *widens* D6's "two materially
  different runs share a fingerprint" problem rather than resolving it.

- **D57 — The non-list exemption is upheld, but on STRUCTURAL grounds, not prompt wording.**
  `synthesize.py:65-75` sends the news path the **full** notes (question/claim/detail/confidence/
  sources) to `llm.good` under a SYSTEM demanding "use only claims the notes support" and
  "attribute every fact inline". So a news brief genuinely is a synthesis produced by a model that
  saw the notes, and "never state a fact that is not in the brief" is a rule the composer *can*
  follow. For lists, the composer was told to copy fields VERBATIM from a note it had never been
  shown — a **structural impossibility**, i.e. guaranteed fabrication. Different class of bug, not
  a smaller dose of the same one.
  **News' residual is lossy relay, not fabrication:** the brief is capped at 250 words, so note
  specifics are compressed away before the composer sees them, and news facts cross two model hops
  (notes -> brief -> slides) where lists now cross one. Handing the composer the notes on the news
  path too is a plausible grounding win — but it is an **eval arm under D4**, measurable, not a
  defect claim. Do not ship it on reasoning.

- **D58 — `synthesize.py` budgets NOTHING. Bigger than the hole T105 closes, and upstream of it.**
  Both the list blob (`synthesize.py:52-54`) and the news blob (`:65-73`) interpolate full note
  `detail` — measured up to ~64,000 chars for a spam README — straight into an `llm.good` call
  with no `pack()`, no cap and no trace. A D5/T61 violation sitting one stage above compose.
  Filed as **T108**.

- **D59 — `RESERVED_TOKENS = 2048` was sized for research's scaffold and is wrong for compose.**
  Compose's own scaffold is **6,737 tokens** (`SYSTEM` 6,840 chars + `SLIDES_SCHEMA` 6,635 chars) —
  3.3x the reserve — before the few-shot block, brief and completion. On a pure-Ollama config the
  worst case reaches ~21k tokens against `num_ctx_good = 16384`, i.e. a silent tail-drop that would
  take the DECK SHAPE block (appended last) with it. Not currently reachable: the live `.env` is
  `LLM_PROVIDER=openrouter`, where the budget is 131,072 chars against a 262,144-token window and
  failure is loud. Needs a **per-stage reserve** — filed as **T109**, paired with T108.

- **D60 — `pack()`'s U+E000 sanitization extends to any caller that makes a whole rendered record
  one `Source.text`.** Probed, not assumed: sentinels planted in `detail`, `name`, `language` and
  inside the `url` query string were all stripped in one pass, and `marker_present()` on the real
  compose prompt returns False. `Source.id` is pipeline-generated (`str(i)`), not attacker
  reachable. **This is now the pattern for admitting untrusted text into a prompt.**

- **D61 — Three renderings of `item.research` into prompt text is NOT a DRY violation.**
  `_notes_block`, `synthesize`'s list listing and `synthesize`'s news blob differ materially in
  field set and format; extracting a shared parameterized renderer would be worse, not better.
  Recorded so a future reader does not "fix" it.

### D62 — D23's projected composite jump was WRONG. Measured instead. (2026-09-23)

D23 projected that populating `FILLER_PHRASES` would move the composite from 5 terms/0.80 to
6/0.90 and lift **every clean deck by +6.4 to +6.7** with zero quality change
(`golden_current 39.570 -> 46.285`, `golden_listonly 42.542 -> 48.926`), derived in closed form
as `(S*0.80+10)/0.90` and independently confirmed by reviewer and tester.

The arithmetic was right; **its premise was not.** It assumed every deck measures **zero** filler,
so the new term would always contribute a full 1.0. An honestly harvested blocklist finds real,
nonzero filler on most decks — this account's decks carry a mandatory CTA slide, so phrases like
"follow for more" and "screenshot this slide" appear almost everywhere. Measured:

```
                  composite v1 -> v2        grounding_ratio
golden_current    overall 39.570 -> 40.285   0.145  unchanged
                  doc     37.588 -> 37.684   0.161  unchanged
                  raw     47.098 -> 50.172   0.084  unchanged
golden_listonly   overall 42.542 -> 41.550   0.182  unchanged   <- moved DOWN
```

So the boundary is real but small, and **not uniformly positive**. D23's operational rule still
stands and is now doubly justified: **pin acceptance criteria on `grounding_ratio` and raw
per-metric values, never on a bare composite** — grounding is identical across the boundary in
every single case, while the composite moves in both directions.

**The wider lesson, third instance in this programme (see D9, D27):** a projection derived in
closed form from a premise nobody measured is not evidence. The arithmetic being independently
verified twice made it *look* like evidence.

- **D63 — The D15 weight-set mechanism is `METRIC_SET_VERSION` + `active_component_weights()`**,
  stamped on every `score_substance()` row, now at version **2**. `scripts/eval_compose.py` still
  needs wiring to print it — fenced to T100/T104, the data is ready on every metrics dict.

- **D64 — The golden set is unusually factual, so classic hype filler is absent.** "Game-changer"
  and its kin essentially never occur. The 28 harvested phrases are CTA boilerplate, generic
  section headers and hedge prose. Every candidate was verified programmatically against real
  `doc`-validated deck fields before inclusion — which caught a near-miss where source text uses
  U+2011 non-breaking hyphen that does not substring-match an ASCII `-`.

### D65 — T96's premise is weaker than reported: the frequency is UNMEASURED (2026-09-23)

The "answer arrives in `message.reasoning`" finding was recorded and relayed upward as "4 of 8
reps". That number is real but was **misattributed and drawn from the wrong arm**:

- It is the **T78 reviewer's** measurement, not T96's. T96's own ~25 live reps across three
  payload shapes did **not** reproduce `content == ""` at all.
- More importantly, the 4/8 is **Arm A — the deliberately WEAK-prompt arm** of that experiment.
  The 10 reps under the **real production prompt** (Arm B 5/5 valid, Arm C 4/5) showed **ZERO**
  empty-content responses.

So the production-path frequency of this quirk is **unmeasured**. The fix is still correct — the
sibling `content`/`reasoning`/`reasoning_details` field shapes were confirmed live, and the
failure mode (burn 3 attempts, drop to a 4B local model, discard an answer that was present) is
real. But it must not be presented as recovering a large share of the 791 logged unusable
completions. The WARNING the fix emits is the **only** instrument that will ever measure its real
frequency — which is why that log line is load-bearing, not decorative, and why a mutation test
pins it.

**Fourth instance of the same programme-wide failure** (see D9, D27, D62): a number that was
accurate *within its own experiment* got carried into a different context where its premise no
longer held. D62 was a premise nobody measured; this is a premise measured under conditions that
do not match production. Both read as evidence.

- **D66 — The `_ollama` empty-completion guard (T116) is safe, verified by call-site walk.** The
  only two `schema=None` call sites are `synthesize.py:55` and `:75`, and both already do
  `brief = (brief or "").strip(); if not brief: raise Retryable(...)`. `BadCompletion`
  **subclasses** `Retryable`, so worker dispatch is byte-identical — one attempt spent, no
  `log.exception`. Every other call site passes a schema, where `parse_json("")` already raised.
  Net: same class, better message. **Trim one over-claim:** the spare-model benefit exists only
  when `LLM_PROVIDER=openrouter`; on the default ollama provider `_chat` calls `_ollama` directly
  with no ladder and no chain.

- **D67 — A `refusal`-only completion should be CLASSIFIED, never consumed (T117, gated).**
  Consuming it would land "I can't help with that" verbatim in a brief and then a deck,
  laundered as a legitimate answer. But it currently burns all 3 attempts (5s + 10s) plus a local
  call for something that will refuse identically every time. `Terminal` is defensible, yet a
  wrong `Terminal` permanently fails an item and `refusal` has never been observed live here —
  mapping it on a guess is the drift `llm.py`'s docstring forbids. Gated on evidence, and note
  nothing today would tell us it ever fired.

- **D68 — A CoT-then-JSON extraction heuristic must land AFTER T81, never before (T118).**
  Reproduced: `reasoning` carrying prose then a fenced JSON block has its fences stripped
  mid-blob by `_FENCE` (MULTILINE), leaving unparseable text -> `BadCompletion` -> 3 attempts ->
  local fallback. A real quality loss. But the only thing that could catch a wrongly-shaped
  extraction is **T81, which does not exist yet**, so a find-the-JSON heuristic landing first
  would hand `normalise_slides` a candidate with no shape check — strictly more silent
  wrong-shape flow, the exact class D27 promoted T81 to P0 to close.

### D69 — CORRECTION to D62: the projection was exact; the METRIC'S SCOPE is the defect (2026-09-23)

**D62 recorded D23's projected composite jump as wrong, and called it a third instance of
"confident conclusion from an unmeasured premise". That framing was itself wrong.** Recomputing
v2 with `filler_count` forced to 0:

```
golden_current   46.285     D23 predicted 46.28
golden_listonly  48.926     D23 predicted 48.93
```

**Exact to two decimals.** D23's arithmetic AND its premise were both correct: "a clean deck
measures zero filler" is true **of model-authored prose**. The measured v2 numbers
(40.285 / 41.550) diverged only because `filler_count` counts **the pipeline's own hardcoded
strings**, not the model's writing.

So the lesson is not "the premise was unmeasured". It is: **nobody measured what the metric's
SCOPE would include.** D9, D27 and D65 remain genuine instances of the unmeasured-premise
pattern; D62 is not, and should be read as superseded by this entry.

### D70 — `filler_count` measures the pipeline, not the model

```
                  filler hits   on structural slides   of which pipeline literals   on content slides
golden_current         51            48  (94%)                   40                     3  (6%)
golden_listonly        56            51  (91%)                   43                     5  (9%)
```

Only 3/24 and 4/24 decks carry any filler on a content slide. Every `follow` slide scores exactly
2 hits — one mandatory two-line CTA. The four dominant phrases are literals at `compose.py:980-981`
and `compose.py:1083-1084`, written by `ensure_closing_slide` and `ensure_links_slide`.

Three grounds, all fact rather than preference:
1. `quality.py:18` states `deck_body_text` is "every prose fragment **the model composed**".
   That is false for a slide the pipeline wrote.
2. **`bare_bullet_ratio`, in the same module, already excludes `STRUCTURAL_SLIDE_TYPES`** with
   verbatim the rationale "isolates the slides where the model actually chose". The module is
   internally inconsistent.
3. **The signal is perverse.** Decks where the pipeline inserted its literal follow slide average
   **2.71** filler vs **1.30** where the model wrote its own — the deck is penalised ~2x for a
   repair the code performed.

Effect of excluding structural slides: composite **+5.68 / +6.90** — larger than the entire
v1 -> v2 delta the task was documenting.

**T77 must stay pinned to `grounding_ratio`** (identical across the boundary in all four cases) and
must NOT use `avg_filler_count`, ~90% of which is `ensure_closing_slide`'s own text.

### D71 — The citation audit: 28/28 real, 1 miscited

All 28 harvested phrases were verified against 2057 prose fragments across 93 decks in 9
`data/eval/*.json` files, at their exact cited file+id+slide-type. **Zero fabricated.** 27 exact;
`"stay updated on"` is real (11 doc hits) but first occurs at `golden_current id=21 follow`, not
the cited id=26.

### D72 — NFKC does not fix U+2011 or U+2019

Recorded because a future reader will reach for it. NFKC maps U+2011 to U+2010 — still not ASCII
`-` — and does not touch U+2019 at all, so `_fold_apostrophes`' explicit `.replace()` is
**load-bearing**, not decoration. Decks also contain U+202F narrow no-break space, so a CTA
written with one would silently not match. Nothing currently enforces that `FILLER_PHRASES` stays
ASCII.

### T74 escalation — architect design, D73-D78 (2026-09-23)

**D73 — `filler_count` is scoped to model-authored slides via a STATED PARTITION.**
`STRUCTURAL_SLIDE_TYPES` splits into `PIPELINE_AUTHORED_TYPES {links, sources, follow}` and
`MODEL_WRITTEN_MANDATED_TYPES {hook, takeaway, repo}`; `filler_count` excludes only the former,
`bare_bullet_ratio` keeps the union and is behaviourally unchanged. The boundary is a structural
fact, not a judgement: every pipeline-written prose literal is emitted by `ensure_closing_slide`
(`compose.py:975-987`) or `ensure_links_slide` (`compose.py:1103-1108`) onto one of those three
types; no pipeline function writes a headline or `sub` onto a `hook` or `takeaway`.
*Rejected:* excluding all six — it discards the hook hits ("worth studying", "curated toolkit",
"ranked by relevance"), the metric's only real signal and the closest match to the user's original
complaint. *Rejected:* reporting both halves — the structural half is a function of deck shape,
already measured by `bare_bullet_ratio` and `shape_score`, and D14 flagged double-counting.

**D74 — `filler_count` LEAVES the composite (METRIC_SET_VERSION 3) and becomes a reported
tripwire.** ~7 genuinely model-authored hits across 48 decks makes the term ~1.0 almost
everywhere: a constant offset with near-zero discrimination that compresses every other term's
spread — the same flat-credit failure the empty-list exclusion existed to prevent, reached from
the other side. Kept raw in `score_substance()` with a written re-entry criterion (re-admit at
filler density >= `FILLER_DENSITY_TARGET` on >=10% of decks), because D64's "this corpus is
unusually factual" means its absence here is not evidence of absence in production.
**The payoff:** v3 is arithmetically the same 5 terms at 0.80 as v1, so **D22's 39.570 / 42.542
are LIVE AGAIN**, the D15/D23 metric boundary is **closed rather than crossed**, and D70's
+5.68/+6.90 scope effect never reaches a published number. T77 no longer re-baselines across
anything.

**D75 — `deck_body_text` is not provenance-aware, and the distortion is BIDIRECTIONAL.**
The general form of D70. Of the seven metrics: `hook_similarity`, `bare_bullet_ratio` and
`code_preserved` are provably unaffected; `filler_count` was ~90% pipeline text; `numeric_density`,
`proper_noun_density` and `grounding_ratio` all read it.
**The part that inverts the intuition:** `_restore_repo_facts` (`compose.py:1052-1053`) copies
`note["detail"]` into a repo slide's `sub`; `sub` is in `_SCALAR_TEXT_FIELDS`; and
`source_text_from_item` **is** brief + every note's claim + detail. So pipeline-copied text is
scored as grounded **by construction**. CTA text **deflates** grounding; repo `sub` **inflates**
it, most on the list arm. Therefore "exclude structural slides from grounding" moves
`golden_listonly` **DOWN**, not up.
This lands on the `golden_current` 0.145 vs `golden_listonly` 0.182 comparison as a **second**
composition artifact alongside D17, pointing the same way — and on D22's "~82-85% untraceable"
headline in the direction of **overstating** grounding.
**Mechanism certain (code-walk); magnitude unmeasured; the `sub` fallback's firing frequency also
unmeasured — exactly D65's trap.** Gated on T120, with three predictions pre-registered BEFORE
measurement:
  1. excluding `PIPELINE_AUTHORED_TYPES` moves grounding **up** on both sets, relative +0% to +7%;
  2. excluding all of `STRUCTURAL_SLIDE_TYPES` moves it **down**, and further down on the list arm;
  3. the 0.145 vs 0.182 gap **narrows** when `repo` slides are excluded from both.
  If any prediction is wrong, that is recorded as the finding, not quietly corrected.

**D76 — `grounding_ratio` is the programme's anchor; its four inputs are FROZEN.**
`deck_body_text`/`_slide_text`'s field set, `_fold_apostrophes`' normalisation, `_ngrams`' `n`, and
`source_text_from_item`'s field set. Editing any one silently re-baselines every recorded figure —
the apostrophe fold alone moved it -0.0006, and adding the U+2011/U+202F folds D72 identifies as
missing would move it again. Any such change gets its own task, re-measures both sets `doc`-only,
and publishes old-vs-new side by side before anything cites the new number.

**D77 — B2 resolved with no shared constant and no new import.** The four duplicated CTA literals
leave `FILLER_PHRASES` (after D73 they are unreachable, and an entry that cannot fire misstates
what is measured). House rule 3's agreement test is satisfied by a test in `tests/` — which may
import both modules — that **derives** compose's literals by *calling* `ensure_closing_slide` /
`ensure_links_slide` and asserts the two sets stay **disjoint**. Stronger than an agreement test:
it fires when someone edits the CTA text in future. `quality.py` still imports only `re` +
`unicodedata`, so D25's structural no-model-judge enforcement is untouched.

**D78 — `scripts/mutate.py`'s anchors are verbatim source text and FAIL SILENTLY when stale.**
`:442-447` anchors on the exact `substance_composite` line T74 must change; `:381-385` on
`filler_count`'s empty check. Once edited, those mutations stop applying while the run still
reports clean — **a mutation that cannot apply tests nothing and is counted as coverage.** Any
task editing an anchored line updates the anchor in the same commit and re-runs to confirm both
still fire.

**Correction to the record:** D70's "+5.68/+6.90" remains true of the option D73 rejected, but
under D73+D74 it is **not** the shipped effect and must not be quoted as a delta the programme
realised.
