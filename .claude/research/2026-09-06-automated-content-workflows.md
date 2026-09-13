# Automated content generation workflows — how they are actually built

Researched 2026-09-06 via self-hosted SearXNG. Root question: how do automated
content generation workflows work?

Findings are mapped to this project where they apply, because the point of the
research is deciding what to change here.

## 0. A finding about the research itself

SearXNG's `general` engines are degraded right now. Half of the queries below
returned dictionary definitions instead of technical results:

| query | top results |
|---|---|
| `automated content generation pipeline architecture LLM stages` | Merriam-Webster "automated", Cambridge "automated" |
| `human-in-the-loop approval workflow AI generated content` | Wikipedia "Human", Human Benchmark, Britannica "human body" |
| `"content pipeline" LLM research draft review publish` | six dictionary entries for "content" |
| `why AI content automation fails quality drift` | Cambridge/Merriam-Webster "why" |
| `durable execution state machine retry idempotency` | Merriam-Webster "durable", durable.com |
| `Langfuse Braintrust LLM observability` | Bing Homepage Quiz ×5 |

Queries carrying a distinctive proper noun (`n8n`, `Langfuse`, `Instagram`,
`Glean`) returned good results. Queries built from common English words
collapsed to lexicography — the engines are matching a single salient token
rather than the phrase.

**This matters directly**: `search.py` uses `DEFAULT_CATEGORIES =
"general,it,news"` against the same backend. Every generic research query the
pipeline issues is subject to the same degradation. This is a plausible partial
explanation for thin research briefs that has nothing to do with the models —
and it is consistent with the earlier measurement in this project that `general`
alone returned 0 results while `general,it,news` returned 106.

## 1. The canonical pipeline shape

Every description converges on the same spine:

```
ingest → research/ground → draft → review (automated) → review (human) → publish
```

Glean's content-review piece states it as
`draft → brand voice edit → fact check → compliance review → final approval → publish`.
A smaller n8n build described on r/n8n is
`one content input → rewrite/adapt per platform → preview to Telegram for
approval → post across channels`.

That second one is almost exactly this project's architecture, independently
arrived at. The Telegram-preview-then-publish pattern is not unusual; it is the
common shape for single-operator content automation.

**Where this project differs, favourably**: stages here are pure `Item → dict`
functions with the status column as the state machine, so a crash resumes at the
last completed stage. Visual-workflow builds generally re-run the whole graph.

## 2. Guardrails belong before generation, not after

Glean's strongest claim: teams that set guardrails *before* generating, rather
than reviewing after, spend far less time in review. Three concrete
pre-generation controls:

1. **Risk-tier the content first.** Review depth is decided before a draft
   exists. "Low-risk content — routine summaries, internal updates, social
   replies — can move through a lighter review path."
2. **Ground in approved sources**, not general training data.
3. **Define voice as measurable rules** — "approved terminology, sentence
   patterns, point-of-view conventions, reading level targets, and concrete
   examples" — rather than adjectives.

Point 3 is the external confirmation of something this project learned the hard
way today: prose rules get partially ignored, worked examples get imitated. The
few-shot eval measured 3/3 correct enumeration decks with an example versus 2/3
without, on identical model and data.

Point 1 is not implemented here and maps cleanly onto the existing `intent`
field. A `list` request that found 8 well-starred repos needs less scrutiny than
a news claim about a named company.

## 3. Regression datasets are built from production failures

Arthur AI's method, which is directly applicable to the eval harness built
today:

1. **Capture whole traces, not final outputs** — "many agent failures happen in
   the execution path, not the final output."
2. **Classify failure modes** (tool selection, retrieval, orchestration) to find
   patterns.
3. **Sanitize** before the trace becomes a permanent fixture.
4. **Behavioural assertions over string matching** — correct sequencing,
   grounded facts, absence of forbidden actions, schema conformance. Make evals
   "binary" and "specific"; avoid subjective ranges.
5. **Version datasets alongside prompt versions**, 20–50 high-signal cases to
   start, clustered to avoid redundancy.
6. **Gate releases on it** — block deploys that reintroduce fixed failures.

Explicitly: prefer **deterministic validators over LLM judges**, and keep eval
data separate from any fine-tuning data.

Langfuse adds:
- "100 diverse items beat 1000 near-duplicates" — coverage over count.
- "A dataset of easy cases scores 95 percent forever and tells you nothing."
- Treat the set as an **append-mostly log**; archive rather than delete when a
  behaviour disappears.
- **Spot-check the evaluator** against real outputs before trusting any
  aggregate.
- Investigate item-level divergence rather than dismissing offsetting swings as
  noise.

## 4. How this maps to what is already here

| Practice | Status in this project |
|---|---|
| Staged pipeline, resumable | done — status column is the state machine |
| Human approval gate before publish | done — `WORKER_HALTS`, Telegram |
| Deterministic validators over LLM judges | done — `eval_compose.py` scores mechanically, by explicit design |
| Behavioural, binary assertions | done — `empty_slides`, `enumeration_ok`, `bad_repo_urls` are all binary/count |
| Ground facts in code, not prompt | done — `_restore_repo_facts`, `ensure_closing_slide`, `MAX_HASHTAGS` |
| Relevance gate on retrieved material | done today — news path had one, enumeration path did not |
| Worked examples over prose rules | measured, **still default-off** (`FEW_SHOT_EXAMPLES`) |
| Failure-derived regression dataset | **partial** — eval runs over live DB items, not a curated pinned set |
| Dataset versioned with prompt version | **missing** — runs are tagged, prompts are not |
| Whole-trace capture | **missing** — `events` records status transitions only, no field history |
| Risk tiering before generation | **missing** |
| CI gate on regressions | **missing** — no linter configured either |

## 5. The gaps worth acting on, in order

1. **Whole-trace capture.** `events` stores `from_status`/`to_status` only. When
   a caption was overwritten today the previous value was unrecoverable, and
   every eval rerun costs full model calls because prior outputs were not kept.
   Arthur's step 1 exactly. Cheapest high-value change.

2. **Pin the eval set.** Scoring "the first 16 items with a brief" means the set
   changes as the DB changes, so two runs are not always comparable. Curate
   20–50 items including today's known failures — panpsychism misclassification,
   the `point`-collapse enumeration, the missing-closing-slide decks — and pin
   them. This is Langfuse's "source from production traces" plus Arthur's
   "20–50 high-signal cases".

3. **Version prompts alongside eval runs.** Runs are tagged (`baseline`,
   `fewshot`) but nothing records which prompt produced them, so an old result
   cannot be attributed later.

4. **Risk tiering.** `intent` already exists; adding a tier that decides review
   depth is a small change with a clear payoff.

5. **A CI gate.** `coding-standards.md:15` still reads
   `Formatter / linter: <cmd>` and no linter is installed, so the "lint clean"
   non-negotiable is currently unenforceable.

## 6. Platform facts confirmed

From Meta's own content-publishing documentation: Instagram accounts are limited
to **100 API-published posts per rolling 24 hours**, and **a carousel counts as
a single post**. This project currently caps at 50, chosen when Meta's docs
appeared to contradict themselves. The 100 figure is what the live docs state;
the conservative 50 is not harmful, but the discrepancy is now resolved in
favour of 100.

Unchanged and still binding: **carousels hold at most 10 slides**, which is why
a "13 slide carousel" cannot be produced, and why an enumeration must choose
between a `links` slide and a `follow` slide rather than carrying both alongside
8 repos.

## Sources

- Glean — *How to implement an AI content review workflow*
  https://www.glean.com/perspectives/how-to-implement-an-ai-content-review-workflow
- Arthur AI — *AI Agent Regression Testing From Production Failures*
  https://www.arthur.ai/column/regression-test-datasets-ai-agents-production-failures
- Langfuse — *Golden dataset evaluation*
  https://langfuse.com/resources/engineering/golden-dataset-evaluation
- r/n8n — *Building an AI social media approval + distribution workflow in n8n*
  https://www.reddit.com/r/n8n/comments/1ruewr3/ (snippet only; reddit.com not fetchable)
- Meta for Developers — *Instagram Platform, Content Publishing*
  https://developers.facebook.com/documentation/instagram-platform/content-publishing
- QASkills — *How to Build a Golden Dataset for LLM Evaluation*
  https://qaskills.sh/blog/golden-dataset-llm-evaluation-guide
- Developers Digest — *Langfuse vs Braintrust vs Helicone*
  https://www.developersdigest.tech/blog/langfuse-vs-braintrust-vs-helicone

## Open threads not chased

- Cost/model-routing strategies for multi-stage pipelines — searches collapsed
  to dictionary results; would need different phrasing.
- Posting cadence and engagement effects — out of scope for the root question.
