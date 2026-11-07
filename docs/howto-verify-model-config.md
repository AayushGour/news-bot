# How to verify your OpenRouter model config won't silently break reproducibility

*Tester-verified how-to (T69/T86) — every step below was actually run and its
exact output is shown, not summarized. Architect owns the general setup guide
(`setup.md`); this is the narrower "did I configure a safe model" check.*

## Why this exists

`OPENROUTER_MODEL_CHEAP`, `_GOOD`, and `_VISION` must each be a single, named,
free OpenRouter model — never one of OpenRouter's own `openrouter/*` router
slugs (`openrouter/free`, `openrouter/auto`, and similar). A router picks a
different upstream model on every call, so nothing about a run using one is
reproducible, and it is refused at startup, not discovered mid-run.

## Step 1 — run preflight before starting the process

```
./.venv/bin/python scripts/preflight.py
```

This is the fast, no-side-effects way to check your `.env` — it stops after
the config check and never touches Telegram, the DB, or your channels if the
config itself is bad.

## Step 2 — what a bad config looks like

Set an auto-router slug (deliberately, to see the failure) and re-run:

```
$ export OPENROUTER_MODEL_CHEAP=openrouter/free
$ ./.venv/bin/python scripts/preflight.py
==============================================================
  OK    imports: all runtime dependencies present
 FAIL   config incomplete
         OpenRouter auto-router slugs are not allowed: OPENROUTER_MODEL_CHEAP='openrouter/free'. An auto-router serves a different model on every call, so no run is reproducible and no A/B result can be trusted — pin one specific ':free' model slug instead (browse them at https://openrouter.ai/models?max_price=0).
```

Verified this is the *same* failure the real process gives — `preflight.py`
is not a separate, weaker check:

```
$ ./.venv/bin/python -m pipeline
...
pipeline.config.MissingConfig: OpenRouter auto-router slugs are not allowed: OPENROUTER_MODEL_CHEAP='openrouter/free'. ...
```

The process exits (code 1) before opening the database, starting the
Telegram client, or making any network call — the failure is at startup, not
three stages into a run.

A **paid** slug (not a router, just a model you'd be billed for) fails the
same way but with a different, distinctly-worded message — so you can tell
the two problems apart at a glance:

```
$ export OPENROUTER_MODEL_CHEAP=google/gemini-2.5-flash-lite
$ ./.venv/bin/python -m pipeline
...
pipeline.config.MissingConfig: Only free OpenRouter models are allowed, but these are not free: OPENROUTER_MODEL_CHEAP='google/gemini-2.5-flash-lite'. Use a slug ending in ':free' (browse them at https://openrouter.ai/models?max_price=0).
```

Both failure messages point you at the same fix (browse
`https://openrouter.ai/models?max_price=0` and pick a `:free` slug that
is *not* under the `openrouter/` namespace) — but they tell you which of the
two problems you actually have.

## Step 3 — the default is already safe

If you have not overridden `OPENROUTER_MODEL_CHEAP` at all, you get the pinned
default:

```
$ unset OPENROUTER_MODEL_CHEAP
$ ./.venv/bin/python -m pytest tests/test_config.py -k default_cheap_model -q
1 passed
```

The default is `nvidia/nemotron-3.5-super-vl:free`. Verified live against
the request pattern the pipeline actually sends (`response_format:
json_schema`) for every schema the cheap tier uses (triage scoring, research
query planning, and the search-relevance gate) — each call came back as
valid, schema-conforming JSON, not free-form prose.

## Step 4 — a clean config passes silently

No news is good news — a valid config produces no output from the config
check at all, just continues into the live-service checks:

```
$ export OPENROUTER_MODEL_CHEAP=nvidia/nemotron-3.5-super-vl:free
$ ./.venv/bin/python scripts/preflight.py
==============================================================
  OK    imports: all runtime dependencies present
  OK    config: all required variables present
  OK    config: watching channels [...]
  OK    config: LLM_PROVIDER=openrouter
  ...
```

## What "auto-router" actually covers

Any slug starting with `openrouter/` is rejected — not just `openrouter/free`
and `openrouter/auto`. This is deliberate, not a bug: OpenRouter has shipped
one-off "cloaked" model releases under this same namespace in the past
(`openrouter/optimus-alpha`, `openrouter/horizon-beta`,
`openrouter/sonoma-sky-alpha` among them) — genuine single models at the time,
but silently withdrawable, which is the same reproducibility problem as a
router. Confirmed against the live OpenRouter catalogue: none of those three
names are listed today; the namespace currently serves six different
`openrouter/*` pseudo-models, and matching on the whole namespace catches
whichever ones exist on any given day rather than a fixed list that goes
stale.

Case and surrounding whitespace do not matter (`" OpenRouter/Free "` is
rejected the same as `openrouter/free`).
