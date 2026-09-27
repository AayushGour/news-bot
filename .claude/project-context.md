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

### D79 — T125 MEASURED IT. All three of D75's pre-registered predictions FAILED. (2026-09-23)

The predictions were registered before measurement, which is the only reason this counts.

```
                     as-shipped   -PIPELINE_AUTHORED   -STRUCTURAL   -repo only
golden_current         0.1607        0.1783 (+11.0%)   0.2053 (+27.8%)  0.1547 (-3.7%)
golden_listonly        0.1824        0.1980 ( +8.6%)   0.2446 (+34.1%)  0.1781 (-2.4%)
gap (listonly-current) +0.0217       +0.0197           +0.0393          +0.0234
```

1. *"Excluding PIPELINE_AUTHORED_TYPES moves grounding up on both sets, +0% to +7%"* —
   **FAILED.** Direction right, magnitude wrong: +11.0% and +8.6%, both above the ceiling.
2. *"Excluding all STRUCTURAL_SLIDE_TYPES moves grounding DOWN, further down on the list arm"* —
   **FAILED, and inverted.** It moved **up** +27.8% / +34.1% on both. Hook and CTA slides drag the
   shipped ratio down far harder than `repo` props it up.
3. *"The gap narrows when repo slides are excluded"* — **FAILED.** It **widened**, +0.0217 -> +0.0234.

**Grounded share of distinct 3-grams, by slide class:**
```
content         22.1% / 27.5%   <- what the model actually chose to write
hook+takeaway    9.5% /  7.8%
pipeline CTA     0.0% /  0.0%
```

**Consequence for the programme's headline.** D22's "~82-85% of deck text untraceable" is dragged
down by slides **the model did not author**. The model's own content prose grounds at **22-27%**,
not 16%. So the reported figure **UNDERSTATES** grounding rather than overstating it — the opposite
of what D75's code-walk suggested.

**D80 — D75's repo-`sub` inflation is UNMEASURED, not measured-false.** 0 of 49 repo slides across
both golden sets carry any `sub`, despite **49/49 (100%)** having a name-matched research note with
non-empty `detail` — the fallback's own trigger, fully satisfied every time. Nothing between
`_restore_repo_facts` and the saved doc would erase a `sub` it set, so the golden snapshots almost
certainly **predate that code path**. Flagged by the dev as D65's trap and caught rather than
repeated: a clean 0 was NOT reported as evidence of absence. Regenerating the snapshots is **T126**,
and until it runs no number about that mechanism can be trusted in either direction.

**D81 — Nothing here moves an acceptance criterion yet.** `grounding_ratio`'s four inputs stay
frozen (D76); T125 was measure-only (`git diff -- src/` empty). What it retires is D75's
*directional confidence*, which is itself the useful result: the composition-artifact story is
more complicated, and partly inverted, from what the code-walk implied.

**D82 — The T79 lint gate caught its first real regression within minutes of merging.**
`scripts/measure_grounding_provenance.py` lacked the `# noqa: E402` that `eval_compose.py`,
`dashboard.py` and `build_golden.py` all carry after their `sys.path.insert`. Two errors, caught by
`ruff check` on the merged tree, fixed in one line. Exactly the class D33 said was invisible while
ruff was unpinned and unconfigured.

### D83 — The research harness CAN gate T71/T72 now; T123 is not a prerequisite (2026-09-23)

Three same-config runs on disk give a real noise floor:

```
metric                        3 runs                      verdict
yield_overall                 0.8 / 0.8 / 0.8             ZERO noise  -> gate on it
notes_produced_total          12 / 12 / 12                ZERO noise  -> gate on it
failure_phrase_rate_overall   0.0833 x3                   ZERO noise  -> gate on it
gate_accept_rate_overall      0.7442 / 0.7442 / 0.7209    +/-0.023    -> usable
avg_grounding_ratio           0.561 / 0.537 / 0.494       12% rel     -> DO NOT gate
avg_numeric_density           3.148 / 2.848 / 2.565       19% rel     -> DO NOT gate
items_completed               2 / 3 / 2                   n=1 eff.    -> useless
```

Conditions: **pre-register the delta threshold** (D79's precedent), and fix F1 first or the arms are
provenance-indistinguishable. Residual caution for **T72 specifically**: its fail-open fires only
when a researcher rejects *every* source — only **3 of 15 queries** produced no note, so n~3
trigger opportunities regardless of reps. Bisecting T71 from T72 needs T123, which is exactly what
D4 already says ("bisect only on disappointment").

**D84 — `--reps` is NOT network-free.** Correcting what was assumed when the flag was specified.
Every rep re-fetches the 9 non-2xx sources, because B2's fix correctly refuses to cache them.
"Zero *new* corpus files" is true; "network-free" is not. Warm rep ~536s, so `--reps 5` ~45 min per
arm. Caching buys **reproducibility, not speed** — model-call latency dominates, not fetch latency.

**D85 — Item 502's `NeedsInput` flip is structural and permanent.** `research.py:260` sets
`clauses = []` for every non-`dm` item and `coverage.unaddressed()` returns `[]` on empty clauses,
so items 501/503 can **never** reach `NeedsInput` while 502 (the only `source:"dm"` item) always
can. Reproduced 2/3/2 across three runs. `--reps` makes it **visible** (`_spread` reports
min=2 max=3 stdev=0.707) but only to a reader who looks past the mean.

**D86 — The corpus is now genuinely clean.** Post-fix audit: **51/51 HTTP 200, 0 non-2xx, 0
zero-length bodies**, versus the prior 48x200 + 9x403 + 1x406 + 1x405 = **18.6% poisoned**. All 51
bodies decoded and scanned for block/challenge markers; the 3 containing "captcha" are genuine
articles. Sizes 7.9KB-2.2MB, median 152KB.

**D87 — A fingerprint over config cannot see a CODE change.** T71 edits `research.py`
(`llm.cheap` -> `llm.good`), not `Settings`, so baseline and arm produce a **byte-identical**
fingerprint and byte-identical saved rows. The `Recorder` captures `RecordedCall.role` and then
discards it before disk. This is D6/D56 reopened in the exact shape `prompt_fingerprint`'s own
docstring claims to have closed — a reminder that **provenance must record what actually varied,
not what the config says could vary.**

### D88 — T72's motivating premise was FALSE, and it was the orchestrator's, not the dev's (2026-09-23)

The framing carried from the original investigation — *"rejecting all sources is far likelier to be
a broken gate than a genuinely irrelevant search"* — is refuted by one grep of the very log it
cited:

```
rejected <domain> as irrelevant:     882    explicit, working verdict
relevance gate errored for             8    actually broken gate
every source rejected as irrelevant  232    facets that would fail open
```

Even assuming every one of the 8 errors caused a total-rejection facet, **<=3.4%** of fail-open
firings are gate-failure; **>=96.6%** override a gate that returned a correct, articulate verdict
(sampled: *"The page is about OpenAI's rumored smart speaker hardware, while [subject]..."*).
`_is_relevant` returns `False` for both, so the code cannot distinguish them.

Fix: split `errored` (no verdict) from `refused` (explicit false); fail open only on `errored`.
That keeps T72's intent and **fully restores** the mouse-cursor anti-poisoning guarantee.
Whether the 232 correct-rejection facets deserve recovery by some other route is **T133**.

**Fifth instance of the programme's dominant failure** (D9, D27, D62-as-corrected, D65, now this) —
and the first where the unmeasured premise came from the orchestrator's own brief rather than a
worker's. A grep that took the reviewer one command was never run before the task was written.

### D89 — `grounding_ratio` is valid only for ranking WITHIN A FIXED MODEL

It is 3-gram overlap of output against source: **a correct fact restated in the model's own words
scores 0**. That is a code fact, not a hypothesis. D14 already limited it to ranking; this narrows
it further, because **verbatim-copy propensity is a model-level trait that varies independently of
factual accuracy.** Across a model swap the anchor degrades into a model-idiom detector — which is
precisely what T71 is. Escalated as **T132**.

**But the paraphrase story does NOT explain the T71 drop.** Disaggregated:
```
numeric_density   item 501  +1.3%    item 502 +106%    item 503  -52%
```
The aggregate +32% is carried **entirely by item 502**, 3 of whose 5 notes are non-answers. On item
**503 — the clean control, where the gate accepted 15/15 in BOTH arms so fail-open never fired at
all** — specificity *halved* alongside grounding (-41%). That is the degradation signature, not the
paraphrase signature. ~7/15 arm notes are refusal boilerplate grounding at ~0 by construction, i.e.
arithmetic contamination.

**Confound larger than declared:** the run is local `qwen3:4b-instruct` -> `qwen3.5:latest` (9.7B
Q4_K_M) — a 4B->9.7B jump *across a model generation*. Production is
`nemotron-3-super-120b` -> `nemotron-3-ultra-550b`. Small models copy phrasing, large ones
abstract, so this pairing **maximises** the grounding delta. Directions may survive; **no magnitude
here may be quoted for production.**

**Cheap discriminator, filed as T134:** `eval_research.py` already computes `is_failure_phrase` and
`grounding_ratio` per note but persists only the item mean. Emitting per-note rows plus note length
answers paraphrase-vs-degradation without re-running any arm. Paraphrase predicts grounding falls on
*answering* notes while densities hold and length rises; degradation predicts specificity falls too.
Item 503 currently leans degradation.

### D90 — fail-open notes reach the brief past BOTH downstream defences

`research.py:305-307`'s `confidence != "low"` filter has an `or [all notes]` fallback that
reinstates every note when all are low — exactly item 501's arm (5/5 `low`). And `unaddressed()`
returns `[]` for non-`dm` items because `clauses = []` (D85), so the one component whose prompt says
a null result "is a report of failure, not coverage" **cannot fire**. Measured: item 501 went
**3 real notes -> 1 real + 4 non-answers** and still completed, while `yield` read 0.6 -> 1.0.
A visible failure (missing facet, `Retryable`) was traded for an invisible one (facet answered with
nothing, silent completion). **T131 must land with the bundle, not after it.**

### D91 — The excerpt budget was deleted, not re-cited
`RELEVANCE_EXCERPT_CHARS = 1200` -> `budget_chars_for_role(role="cheap")` = 12,288 local /
131,072 hosted. House rule 2 asks a limit to cite its source; it does not sanction removing the
limit, and the *note-corpus* budget is not the right quantity for a binary relevance verdict.
Measured benefit: `gate_accept_rate` +0.0232 = **one extra accepted source in 43**, inside D83's
noise band. Measured cost: per-item wall clock 157/384/229s -> 218/684/**651**s, with item 503
tripling at identical 15/15 gate counts and identical note counts.

### D92 — `scripts/mutate.py` has no lock and assumes it is the sole writer (2026-09-23)

A background `mutate.py` full-suite pass run concurrently with manual red/green reverts in the same
worktree **raced and left a mutation applied to an unrelated file**:
`src/pipeline/stages/enumerate_items.py` still carried `catalogue-scoring`
(`score_candidate(candidate, repos)` -> `score_candidate(candidate, True)`), uncommitted, on disk.
Caught via `git status --short`, reverted, suite re-verified serially.

**This is the third mutation-harness hazard found in this programme**, and all three share a shape:
the harness reports success while the thing it guards is not actually guarded.
- **D78** — anchors are verbatim source text; a stale anchor silently stops applying while the run
  still reports clean.
- **T127** — `clear_bytecode()` never cleared `scripts/__pycache__`, so a stale `.pyc` failed three
  tests on a byte-identical, unmutated tree.
- **D92** — no lock; a concurrent writer leaves live mutations in unrelated files.

Rule: **never run `mutate.py` concurrently with manual edits or another `mutate.py` in the same
worktree**, and always `git status --short` before committing after any mutation work. An
uncommitted mutation in an unrelated file is exactly the class that survives into a merge unnoticed.

### D93 — `RELEVANCE_EXCERPT_CHARS = 1200` is now MEASURED, not merely cited

`scripts/measure_relevance_excerpt.py` (committed, re-runnable):
```
size= 1200:  gate_accept_rate=0.6667 (10/15 candidates),  wall_clock=455.1s
size=12288:  gate_accept_rate=0.6000 ( 9/15 candidates),  wall_clock=516.0s
```
The **wider** excerpt accepted **fewer** sources and cost more wall-clock — so 1200 wins on both
axes, not merely by being the smallest previously-measured point.

Stated limits, per house rule 10: **n=1 rep, single item, 15 candidates.** One gate call flipped
between runs — the same magnitude D91 called noise on 43 candidates, but a much larger *fraction*
here. Treated as **corroboration of D91, not an independent finding.**

### D94 — T71 ships as a MECHANISM with the default unchanged; the flip waits for T135 (2026-09-23)

The bundle passed review, but the ship decision splits three ways:

- **B3** (excerpt back to 1200) is behaviourally a **no-op versus main** — safe on code review.
- **T72-as-fixed** only recovers facets lost to a genuinely **errored** gate — **8 events in the
  entire log** (D88) — safe on code review.
- **`RESEARCH_NOTE_ROLE=good` IS the behaviour change the measurement existed to justify**, and its
  only measurement showed `avg_grounding_ratio` **-61%** and `items_completed` **3 -> 2**.

So the default ships at `cheap` and flips when **T135** re-measures. **That option exists only
because the dev made role routing config rather than a code edit** — the design choice paying for
itself. Recorded because the instinct will be to read `cheap` as the settled answer; it is not.

**D95 — The bounce-1 code invalidated its own measurement.** The measured arm was `2117cf4`. This
bounce changed behaviour three ways that all move the metrics: fail-open now fires on <=3.4% of its
former population, non-answers no longer satisfy `MIN_NOTES`, and the excerpt reverts to 1200 (D91
measured that alone at 0.6744 -> 0.6512). The `failure_phrase_rate` 0.4667 and the grounding -61%
were **substantially driven by the very notes B1 removes.** The earlier table no longer describes
this branch — which is why T135 is a prerequisite for the flip, not a follow-up.

**D96 — The tri-state's default is REFUSED, and both misclassifications fall SAFE.** Probed across
13 reachable shapes: `{"relevant":true}`->ACCEPTED; `{"relevant":false}`, a missing key (the D18
off-schema shape) and `{}` all ->REFUSED; `Retryable`/`Terminal`/`ValueError`->ERRORED;
`Retryforever` and `CancelledError` propagate correctly. **The only ERRORED producer is the
`except Exception` handler** — exactly the `relevance gate errored for` line D88 counted 8 of. Both
misbins *drop* the source; **neither can re-admit one**, so the refuted behaviour cannot return by
default.

**D97 — A 15-candidate sample cannot resolve a +/-0.023 threshold.** The smallest move 15 candidates
can express is **1/15 = 0.0667 = 2.9x D83's entire noise floor**, so D93's excerpt run sits *below
the resolution of the threshold it is compared against*. It corroborates D91 by failing to
contradict it; it cannot settle 1200 vs 12288 in either direction. Only a full-golden-set `--reps`
run can. **Generalise this before designing any future measurement: check the sample's resolution
against the threshold before running it.**

**D98 — `mutate.py`'s crash-recovery net is voided by concurrency.** Sharper than D92's "no lock":
`scripts/mutate.py:566`'s `INFLIGHT` is a **single-slot, unlocked** record, and `main()`'s
`restore_any_leftover()` unlinks it **unconditionally at startup** — so a second invocation destroys
the first run's only recovery record, and if that run is then killed the mutation stays on disk with
nothing to recover it. The comment at `:561` says the mechanism exists because a leftover `if False:`
once cost an hour. Separately, `check()`'s `finally` restores from its own snapshot, silently
clobbering any manual edit that lands mid-run. Cheap fix: `open(INFLIGHT, "x")` as an exclusive
lock; refuse to start when it exists or when the tree is dirty.

### D34-D42 — T101's original design (2026-09-22, architect). RECORDED LATE — orchestrator error.

These were returned by the T101 architect and cited in later task briefs, but never appended here.
T102 went looking for them, found nothing, and reported it. Recording now; the gap is the
orchestrator's, not any worker's.

**D34 — D5 has an output side, and it is worse than the input side.** "Bytes may be dropped, never
without a trace" was established for model *input* (T70). The same rule binds anything trimming
model *output*: `normalise_slides`' six field caps and four deck-level `MAX_SLIDES` cuts. Worse
than the input side because **the headline actively advertises the missing content** — a reader is
told there are 7 and shown 4. Every trim logs type/field/kept/dropped; `kept + dropped == received`;
**no deck may assert a count it does not deliver.** *Rejected:* a visible "… 3 more" marker on the
slide — a carousel is not a byte stream, and an ellipsis on a published slide is a worse artifact
than a corrected numeral.

**D35 — There is no single slide layout limit; it is theme-dependent and compose cannot know the
theme.** 11 theme files -> 5 CSS styles with different padding, wrapper boxes and `--font`.
`render.py:134-139`: `rotate`/`random` **ignores the model's chosen theme entirely**. Therefore
every compose-time cap is the **minimum across all 11 theme files, measured pre-shrink** (shrink is
the overflow guard's headroom, not compose's budget). Caps are keyed by **(type, field)**, not
field: `bullets` serves both `point` (38px) and `repo` (30px, sharing its body with
logo/name/badge/stars/url).

**D36 — `bullets[:4]` was a guess; `MAX_SLIDES = 10` is a citation. Constants must say which.**
`MAX_SLIDES = 10  # Instagram carousel hard maximum` cannot move and says so. A bare `[:4]` was
indistinguishable from a platform fact. House rule: a limit is cited from a platform constraint or
produced by a re-runnable measurement whose output is pasted into the task log. Never reasoned into
place.

**D37 — SUPERSEDED by D45.** (Enumeration budget derived from `MAX_SLIDES`; D43's user decision
removed its premise.)

**D38 — The compose prompt gives a non-dm `list` item two contradictory closing instructions.**
`compose.py:482` appends *"end with a sources slide"* for every non-dm item, including `list`;
`:502` then says *"a follow slide last"*. If the model obeys the first, list-deck capacity silently
drops from 7 repo slides to 6 — **capacity is currently non-deterministic.** `ensure_links_slide`
already carries every researched URL, so a list deck needs no `sources` slide.

**D39 — A count mismatch is repaired deterministically, never escalated.** The reconciler does
**not** join `check_deck`'s problem list, which fires rebuild-then-`NeedsInput` and would spend
free-tier quota and eventually stall the item on the operator for something with exactly one right
answer. Same precedent and wording as `drop_duplicate_ctas`. Detector in `integrity.py` (imports
only `logging`+`re`); repair in `compose.py`. **Zero added model calls.**

**D40 — `maxItems` is documentation with a chance of enforcement, never the guarantee.** Consistent
with D27. Added because (i) D18 measured that a single schema arm sent alone *does* bind on Ollama —
the hole is specifically the top-level `oneOf` — so a within-arm constraint has real odds on the
weak path; (ii) `rows` already pins `minItems`/`maxItems` per row, so omitting list lengths is
internally inconsistent; (iii) cost is ~100 chars against D20's 6553. `normalise_slides` keeps the
code-level cap regardless.

**D41 — T102 invalidates every saved eval baseline's fingerprint, by design.**
`prompt_fingerprint()` hashes `SYSTEM` + examples (not the schema — D6). Generating the count limits
into `SYSTEM` from the registry changes it. Correct, but T77 must know it is re-baselining across a
prompt boundary. Adding `maxItems` alone would **not** have moved it — a reminder the fingerprint
does not cover the schema.

**D42 — Deferred deliberately, not overlooked.** Count claims in the **caption** and in a
`takeaway`'s `sub` are out of scope: the user's report was about the heading, and the headline case
is the one a reader sees on the image itself. T104's metric measures headlines only, matching the
fix.

### D99 — The measured capacity table CORRECTS the "4 is conservative by 2x" hint (2026-09-23)

`scripts/measure_slide_capacity.py`, all 11 themes, pre-shrink, zero model calls:

```
                MIN across 11 themes     was        note
point.bullets          4                  4        the hint said ~7; 4 is RIGHT
repo.bullets           4                  4
facts.rows             5                  4
compare.rows           6                  4
sources.urls          10                  4        was too LOW
flow.steps             4                  5
kpi.tiles              4                  4
chart.series           5                  6
links.links            7                 10        was too HIGH - could already overflow
```

**The floor is `aurora`/`tidal`**, which nest an extra `.frame{padding:64px 56px}` inside the base
96px/88px slide padding. Most themes (signal, carbon, ember, manuscript, newsprint, orchid,
sketchbook) support **7** bullets — the hand-computed "~7 on signal" was right about signal and
wrong about the worst case, which is exactly why D35 requires the minimum across all 11.

**`links.links` at 10 was an unmeasured guess that could already overflow pre-shrink on 4 of 11
themes**, relying on shrink to rescue it every time. Now honestly bounded at 7.

Verified separately: a 7-bullet/95-char `point` slide **does** overflow pre-shrink on
aurora/blockprint/riso/tidal, and the shrink-then-`Recompose` path rescues it on all 11 — which is
why the cap stays conservative at 4 while AC2 still passes.

`kpi.tiles` deliberately not tested past 4: `base.html.j2` defines only `.kpis-2/3/4`, so a 5th tile
has no grid rule and silently stacks — not the layout being measured, and the prompt already says
"2-4".

### D100 — Item 238: `research_or_enumerate` is an exclusive fork, and it misrouted (2026-09-27)
User report: *"I'd like the system to research parallely on multiple aspects and then curate the
post, i sfeel when i give a prompt, it isn't able to get the appropriate information as needed."*

Parallel multi-aspect research **already exists** (`research.py:356-371` — `asyncio.gather` over
4-5 facet queries under a `Semaphore(research_concurrency)`, plus `_find_background`). It did not
run for item 238.

`__main__.py:109-122` is an exclusive fork:

```python
plan = await enumerate_items(item, llm, http, settings)
if plan.get("intent") == "list":
    return plan          # research() never runs
fields = await research(item, llm, http, settings)
```

One `llm.cheap` `classify()` call (`enumerate_items.py:98`) labelled *"What is jev model, how does
it work, what does it do, how is our different from chatgpt and Claude"* as `intent:list`,
`catalogue:repos`. There is no both-paths option and no path back when the classifier is wrong.

Item 238 is `source="dm"`, and `research.py:351-352` discards `clauses` only for NON-dm items — so
had it routed to `research()`, its 4 correctly-decomposed clauses **would** have driven the facet
fan-out. Correct routing alone would have materially helped this item.

Designed under T155.

### D101 — Confidence measures retrieval, not whether the question was answered (2026-09-27)
`_confidence(kept, wanted)` (`enumerate_items.py:625`):

```python
coverage = min(len(kept) / max(wanted, 1), 1.0)
quality  = sum(c["score"] for c in kept) / (len(kept) * 100)
return int((coverage * 0.65 + quality * 0.35) * 100)
```

Item 238's stored **97** reproduced exactly: kept=4, wanted=4 -> coverage 1.0; quality
(98+93+90+90)/400 = 0.9275 -> `int(97.46)` = 97. "Coverage" means repos-found / repos-wanted.
**Clause coverage is not in the formula.**

Meanwhile `gaps` recorded **all 4 clauses unanswered** — the decomposer and the gap detector both
worked. `gaps` is written to the DB and reaches `compose.py:688` as a prompt hint permitting the
deck to name a gap. It gates nothing. A deck answering 0 of 4 user questions scored 97 and went to
`awaiting_approval` with 8 rendered slides. Designed under T156.

### D102 — The item-238 deck is not fabricated; it answers the wrong question from the wrong source class (2026-09-27)
All four `research` notes are `kind:"repo"`, all carrying the same `question:"Jev model"`, three of
them *awesome-lists*. Every note's `detail` was checked **verbatim against the live GitHub API**
and every repo returned HTTP 200; star counts match within a few days' drift (1815->1819,
7349->7355, 862->863, 726->726). Nothing is invented — the data layer is honest.

The deck reads as empty because the phrase "System One model for typed decisions" appears in 3 of
the 4 repo descriptions, so one marketing sentence is restated across four slides.

Two structural causes:
1. **Source class is unscored.** `score_candidate`/`_score_web`/`authority`
   (`enumerate_items.py:224-328`) have no source-class penalty; `_SUBJECT_STOPWORDS` strips
   "awesome"/"curated"/"list" from *subject matching* only. Three curated lists scored 90+ as
   evidence for "how does it work". T157.
2. **The enumeration path cannot answer how/why even in principle.**
   `synthesize.py:52-60 LIST_SYSTEM` allows **80 words** ("a short framing paragraph... Do not
   describe the items one by one — each gets its own slide") against the news path's 250
   (`synthesize.py:42`). Total prose available to the composer on the list path is 80 words plus N
   repo cards. There is no slot where "how does Jev work" could live. The path is
   search -> score by stars/authority -> one slide per result: no page fetch, no reading, no prose
   synthesis.

### D103 — CORRECTS D102: item 238 was gathered as a list and composed as NEWS, because `intent` does not survive the park (2026-09-27)
D102 said the enumeration path caps prose at 80 words and so has nowhere for "how does it work" to
live. That is true of the list path in general and true of **item 100** (`intent='list'`, brief **66
words**, inside `LIST_SYSTEM`'s 80). It is **not** what happened to item 238, whose brief is **122
words** — over the list cap, inside the news cap. Item 238's `intent` is **NULL**.

Measured across all 239 items: `intent` is `'news'` 142, `NULL` 73, `'list'` 24. Exactly two items
carry `kind:"repo"` research notes — item 100 (`intent='list'`) and item 238 (`intent=NULL`). So 238
is the only item in the corpus that was gathered as a list and composed as news.

Full causal chain, each step verified in source:

1. `classify()` (`enumerate_items.py:98`) returns `intent:"list"`, `catalogue:"repos"`; the stage
   gathers 4 repo notes.
2. `unaddressed()` finds all 4 clauses uncovered, so `enumerate_items.py:446-458` raises:
   ```python
   raise NeedsInput(..., resume_status=Status.TRIAGED, confidence=confidence,
                    fields={"research": notes, "clauses": clauses, "gaps": uncovered})
   ```
   **`intent` is absent from `fields`.** The normal `return {"intent": "list", ...}` at `:460` is
   never reached.
3. `NeedsInput.fields` (`conversation.py:40`, consumed only at `worker.py:119`) is the **only**
   channel that survives the park.
4. `requeue.py:28` clears `intent` on the `Status.TRIAGED` entry — correct in isolation, because
   the stage is expected to rewrite it.
5. The operator replies `/post`. `conversation.py:219` transitions straight to `Status.RESEARCHED`
   writing only `{"answer": None, "question": None}`, **deliberately** skipping the research stage
   (its comment cites item 116, which was parked holding eight notes, re-searched, came back with
   none, and died at synthesize). So the skip is right; the missing key is the bug.
6. `intent` is therefore NULL at compose. Every `item.intent == "list"` branch evaluates false:
   `compose.py:584/643/771`, `synthesize.py:183`, `examples.py:142`, `config.py:85`.

Net: news 250-word brief + news few-shot + news composition, over repo-card notes — a hybrid worse
than either path. This fires **precisely** on the items whose clause coverage failed, i.e. the ones
most in need of careful handling.

Fix is one key at `enumerate_items.py:457` (plus `"intent": "news"` at `research.py:428` for
symmetry — currently harmless there, since every downstream branch tests `== "list"`). T160.

### D104 — Prevalence: the misroute is 1 of 7, not systemic (2026-09-27)
Before designing a fix, measured over `data/app.db` (239 items, 33 `dm`, 15 with clauses): of the
**7** multi-clause `dm` requests, **5** had every clause answered, **1** had some unanswered, and
**1** (item 238) had all 4 unanswered. So the clause machinery usually works and the fix for
T155/T156 should be targeted, not a rewrite of the fork. Recorded so the design is scoped to the
measured rate rather than to one anecdote.

## T155/T156 design — the item-238 fix set (2026-09-27, architect)

- **D105 — The exclusive fork is EXONERATED; the router was the defect.** Rejected making
  `research_or_enumerate` non-exclusive (an `intent` is a scalar; two note sets with different
  `kind` shapes have no defined merge, and all five readers are binary — +~18 calls/item for an
  undefined downstream), and rejected a third `explain` intent on D104's prevalence. Item 238's deck
  shape is explained by the lost `intent` (D103), not by the fork. What IS taken from the fork is a
  zero-cost structural lift: `classify()` moves out of `enumerate_items` (`:332`) into the router, so
  the routing decision stops being made *inside the branch it selects*. The router then holds a plan
  it can re-decide on (D112) and can own `intent` across the park boundary (D107). **+0 model calls**
  — still exactly one classify call. *Option value, deliberate:* if T161's tripwire later shows a
  real misclassification rate, the third intent becomes a one-line change BECAUSE of this lift.
  Files: `__main__.py:110-123`, `enumerate_items.py:330-334`.

- **D106 — T160 alone is a REGRESSION for this item class, and must not ship as a standalone win.**
  Post-T160, item 238 parks with `intent="list"` preserved and `/post` yields a *coherent* deck of
  four awesome-list repo cards for "what is the jev model, how does it work" — list DECK SHAPE,
  notes block, LIST example, `_restore_repo_facts` and `ensure_links_slide` all correctly applied.
  A confidently wrong artifact is worse than an incoherent one. T160 is correct and necessary; the
  quality fix is the re-route (D112), and T160 must land with it or behind it. **Merge fence
  recorded on the board.**

- **D107 — A field that crosses a stage boundary is owned by the ROUTER, not by each raise site.**
  Rejected "remember to add the key": the omission fires precisely on the clause-coverage-failure
  path, i.e. the items most in need of care. Also rejected **auto-deriving `NeedsInput.fields` from
  the stage's return shape**: a `NeedsInput` raised mid-stage has no return dict yet, and
  `enumerate_items.py:419` deliberately persists a *different, cheaper* note set (no logos, no
  extraction) than its eventual return — deriving would persist a shape the stage never produced.
  Instead the router augments `exc.fields.setdefault("intent", ...)` on the way past and re-raises;
  `worker._run_one:117-120` reads `exc.fields` after the stage frame is gone, so this works and no
  raise site inside the stage can forget it. Within a stage, a test enumerates the raise sites.
  Recorded as house rule 14.

- **D108 — `confidence` stays a RETRIEVAL metric; clause coverage gets its own axis; the operator
  sees BOTH.** Three grounds, not preference: (i) `MIN_SET_CONFIDENCE = 45` fires at
  `enumerate_items.py:408`, 34 lines before coverage is computed at `:442`, so folding coverage in
  silently redefines an editorial threshold; (ii) the axes fail independently — item 238 is
  retrieval 97 / coverage 0, while two thin sources answering both asks are the inverse; a number
  mixing two provenances measures neither (house rule 11); (iii) `confidence` is stored per item and
  rendered to the operator, so redefining it re-baselines every value on disk.
  **The load-bearing half is the string, not the schema.** `conversation.py:58-60` showed
  "(confidence 97/100)" in the same message as "nothing answers:" and all four of the operator's
  questions. 97 is the number a human acts on; pressing `/post` was reasonable. `coverage_ratio`
  comes from the SAME `unaddressed` call — **+0 model calls**.

- **D109 — The teaching-phrase check is a TRIPWIRE, never a gate.** `examples.py:146`'s `teaching`
  tuple contains `"what are"`, which matches *"what are the best vector databases"* — a legitimate
  list request, so a veto on it would break the working path. The safe signal is the arithmetic
  coincidence actually observed: item 238 stored `count=4` with `len(clauses)==4`, and
  `INTENT_SYSTEM:70`/`:86` describe the two in adjacent prose while never saying they are unrelated.
  Ships as a WARNING with a named re-entry criterion, per house rules 4 and 10 — and per D65 that log
  line is the only instrument that will ever measure it, so it is load-bearing, not decorative.

- **D110 — `EXPLAINER_EXAMPLE`, `NEWS_EXAMPLE` and `examples.pick`'s whole `teaching` tuple are
  UNREACHABLE under the shipped default.** `few_shot_enabled(mode="list", intent)` (`config.py:85`)
  is true only when `intent == "list"`, and `pick` (`examples.py:142`) short-circuits to
  `LIST_EXAMPLE` for exactly that case. Mutually exclusive, so `block()` is only ever called on the
  one branch whose example is chosen without consulting `teaching` at all. A committed,
  correct-looking detector for "this is a teaching ask" has never run in production, and item 238
  received no worked example whatsoever. **Provable from logic — no prevalence argument, no
  measurement needed.** D36/T36's measured `"list"` default was an experiment over list-vs-news
  items only and never contained this case, so covering it fills a gap rather than contradicting a
  measurement. Flagged for the next eval run regardless.

- **D111 — Curation needs no new stage and no new model call.** The "research in parallel on
  multiple aspects" half already exists (`research.py:356-371`, `PLAN_SYSTEM:183-194`); item 238
  never reached it. The missing half is that **the composer never sees the notes on the news path** —
  `_notes_block` is inside `compose.py:643`'s list branch, so T105/D50's fix did not apply to item
  238, whose composer had a 122-word brief and four URLs. Ships as D57's named arm (config-gated,
  default off, D94's pattern), **+0 calls, +tokens only**. What makes it curation rather than a dump:
  `coverage.py:86-98` already parses the model's clause verdict and **discards everything except the
  matched names** — widening the schema to a per-clause verdict with material indices yields the
  deck's spine from a call already paid for. *Rejected:* a dedicated curation call (+1
  `llm.good`/item) for that exact reason.
  **Honest limit, so this arm is not oversold:** curation cannot fix wrong material. Item 238's four
  notes were four awesome-lists and the brief summarised them faithfully. Only routing (D112) and
  retrieval (T157/T170) fix that.

- **D112 — The re-route is safe ONLY at TOTAL coverage failure.** At 0-of-N the material answers
  nothing, so re-routing risks nothing; at partial coverage it would discard notes that do answer
  something. Scoped to `intent==list`, `source=="dm"`, every clause uncovered; ONE attempt, recorded
  on the item, structurally unable to loop. Cost **+~18 calls** (7-call list path → ~25-call news
  path, D8) on the ~1-in-7 items that already failed — approximately cost-neutral against the
  realistic alternative, since the operator answering resumes at `Status.TRIAGED` and re-runs
  classify + expand + search from scratch anyway. **See D113: that premise is weakened until T169
  lands.**

## Item 238 — three further findings from the DB, which the architect could not reach (orchestrator)

- **D113 — SETTLES architect OQ1, and WEAKENS D112's premise: the operator's help made it strictly
  worse.** The `messages` + `events` tables show **two** coverage failures, not one:
  ```
  14:17:23  triaged -> needs_input   pass 1: ONE of 4 uncovered ("how is our different from chatgpt and claude")
  14:18:12  needs_input -> triaged   operator answered: "How is jev model's Architecture diffrent from chatgpt or Claude"
  14:21:42  triaged -> needs_input   pass 2: ALL FOUR uncovered
  14:22:24  needs_input -> researched  operator: /post
  ```
  **Pass 1 answered 3 of 4.** The operator supplied a genuinely good angle, the item re-gathered, and
  coverage collapsed to 0 of 4. `/post` is hardened against exactly this (`conversation.py:196-206`
  cites item 116 — parked holding 8 notes, re-searched, came back with none, died at synthesize) but
  **the answer path is not**: `conversation.py:230` transitions to `item.resume_status` = TRIAGED,
  re-running the whole gather. Filed P0 as **T169**.
  Consequence for D112: item 238's 0-of-4 was **manufactured** by this re-gather. The true retrieval
  state one step earlier was 3-of-4. So "at 0-of-N there is nothing to lose" describes a state that
  is itself a bug — T162's trigger must not fire on it until T169 lands. D108's operator-authority
  framing is *confirmed* (the `/post` is real and deliberate) and is more sympathetic than assumed:
  the operator had already tried helping once.

- **D114 — ROOT of the retrieval failure the user actually reported: a 3-character subject is
  invisible to the relevance gate.** `_MIN_TERM_LEN = 4` (`enumerate_items.py:133`) drops every
  shorter token, so measured on this code:
  ```
  subject_terms("Jev model")                -> ['model']
  subject_terms("jev")                      -> []
  subject_terms(<the operator's answer>)    -> ['model','architecture','diffrent','from','chatgpt','claude']
  ```
  **The entire relevance gate for item 238, on both passes, was the word "model"** — which all four
  awesome-lists contain, and all four pass `is_relevant` under both the subject-derived and
  answer-derived term sets (verified by running them). This, not the fork and not the lost `intent`,
  is why the material was wrong.
  It generalises: `subject_terms()` returns `[]` for **gpt, llm, rag, api, sdk, JWT, vim, npm, git**,
  and `is_relevant`'s `if not terms: return True` then accepts **everything** — the exact failure its
  own docstring exists to prevent ("the pipeline then published eight developer tools under a
  philosophy request, at confidence 96"). The length floor has a real motivation ("ai" is inside
  "chain", "detail", "email"), so the fix is not simply lowering it. Filed P0 as **T170**.

- **D115 — SETTLES architect OQ4: the 73 NULL-`intent` items do not fund a third intent.** Of 73,
  only **22** ever reached research, and ids 3-23 are pre-migration (all `resume_status` empty, notes
  carrying no `kind` field at all). The genuinely post-migration ones are ids **58, 92, 116, 122,
  126, 238** — every one with `resume_status=triaged`, i.e. every one went through a park+resume,
  which is D103's hole. Only 238 carries `kind:"repo"`. Item **116** — the very case
  `conversation.py`'s comment cites — is in this list with `gaps=2`. So D103's defect has touched ~6
  items and visibly damaged one; the remaining 51 NULL-intent items never reached the research stage.
  Confirms the architect's decision to reject the third intent (D105).

## T157/T162/T169 follow-on design (2026-09-27, architect pass 2)

- **D116 — The relevance gate and the candidate scorer are LIST-PATH ONLY; the explanatory path
  scores nothing.** Grep census: `score_candidate` has one call site (`enumerate_items.py:392`),
  reached only after `:333` returns early for non-list; `_score_web` only via `:237`; `is_relevant`
  only `:389`; `subject_terms` only `:346`. The news path uses a *model* gate (`research.py:630`) and
  picks docs by raw search order (`research.py:511`). Impact: **T157's "intent-relative scoring" is
  unimplementable** — intent is always `list` where the `+10` fires. Also `_note:491`'s `confidence`
  has exactly one reader in `src/` (`synthesize.py:153`, the NEWS blob), reached only via D103's bug
  that T160 closes; `research.py:413` never sees a `_note`. So T157's stated blast radius is void.

- **D117 — A rank property over a homogeneous fixture is invariant to any uniform offset, so T157's
  AC1 tested nothing even before T170.** Four container-class candidates move together under
  "subtract 10 from everything" as readily as under "penalise the container class". Post-T170 those
  four are additionally unreachable for that subject. Rule: a scoring-change AC needs at least one
  candidate of each class it means to separate, **on a subject where both pass the gate** — else the
  gate, not the scorer, is doing the work. *Rejected:* repairing the fixture by inventing an
  on-subject "jev model" candidate — no such candidate exists on the real web, which is *why* search
  returned generic lists; a fabricated one tests a scenario that cannot occur.

- **D118 — CORRECTS an AC the orchestrator wrote: T170 is unreachable by term extraction alone,
  because `is_relevant` is a disjunction.** `:221` is `any(_matches(words, term) for term in terms)`,
  so adding `jev` to `['model']` leaves all four awesome-lists passing on `model` — a disjunction only
  gets more permissive. The **quantifier is part of T170**: a token surviving only because it came
  from the subject is matched as a whole word AND required; longer tokens keep the disjunction. Three
  answers depend on this landing (T157's fixture reachability, T162's live exit, D119). Stated
  tradeoff: a required short token converts a false positive into a false negative ("how does rag
  work" rejects a page spelling out only "Retrieval-Augmented Generation"), which parks at `:408` and
  asks — the honest failure, and the whole justification.

- **D119 — The thin-set exit precedes the coverage exit by 34 lines, so T162's trigger was keyed to a
  state item 238 does not reach.** `:408-420` raises on `confidence < MIN_SET_CONFIDENCE`; `:442-458`
  runs coverage only if that passes. Post-T170 item 238 yields `kept=[]` → `_confidence` 0 → exits at
  `:415`, never reaching coverage. Fix: fire at the thin exit, `source=="dm"`,
  `len(clauses)>=2`, `item.answer is None`. *Rejected:* keying on the **first** coverage verdict —
  post-T169 only the union's verdict exists, and D112's "nothing to lose" is a claim about the
  material, not about which pass produced it. *Rejected:* cancelling T162 — it is the only mechanism
  delivering the capability the user's report asked for. **D106's fence is discharged by T170, not by
  T162.**

- **D120 — The answer path does not clear `research`, so T169 needs no new state and no
  `conversation.py` change.** `conversation.py:231` calls `db.transition` directly and bypasses
  `requeue()`, whose `TRIAGED` entry (`requeue.py:27`) *does* clear `research`/`intent`/`gaps`. So
  "retry fresh" (requeue) and "retry with help" (answer) are **already** distinguishable at the
  storage layer, and the merge belongs inside the stage. *Rejected:* a new `carried_research` column —
  it would duplicate a field the row already holds.

- **D121 — The anti-regression invariant is coverage-monotonicity against the stored `item.gaps`. +0
  model calls.** If the union's uncovered count exceeds `len(item.gaps)`, keep the old set. Needs no
  per-note attribution (`coverage.py:86-98` returns clause strings only) and no second model call,
  because `item.gaps` already records what the old set achieved. Generalises item 116 from an
  anecdote in a comment into a mechanical rule: **a resume may not reduce coverage.** *Rejected:*
  widening the coverage schema to per-clause material indices (D111's arm) — it would block a P0
  behind a gated P2 experiment. **See D128: this invariant is unsound until clauses are frozen.**

- **D122 — A carried note keeps its original `question` verbatim; never re-stamped.** `_note:487`
  stamps `question` from the then-current subject, so leaving it alone records what the note was
  gathered *for* — that field is the provenance trace, and is what stops a carried note reading as
  evidence for the operator's new angle. Same discipline as `FAIL_OPEN_KEY`
  (`research.py:387-400`): mark evidentiary weight, do not discard. Corollary: the union's dedupe is
  **old-first-wins** on `url`, so a same-URL re-find never overwrites the older, truthful stamp.

- **D123 — T169/T170 boundary is by line range, and the ranges are disjoint.** T170 owns
  `enumerate_items.py:120-221` (extraction + matching + quantifier + fail-open); T169 owns `:346`
  (which strings the terms derive from on a resume) and `:382-458` (the merge). No shared line.
  Still serialized by `deps` per D10; T170 first.

- **D124 — `research()` never reads `item.answer` at all.** Three readers in `src/`, all in
  `enumerate_items.py` (`:338`, `:341`, `:346`); `plan_queries`/`_item_context`
  (`research.py:492-501`) build the planner's context from `raw_text` + image descriptions + link
  bodies only. So answering a parked **news** item discards the operator's angle entirely and
  re-gathers from scratch. Separate from D113 (which is about losing notes). Filed as T171. It also
  forces T162's `item.answer is None` guard.

- **D125 — Region map: `enumerate_items.py` is two disjoint chains, not one.** T170 `:120-221` ·
  T157 `:224-263` · T161 `:330-334` · T169 `:346` + `:382-458` · T160 `:457` · T162 `:408-420`,
  `:446-458` · T166 `:437-458` · T173 `:441`. The collision cluster is `{T173, T160, T169, T162,
  T166}` on `:408-458` and must be serial; T170 and T157 are disjoint from all of it and may run on
  their own branch under worktree isolation. T166 gains `deps:T169`. **T170 lands first regardless** —
  it is the release gate (D119).

## Three measurements the architect specified, now run (orchestrator)

- **D126 — SETTLES architect queries 1 and 2: T162's coverage-exit branch has NEVER been reachable,
  and its call bill is currently zero.** Nine items have ever hit a coverage gate (58, 85, 86, 88, 92,
  100, 116, 126, 238). The only **first-pass total** coverage failures are ids **92, 100, 126** — and
  all three have `len(clauses)==1`, below T162's `>=2` guard. So under the designed trigger the
  coverage exit has never fired. Per the architect's own re-entry criterion: implement the **thin**
  exit only and ship the coverage branch as a WARNING. Query 2: only **3** thin-set questions exist in
  the whole corpus (ids 100 ×2, 122), every one on a single- or zero-clause item, so the thin-set rate
  on dm items with `>=2` clauses is **0**. T162 costs nothing today; **T170 generates its entire
  bill**, and it must be re-measured after T170 lands.

- **D127 — SETTLES architect query 3: item 116 lost its notes through the ANSWER path, so T171 is
  P0.** Its `events` show **four** `needs_input -> triaged` transitions (18:24:58, 19:34:19, 01:49:08,
  02:16:37) and then `researched -> failed` at 02:22:44 — the "cannot synthesize with no research
  notes" death `conversation.py:196-206` cites. Its operator typed **prose** — "Post what you have",
  "Post it", "post what you have" — which was read as an *answer*, not as the `/post` command, so each
  one triggered a full re-gather. (`POST` is a literal command today, `conversation.py:104-110`; the
  prose spelling is exactly what its docstring discusses.) T171 raised P1 → P0.

- **D128 — THE COVERAGE TARGET MOVES: clauses are re-derived by a model on every pass and never
  frozen, so the gate can never converge.** `clauses = plan["clauses"]` (`enumerate_items.py:441`)
  takes a fresh `classify()` decomposition each pass. Item 116's six parks reported six different
  clause sets for one unchanged request:
  ```
  pass 1  ["how narcissistic people affect kids in their childhood which turns into illness as adults"]
  pass 2  ["Research about the psychology of narcissistic people and how they affect ..."]  <- the WHOLE raw_text
  pass 3  ["produce search queries for the news item"]                                     <- a PLANNER-PROMPT FRAGMENT
  pass 4  ["psychology of narcissistic people", "how narcissistic people affect kids in childhood ..."]
  pass 5  ["research about the psychology of narcissistic people and how they affect ..."] <- raw_text again, lowercased
  pass 6  ["how they affect kids in their childhood", "which turns into illness as adults"]
  ```
  `requeue.py` does **not** clear `clauses`, so the column already persists — the stage simply
  overwrites it. Filed P0 as **T173**, and it **blocks T169**: D121's monotonicity invariant compares
  the union's uncovered count against `len(item.gaps)`, which is meaningless if the clause SET changed
  between the two measurements. Fix: derive once, reuse for the item's life, re-derive only on an
  explicit `requeue()` that clears them.

- **D129 — Historical only, ALREADY FIXED, recorded so it is not re-filed.** Channel items 85/86/88
  reported uncovered "clauses" that are `PLAN_SYSTEM`'s own facet descriptions ("prior comparable
  events of tech protests demanding AI regulation", "criticism and consequences of...") and, for id
  88, a truncated mid-word fragment ("r considerably harder to dismiss as pure speculation"). The
  non-dm guard now exists on both paths — `research.py:351` and `enumerate_items.py:441` — so a
  channel item gets `clauses=[]` and cannot reach the gate. No task; the stored `clauses` column is
  `[]` for all three, which is why the discrepancy showed up as `uncovered=N/0`.

## T160 built (2026-09-27, senior-dev) — renumbered D130-D132; its proposed D116-D118 collided

- **D130 — The `intent` omission was at THREE raise sites, not two.** D103 named the clause-coverage
  raise (`enumerate_items.py:~447`, item 238's path) and `research.py`'s raise. The build's audit found
  a **sibling** defect: `enumerate_items.py`'s low-confidence raise (`~:415`) has the identical
  omission, in the same function, with the same success shape, reachable by the identical `/post`
  path. All three now carry `intent`. `clauses` was deliberately NOT added to the low-confidence
  raise: clause coverage is not yet computed at that point, so carrying it would persist a shape the
  stage never produced (the D107 rule, applied by the dev rather than recited).

- **D131 — The anti-recurrence mechanism is scoped to ONE key, and that scoping is the point.** A
  codebase-wide "`fields` ⊇ eventual success return" contract was designed and then rejected mid-task
  on D107's reasoning: `enumerate_items.py`'s low-confidence raise legitimately persists a *cheaper*
  note set (no logos, no extraction) than its final return, so forcing symmetry there would
  manufacture a defect. **`intent` uniquely has no such excuse** — it is a verdict decided before
  either function does anything else, not a fetched artifact. So the mechanism is an AST walk over
  `enumerate_items()` and `research()` (the only two functions that ever decide this item's `intent`)
  asserting every `raise NeedsInput(` carries `intent` in a **literal** `fields=` dict, and it fails
  **loudly** on a non-literal dict — proven by its own test rather than asserted in a comment. No
  fourth hand-maintained contract copy. `compose.py`'s raise was confirmed deliberately empty
  (`intent` is already durably persisted by the earlier TRIAGED→RESEARCHED transition, and carrying a
  twice-rejected deck's slides forward would defeat the escalation) and is now pinned by a test so the
  deliberate omission cannot be "fixed" by a later reader.

- **D132 — `research.py`'s raise also drops the researched background image.** `_find_background` runs
  in the same `asyncio.gather` and completes *before* the raise, so the download is already paid for,
  and its result never reaches `fields`. Wasted fetch, and lost hook-slide material on `/post`. Same
  lost-work class as D103, different field. Filed T174.

- **D133 — `scripts/mutate.py`'s lock and dirty-tree refusal live in `main()`, not in `check()`.** The
  build called `check()` directly and legitimately, to avoid `main()`'s dirty-tree refusal mid-task —
  which demonstrates that T151/D92/D98's hardening protects the **CLI**, not the mechanism: `check()`
  takes no lock and unconditionally restores the targeted file's original bytes regardless of tree
  state. Seventh member of the verification-layer family (D78, T127, D92/D98, T140, T149, T151): the
  guard reports success while guarding nothing, this time because the guard is on the wrong side of
  the entry point. Filed T175.

- **D134 — Worktree isolation silently broke the LEDGER, which is the one thing the DONE gate reads.**
  D10 adopted `isolation: "worktree"` after a subagent's `git checkout -b` reverted the board and
  project-context. But `.claude/logs/` lives *inside* the repo, so every worktree gets its own copy and
  a spawned agent's log line never reaches `main`. T150's tester wrote 174 lines to
  `.claude/worktrees/agent-a6438bcd016cf2711/.claude/logs/tester.md` (2765 lines); main's copy was
  still 2591, so **board-lint correctly refused `evidence:logs/tester.md#T150`** and the orchestrator
  had to hand-append the tail (prefix verified byte-identical first, then appended lines 2592-2765).
  The hook did its job — the evidence genuinely did not exist where the board claimed. Every
  worktree-isolated task since T78 has this exposure, and the failure mode is a *refused* done rather
  than a false one, which is the safe direction but still manual. Filed P1 as **T177**.

- **D135 — T100/T104 landed. Main is `94d39cc`, 1190 passed, ruff 0.16.9 clean.** Tester PASS on all 8
  ACs (`logs/tester.md#T150`), 72/72 mutations caught, `--rescore` reproduces 39.570 / 42.542 to the
  digit, `git diff --stat -- src/pipeline/quality.py` empty. The merge was **not** clean — three
  additive conflicts (`.claude/logs/senior-dev.md`, `scripts/mutate.py`, `tests/test_integrity.py`),
  each two features appending distinct entries to the same list; resolved by union, landed via the
  tester's own verified merge commit `9f608ca`. Also recorded from that run: the phantom
  `test_every_filler_phrase_has_a_real_non_closing_citation` failure (T158) manifests **only** with a
  partial `data/eval` copy — with all 8 `RUN_FILES` plus `golden.json` present it does not fail, and
  removing `golden_fewshot.json` reproduces it exactly. So T158 stands, but the correct worktree
  procedure is to copy the full set, not the two golden files an AC happens to name.

- **D136 — T160 has a tester PASS and is still not `done`, deliberately.** T178 verified all 7 ACs
  independently (`logs/tester.md#T178`): reverted the fix itself and measured **7 failed / 3 passed**,
  with the failure showing a resumed item at `intent=None` holding 4 `kind:"repo"` notes — item 238's
  exact shape — then **10 passed** restored. 1200 passed after merging main `94d39cc`; **75/75**
  mutations caught; tree byte-clean before and after.
  It verified AC4 more strongly than the dev had claimed it: `grep -n "llm\|http"
  src/pipeline/conversation.py` returns **zero matches**, so `handle_answer` has no llm/http client in
  scope and a model call on `/post` is impossible **by construction**, not merely untested. That is a
  structural proof where the dev offered a call-count assertion.
  The board line stays at `status:test`. A tester PASS *authorises* `done` (integrity rule 2), but
  D119/D106's merge fence is a separate gate: T160 alone converts item 238's incoherent hybrid deck
  into a coherently-wrong one. `done` is recorded when it lands with or behind T170. **Worth stating
  as a rule: "tester passed" and "safe to ship" are different questions, and the board must be able to
  hold the first without implying the second.**

## T170 built, and it REFUTES D119's premise (2026-09-27)

- **D137 — MEASURED: T170 does NOT make item 238's material fail relevance, so it does NOT discharge
  D106's merge fence.** D119 claimed post-T170 the four awesome-lists fail relevance → `kept=[]` →
  `_confidence` 0 → the item exits at the thin-set raise (`enumerate_items.py:408-420`) and never
  reaches coverage, and concluded "T170 is the release gate … it (not T162) discharges D106". Measured
  on `senior-dev/t170-a-short-subject-is-not-invisible@548ee55`, import path asserted to the correct
  worktree:
  ```
  subject_terms('Jev model') -> ['jev', 'model']       # the fix works
    yibie/awesome-jev                      relevant=True  score=50
    jaredpalmer/kev                        relevant=True  score=40
    Anil-matcha/awesome-jev-by-typesafe    relevant=True  score=50
    v-modal/awesome-jev-tools              relevant=True  score=50
  kept=4  confidence=81  MIN_SET_CONFIDENCE=45   parks at :408? False
  ```
  **All four pass, and that is CORRECT** — every one of their real GitHub descriptions literally
  contains "Jev", so they genuinely are on-subject. The material is **on-subject but wrong-genre**:
  four curated link-lists cannot answer "how does it work". The T170 dev found this against the AC I
  had written, reported it honestly rather than contorting the gate, and I confirmed it independently.
  Consequence: on the measured record **nothing currently in the set prevents item 238 from producing
  a confident deck of four awesome-lists**, and T160 makes that deck more coherent. T162 is unreachable
  by both its candidate triggers; T157-as-reduced reorders without rejecting (removing the `+10` leaves
  ~40-50, above `MIN_ITEM_SCORE = 35`); T169/T173 change what is reported, not what is retrieved.
  Escalated to the architect as the fence-has-no-discharger question.

- **D138 — A worktree's symlinked `.venv` silently resolves imports to a DIFFERENT worktree.** My
  first verification of T170 ran `./.venv/bin/python -c "import pipeline..."` in T170's worktree and
  imported
  `.claude/worktrees/agent-a6438bcd016cf2711/src/pipeline/stages/enumerate_items.py` — the **T150
  tester's** tree, a third agent's code. It reported `subject_terms('Jev model') == ['model']` and I
  was one step from telling the user T170 did not work. The symlink points at the main checkout's
  `.venv`, whose editable install resolves to whichever tree it was last installed from.
  **`pytest` is safe** — `pyproject.toml`'s `[tool.pytest.ini_options]` sets `pythonpath = ["src"]`, so
  the local tree always wins; I reproduced the dev's 1165 myself. The exposure is **ad-hoc scripts
  only**, which is exactly what an agent reaches for when spot-checking. Countermeasure now in every
  tester brief: assert the import path (`assert "<worktree-id>" in module.__file__`) or set
  `PYTHONPATH=src`. Eighth member of the verification-layer family (D78, T127, D92/D98, T140, T149,
  T151, D133) and the first where *I* was the one measuring nothing.

- **D139 — T170's build made two changes the spec did not ask for, both regressions its own fix
  exposed.** (1) `_SUBJECT_STOPWORDS` gained ~40 short grammatical filler words, because with the
  length floor gone a pinned pre-existing test (`test_an_operator_answer_is_searched_first`) began
  raising `NeedsInput` once "try" became a spurious **required** term — the AND-quantifier makes every
  short token load-bearing, so filler that was previously harmless became fatal. (2) Unicode diacritic
  folding before tokenizing, because `subject_terms("jëv mödel")` returned `['del']`, a garbage
  tokenizer fragment. Both are in-fence and were fixed rather than deferred. A hand-maintained
  stopword list is exactly the contract class this repo has been bitten by (T100's `BODY_FIELDS`, D70),
  so T179's brief requires enumerating every added word and probing subjects that ARE short filler
  words — "go", "no", "id", "ai", "ml", "os", "vm" — for unsearchability.

- **D140 — T170's AC4 was proved by term-set equivalence, not live replay, and the substitution is
  the dev's own flagged judgement call.** No cached candidate pool exists for the list path
  (`data/eval/corpus_cache` is empty and belongs to the research path, T71/T72), so "golden set
  byte-identical" could not be replayed. The dev instead showed old/new term lists identical for the 4
  golden list items (26, 29, 30, 34) reconstructed from `data/app.db`, plus a differential fuzz over
  every fixture candidate x subject pair with 0 mismatches on the long-only slice. T179 is asked to
  judge whether that substitution is sound and, if not, to say what would settle it.
