# How to verify the research corpus budget packer (T70/D5)

*Tester-verified how-to (T94/T70) — every command and output below was
actually run and is pasted verbatim, not summarized. Architect owns the
general setup guide (`setup.md`); this is the narrower "is my local fallback
protected from silently dropping research material" check.*

## Why this exists

When the pipeline's `cheap` LLM role falls back to the local Ollama model
(`llm_provider=ollama`, or OpenRouter exhausted its retries), that model is
loaded with a fixed context window (`NUM_CTX_CHEAP`, default 8192 tokens).
Research articles are fetched uncapped, so three full articles can easily add
up to more text than that window holds — and Ollama does not error on
overflow, it silently drops whatever doesn't fit. `src/pipeline/budget.py`
fixes this: it fair-shares the corpus across sources and, for anything it has
to cut, leaves an explicit `[... N chars omitted ...]` marker so the loss is
visible instead of silent. Nothing is ever dropped without a trace.

## Step 1 — check what budget your configuration actually gets

```
$ ./.venv/bin/python -c "
from pipeline.config import Settings
from pipeline.budget import budget_tokens_for_role, budget_chars_for_role
s = Settings.load()
print('llm_provider:', s.llm_provider)
print('num_ctx_cheap:', s.num_ctx_cheap)
print('cheap budget, tokens:', budget_tokens_for_role(s, 'cheap'))
print('cheap budget, chars:', budget_chars_for_role(s, 'cheap'))
"
llm_provider: ollama
num_ctx_cheap: 8192
cheap budget, tokens: 6144
cheap budget, chars: 12288
```

(`6144 = 8192 - 2048` reserved for the system prompt/question/completion;
`12288 = 6144 tokens × 2 chars/token`, a deliberately conservative estimate —
see `CHARS_PER_TOKEN`'s docstring in `budget.py`.) If `LLM_PROVIDER=openrouter`
in your `.env`, the budget is far larger (a quarter of the shared 262k-token
window OpenRouter's free tier declares) — the packer only meaningfully
constrains the local fallback path, by design.

## Step 2 — see it fire on a real oversized corpus, with the log line it leaves

This runs the actual research stage function (`_research_one`) against three
~51,000-character fetched articles — well over the 12,288-char budget from
Step 1 — through the same code path production uses, with the INFO-level log
enabled so you can see exactly what an operator would see in `pipeline.log`:

```
$ ./.venv/bin/python <driver that calls research_mod._research_one(...) with
  three oversized fetched pages, INFO logging enabled>

INFO pipeline.stages.research: researcher 0: packed corpus into 12267/12288
chars budget: [{'url': 'https://a.example/1', 'kept': 4058, 'omitted': 43941},
{'url': 'https://b.example/1', 'kept': 4058, 'omitted': 40941},
{'url': 'https://c.example/1', 'kept': 4058, 'omitted': 49941}]
```

That line is your audit trail: it names every source URL, how many chars each
kept vs. omitted, and the total against the budget — grep `pipeline.log` for
`"packed corpus into"` to find every time this fired in production. Note all
three sources kept a fair, roughly-equal share (4058 chars each) rather than
one source starving the other two — that's the max-min fair-share guarantee.

The equivalent as a committed regression test, run directly:

```
$ ./.venv/bin/python -m pytest tests/stages/test_research.py \
    -k "packs_an_oversized_corpus or logs_a_manifest or leaves_a_small_corpus" -v
tests/stages/test_research.py::test_research_one_packs_an_oversized_corpus_for_the_local_budget PASSED
tests/stages/test_research.py::test_research_one_logs_a_manifest_when_it_truncates PASSED
tests/stages/test_research.py::test_research_one_leaves_a_small_corpus_byte_identical PASSED

======================= 3 passed in 0.34s =======================
```

## Step 3 — a normal-sized corpus is untouched, no marker, no log line

The common case (D11's live-tested finding: OpenRouter never truncates a
real research payload) is the packer doing nothing at all — a source that
already fits passes through byte-identical, with no marker and no log line.
`test_research_one_leaves_a_small_corpus_byte_identical` (passed above)
covers exactly this: a small fetched page's text survives verbatim into the
prompt (`"Body. Body." in prompt`), and `marker_present(prompt)` is `False`.

## Step 4 — confirm the hard guarantee (D5) holds for any input, not just these examples

D5's requirement is that bytes may be dropped, never without a trace: for
every source, `kept_chars + omitted_chars == original_chars`, always, and the
packed total never exceeds the budget it was given — including budgets too
small to even fit the marker itself. This is checked by a property test
against hundreds of randomly generated corpora and budgets on every run, not
just the two hand-picked examples above:

```
$ ./.venv/bin/python -m pytest tests/test_budget.py \
    -k "never_exceeds_budget_and_never_loses_a_char_unaccounted or 40k_char" -v
tests/test_budget.py::test_pack_never_exceeds_budget_and_never_loses_a_char_unaccounted PASSED
tests/test_budget.py::test_a_40k_char_three_source_corpus_packs_under_an_8192_token_budget PASSED

======================= 2 passed in 1.19s =======================
```

## What the marker looks like, and why you can trust it

A genuine omission marker is `[... N chars omitted ...]`, wrapped in an
invisible Private Use Area sentinel (U+E000) so it cannot be spoofed by a
fetched web page (or a crafted URL) containing that same literal text —
`_sanitize()` strips the sentinel from every source's id and text before
packing, so the wrapped form can only ever appear once, inserted by the
packer itself. `marker_present(text)` is the one function to call if you need
to check programmatically whether a given prompt was actually truncated.

## Full suite, from a clean environment

```
$ uv sync --extra dev
$ ./.venv/bin/python -m pytest -q
725 passed in 48.52s
```

(`uv sync --extra dev` is required — `hypothesis` is a dev dependency the
property tests need, and a missing `hypothesis` is a *collection* error that
runs zero tests, not one failure. See T91.)
