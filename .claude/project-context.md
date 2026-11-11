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

## The D106 fence, resolved (2026-09-27, architect pass 3)
Its full return was lost to an API failure mid-response; these are reconstructed from the log line it
wrote first (`.claude/logs/architect.md`, 2026-09-27 [T155/T156 D106-fence]). Renumbered from its
D137-D145 to avoid collision with D137-D140 already recorded. The four DB queries it mentioned did
not survive and are named as an open item below.

- **D141 — CORRECTS D137 (mine): item 238 DOES park post-T170 — at the COVERAGE gate (`:446`), not
  the thin-set gate (`:408`).** I walked the reachability and concluded "nothing in the set stops item
  238 producing a confident deck". Wrong. It reaches `:446` and parks, exactly as it always has —
  D113's `events` show it parked twice and has **never** shipped a deck without an explicit operator
  `/post`. My error was treating "does not park at `:408`" as "does not park". **So D106's harm is
  post-override only**, which lowers the severity considerably: the pipeline always asks first.

- **D142 — The fence is RE-POINTED, not retired, and the word doing the work in D106 is
  "confidently".** The discharge is therefore about the *number the human reads at the moment of
  override*, not about blocking the deck. Two parts: **T163** (accurate figure at the ask) plus new
  **T181** — the approval preview must state residual coverage. `build_preview_text:77-92` names
  triage / notes / slides / sources and says **nothing** about coverage, and it is the **only** human
  checkpoint on the no-park path. **T164** lands behind them. **T160 + T163 + T181 are one landing**;
  T170 lands independently on T179 PASS.

- **D143 — T162's re-route is CANCELLED, reduced to two WARNINGs (P3).** D119 spared it only as "the
  only mechanism delivering the capability the user asked for". New **T180** delivers that capability
  earlier and cheaper: a router **pre-gather** re-decision — **25 calls**, versus T162's 7 + 25 = **32**
  — and with no park at all. Building a post-hoc re-route when the decision can be made before the
  gather is strictly worse on both cost and operator friction. **D112 survives untouched**: it governs
  re-routes, and after this there are none.

- **D144 — T157 is NOT strengthened, and D116's rejection of a genre penalty is REINFORCED.** The four
  candidates being *genuinely* relevant (their descriptions contain the subject token) proves the
  defect is **routing**, not scorer discrimination. A scorer cannot be asked to separate "about Jev"
  from "answers how Jev works" — that is a genre judgement, and the honest fix is to route an
  explanatory request to the explanatory path (T180) rather than to teach the list scorer to dislike
  lists.

- **D145 — There is a SECOND clause decomposer, so T173's freeze must cover both.** `research.py:330-338`
  (`PLAN_SYSTEM`) writes the same `clauses` column as `enumerate_items.py`'s `INTENT_SYSTEM`. If T173
  freezes only the enumeration decomposer, a re-route through the research path produces a **third**
  decomposition of the same request — reintroducing D128's moving target by a different door.

- **D146 — D125's region map was incomplete: T163 sits inside the `:408-458` collision cluster.** It
  was listed as dependency-free and parallel-safe; it is not. Serialize it with the rest of chain B.

**OPEN — lost with the architect's response:** the exact corrected board-line text for T180/T181 and
four DB queries it had prepared. The rulings above are complete enough to act on; the queries are not
recoverable and should be re-derived if a measurement gap shows up.

## T170 MERGED (2026-09-28) — main `b18aa7e`, 1214 passed, ruff clean

- **D147 — T170 is done and verified; T179 PASS on all 11 ACs.** Confirmed on merged main with the
  import path asserted: `subject_terms("Jev model")` → `['jev','model']`, and `gpt/llm/rag/api/sdk/
  JWT/vim/npm/git/go/os/vm` all now yield a term where they previously yielded `[]`. The
  `ai`-inside-`langchain` substring match stays excluded and `subject_terms("best open source github
  repos")` is still `[]`, so the deliberate fail-open survives for a genuinely all-container subject.
  74/74 mutations caught, security CLEAR, and an **80,026-char** spam candidate with the required term
  buried past `DESCRIPTION_CHARS` is still rejected — the spam-README defence is not reopened.

- **D148 — T179 corrected two things in its own brief rather than satisfying them.** (1) AC8 asked it
  to confirm the diacritic fold does not break non-ASCII subjects. CJK, Cyrillic and Greek all return
  `[]` — but it `git show`-ed the pre-T170 parent and proved this was **already** true, because the
  tokenizer regex is ASCII-only and the fold is irrelevant either way. Not a regression, and it said so
  instead of reporting a failure. (2) AC9 asked it to judge whether term-set equivalence soundly
  substitutes for the golden-set replay it could not run. It ruled **sound**, on a stronger basis than
  the dev had offered: items 26/29/30/34 are the **only** enumeration-path cases in the 24-item golden
  set — complete coverage, not a sample — and `is_relevant`'s new AND-branch is a **provable no-op**
  whenever every extracted term is ≥4 chars, which holds for both real golden subjects. 0 mismatches
  across all 28 real notes.

- **D149 — T170's own fix class survives in two tokens: `"no"` and `"yes"`.** Both are in the 48 short
  stopwords added by D139, so `subject_terms("no") == []` and `subject_terms("yes") == []`, which
  re-enters `is_relevant`'s `if not terms: return True` fail-open — the exact defect T170 closed.
  Verified on merged main. Narrow (a Go library is named `no`), hence P3 as **T182**, but recorded
  because it is the same class and a stopword list is the hand-maintained-contract shape this repo has
  been bitten by (T100's `BODY_FIELDS`, D70).

- **D150 — Two agents were lost to the machine sleeping, and the recovery cost was asymmetric.** The
  architect had written its log line **first** and its entire ruling set survived (reconstructed as
  D141-D146); only the board-line text and four DB queries were lost. The tester had banked nothing and
  lost 2 of 11 ACs' work entirely. Its re-run brief therefore required appending the log
  **incrementally**, and it did — 13 entries as it went. **Standing practice: write the log line as work
  completes, not at handoff.** The DONE gate already requires the log; this makes it crash-survivable
  at no extra cost.

## Breaking-news starvation on the NEWS path (user report 2026-09-30)

User's words: *"the headline/the actual post doesn't describe the post sent, it just researches and
tells if the post was right or not, but if a latest news comes in, then there wouldn't be any oppsts
which can be scraped, which would result in very few research results"*

Both halves confirmed in code and measured in the DB. This is a **separate defect class** from the
item-238 set (D100-D150) — that one is the enumeration path, this one is the news path.

- **D151 — MEASURED: half of all channel-item failures are research starvation.** 5 of 10, verbatim
  from `items.last_error`: items **71** ("only 0 of 5 researchers produced notes; need at least 2"),
  **120**, **143**, **177**, **255** (each "only 1 of 5"). The other 5 are infrastructure (4x
  OpenRouter 404, 1x unparseable JSON). The note distribution for channel items is **pressed against
  the floor**: 1 note → 1 item, **2 notes → 35 items**, 3 → 44, 4 → 55, 5 → 36, 6 → 1, 8 → 8. Many
  items barely clear `MIN_NOTES`. Items 85/86/88 (all channel) additionally parked with the news-path
  *"I researched this but found nothing that answers:"* message — commit `fbc478d` stopped a **deck**
  presenting our own failed search as a finding, but the **gate** still does it to the operator.

- **D152 — The news item is never treated as a source, which is why the deck reads as a verdict
  instead of a story.** `MIN_NOTES = 2` (`research.py:40`, enforced `:401`) counts only non-fail-open
  notes (D90/`FAIL_OPEN_KEY`), and `item.raw_text` is not one — so a fresh story needs **two external
  corroborations before it can be written about at all**. `synthesize.py:197` *does* pass the story
  (`ORIGINAL NEWS ITEM:\n{item.raw_text}\n\nRESEARCH NOTES:\n\n{blob}`), but `SYSTEM`
  (`synthesize.py:22-44`) says *"Combine the **research notes** into one tight factual brief"* with
  rule 1 *"Use only claims the notes support. Drop anything unsupported."* The story is handed over as
  context and then forbidden as evidence. Note the asymmetry: `PLAN_SYSTEM` (`research.py:149`) already
  asks for enrichment facets — what happened / background / parties and relations / prior comparable
  events / criticism, risks, consequences — and `synthesize` discards that framing.

- **D153 — THE KILLER: the retry backoff is in MINUTES, so a breaking story gets three of them.**
  `db.py:290` computes `delay = timedelta(minutes=2 ** (attempts - 1))` and `worker.py:35` sets
  `max_attempts: int = 3`. A starved item is retried after **1 minute**, then **2 minutes**, then
  marked `FAILED`. The generic backoff treats "the internet has not caught up yet" identically to an
  HTTP 404. This one conflation is what killed items 71/120/143/177/255 — not the floor itself, but the
  floor plus a three-minute window in which to meet it.

- **D154 — No recency handling exists anywhere in the pipeline.** Grepped all of `src/pipeline/`: no
  `time_range`, no date sort, no published-after filter, no notion of breaking news or staleness (the
  only occurrence of the phrase is a comment at `examples.py:140`). A story published 20 minutes ago is
  searched exactly like one from 2023, and a story with no corpus **yet** is indistinguishable from a
  false claim.

- **D155 — USER DECISIONS (their own words, via AskUserQuestion 2026-09-30). Settled; do not
  re-litigate.**
  1. **Breaking story with 0-1 sources → hold and retry on a schedule.** Their chosen preview: *"item
     arrives, 0 notes → park, next_attempt_at = +2h → retry: 1 note → park again, +4h → retry: 3 notes
     → compose normally. Ceiling on attempts, then ask you. Deck is fully sourced when it ships."*
  2. **Deck framing → tell the story, enriched.** Their chosen preview: *"item is a cited source
     alongside the notes; research supplies background / parties / stakes"*, replacing *"use only
     claims the notes support / drop anything unsupported"*.
  They explicitly did **not** choose publish-single-sourced-with-a-label, and did **not** choose
  keeping the hard floor.
  **Open risk the design must respect:** this pipeline posts to a live public Instagram account, and
  item 88's `raw_text` is literally *"🤯 Google DeepMind may have reached RSI — Rumors are spreading
  fast"*. "Tell the story, enriched" must not make a rumour publishable as established fact. Any part
  of that needing explicit sign-off goes back to the user (integrity rule 4).

## The four settling queries, run — and they REINTERPRET the defect (2026-09-30, orchestrator)

- **D161 — CONFIRMS D159 and sharpens it: all 8 starvation failures pre-date T131, and the most
  recent is TWO DAYS OLD.** Today's code emits `"...produced non-fail-open notes..."`
  (`research.py:403-404`); all 8 failures say plain `"...produced notes..."`. So T131's stricter count
  landed *after* item 255 failed on **2026-09-28**. The stricter gate has barely been in production,
  and every measured failure happened under the looser one. Under today's gate starvation should be
  **more** frequent, not less. **T185/T186 are P0, not archaeology.** Full list: channel 71, 120, 143,
  177, 255; dm 13, 18, 27 — 8 items, all `attempts=3`, all `failed`.

- **D162 — CORRECTS my own OQ4 alarm: item 84 did NOT publish with zero notes.** Its `research` is
  `'[]'` and `brief` is `''`, but its events end `published -> ingested` — it was **requeued after
  publishing**, and `requeue.py` clears `research`/`brief`. The empty columns are the post-requeue
  state, not what shipped. (Its last two events also carry inverted timestamps — 10:49 then 10:30 —
  worth noting, not chasing.) The honest OQ4 result: **8 channel items published at exactly 2 notes**,
  none fail-open, real IG post ids. So thin decks do reach the live account, but at the floor, never
  below it. 37 thin items reached compose-or-beyond (28 still `awaiting_approval`).

- **D163 — OQ2 does NOT show corpus growth, and OQ3 cannot size the backoff.** The near-duplicate gaps
  are **negative**: starved 13 vs dup 14 = **-9.16h**, starved 18 vs dup 16 = **-0.45h**, starved 18 vs
  dup 19 = **+2.21h**. These are near-simultaneous reposts of one story by different channels, not the
  same story revisited after the web caught up. What they actually show is **run-to-run variance**: the
  same story, researched hours apart, yielded **0 notes one run and 2-5 the next**. And OQ3's
  time-to-settle ladder (0 notes 2.57h → 2 notes 4.67h → 4 notes 21.1h) is confounded —
  `status_updated_at - created_at` includes operator approval latency and pipeline stage count, so it
  measures how long a deck waited for a human, not how long the corpus took to grow. **Neither query
  supports "wait and the corpus grows."** The architect's own honesty clause applies: the hold is
  justified by mechanism, not measurement, and `HOLD_CEILING`'s comment must say so.

- **D164 — THE REINTERPRETATION: "starvation" conflates an empty web with a CRASHED researcher, and
  the design must separate them before choosing any backoff.** `research.py:377-381`:
  ```python
  if isinstance(result, BaseException):
      # One researcher dying is survivable; the others carry the item.
      log.warning("researcher failed for %r: %s", query[:60], result)
      continue
  ```
  Only `Retryforever` is re-raised (`:373-376`, correctly — infrastructure is down for all of them).
  **Every other failure — an OpenRouter 503, an httpx timeout, a provider error, unparseable JSON — is
  logged at WARNING, silently dropped, and then counted as a researcher that "produced no note."** The
  item's `last_error` reports starvation either way. So
  `only 1 of 5 researchers produced non-fail-open notes` means any of: (1) the web has nothing,
  (2) the gate rejected everything, (3) **the researcher crashed**.
  Supporting evidence: 4 of 10 channel failures are explicit `openrouter 404: Provider returned error`
  (items 121/122/123/125), so provider failures are routine in this environment; and item **71** had
  **0 of 5** researchers produce anything, which is far more consistent with one infrastructure event
  than with five independent facet queries each finding an empty web.
  **Consequence for the design:** a 14-hour hold is the right response to an empty web and the *wrong*
  response to a transient 503 — you would wait 14 hours to retry something an immediate retry would
  have fixed. This does not overturn D152's `TooEarly` type; it corrects its **input**. `TooEarly` must
  be raised only on true starvation, and a crash-dominated shortfall stays on the existing fast
  minutes-backoff. Filed as **T194**, a prerequisite to T185/T186.

- **D165 — USER SIGN-OFF, given in their own message (2026-09-30). Integrity rule 4 satisfied for both
  items the architect escalated; no agent relay was involved.**
  1. **A self-described rumour DOES get a deck — attribute and hedge.** Chosen against "refuse
     rumour-shaped items" and against a raised note floor for them. So item-88-shaped items compose,
     with three guards: the brief must keep the item's own hedging (T188's rewritten rule), T189's
     regex tripwire warns when a hedge present in `raw_text` is absent from the brief, and T191's
     preview names the divergence before approval. The `MIN_NOTES = 2` floor still applies first —
     nothing composes on one source. Their selected preview: *"Deck hook: 'Reports claim DeepMind hit
     RSI' NOT 'DeepMind hit RSI'"*. This **authorises** what D154 flagged as strictly looser than
     today's accidental refuse-by-starvation, so T188 is unblocked.
  2. **`HOLD_CEILING = 2`, not 3 — two holds, ~6h total.** 2h then 4h, then ask. Chosen against the
     architect's ~14h/3-hold recommendation and against ~30h/4-hold. Rationale in their choice: hear
     about a starved item within a working day and keep the option to post it while it is still fresh,
     accepting that fewer items get a third chance for sources to appear and so more reach the operator
     thin. **Consequence to honour:** with only two holds the ceiling message matters more, because a
     larger share of starved items will reach the operator — so T186's operator text is load-bearing,
     not decorative. They did **not** choose the "reach in mid-hold" option, so a held item still has no
     `/post` handle until the ceiling; T190's dashboard visibility is therefore the only surface a hold
     has, which raises its value from P2-nice to genuinely needed.

## T194 built (2026-09-30) — crash separated from empty web

- **D166 — OpenRouter error bodies echo provider content into exception text, and that text must never
  reach `last_error`.** `llm.py:744-760` interpolates `body[:200]` into exception messages, so a
  provider error can carry remote content — a real secret-adjacent surface. T194's new aggregate
  `Retryable` message is built **only** from integer counts and static English, never from `result`, and
  that is pinned by a test (`test_crash_exception_text_never_reaches_the_raised_message`, asserting the
  message stays under 300 chars and contains neither a secret-shaped marker nor a hostile payload). This
  matters because `worker.py`'s generic `except Exception` stores `str(exc)` as `last_error`
  (`db.py:281`/`:294`, truncated to 2000 chars) and the dashboard renders it (`dashboard.py:430`,
  `:749-750`, via `esc()`). The leak vector survives only in the per-researcher `log.warning`
  (`research.py:383`), which is file-only, never persisted to the item, and unchanged by this diff.
  **Verified by reading the diff, not only by the test:** the f-string interpolates `crashed`,
  `starved`, `fail_open`, `len(real_notes)`, `len(queries)` and `MIN_NOTES` — six integers, nothing else.

- **D167 — No new column for the breakdown; the message string is the vehicle.** It reaches `last_error`
  through `worker.py`'s `str(exc)` path, which is where a human and any later query actually look. T185
  is already adding a `holds` column and two schema changes for one defect class is worse than one
  string. If T185/T186 want structured access rather than parsing, that is their call to argue
  explicitly.

- **D168 — `starved` deliberately conflates two of D164's three causes, and only `crashed` is split
  out.** "The web has nothing" and "the gate rejected everything" both collapse to `_research_one`
  returning `None` today, so separating them would mean widening its return contract — out of scope at
  +0 calls. `crashed` is the one distinction D164 established that T185/T186 actually need before
  choosing a backoff, because it is the one that makes an hours-long hold the wrong response. Filed the
  remainder as optional **T195** (P3), flagged rather than asserted as needed.

- **D169 — The four buckets are an exact identity, not an estimate.** `fail_open = len(notes) -
  len(real_notes)` and `starved = len(queries) - crashed - len(notes)`, so
  `crashed + starved + fail_open + len(real_notes)` collapses algebraically to `len(queries)`. `starved`
  cannot go negative (each non-crashed query appends at most one note) and no division appears anywhere,
  so the empty-`queries` hazard is structural rather than guarded — and it is unreachable regardless,
  because `if not queries: raise Retryable("no usable queries")` fires earlier. The dev pinned the
  invariant directly with a regex-parsed test over a 5-query mixed scenario rather than only testing the
  named cases, which is the right shape: it would catch a future bucket added without updating the sum.

## T187 built (2026-09-30) — the item_echo baseline, measured before T188 exists

- **D170 — BASELINE RECORDED. `golden.json`, set_version 1, 24 cases, ZERO model calls.** This is the
  number T188 will be judged against, and it is unrecoverable once T188 lands.
  | group | n | mean | stdev | min | max |
  |---|---|---|---|---|---|
  | ALL | 24 | 0.0581 | 0.0741 | 0.0000 | 0.2089 |
  | channel | 13 | **0.1069** | 0.0703 | 0.0000 | 0.2089 |
  | dm | 11 | 0.0004 | 0.0013 | 0.0000 | 0.0046 |
  **Finding worth more than the number: channel briefs ALREADY echo their source article ~10.7% on
  average (up to 20.9%) today, under the prompt that forbids the item as evidence.** So the current rule
  is not actually being obeyed — the model already leans on `raw_text` despite being told to use only
  what the notes support. T188 legitimises and directs something that is happening anyway. dm sits at
  ~0 correctly: a dm's `raw_text` is the operator's short request, not source material.

- **D171 — `item_echo_ratio` is a standalone `(brief, raw_text)` function, deliberately NOT wired into
  `score_substance()`/`substance_composite()`.** Unlike every other metric in `quality.py` it needs no
  `doc` and no deck at all, so folding it into `score_substance` would widen scope rather than satisfy a
  requirement. Empty denominator on either side returns 0.0 rather than raising — documented, not
  incidental.

- **D172 — The denominator swap is the sharp mutation, and it SURVIVED all 13 original tests.** Swapping
  `len(brief_grams)` for `len(raw_grams)` changes the metric's meaning while keeping it plausible, and
  every hand-built fixture happened to have **equal** brief/raw gram counts, so none could tell the two
  apart. Caught only after adding a deliberately asymmetric case (`brief="a b c d e"` vs `raw_text="a b
  c d e f g h i j"`: correct 1.0, swapped 0.375). **Generalisable rule: a ratio's tests must include an
  asymmetric-denominator case, or the denominator is unverified.** Same family as the label-transposition
  probe T196 is running against T194 — a metric can be wrong in a way that only shows on inputs nobody
  thought to build. The dev found this themselves via the house mutation rule, which is the rule earning
  its keep.

- **D173 — TENTH instance of the verification-layer family, and this one is MINE: every lint result this
  session was produced by an UNPINNED ruff.** `pyproject.toml:25` pins `ruff==0.16.8` and
  `./.venv/bin/ruff --version` reports **0.16.8** — the pin is satisfied. But `uvx ruff --version`
  reports **0.16.9**, because `uvx` ignores the project's pin and resolves latest. I put
  `uvx ruff check src tests scripts` into every task brief, so every agent reported a version that was
  not the pinned one, and the long-carried "T119 pin drift" caveat was never drift at all — it was the
  wrong binary. Verified today that both agree (`All checks passed!` from each), so **no result was
  actually wrong** — but it was right by luck, and a future 0.16.10 that disagreed would leave nobody
  able to say which tool was authoritative. D33 pinned ruff precisely for reproducibility. Standard
  command becomes `./.venv/bin/ruff`; filed as **T199**. Joins D78, T127, D92/D98, T140, T149, T151,
  D133, D138, D172.

- **D174 — Sandbox note, not a project decision:** this environment's Bash tool intermittently refuses
  commands containing the string "eval" when they also look like a write into a path (`mkdir`, `cp`,
  redirect). Single-target `cp <src> <dst>` per call works around it. Recorded because two agents have
  now spent time on it and the next one should not.

## T194 and T187 both PASS and both MERGED (2026-09-30) — main 1238 passed

- **D175 — T196 PASS on T194. The security property was proved through the REAL code path, not a
  reimplementation.** The tester built a hostile `RuntimeError` (a `sk-or-v1-` secret-shaped string,
  `%s %d {0} {reason}` format tokens, `<script>`/`onerror` HTML, a 3000-char flood, an embedded null
  byte) from a crashed researcher and traced it through four actual stages: `research()`'s raised
  message, `worker.py:148`'s own `f"{type(exc).__name__}: {exc}"`, `db.py:281`/`:294`'s `error[:2000]`
  truncation, and the **live-imported** `dashboard.py` `esc()`. None of the hostile content appears at
  any stage. It then added a **positive control** — `esc("<script>alert(1)</script>")` escapes
  correctly — so the finding is "never arrives", not "arrives but is caught". That distinction is the
  difference between a proof and a coincidence. It also confirmed `llm.py:744-760` genuinely
  interpolates `body[:200]` into `Retryforever`/`RateLimited`/`Terminal`/`Retryable`, so this was a real
  trust boundary rather than a hypothetical one.

- **D176 — CORRECTS D169, which I wrote: the identity test proves less than I claimed.** I recorded that
  `test_shortfall_breakdown_counts_are_exhaustive_over_every_query` "would catch a future bucket added
  without updating the sum". True for the **sum**, false for the **labels**. T196 transposed `starved`'s
  and `fail_open`'s values in the message and ran that test alone: it **passed**, because its crafted
  scenario has `starved == fail_open == 1` and swapping two equal values is invisible. Ten other tests
  do catch the swap, so nothing shipped broken — but the test D169 singled out is not the one doing the
  work. Ninth instance of this project's "measures nothing" family, in a narrow but real form. Fix is
  one line (pick `starved != fail_open`, e.g. 2 and 1); filed as **T197**. **Generalises with D172:** a
  fixture whose two compared quantities are equal cannot distinguish them — that applies to a ratio's
  denominator *and* to a breakdown's labels.

- **D177 — T198 PASS on T187, and it CORRECTS D170's interpretation, which I also over-reported.** I
  said channel briefs "already echo their source ~10.7%, so the rule is not being obeyed". The tester
  re-derived the baseline from a **fresh independent implementation** (exact match, so the script is not
  the suspect) and then decomposed the three highest cases by whether each shared 3-gram also appears in
  that item's *research notes*:
  - **id 35** (0.2089): 33 shared, **23 explained by research** — the same passages are independently
    present in notes sourced from Forbes. Of the 10 unexplained, **7 are literal citation-URL
    substrings** (`arxiv org pdf`), because `synthesize.SYSTEM` itself mandates `(source: https://...)`.
  - **id 36** (0.1735): 17 shared, 12 explained by research; **all 5 unexplained are pure URL
    substrings**.
  - **id 10** (0.1901): 23 shared, only 3 explained; **17 of 20 unexplained are genuine narrative prose**
    ("reached the summit at 7 p.m. on August 30", "wandered into Mud Creek Canyon") with zero research
    backing — **unambiguous real leakage**.
  **Verdict: the effect is real (id 10 proves it) but the 10.7% mean overstates it**, inflated by the
  prompt's own attribution rule and by notes independently converging on phrasing the post also carries.
  Filed as **T200**: caveat D170, and consider whether T197's before/after delta needs a URL-stripped
  variant so T188 is not credited for citation-format artifacts.

- **D178 — Gram multiplicity DILUTES the echo ratio rather than inflating it.** `_ngrams` has no
  sentence-boundary awareness, so a phrase repeated 20 times mints new cross-repetition 3-grams and
  *lowers* the ratio (1.0 → 0.6) versus a single occurrence. Pre-existing inherited `_ngrams` behaviour,
  frozen by house rule 12, correctly untouched by T187. Recorded as a property of the instrument so a
  future reader does not mistake a low ratio for a low echo.

- **D179 — MERGE: both landed, and the predicted additive conflicts were exactly as documented.**
  `scripts/mutate.py` and `.claude/logs/senior-dev.md` both conflicted — T187 and T194 append at the same
  end-of-`MUTATIONS` anchor, precisely the seam T184 anticipated. Resolution was pure concatenation, with
  one subtlety: both sides ended mid-`Mutation(` sharing the closing `),` after the conflict, so HEAD's
  final entry needed its own close inserted at the `=======` marker. Verified by `ast.parse` before
  running anything, then by importing the module: **78 mutations** (76 from each branch's view; the union
  is 78, not 80, because both counts included the 74 shared). Main: **1238 passed** = 1214 + 10 + 14,
  exactly additive. Lint clean under the **pinned** `./.venv/bin/ruff` 0.16.8 (D173).

- **D180 — The board-lint hook blocks ANY Bash command containing the literal task-board filename,
  anywhere in the command text — not only writes.** T196's tester lost several log appends to it
  silently before diagnosing it. I hit the same thing repeatedly. The hook is correct in intent (the
  board must go through Edit/Write so the evidence lint runs) but its match is broad enough that a
  read-only `grep` naming the file is refused, and a compound command that merely mentions it loses its
  *other* effects too. Recorded so the next agent does not spend time on it: name the file only in
  Edit/Write calls, and verify any log append with a following `grep -c`.

## T185 built (2026-09-30) — the hold mechanism

- **D181 — `TooEarly` subclasses `Retryable`, not `Retryforever`**, following `RateLimited`'s recorded
  precedent (`errors.py:34-40`): any handler that does not know `TooEarly` by name falls through to
  `worker.py`'s generic `except Exception` and gets `record_failure`'s minutes curve rather than
  crashing. Degradation, not breakage.

- **D182 — The ceiling check lives at the RAISE SITE (T186's `research.py`), never in `db.hold()`.**
  `hold()` is pure mechanism: it increments, backs off, returns the count, and has no policy. The caller
  decides whether a hold was warranted by reading `item.holds` against `db.HOLD_CEILING` **before**
  raising. Verified that `hold()` never refuses even past the ceiling, so a future miscalculation
  degrades to an 8h wait rather than a crash.
  **The exact contract T186 codes against:** `db.hold(item_id, error) -> int`, called unconditionally
  from `worker.py`'s `except TooEarly` (no ceiling check in the worker). It increments atomically,
  sets `next_attempt_at = now + HOLD_BACKOFF_BASE * 2**(new_holds-1)`, truncates `error` to 2000 chars
  into `last_error`, never touches `attempts` or `status`, and returns the true post-increment count.
  T186 branches three ways at the shortfall raise: crash-dominated (T194's `crashed`) → plain
  `Retryable` unchanged; genuine starvation with `item.holds < HOLD_CEILING` → `TooEarly`; genuine
  starvation with `item.holds >= HOLD_CEILING` → `NeedsInput`. That last branch is what caps the total
  at two holds. Nothing else in `worker.py`/`db.py`/`models.py` needs to change for T186.

- **D183 — The increment is `UPDATE ... SET holds = holds + 1 ... RETURNING holds`, a SQL expression,
  not `record_failure`'s read-then-write.** So the count cannot lose an update under concurrent holds,
  and `RETURNING` reads it back from that same statement rather than a separate `SELECT` that would
  already be stale. `RETURNING` needs SQLite ≥ 3.35; this machine has **3.51.0** (verified both in the
  venv and the system CLI). The dev **flagged this as machine-specific rather than asserting
  portability** — correct, and worth keeping: dev and production are the same single Mac here
  (`docs/usage.md`'s `launchctl` references), so it is a safe local assumption and not a general one.

- **D184 — `holds` is cleared by `requeue.py`'s `STAGE_OUTPUTS` `Status.TRIAGED` entry, NOT by
  `_ALWAYS_CLEARED`.** Requeuing to RESEARCHED-or-later must not erase hold history that stage never
  produced; requeuing to TRIAGED-or-earlier must. The existing slice-union logic already handles it, so
  no extra code.

- **D185 — `conversation.py`'s answered branch zeroes `holds` in the `transition()` fields dict it
  already writes** (`{"answer": text, "question": None, "holds": 0}`) — +0 db calls. `/post`
  deliberately does not, because it does not re-gather. New operator input earns a fresh budget; a
  request to ship what exists does not.

- **D186 — The schedule's comments record D163's honesty clause.** `HOLD_BACKOFF_BASE = 2h` and
  `HOLD_CEILING = 2` are the operator's own figures (D165) plus mechanism — **never a measurement** —
  because D163 proved neither OQ2 nor OQ3 can size them. The constant says so rather than implying a
  measured basis a future reader would trust.

- **D187 — Rollback is code-level and stated, not implied:** revert the code, leave the `holds` column.
  SQLite before 3.35 cannot `DROP COLUMN`, and an unread column is harmless. No data migration either
  direction — existing rows read `holds=0`, which is factually true: they have been held zero times.
  Verified against a copy of the real 269-row `data/app.db`, not a fresh one.

- **D188 — A test must not import the constant it is pinning.** `test_hold_backs_off_in_hours_not_minutes`
  hardcodes `timedelta(hours=2)` instead of importing `HOLD_BACKOFF_BASE`, so the
  `hold-hours-to-minutes` mutation cannot self-validate by moving the constant and the assertion
  together. Small, easily missed, and exactly the shape of D172/D176's family — a test that moves with
  the thing it measures proves nothing. Recorded as a rule, not an anecdote.

## T188 built (2026-09-30) — the item is a source. Renumbered from its D181-D185 (T185 claimed those).

- **D189 — P1 HELD, verified mechanically rather than asserted.** Both `prompt_fingerprint`s are
  byte-identical before and after the edit (`eval_compose` = `6debf99df623+fewshot:list`,
  `eval_research` = `5ddbbd1aa637+cheap:...+good:...+temp:0.3`), and `--rescore` reproduces **39.570**
  and **42.542** to the digit on both golden sets. The dev checked `synthesize` is absent from either
  fingerprint via `inspect.getsource`, not by reading the code. Shipped as 5 committed tests
  (`tests/test_d22_baseline_pin.py`) so a future edit that *would* reach a fingerprint is caught. This
  closes out my own reversal: I claimed this change would move D22's baselines, then verified it cannot
  (D155); it is now mechanically pinned.

- **D190 — `item_echo_ratio` ROSE, with a proper control, and the rise SURVIVES URL-stripping — which
  answers T200.** Two independent live replicates: channel **0.1069 → 0.1465 / 0.1631**; ALL 0.0581 →
  0.0804 / 0.0883. **Control group:** 4 dm `list`-intent cases whose `LIST_SYSTEM` is byte-identical
  before and after measured 0.0 → 0.0, which rules out "any fresh generation echoes more" and isolates
  the rise to the path T188 touched (affected n=20: 0.0697 → 0.106).
  **T200's question answered: ~83% of the channel delta survives URL-stripping** (raw +0.0396 →
  stripped +0.0330). And counter to both my expectation and the dev's: stripping URLs from D177's three
  flagged cases *slightly increased* their ratio, because **most citation URLs are denominator bulk
  that never matched `raw_text` at all** — not numerator inflation. So D177's caution is qualitatively
  real (URL substrings do explain some of the shared grams it found) but does **not** explain the
  measured rise. T200 can close on this evidence.
  **Caveat the dev stated rather than buried:** measured on `granite4.1:3b` as a local proxy, since
  neither pinned model is available in the sandbox. **Direction trustworthy, magnitude not
  production-calibrated.**

- **D191 — `grounding_ratio`'s live AFTER measurement was INFEASIBLE and is reported as such, not
  fabricated.** `compose()` on the AFTER briefs hung 19+ minutes on the first item with the weak local
  model and was killed. Structural argument for why the ±0.023 prediction should still hold:
  `quality.source_text_from_item` (`:180`) always folds `item.brief` in, so the metric structurally
  cannot see a raw_text-vs-brief provenance shift — which is precisely why `item_echo_ratio` exists as
  a separate instrument (D156). **Unconfirmed empirically.** Filed T204 for whoever has real model
  access. Reporting "I could not measure this" is the correct outcome here; a number from a proxy model
  would have been worse than no number.

- **D192 — SECURITY, P1, PRE-EXISTING: `item.raw_text` is interpolated into the prompt UNSANITIZED, and
  a fake `SYSTEM OVERRIDE` block inside it FULLY SUCCEEDS.** The dev's injection got the model to drop
  hedging, assert a fabricated claim ("Widget Co's CEO has resigned"), and echo an exfiltration marker
  — **identically on both the pre-T188 and post-T188 prompts**, same output, same marker. So T188
  introduces no new attack surface and no new blast radius: the old "use only claims the notes support"
  rule was already being ignored under injection, so it was never protecting anything.
  **The asymmetry I verified independently:** `budget.py:66-77` documents stripping the U+E000 sentinel
  from fetched web text and calls that sanitization **"load-bearing, not defense-in-depth"**, because a
  hostile page could plant the sentinel-wrapped pattern itself (D60). `item.raw_text` gets none of it —
  raw at `synthesize.py:188` and `:197`. So the pipeline defends against injection from *scraped pages*
  and not at all against injection from the *Telegram post itself*, which is the more direct path,
  since a channel post is authored content rather than incidentally-scraped text. Filed **T203**, P1,
  with a mandatory security pass. This does **not** gate T188's merge — the hole is identical with or
  without it — but it is the most important thing this task found.

- **D193 — The contradiction rule is textually scoped to note-vs-note, and T188 makes item-vs-note a
  live scenario for the first time.** `SYSTEM`'s rule 2 says *"If two **notes** contradict each
  other…"*. Before T188 the item was excluded as evidence, so an item-vs-note conflict was moot. Now it
  is reachable and **nothing in `SYSTEM` says which rule governs** — attribute the item's claim, or flag
  the contradiction? A test proves both sides reach the model whole, but the model is left to choose.
  Correctly out of T188's line-34-and-rule-1 fence. Filed **T202**. This matters most for exactly the
  rumour-shaped items the user just authorised (D165).

- **D194 — T206, containerizing `scripts/dashboard.py`: bind-host config name, and a pre-existing
  Docker build defect fixed in passing.** `Settings.dashboard_host` / env var `DASHBOARD_HOST`,
  default `"127.0.0.1"` — routed through `config.py` like every other env var, never a bare
  `os.environ` read. The container-internal bind is the only legitimate override (Docker can't route a
  published port to a process bound to the container's own loopback); the actual security boundary —
  the host-side publish — stays `127.0.0.1:8770:8770` in `docker-compose.yml`, never bare `8770:8770`.
  A bare host run with no env var set is unchanged (127.0.0.1), verified empirically (`lsof`, LAN-IP
  curl refused). Separately: `Dockerfile`'s base image
  (`mcr.microsoft.com/playwright/python:v1.47.0-jammy`, Python 3.10.12) never satisfied
  `requires-python = ">=3.13"` — `docker compose build app` failed identically before T206 touched
  anything, and no Microsoft playwright/python tag ships 3.13. Only one prior commit (a0f4744) ever
  touched the Dockerfile and no CI exercises the Docker path, so this had apparently never been built
  successfully. Fixed by swapping to `FROM python:3.13-slim` + `playwright install --with-deps
  chromium` (replaces the apt-get setup the old image pre-baked) — both `app` and `dashboard` now build
  clean off the shared Dockerfile. Also promoted `aiohttp` from a transitive dep (via `aiogram`) to a
  direct one in `pyproject.toml` — `dashboard.py` imports it directly; same resolved version (3.14.3)
  before and after.

- **D195 — The `good` role runs on local Ollama; `cheap` and `vision` stay on OpenRouter (T216, user
  decision 2026-10-16).** Measured in production: OpenRouter's good slug
  (`nvidia/nemotron-3-ultra-550b-a55b:free`) returned "unusable completion" on every good call,
  ~5 min per attempt × 3, before `llm.py` fell back to local — ~15 min and 3 wasted free-quota
  calls per good call, twice per item (synthesize + compose). The local Qwen3.8-4B was already doing
  all the real good-role work. Mechanism: optional `LLM_PROVIDER_CHEAP` / `_GOOD` / `_VISION`, each
  defaulting to `LLM_PROVIDER`; `Settings.provider_for(role)` is the ONE resolver, called by
  `llm._model/_chat`, `budget.budget_tokens_for_role` (load-bearing — sizing a local role from
  OpenRouter's 262144-token window is the T61/T70 truncation class), preflight, smoke and the eval
  scripts. OpenRouter key + free/auto-router slug checks apply exactly to the roles that resolve to
  openrouter, in both directions. Live check (T217): one real good call, 1 request, localhost
  only, 4.5s. Tradeoff accepted: good-role quality is now the 4B local model's, not a 550B hosted
  one — but the hosted one was producing nothing. Re-entry: if a hosted good slug is later measured
  to respond reliably, flip `LLM_PROVIDER_GOOD` back (T80's A/B is the place to measure it).

### Image gate + advisory pre-render verifier (T218, user request 2026-10-21)
User's words: images "not in accordance with the article" — check them with a local vision model;
pass the final pre-render data plus "all the curated context" through a local Qwen3.8. Root cause
of the image complaint, verified: `research._find_background` (research.py:479) takes the FIRST of
6 SearXNG image results that downloads and never looks at it. User decisions: vision model
qwen3.8:27b (18 GB on a 24 GB host, chosen over qwen3-vl:8b knowing it swaps); no fit -> retry with
a new search once; verifier findings shown in the approval preview, advisory. All numbers in the
design are PROVISIONAL until T219's host spike measures them.
- **D196 — Image gate gets its own local-only path** (`llm.image_check`, `MODEL_IMAGE_CHECK=qwen3.8:27b`); the `vision` role is untouched. Rejected: moving the whole vision role local — extract.py's vision pass runs on every ingested post before triage and would thrash an 18 GB model.
- **D197 — Verifier gets a local-only `verify` path**, pinned local regardless of `LLM_PROVIDER_GOOD` (user said "running locally"); `MODEL_VERIFY` defaults to MODEL_GOOD, `NUM_CTX_VERIFY` to num_ctx_good so Ollama doesn't reload the 4B. Rejected: reusing `good` (a config flip could send it hosted) and adding to `LLM_PROVIDER_ROLES` (makes it OpenRouter-routable).
- **D198 — One ranking call over all candidates, pick in code by threshold** (`IMAGE_MATCH_MIN`, editorial, provisional 7). Rejected: 6 calls (6× 27B cost) and trusting the model's own "best". Because `LLMClient.vision` silently skips an unreadable file (llm.py:451-454), only images actually encoded are numbered.
- **D199 — Fail-closed on images**: check unavailable, or nothing fits after one retry -> no background, never an unchecked image. `IMAGE_CHECK=off` is the rollback. Confirmed by the user 2026-10-21.
- **D200 — Memory: client-side shared/exclusive lane in `LLMClient._ollama`** (27B exclusive, writer-preferred, explicit `keep_alive:0` unload) with `OLLAMA_MAX_LOADED_MODELS=1` / `OLLAMA_NUM_PARALLEL=1` as host backstop (T220, needs user's own OK). Rejected: semaphore on image calls only (still thrashes vs concurrent 4B calls); server env alone (wrong on a default server).
- **D201 — Verifier runs at the end of `compose()`**, record in compose's output: re-verifies automatically after every Recompose / regen. Rejected: a new VERIFIED status (state-machine + requeue churn for an advisory step) and the render stage (repeats on Chromium retries).
- **D202 — Claims enumerated in code, per-claim verdicts, reduced in code.** Rejected: one overall model verdict — a single injected sentence (T203) flips it.
- **D203 — "All curated context" via chunked passes with coverage reported** (raw_text, brief, all notes, clauses, gaps, image descriptions; ≤ `MAX_VERIFY_PASSES`, provisional 4). Rejected: one packed pass (silently sees ~37% of notes — T61/T70 class) and raising num_ctx (reloads the 4B, ~6 GB KV at 40k). Raw fetched page bodies EXCLUDED pending user input.
- **D204 — New `items.verification` JSON column**, cleared on requeue to SYNTHESIZED. Rejected: `extracted` (research rewrites it; a stale verdict would survive recompose).
- **USER DECISIONS 2026-10-21 (their own answers in the orchestrating conversation):** (1) host Ollama change APPROVED — `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1` (T220; apply after T219 measures; the separate `iogpu.wired_limit_mb` raise is NOT covered by this approval and needs its own ask); (2) D199 CONFIRMED in full — check unavailable -> no background, never an unchecked image; (3) D203 CONFIRMED — notes only, raw fetched page bodies excluded; (4) `IMAGE_MATCH_MIN` set FROM T219's measurement, shown to the user.
- **D206 — T219 host spike REFUTES the image-gate model choice; the verifier half stands** (evidence logs/product-engineer.md#T219, 2026-10-21). qwen3.8:27b on this 24 GB host: correct and schema-following when it finishes (item 388: two real Tao photos scored 9/10, distinct descriptions), but 1 image 181-372s (load 38-71s + prefill 81-161s), 2 images 399s, 3-6 images never complete within 400-450s; `ollama ps` 20%/80% CPU/GPU with `iogpu.wired_limit_mb`=0 (default ceiling); PhysMem 23G/24G, ~160 MB free; evicted mid-response twice by the live worker's 4B/2B calls; model reloads 46-122s. So D198's one-call-over-6 is infeasible, and even 2 candidates costs ~7 min per item on 59% of items. Measured constants: NUM_CTX_IMAGE_CHECK 8192 is ample (max prompt_eval_count 535); IMAGE_CHECK_MAX_EDGE 512 (still read in-image subtitles); IMAGE_MATCH_MIN stays provisional 7 (1 scored item). Complaint CONFIRMED: by human judgement 6/10 historical production picks are clear topic mismatches (dessert for a Terence Tao story, stock "businesswoman" for an AI-toy leak). Verifier: p50/p90/max context 3,718/5,517/6,400 chars (notes only); real tokenizer ~4.2 chars/token (budget.py's CHARS_PER_TOKEN=2 is ~2× conservative, safe direction) -> p90 = 1 pass at num_ctx_good 16384; pass latency 79-177s; MAX_VERIFY_PASSES=4 kept as a ceiling. Model choice for the gate goes back to the user.
- **D207 — USER DECISION 2026-10-21 after D206: image gate model becomes `qwen3-vl:8b`** (dedicated Qwen vision model, ~6 GB, fits beside the 4B), replacing qwen3.8:27b (which stays installed, unused, until the user says to remove it). Re-measured before building (T230). Consequences: (a) T220's `OLLAMA_MAX_LOADED_MODELS=1` was sized for an 18 GB model; with ~6 GB it would force the live 2B/4B fallbacks to swap constantly — the user's approval of it stands but APPLYING it is re-decided on T230's numbers; (b) T222's shared/exclusive lane is deferred to the same decision — it may be unnecessary when the models co-reside; (c) T222 shrinks to the local-only `verify()` path the verifier needs now. User also chose to BUILD THE VERIFIER NOW, independent of the image model.
- **D208 — Verifier as built (T222 reduced + T224, 2026-10-21).** `src/pipeline/verify.py` is a stage-independent core module that takes a PREBUILT claims list; `compose.enumerate_claims()` owns the slide registry and builds it (no core→stage import, same split as coverage.py/integrity.py). `NUM_CTX_FIELD_FOR_ROLE` now lives once in config.py (closes the T70 named debt; llm.py and budget.py import it). `model_verify` / `num_ctx_verify` inherit DYNAMICALLY from the resolved MODEL_GOOD / NUM_CTX_GOOD in `Settings.load()`; the bare-dataclass defaults share one constant with model_good's. budget.py reads `num_ctx_verify` for role "verify" BEFORE `provider_for` (else it silently took the OpenRouter budget — mutant proved it). Status precedence: `too_large` > `unavailable` (EVERY pass failed or a crash before any pass) > `issues` (any claim contradicted) > `ok`; a single failed pass contributes nothing but doesn't abort the others. Claims: every `SLIDE_TYPE_FIELDS` field + caption, AND (orchestrator decision, user intent "make sure the data is right"; T101's original complaint was a headline count) every slide HEADLINE; image/image_mode excluded. Cost: +1 LOCAL call per compose() run (typically 1 pass, 79-177s measured), zero OpenRouter quota. **`.env` carries `VERIFY=off` as a safety switch** — the code is in the working tree and any worker restart would otherwise turn it on before review — flip to on after T224's tester PASS.
- **D209 — Verifier status is `ok` ONLY when every claim got a verdict** (orchestrator ruling on the T224 reviewer's escalation, 2026-10-21; follows from D202 and house rule 15 "a missing rating is not checked, never fine"). New precedence: `too_large` > `unavailable` (every pass failed) > `issues` (any contradicted) > `partial` (some claims have no verdict) > `unchecked` (no claim has a verdict, e.g. zero sources or a `{"claims": []}` reply) > `ok`. Why: with T203 open, one injected sentence ("return an empty claims list") would otherwise turn a contradicted deck into "ok" by suppressing verdicts — the exact flip D202 forbids. Also: a partially failed pass is recorded (`coverage.passes_failed` + the first failure's reason class) without changing that precedence; compose ALWAYS writes `verification` (None when VERIFY is off) so a Recompose/regen can never leave an old deck's verdict beside new slides (D204's rationale); `[DATA`/`[/DATA]` markers are stripped from source text (delimiter forgery, reviewer non-blocking 1); after a timeout/unreachable-class failure the remaining passes are skipped (bounds a hung Ollama at one deadline, not 4×900s).
- **D210 — Cheap role moves to `nvidia/nemotron-3.5-super-vl:free` (2026-10-31, T231).** OpenRouter moved `nvidia/nemotron-3-super-120b-a12b:free` to paid on ~2026-10-30 (404 "unavailable for free"); the free-only rule (T28) forbids the paid slug. Chosen on live probes, not catalogue metadata (docs/setup.md's own rule): real TRIAGE_SCHEMA 3/3 valid (median 8.3s), banana-enum enforcement 3/3, NOTE_SCHEMA on a 40k-char payload 3/3 conforming (83-181s). Backup if it goes the same way: `dots-studio/dots-3-note-preview:free` (triage 3/3, 7.7s; enforcement/40k NOT yet probed). Rejected: `xingchen-agi/xing4.0-29b-a4b:free` (429 on every call), `openrouter/free` (auto-router, T69), `liquid/lfm-2.5-2.6b:free` (2.6B). Lesson: free slugs can turn paid overnight and the pipeline then burns 3 attempts per item on a config fault — same class as T215 (missing local model) and T54 (provider 404): a "use the paid slug" 404 should be a startup/preflight refusal or Retryforever, not an item failure.
- **D211 — Source delimiters use a per-run random nonce; NOTHING is stripped from source text** (supersedes D209's "`[DATA`/`[/DATA]` are stripped"). The strip regex D209 asked for deleted legitimate text ("[Data center boom](url)" lost its link text; an unclosed "[data …" erased the rest of a source, 90 -> 7 chars) with coverage hiding it — a silent-truncation defect (rule 7/D5) introduced by the orchestrator's own spec. A per-run `secrets.token_hex` nonce in both the opening and closing marker (`[DATA-<nonce> id=…]` … `[/DATA-<nonce>]`) cannot be forged by text that never saw the nonce, and every byte of the source is preserved. Reviewer agreed: zero enumerated claims -> `ok` is correct (claims come from compose's own slides, not from injectable text).
- **D212 — Verifier shipped but stays OFF (`VERIFY=off`) pending the user's call (2026-10-31, tester T233).** Live end-to-end on item 456 (copy): local-only proven (2 POSTs to localhost:11434, zero OpenRouter), 100% coverage in 1 pass, status `issues` — it caught the fabricated "SamsungVoiceClassifier" code slide — but the 4B's per-claim verdicts are unreliable: two other fabrications ("tracking pipeline", "TV streams video to Samsung servers") marked supported, one stretched claim passed, evidence strings mismatched on 3 claims. Cost: verify ≈ 8 min of a 612 s compose under load average ~12, serial on the worker. So the record is a useful smoke alarm for gross fabrication, not a per-claim truth source. Tester recommends staying off until latency is measured on an idle box and/or a per-call cap lands; the decision is the user's. USER DECISION 2026-10-31 (their own message, after first picking "switch on now" and then interrupting it): "keep off for now". `.env` unchanged at `VERIFY=off`.
- **D213 — T230: qwen3-vl:8b is also not viable as a synchronous per-item image gate on this host** (evidence logs/product-engineer.md#T230, 2026-10-31, measured under live load: 44 requeued items on the 4B). Correct when it finishes (multi-image 2/4/6 distinct; Musk named; schema 3/3), but 6 candidates @512px: 305/310/441s, 2 of 5 timed out at 590s; ~1,090 prompt tokens per image; Ollama never kept it and the 4B resident together (211 polls; scheduler evicts on "5.8G needed, 5.5G free"), 31-90s per swap, and it queues behind the 4B's multi-minute compose calls. Thinking can't be disabled (think:false / /no_think ignored) — the `format` schema is what bounds output (64s/667 tok at 2 images vs 335s/3,877 tok without). Scoring is bimodal (0-3 or 10): it judges "depicts the news event", not "fitting hook" — 6 false rejects on item 385's on-topic toy-robot photos — so IMAGE_MATCH_MIN can't be set from this sample. Fail-closed (D199) at a 2-in-5 timeout rate means ~40% of eligible items lose their background. Root constraint, now measured twice (D206, D213): one 24 GB machine whose local 4B carries all writing cannot also host a local vision judge on the critical path. Also confirmed: production's vision fallback (MODEL_VISION = the text-only 4B) fails on images ("provide the mmproj") — T211/T214's open gap. Model choice for the gate goes back to the user.
- **D214 — USER DECISION 2026-11-01: the image gate runs on a HOSTED free vision model, `nvidia/nemotron-3.5-super-vl:free`** (the cheap-role model since D210: takes images, enforced structured output 3/3 in T231, ~8s), after D206/D213 measured both local vision models infeasible beside the live 4B. This is a deliberate exception to the original "local vision model" wording: candidate images (already-public web images found by SearXNG) and the article subject/excerpt go to OpenRouter. Consequences: T222b (local shared/exclusive lane) and T220 (OLLAMA_MAX_LOADED_MODELS=1 / NUM_PARALLEL=1) are CANCELLED as unnecessary — the user's approval for T220 is not exercised; qwen3.8:27b and qwen3-vl:8b stay installed but unused by the gate. Cost: ~1 call per item with a researched background (~59% of items), +1 on retry, against D8's account-wide free quota — small next to research's ~25. Fail-closed (D199) still holds. Must be probed on the same 10 items before T223 builds (T237), including an image-schema ENFORCEMENT check (gemma-4-31b advertises and ignores schemas — docs/setup.md).
- **D216 — Storage retention + 120-day expiry (user request 2026-11-01; T241).** Found: NO automatic cleanup existed — scripts/cleanup.py is a manual, report-only CLI (`--apply` deletes media of TERMINAL items), scheduled nowhere; data/ was 3.6 GB (media 3.3 GB). User asked for cleanup after approval/rejection, auto-reject + delete for posts not approved within 120 days, wired into the backend, all config in one place. Orchestrator calls (user may redirect): "delete" = delete FILES (local media, renders, MinIO objects), KEEP the DB row (status `rejected`) because dedupe (text_hash) and publish history depend on it; the 120-day clock runs from entering awaiting_approval and applies to awaiting_approval only (needs_input / in-flight untouched); published items lose local files and MinIO objects only after publishing is confirmed (dashboard's old-deck previews lose images); a periodic task inside the worker, config in config.py via .env. The user's own message authorises the deletion (integrity rule 4).
### Deck length follows the material (T238 design, 2026-11-01; architect's own numbering T239-T247/D215-D223 RENUMBERED by the orchestrator to T248-T256 and D215, D217-D224 to avoid collisions with T240-T242/D216 assigned meanwhile)
User: "each post needn't be 10 pages long… if the data found is not enough, then just create shorter posts… no unnecessary fluff… on point." Measured: news decks with ≤2 notes median 8 slides (n=32), 3-4 notes 8 (n=69), 5+ notes 9 (n=80). Verified in compose.py: only `follow` is always code-added (ensure_closing_slide :1329); `sources` only for non-DM items with URLs (:1319, DM skips it :631-639); hook/takeaway are model-chosen; ensure_closing_slide :1345-1347 trims `body[:room]` from the TAIL — silently drops the takeaway first on an over-long deck (rule 7 violation); quality.bare_bullet_ratio (0.15 composite weight, quality.py:564) counts code/flow/compare/kpi/chart/quote as "rich" and so rewards the invented structure this removes.
- **D215 — Floor = hook + takeaway + ≥1 content + sources + follow = 5; MIN_SLIDES stays 3, now explicitly "model-written slides"** (derived from MODEL_FIXED_TYPES + MIN_CONTENT_SLIDES, asserted == 3). Rejected MIN_SLIDES=5 (would count code-added slides and reopen the padding hole the :775 comment describes).
- **D217 — Deck length from research material, computed in code by ONE planner** `news_deck_plan(item)` that both writes the prompt's budget and checks the output (rule 8): units = distinct non-fail-open notes with a non-empty claim (deduped), candidates +1 for substantive raw_text and the low-confidence filter decided by spike T248; content_budget = clamp(max(units, n_clauses − n_gaps), 1, MAX_SLIDES − 4 = 6). Rejected: prompt-only (measured ignored); a model-extracted claim count (+1 call, judges the model-written brief).
- **D218 — Over budget → deterministic, logged, tail-first trim in `fit_deck`, +0 calls**, run after all 3 normalise_slides calls (:717/:740/:758) so unaddressed()/check_deck() judge the fitted deck; never drops hook/takeaway/closing/photo; kept+dropped==received logged. Rejected: a budget Recompose (local 4B wall-clock, may not comply); trimming ranked by grounding (would game the anchor metric).
- **D219 — The :497 variety rule is removed; code/quote/chart/kpi must be backed by the material, enforced in code** (code: fenced/verbatim code in material; quote: substring of material; chart: ≥_MIN_CHART_SERIES values in material; kpi: tile values in material). compare/flow prompt-only — known residual. Rejected: a softer nudge (still invents structure, item 456).
- **D220 — News-only via models.Intent; list path and T103 unchanged**; the removed shared-SYSTEM lines move verbatim to a list-only part; unknown intent → news (fails toward shorter). Rejected: a global change moving list decks the user didn't ask about.
- **D221 — News deck missing hook or takeaway → Retryable; the :1345 trim protects the takeaway and logs.** Rejected: code-written hook/takeaway (no truthful content).
- **D222 — Pre-registered predictions + STOP criteria against a fresh 3-rep HEAD control (not D22, which predates D195's local good role)**: P1 Spearman(content slides, material units) ≥ 0.4; P2 news decks ≤2 notes median 5-6 total (6-7 if T248 picks raw_text +1, fixed in T248's output); P3 zero news decks missing hook/takeaway/follow, none below floor, unsupported_rich = 0; P4 grounding_ratio and numeric/proper-noun densities flat or up; P5 bare_bullet_ratio rises, composite may drop — NOT a stop. STOP on: mean grounding_ratio < control 3-rep min; filler_count > control max; composite drop with bare_bullet_ratio held at control; unsupported_rich > 0 or any deck below floor; golden_listonly enumerations_ok change, any repo slide lost, or list avg_slides outside control range; more clause-retry firings or more unaddressed clauses than control.
- **D223 — bare_bullet_ratio's weight frozen for this change; re-decided separately (T254, rule 12).**
- **D224 — The evidence gate reads research material (notes claim+detail + raw_text) through its own integrity.py helper — never the brief, never quality.py** (source_text_from_item includes the model-written brief and is a frozen metric input, rule 12).
- **D225 — USER DECISIONS 2026-11-01 on fixed slides:** DM-request posts now GET a sources slide listing the research URLs (reverses the by-design DM skip at compose.py:631-639); items whose research found ZERO URLs ship WITHOUT a sources slide (so their floor is 4: hook + ≥1 content + takeaway + follow); list/enumeration decks unchanged (no takeaway, links index instead of sources). D215's floor of 5 applies to news decks with URLs.
- **D226 — T248 calibration fixes D217/D222's open choices:** budget = formula A — distinct non-fail-open notes with a non-empty normalised claim, LOW-confidence notes counted (dropping them cuts the budget in 118/181 decks and removes honest "unconfirmed" slides), NO raw_text +1 (≥600 chars on 70% of decks → a constant offset, not a signal), clamp(max(units, clauses−gaps), 1, 6). P2 is fixed at 6 (pass 5-6, STOP at ≥7). Evidence gate (D219) as measured drops ~60% of today's rich slides (code 27/30, quote 70/106, chart 7/14, kpi 13/46) with a ~10-13% false-drop rate; required: number normaliser (commas, $, k/m/b/t scaling, %/percent/x, number words 0-20 + tens + hundred) and a quote tolerant fallback (pass when ≥0.9 of the quote's words sit in 3+-word runs from the material — recovers 14 near-verbatim drops, item 279 at 0.89 stays dropped). 21% of channel decks have no takeaway today, justifying D221.
- **D227 — Render overflow predicate (T240):** a slide overflows when `body` OR any descendant of `.body` whose computed overflow is not `visible` has scrollWidth/scrollHeight beyond its client size + 2px. Horizontal clipping (code slide, item 499) was previously invisible because the predicate checked body height only. The code block wraps (`pre-wrap` + `overflow-wrap:anywhere`), turning width into height the check sees. Capacity table unchanged across all 11 themes.
- **D228 — Retention as built (T241, in review):** periodic task inside the worker, first sweep 60s after boot then every `CLEANUP_INTERVAL_S` (6h). 120-day clock = `status_updated_at` (written with the events row on every status change; a regen back to the gate restarts it, since the operator sees a new preview); strictly > N days; `AWAITING_APPROVAL_MAX_DAYS=0` disables. Auto-reject = status `rejected`, last_error "auto-rejected: awaiting approval > 120 days", via `Database.transition_if` (compare-and-set on status + status_updated_at, so an operator's approve mid-sweep wins). Grace: published/rejected/dropped 1 day (`TERMINAL_MEDIA_GRACE_DAYS`), failed 30 days (`FAILED_MEDIA_GRACE_DAYS` — requeue-to-ingested re-runs extract on raw_media_paths). Published purged only with `ig_post_id` set. Per item: media/<id>, researched/<id>, logos/<id>, raw_media_paths, rendered_paths (the old cleanup.py missed media/channel's raw downloads, 2.2 GB); files shared with a non-selected item kept; bucket deletes only under `items/<id>/`. One Telegram summary per acting run. Dry-run on the live data shape: 0 auto-rejects (oldest awaiting 57 days), 883 files / 199 items / 1.0 GB + 733 bucket objects / 221 MB reclaimable now; the other ~2.3 GB belongs to ~240 awaiting items and is reclaimed as they're decided or expire. Defaults ship live; `.env` carries `RETENTION_ENABLED=false` until review + security + tester pass.
- **D229 — Hosted image gate probed and adopted (T237, 2026-11-01).** `nvidia/nemotron-3.5-super-vl:free` through the real LLMClient vision path: banana-enum WITH an image enforced 3/3; T218 schema valid 3/3 and on all 120 ranking calls; latency at 6 candidates median ~10-11s, max 109s (tail → keep a generous deadline, fail-closed). Prompt B (score topic/domain FIT AS A HOOK IMAGE, anchors 9-10 shows the specific subject / 7-8 / 5-6 on-topic stock / 3-4 / 0-2 unrelated) fixes the bimodal scoring both prompt A and the local 8B showed (A: 29-64% false rejects; A unstable on identical input, item 363 9,9,9 then 2,2,2). Chosen: prompt B, IMAGE_MATCH_MIN=6 (FA 3/42, FR 5/87 pooled over 3 reps; 7 trades 1 FA for 4 FR), IMAGE_CANDIDATES=6 (no phantom indices at 6 vs 17% at 3), shown to the user. Item level at 6/B/6: fit pick 18/30, borderline 9, bad 0, none only on the all-junk item 388 (correct). Hazards the build must handle: phantom out-of-range indices; hallucinated identity (item 367: two generic portraits scored 10 as "Jack Clark") → name a person only when a caption/visible text confirms it, cap unidentified generic portraits at 6 (UNTESTED rule — T223 measures it). Quota ~6.5-8 calls/day (<1% of D8's cap). Labels: one labeller, 10 items — threshold provisional. Gate gets its OWN slug config, not the `vision` role (whose gemma-4-31b ignores schemas).
- **D205 — Verifier is advisory and NOT a metric** (house rule 6 scope note, rule 15): nothing in quality.py or any eval reads it; it may gate only through a decision citing T226's measurement.

### Whole-workflow fabrication audit (T257, user request 2026-11-01: "the articles just put made up json … go through the entire workflow and properly add checks and fallbacks")
Gap table + infra-fault map: logs/architect.md [T257]. Headline: D219/T251 as specified would not stop invented code — the T249 helper accepts any ``` fence in material (integrity.py:436-437), and the material itself (research-note claim/detail) is model-written and never checked against the page it came from (research.py:646). Tasks T259-T276; serialization compose.py T251→T103→T139→T236→T266, research.py T186/T223→T261→T265, bot.py T181→T225→T273→T274, llm.py T259→T269, enumerate_items.py T172/T260→T268, render.py T210→T263. No specialist; size L.
- **D230** Code evidence = content match (≥50% of the slide's ≥8-char lines in one fenced block or in material), never fence presence. Corrects T249's reading of D219. Rejected: fence presence (one YAML note would admit any invented JSON).
- **D231** Image transcripts are not D219 evidence; vision never runs with zero readable images. Cost: a real code screenshot loses its code slide (can still show as image).
- **D232** Note provenance checked at write time against the fetched page: unbacked fences/quotes stripped, unbacked numbers → note confidence low + `unbacked_numbers`. +0 calls, no stored corpus. Rejected: storing page excerpts; a model judge (rule 6, T233).
- **D233** URL + repo provenance in code: shipped URLs must come from research / raw_text / url_texts; unmatched repo slides dropped (compose.py:1384-1385). Rejected: preview flag only.
- **D234** Number provenance extends to point.stat and numeric facts/compare cells: drop value/row, keep slide. Residual: prose bullets + caption → verifier/preview.
- **D235** `ConfigFault(Retryforever)` for config/infra faults; no gate fails open on Retryforever/Terminal/ConfigFault; deterministic data faults Terminal. Rejected: per-site string matching.
- **D236** Render→recompose loop bounded by persisted `recomposes` counter (holds precedent); ceiling → NeedsInput.
- **D237** Approval binds content: `approved_hash` (sha256 of ordered slide PNGs + caption) checked at publish; limit failure → back to approval (NeedsInput), never Terminal.
- **D238** Integrity drops recorded in new `items.integrity` column (compose writes, preview reads). Rejected: reusing `items.verification` (advisory, rule 15).
- **D239** verify: `not_found` is not support; "ok" requires every checked claim supported. Still advisory.
- **U1 (user, 2026-11-01): account-level faults (OpenRouter 401/402/403, IG token 190/permission revoked) PAUSE the whole queue and alert once; no item is failed.** Applies to T259/T263.
- **U2 (user, 2026-11-01): preflight splits by fault type — local deterministic faults (missing model, Chromium, templates) refuse startup; network faults (SearXNG, MinIO, Instagram, OpenRouter) start + alert and become Retryforever at runtime.** Applies to T262.
- T241 accepted residual (reviewer, bounce-1 re-review): a tiny window between retention's per-plan re-read and the to_thread delete; the requeue comes from the separate dashboard process so no in-process lock closes it; worst case a just-requeued item past its 30-day grace fails visibly in extract.
- Retention follow-up (T241 bounce 1): a failed item purged after 30d then requeued to ingested → extract runs vision on zero images (covered by T269).

### Doodle theme (T278, user request 2026-11-01 — copy of a reference carousel; user chose text-only and random rotation)
- **D240** `doodle` is text-only by design: slide images (photo, inset, background, repo logo) are never rendered for it, enforced in the templates.
- **D241** Fonts for a theme are bundled under `config/fonts/<theme>/` with their OFL licence and loaded via `file://` (`FONTS_DIR` in render.py); `@font-face` is emitted only for the theme that needs it. Doodle: Shantell Sans (headlines) + Patrick Hand (body), both SIL OFL 1.1.
- **D242** Themes in `TEXT_ONLY_THEMES` (render.py) are never auto-picked by random/rotate for a deck containing a photo slide; an explicit THEME pin is still honoured. (Orchestrator call on reviewer N1: a photo slide would become a lone headline about an absent image.)
- **D243** compose.py MAX_BODY_ITEMS caps were NOT lowered for doodle's lower pre-shrink capacity (repo 3 / sources 7 / flow 3 / chart 3 / links 6 vs caps 4/10/4/5/7); doodle relies on its own shrink step, verified at the caps with worst-case text (only all-Title-Case 4-step flow still recomposes — accepted). Footer hidden for doodle (reference has none; Instagram shows its own counter).
- **D244 (orchestrator, from T260 review C1/C2):** Retryforever needs a visible ceiling. An item deferred continuously longer than DEFER_ALERT_AFTER_S (default 6h, config.py) alerts the operator once per stall (T284); every ConfigFault — LLM or search — alerts once per process through one shared mechanism. A suspended search engine that only made ONE researcher's query empty is not an outage for that item (EnginesBlocked; T260 bounce 1).

## T287 — Instagram throttles (2026-11-02)
- D245: Meta throttle responses (codes 4/9/17/32/613/80002, subcode 2207042, HTTP 429, or 'too many actions'/'request limit'/'rate limit' text) are PlatformThrottled(Retryforever): no attempt spent, retry in IG_RATE_LIMIT_BACKOFF_S=2h. Code 368 (abuse/policy block, can last days, writes while blocked may extend it) waits IG_POLICY_BLOCK_BACKOFF_S=24h. Other 4xx stay Terminal.
- D246: publishes paced >= IG_MIN_PUBLISH_GAP_S=180s (max 20/h) — 11-01 burst of ~40/h tripped Meta's action block on item 520. In-process lock + DB MAX(published_at); single worker assumed.
- D247: one operator alert per throttle stall (account-wide key), re-armed by the next successful publish; alert text carries no Meta/exception text.

## Agentic regeneration (2026-11-07, user request)
- User: "this system is not truly agentic when it comes to regeneration". Pain points (user-chosen): regen note ignored/half-applied with nothing checking; regen can't fix images or facts (only rewrites text from the same brief); regen changes too much (compose never sees the previous deck).
- Today: approval/bot.py REGEN → db.transition(SYNTHESIZED, regen_note) → compose.py appends "REVISION REQUESTED" to a fresh one-shot compose; no prior slides, no re-research, no image re-pick, no check.
- U3 (user decision): build an EDIT AGENT — reads note + current deck, plans tool actions (re-search, swap image, edit slide N, theme, caption), applies only those, verifies the note was satisfied before re-previewing.
- Design: docs/design/agentic-regen.md (T291). Decisions:
- D248 Regen runs new `revise` stage on new operator status REVISION_REQUESTED (APPROVED precedent); not a requeue target. Rejected: branching inside compose.
- D249 Planner = one schema-constrained llm.good call (temp 0.2) → flat JSON plan from closed ACTION_SPECS registry (single source for schema/validator/dispatch/prompt/preview copy). MAX_ACTIONS=6, MAX_RESEARCH_ACTIONS=2, MAX_ROUNDS=2. Rejected: open tool-calling loop (4B weak, unbounded). Gated by T293 GO/STOP; STOP → rules-first planner.
- D250 Ops: edit_slide, add_slide, remove_slide, reorder, set_theme, remove_image, replace_image, research, fix_fact (= research + edit_slide in code), rewrite_caption, full_recompose (sole action only). Code-built closing slides never targets; stable slide ids.
- D251 Untargeted slides byte-identical (sha256 canonical JSON), enforced in code; violation restores + logs error. Images change only via image ops.
- D252 Edited values pass compose's integrity checks vs material (incl. revision notes); fail → 1 retry with reason → revert + report. Infra faults propagate (rule 17).
- D253 Note check = code checks + one tri-state judge; status satisfied/partial/unsatisfied/unchecked computed in code; unchecked never reported done. Unsatisfied after 2 rounds → honest report, never auto full recompose.
- D254 Fallback ladder (no slides, /skip, global plan, planner invalid twice, REVISE_ENABLED=false) → compose with previous deck; preview says so. Flag = rollback switch.
- D255 Data: items.revision_request + items.revision ({v:1, runs[≤10]}) nullable JSON; regen_note kept for legacy/fallback. No history table, no deck snapshot. Rollback SQL: UPDATE items SET status='synthesized', regen_note=json_extract(revision_request,'$.note') WHERE status='revision_requested'.
- D256 items.theme_pin: operator per-item theme beats configured/rotate/random then item.theme; text-only pin honoured with warning.
- D257 Next stage: COMPOSED if slides/theme changed; RENDERED if caption-only or no change.
- D258 Render overflow (Recompose) → code-built edit_slide(n,"shorten"), 0 planner calls, bounded by T272 counter.
- D259 Cost: typical 3-5 calls, worst ~26 (today 1-4); operator-driven, negligible.
- D260 Security: planner sees note+deck+research claims only, never page text; web text to edit calls as delimited packed data; actions registry-validated; images under T218/T227 rules; no exception text in preview/revision.
- D261 T292 (compose sees previous deck on regen) ships first, independent of T293 outcome.
- U4 (user 2026-11-07): no Undo button (no per-run deck snapshot). U5: /skip stays "fresh full version" (fallback compose with previous deck in view).
- D262 (T292): a regen compose splits one context budget 40% previous deck / 60% notes after reserving SYSTEM, schema, brief, scaffold and completion. Previous-deck blocks never contain local paths: image → AVAILABLE IMAGES index, logo and pct dropped. Block wrapped via shared budget.data_block (nonce, D211), also used by verify.
- T293 spike (.claude/research/2026-11-07-t293-planner-spike.md): CONDITIONAL GO on the local 4B planner. Doc sketch (flat, both slide+after args, no examples) fails (valid-plan 82.6%, target 74.4%); design D (flat, single `slide` arg, no `after`, all args required w/ defaults, one few-shot example per op family) passes (valid-plan 96.7-98.7%, target 93-100%, p50 ~9s). edit_slide: 0/30 collateral change, 27/30 applied (misses = silent no-op).
- D263 Planner = design D, prompt/schema/examples generated from ACTION_SPECS (examples must pass the validator, tested). Two-step planner rejected (+3pt at 2× latency).
- D264 Rules-first front end before the model: regex for swap/move/remove slide N, theme names from available_themes(), image on slide N, /skip; a note fully explained by rules skips the model.
- D265 Grounding validator after schema validation: an action survives only if traceable to the note text (slide-targeted ops need a slide reference; caption/theme/research/reorder/recompose need trigger words). Zero grounded actions → ask the operator what to change, never full_recompose (pain c). Spike: dropped 36/36 vague+adversarial actions, kept 98/99 legit. Open: free-form slide references ("the kpi slide") must be groundable by slide type/headline, not only numbers — T295 to design + test.
- D266 Executor reads only the args named for the op (model fills junk args). Planner gets its own deadline (~120s); timeout = deferral (Retryforever), not failure.
- D267 (T295): image ops (replace_image, remove_image) are kind=image, executed after fetch and before text edits (supersedes design §3 table). full_recompose next to other actions → the recompose is dropped. Destructive ops (remove_slide, full_recompose) ground only on explicit phrasing — false negative costs one question, false positive costs the deck (no snapshot, D255/U4). Slide-count constants live in a leaf module so revision ↔ compose can't cycle.
- U6 (user 2026-11-07, after T295 hit bounces:3 on grounding leaks — "cut slide 3 down" / "do not remove slide 3" / "slide 3's jargon" grounded remove_slide; generic words grounded set_theme/reorder): "Rules + confirm". remove_slide, reorder and set_theme come ONLY from the rules parser (explicit phrasings, negation detected), never from the model plan; model may plan only edit_slide, add_slide, rewrite_caption, research, fix_fact, replace_image, remove_image. Before any remove_slide or full_recompose executes, the bot asks the operator Yes/No; No drops those actions and the rest proceeds. Supersedes D265 grounding for structural ops. User authorised a 4th T295 attempt on this design.
- D268 (T295/U6): remove_slide/reorder/set_theme produced only by parse_rules (negation-aware within clause; "cut/trim/shorten" = edit; possessives never remove; theme needs a real name; reorder needs swap/move phrasing); ground() drops model-planned rules-only ops (Reason.RULES_ONLY). needs_confirm on remove_slide + full_recompose. Negated-only triggers drop model ops (Reason.NEGATED) — known cost: "I don't like the caption, make it casual" asks the operator instead of rewriting. Partial rules results (note not fully explained): rules actions are kept and merged with the planner's grounded model actions; destructive ones still go through the confirm (T296/T301).
- D269 (T295 review 4): per the user's U6 wording ("the model handles only edits, caption, research and images"), full_recompose is ALSO rules-only (whole-deck phrasing via parse_rules, negation-aware) — the model may plan only edit_slide, add_slide, rewrite_caption, research, fix_fact, replace_image, remove_image. The Yes/No confirm is MANDATORY in T301/T302 for every action needs_confirmation() returns (rules can still fire on questions/conditionals like "should I remove slide 3?").
- Copy spec: docs/design/revision-copy.md (T303). D270: the planner's `global: true` flag no longer triggers a whole-deck rebuild (consistent with U6/D269 — rebuild only from rules-parsed full_recompose behind the Yes/No, or /skip per U5); a global-only plan with no grounded actions → ask the operator. D271: REGEN_PROMPT moves from a callback toast (Telegram 200-char cap) to a chat message; toast = "Reply with what to change, or /skip."; callback-answer limit constant in limits.py. D272: all revision copy lives in one pure module revision/copy.py with tests failing on any unmapped OutcomeCode/Reason/op; revision_request gets a one-shot `notified` flag for the deferral message; pure ask_cause(plan_result, note) helper (VAGUE/NEGATED/WHICH_SLIDE/EXPLICIT_NEEDED/WHICH_THEME). Changes block sits at the top of the preview, ≤1200 chars, plain text, never exception text; confirm buttons "✅ Yes, remove"/"↩️ No, keep it" (❌ is reserved for Reject).
- D273 (T297): structural executors on a stable-id Deck (ids = 0-based original index; added ids ≥ next_id; by_id() feeds check.untouched_invariant). Refused_invariant beyond spec: removing the hook, removing a closing slide, min/max slides (checked after compose's CTA/closing pass), removing the only body of a slide (bodyless, e.g. photo), an insert the CTA pass would drop. Render theme precedence: theme_pin > THEME setting (named/rotate/random) > item.theme; a pin naming a deleted theme falls through with a warning; unpinned items unchanged. items.theme documented as "composer's pick", not "what shipped" — absorbs T283.
- D274 (T300 review): coverage.unaddressed() on a non-dict judge reply now returns [] (was an uncaught AttributeError) — deliberate, advisory/fail-open for its live callers; the strict core judge_uncovered returns None (fail-closed) for revision. check.py: `after` MUST be a Mapping keyed by stable id (TypeError otherwise). Only APPLIED outcomes cover an explicit slide reference (UNCHANGED never does). Theme check = any applied set_theme (set_theme only comes from rules naming a real theme, U6). replace_image "path actually changed" is enforced by the executor (T299) as UNCHANGED, not by check.py.
- D275 (T296 planner): rules first (fully explained → 0 calls); else ≤2 llm.good calls (temp 0.2, PLANNER_TIMEOUT_S=120 → Retryforever); retry prompt carries code-built reason strings only (model text never echoed). A rules-parsed full_recompose on a partially explained note wins alone (model skipped) — still behind the Yes/No. Empty plan for a slide-naming note → one retry, then ask_operator (not PlanFailed). PlanFailed carries .rules_actions so T301 can keep explicit rules actions. Real model: 45/45 valid, 44/45 target, vague/adversarial 6/6 empty, 0 overplan.
- D273 addendum (T297 review 2): structural ops refuse when the closing pass would lose any slide they meant to keep (cta_dropped / closing_clamp) — a remove/reorder never deletes a different slide; inserting a closing-type slide is refused. On legacy decks without a follow slide, body-trimming structural ops are refused with closing_clamp.
- D276 (T298 review, refines D252): figure backing for revision edits comes only from material written by someone other than the model — sources plus the operator's RAW note — never the planner's action.instruction. Revision executors lock repo fact fields (owner, name, url, stars, language) to the original, and reject CTA text in body slides via compose's CTA predicate (one shared public helper).
- D277 (T296 review): planner timeout is per model call (PLANNER_TIMEOUT_S=120, worst case ~240s over 2 calls) — accepted, a revision is operator-driven and the worker defers on timeout. If either attempt returned an honest (valid) empty plan, the result is ask_operator, not PlanFailed (any non-broken attempt wins). Merged plans: rule asks deduped one per op label and renumbered; model asks are never silently cut by MAX_ASKS — every action's ask index points at the ask it was planned for.
- D278 (T296 review 2): an ask whose action was dropped by merge validation (e.g. edit on a slide the same note removes) stays in the asks list — intended: the operator asked, it wasn't done, and the reason is reported. The ask-cap drop uses Reason.MAX_ACTIONS for now; T302 (copy.py) adds Reason.MAX_ASKS or rewords the max_actions copy so it never states a wrong count. Digest field allow-list (body-field contract moved to deck_shape.py) is a follow-up, deny-list accepted meanwhile.
- D279 (T298 review 3): revision edits pass only type, headline and the slide type's declared fields (+ fixed fields image/image_mode/logo/locked repo facts reattached before gates); undeclared legacy extras are dropped on the edited slide (unrendered). Legacy headline-only originals may be edited without meeting the body requirement; add_slide must meet it. Over-cap legacy charts lose points beyond the cap on edit, same as compose.
- D280 (T296 tester fix): ask integrity enforced in model.validate_plan — asks positional (blank keeps its slot), an action keeps the ask it was planned for, missing/invalid index → its own label ask, a 5th distinct ask → action DROPPED_CONFLICT/MAX_ACTIONS, never repointed. Planner prompt carries a code-built ALREADY HANDLED list of rules actions (labels/slide numbers/theme names only). RevisionPlan.unaddressed lists planner asks that ended with no action — T301/copy/check must report them (never silently lost).
- D281 (T296 re-review): RevisionPlan.unaddressed may over-report asks the rules already served — copy phrases it softly ("I may have missed: …"), never "not done"; it is model text, so T301 must wrap it in a data block if it ever enters a prompt.
- D282 (T299): revision image downloads go through new core module src/pipeline/safe_download.py (http/https 80/443 only, every resolved address globally routable, manual redirects ≤3 re-checked, 8MB streamed cap, 25s deadline, image/* + full decode jpeg/png/webp, ≤40MP, ≥900px long edge) — reusable by T227 for search.download_image, which today has NO SSRF guard and follows redirects blindly (live research path). Applied revision images carry Outcome.reason image_unchecked until the T223 image gate exists. Residual: DNS rebinding (needs pinned transport, T227); bytes stored raw, not re-encoded (same as research). fetch reason codes (no_results, not_answered, source_unverified, no_candidates, no_usable_image, same_image, image_unchecked) must be mapped in copy (T302).
- D283 (T315): in ground(), a slide named only inside a swap/move/remove-image phrase is moved, not edited — a model slide action on it is dropped as Reason.CONSUMED_BY_RULES (shared _positional_rules helper with parse_rules). remove_slide spans aren't counted (merge already drops edits on removed slides as TARGET_REMOVED); remove_image actions are exempt (rules' own op, deduped).
- D283 addendum (T315 review): if the note's remaining text (outside the rules-consumed phrases) has a pronoun (it/its/this one/that one/both/them/they/these/those), the slides the rules acted on still count as referenced — "move slide 5 to the front and make it punchier" keeps edit 5. The linear-time test becomes ratio-based (L vs 2L), not a fixed-seconds budget. CONSUMED_BY_RULES needs a copy row (T302).
- D283 addendum 2 (T315 re-review): a move's destination slide is never re-added; a stray pronoun over-keep ("swap 2 and 4, the caption is long, shorten it") is accepted — worst case equals pre-T315 behaviour and still shows in the preview; refine to same-clause edit verb only if real usage shows over-edits.
- D284 (T299 tester fix): safe_download refuses images with short side < MIN_IMAGE_SHORT_EDGE=500 (LOW_RESOLUTION) or aspect > MAX_ASPECT_RATIO=3.0 (BAD_ASPECT), editorial. search._classified_results is the single status/engine classifier for searx() and image_search(); image_search now raises Retryforever/ConfigFault/EnginesBlocked like searx — live research._find_background still swallows them (background image is optional), so live behaviour is unchanged; revision replace_image defers on outage. integrity.unbacked_figures skips text-side raw_mag tokens (the paired mag token is still checked) — revision-only; live compose's figure_backed already handled magnitudes.
- D285 (T301 revise stage): confirm gate is a dedicated Status.REVISION_CONFIRM (in WORKER_HALTS); validated plan stored in revision_request.pending_confirm {token, plan, answer, asked_at}; Yes/No re-queues to REVISION_REQUESTED and runs the stored plan with no second planner call; No with nothing left → AWAITING_APPROVAL, "declined" run record. New OutcomeCode.DECLINED; declined asks masked from the check. Round 2 replans only model-source actions and only while the operator's slide numbering still holds (no replan after remove/add/reorder); never auto full_recompose. Execution order fetch → image → text → structural (remove, theme, reorder), add_slide after reorder; revision research notes persisted to items.research with origin "revision". A non-infra Retryable (model unusable) costs one action (unchanged/model_unusable), infra faults propagate. EnginesBlocked: blocked_since stamp, retry every 5 min, after REVISE_ENGINES_BLOCKED_MAX_S (1800s) fetch ops → not_found/search_blocked and the rest runs; one deferral notice per request. run_record reasons closed codes only. verify.run_advisory replaces compose's inline hook. API for T302: request_revision(db, item_snapshot, origin, note, …) transition_if-guarded; confirm_revision(db, item_id, yes, token) → ConfirmResult (idempotent, one winner); revise(notify=…) Notices CONFIRM/ASK_OPERATOR/DEFERRED; preview data = items.revision.runs[-1] (docs/revision-api.md).
- D286 (T227 SSRF): all live public fetches (search.fetch_text, download_image, download_avatar) and revision downloads go through safe_download.fetch_public(http, url, Policy) — check_url pre-check, manual redirects ≤3 re-checked, https→http refused, streamed caps, deadline, code-only logs. DNS rebinding closed at connect time: VettingBackend (custom httpcore backend) resolves inside connect_tcp, requires EVERY address public, dials the vetted IP while keeping Host/SNI/cert verification; PinnedTransport used via RoutingTransport for requests carrying PUBLIC_ONLY_EXTENSION (the shared client also talks to loopback SearXNG/Ollama). Pages: text/*/xhtml/xml/missing type, 2MB cap, ONE gzip/deflate layer inflated under the cap (stacked/br/zstd refused); images identity-only. Behaviour changes: ports 80/443 only, https→http and non-text pages refused (fallback = search snippet, as for an unreachable page). Constraint: the shared httpx client no longer reads HTTP_PROXY env (none configured, checked 2026-11-07). Relies on httpx private AsyncHTTPTransport._pool (guarded by a test).
- D287 (T301 review): revision research notes stay in items.research (origin "revision", useful backing for later edits/checks) but compose's LIST consumers (_notes_block, _restore_repo_facts, ensure_links_slide, check_deck list paths) must exclude them via one helper (e.g. list_notes(research)) — a revision note must never become an extra list entry. confirm_revision requires a token; missing/mismatched token = STALE. EnginesBlocked cap measures a continuous block: blocked_since is cleared on any non-EnginesBlocked deferral. Destructive actions run only when the stored answer is exactly True. CORRECTION to D255 rollback SQL: it must cover both new statuses — UPDATE items SET status='synthesized', regen_note=json_extract(revision_request,'$.note') WHERE status IN ('revision_requested','revision_confirm').
- D288 (T302): revision run records carry `deck` (sha256 of the slides the run left behind) so a later legacy regen/requeue never shows an old "Revised per your note" over a different deck (records without it are trusted). With the flag on, Regenerate replies starting "make …" or naming deck words are no longer held back as "new request" (genuine new topics still are). Confirm callback_data `rvc:y|n:{id}:{token}` ≤64 bytes; undelivered confirms are answered from the dashboard item page (same token). Dashboard hides items.last_error behind a fixed sentence while revision_requested (rule 15). Reason.MAX_ASKS added (fixes D278).
- 2026-11-07 orchestrator: REVISE_ENABLED=false set in .env (backup /tmp/env.bak.revise-off) — code default is true and the worker runs from this tree, so an unplanned restart would otherwise expose unreviewed T302. Flip to true only after T302 review+test and the user's go.
- D288 CORRECTION (T302 review): the relaxed new-request check (flag on) currently swallows natural new-carousel requests ("make a post about…", "create a carousel on…") as revision notes — being fixed before go-live: new-topic markers (a/an/another/new + post/carousel/deck, creation verb + about/on) still count as a new request.
- D289 (T302 test): a planner that fails twice (PlanFailed, no rules actions) no longer falls back to a whole-deck rebuild — per U6 a whole-deck rewrite needs the operator's Yes; it becomes ask_operator ("I couldn't work out what to change — nothing was touched"). /skip (explicit) and render overflow keep the fallback compose. Supersedes that rung of D254.
- D288 update (T302 delta review): with the flag on, a Regenerate reply is a NEW request only if it matches the anchored new-content pattern (optional please/can you + creation verb or article + post|carousel|deck|thread|one + about|on|for|from); "slide N"/"the caption" referencing this deck keeps it a note. Being tightened so "one" counts only before about/on and "of" never does ("make one of the photos bigger" stays a note).
- U7 (user 2026-11-07): go-live = run T305 integration/security review + T306 full test on an app.db copy, then switch REVISE_ENABLED on and restart. User OK'd committing the finished, tested work (local commits on main, no push).
- D290 (T305): go-live needs no production change beyond small pre-go-live fixes: re-pin 4 parity tests to pre-feature commit b1bcae7 (not HEAD); wrap judge asks in a data block; digest counts revision_confirm as waiting; rollback SQL sends revision_confirm items to awaiting_approval with revision_request=NULL (never an unconfirmed rebuild). FAILED-path exception text (rule 15) is a follow-up (T318); render overflow after a revision stays a full recompose until T304.
- D291 (T319): in strict judge mode (nonce given), every judge entry must resolve to a known ask (exact text → stripped DATA marker/bullet/number/quote → ask-N id); any unresolved entry makes the whole coverage verdict UNCHECKED, never SATISFIED. Refines D253. UNCHECKED adds no replans and shows the "read the slides" hint. The lenient no-nonce path (unaddressed()) is unchanged. Id/text collision is follow-up T320.
- D292 (2026-11-07): agentic Regenerate is LIVE — REVISE_ENABLED=true after T305 review, T319, T306 PASS (user chose "switch on", U7). Rollback = set REVISE_ENABLED=false + restart (D255/D287 SQL for items parked in revision_requested/revision_confirm). Known quality limit: text-edit asks are weak on the local 4B model (T306: 56% satisfied overall, honest unsatisfied otherwise); follow-ups T320, T321.
- U8 (user 2026-11-07): dashboard redesign — "make the entire UI look similar to a nice dashboard" (full admin-dashboard look: app shell, overview KPIs, tables, cards), Web dashboard only; dashboard /post button "Not now". Approve placement: ux recommendation (item page + confirm, rows get "Review") adopted by default — user did not pick it explicitly; reversible.
- D293 (T322 rev 2, U8): dashboard redesign = admin-dashboard app shell; CSS/JS in scripts/dashboard_static/ inlined into each page at import (no static route, no CDN); "/" renders a new Overview (old list kept: "/" with lane/q/all/thin query delegates to index(); new read-only aliases GET /overview, /items); Approve only on the item page behind a confirm (reversal: row_actions allow_approve=True); no IG quota KPI (not stored) — "Held" derived from next_attempt_at; KPI windows rolling, chart days UTC.
- D294 (T307): full config reference lives only in docs/setup.md#configuration-reference; new settings must update Settings, .env.example and that table together. Revision internals documented only in docs/revision-api.md. Docs recommend `uv sync --extra dev` until T331 (Pillow) ships, then drop the caveat. D236/D237/D244 are not in code (not documented).
- D295 (T307): stages may call another stage only as a deliberate fallback or routing step — today revise._fallback→compose and __main__.research_or_enumerate (enumerate_items→research); any new one must be documented in docs/architecture.md and docs/development.md.
- D296 (T325, UX-3): list rows show Review (+ quiet Drop on non-terminal) instead of Approve/Reject; reversal = row_actions(item, allow_approve=True). A TooThin "New post" draft is carried back in the redirect URL (capped at DRAFT_MAX=1000 chars, escaped) rather than server session state.
- D297 (T324, UX-2): "/" is the Overview; the list is /items or "/" with lane/q/all/thin (delegates to index() unchanged). "Why items stopped" fetches GROUP BY detail LIMIT 50 and merges by first line in Python, top 5 shown (LIMIT 5 alone yields near-duplicates). OverviewStats is a plain class (dataclass breaks path-loaded test imports under __future__ annotations).
- U9 (user 2026-11-08): T323 (phone drawer focus) and T329 (/queue layout) hit bounces:3 — user chose "Fix both, 4th try"; same quality bar (review + T330 re-test).
- U10 (user 2026-11-08): commit the dashboard redesign + docs + T331/T335 locally (no push); rebuild + restart only the docker dashboard container.
- U11 (user 2026-11-08): after T338, keep the dashboard in Docker but read-only — it must never open the live SQLite file; acting (approve etc.) happens elsewhere (Telegram). D298: never share a live SQLite WAL database between a host process and a Docker Desktop container (no cross-VM locking/shm) — it corrupted app.db (T338).
- D299 (T337): Instagram "not ready" after FINISHED (code 9007 / subcode 2207027 / "Media ID is not available") → retry media_publish in place 3×10s inside the publish lock, then Retryable (spends an attempt) — never Terminal. Double-post guard now matches recent media by normalized caption within 24h, skipping ids already in publish_log (no schema change; accepted tiny false-positive risk of an identical caption within 24h).
- D300 (T339): dashboard snapshot is a 4th _periodic job inside the worker (src/pipeline/snapshot.py; online backup API → tmp → journal_mode=DELETE + snapshot_meta + quick_check → os.replace), SNAPSHOT_INTERVAL_S default 60 (0=off). Rejected: separate launchd job (macOS blocks new background items), shell sqlite3 .backup.
- D301 (T339): container reads data/snapshot/dashboard.db with mode=ro&immutable=1, fresh connection per request; compose mounts the snapshot DIRECTORY (file mounts go stale on replace).
- D302 (T339): app log moves to data/logs/pipeline.log so the container mounts only data/snapshot, data/media, data/logs (:ro) — never ./data.
- D303 (T339): read-only dashboard refuses all non-GET/HEAD via middleware (403); hiding forms is cosmetic.
- D304 (T339): read-write Database.connect refuses inside any container (PIPELINE_IN_CONTAINER / /.dockerenv), no override — retires the compose `app` worker service (pending user confirmation).
- U12 (user 2026-11-08): "i want everything running in docker" — supersedes U11 read-only dashboard and the D304 "retire app service" proposal: worker AND dashboard run in Docker. Architect to redesign (shared DB must live where SQLite locking works for both containers, e.g. a Docker named volume, never a host bind mount shared with a host process).
- D305 (T346): live SQLite WAL is shared only on a Docker named volume (one VM kernel), never a bind mount; spike T347 gates it. Fallback if it fails: dashboard served inside the worker process.
- D306 (T346): external named volume pipeline-state holds only app.db, telegram.session, ig_token.json; media, logs, backups and the worker lease stay on the ./data bind (rw for app, :ro for dashboard).
- D307 (T346): one-owner rule = heartbeat lease file data/worker.lease (30s heartbeat, stale 120s) + fencing; host rw DB connects refuse while a container lease is fresh.
- D308 (T346): in containers, rw DB / Telethon start only on an allowlisted local fs (ext4/xfs/btrfs per /proc/self/mountinfo); unknown fs fails closed.
- D309 (T346): MinIO stays a standalone container reached on its published port during cutover; adopted into compose later (T357).
- D310 (T346): T340 snapshot code becomes an hourly backup job to data/backups (keep 48) — guards against Docker Desktop reset/purge deleting the volume.
- D311 (T346): launchd retired after cutover; plist kept (moved out of LaunchAgents) for rollback.
- U13 (user 2026-11-08): slide fonts in Docker = open look-alikes (Inter for SF/Helvetica, Gelasio for Georgia, JetBrains Mono for Menlo), added after the Apple names so host rendering is unchanged; side-by-sides (T350) shown before switching. Container TZ = local (Asia/Kolkata). User will set Docker Desktop "start at sign-in" + ≥4 GB memory.
- D312 (T347): spike measured named-volume WAL across containers safe (2×10 min, 19 recreations, 0 errors, integrity ok); host+container on one bind-mounted file reproduced T338 corruption. Image must COPY config/ and templates/ (themes/fonts not in the image today) and add cloudflared + sqlite3 CLI.
- D313 (T348): storage guard runs before lease acquire (a refusal leaves no lease); a missing lease file during heartbeat is rewritten, only a lease naming another instance fences; acquire() write-then-reread (not a hard mutex; ms window, Telethon/bot start after); crashed container waits ≤120s for staleness; host fence refuses host rw DB opens only for a fresh container-owned lease; tests point LEASE_PATH at tmp (conftest autouse).
- U14 (user 2026-11-08): font sign-off after T350 contact sheet — "Looks good": Inter / Gelasio / JetBrains Mono for Docker rendering, no tracking tweak. TZ default Asia/Kolkata in compose = U13 local-time choice (design §1 said UTC).
- D314 (T348 bounce 1): heartbeat starts right after lease.acquire() and covers startup; _run checks stop before/after telethon.start(); absent lease claimed with O_CREAT|O_EXCL; takeover of a fresh lease only for same owner_kind + hostname with a dead pid (host) or same pid (restarted container); lease temp file per instance.
- U15 (user 2026-11-08): no two consecutive Instagram posts share a slide theme. Record the theme each item actually rendered with; THEME=random picks exclude the last PUBLISHED theme; at publish time, if the item about to go out has the same theme as the previous published post, re-render it with a different theme first and tell the operator in Telegram. Approved previews are not changed before publish. Operator theme_pin still wins (D256).
- U16 (user 2026-11-08): finish T358 (theme no-repeat) and T359 (pre-cutover polish) through review+test, commit, then do the Docker cutover with a final explicit "go" right before stopping the worker. Commit finished, tested work locally now (no push).
- D315 (T358): items.rendered_theme (nullable, additive migration) = theme stem actually rendered; requeue to ≤COMPOSED clears it. Auto picks use a soft avoid={last published rendered_theme} (dropped first if it would empty the set) next to the hard TEXT_ONLY exclude. Publish guard runs inside _publish_lock before containers are created: same auto theme as last published → re-render + upload + one Telegram note; Recompose on re-render → publish original + note; other failures → Retryable (never silently publish a repeat). Pins and named THEME never swap. Pre-T358 posts have NULL rendered_theme — at cutover, backfill the latest published item from the "rendering with theme" log line.
- D316 (T353): at cutover the DB held host-absolute media paths; rewritten in place to /app/ (user-approved). Rollback to the host now also needs the reverse replace (/app/ → /Users/aayushgour/Desktop/projects/telegram-automation/) on raw_media_paths, extracted, research, slides, rendered_paths. Follow-up T361: store media paths relative to the media dir so location changes never need a data rewrite.
- U17 (user 2026-11-09): no raw JSON/YAML/code/markdown-source on slides. The "code" slide is replaced by rendered Markdown only (lists, numbered steps, tables, emphasis, quotes) converted server-side to styled HTML; model-authored raw HTML is not inserted (escaped). Existing waiting decks convert automatically when re-rendered (regenerate or publish-time theme swap); published posts untouched.

## Explainers, DM continuation, search engines (2026-11-10/11)
- T364 (user: "Remove this /new thing"): every DM is its own item. A text DM joins the previous one only when that fragment sat on a Telegram ceiling (text ≥ 4096−300, caption ≥ 1024), recorded in items.at_ceiling; the append is status-guarded (lost race → new item).
- T366 (user, items 612/615/616 on NCM): third intent `explain` (what is X / how does it work / X vs Y / a bare link); stored item.intent stays "news" (downstream only checks "list"). Operator-linked pages are primary notes (DM only, ≤2 pages). Non-answer notes are dropped. Bare acronyms are expanded from search results, never guessed. Queries are capped at 6 words with the bare subject first. Thin research on a DM asks the operator (with /post) instead of retrying; channel items still retry. User decision 2026-11-11 "Post from 1 good source": an explain DM proceeds from 1 real note; news DMs and channel items keep 2.
- T368: SearXNG general engines (brave, duckduckgo, google cse, mojeek, startpage) CAPTCHA/suspend under load, leaving Bing alone; yahoo + semantic scholar enabled in searxng/settings.yml.
- T367 open: revision "write a post from <repo/URL>" as a confirm-gated regenerate_from_source op.
