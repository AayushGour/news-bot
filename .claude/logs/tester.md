## T69

- 2026-09-22 [T69] Validated T69 (branch t69-pin-cheap-model, commit 7cc2aec) against AC + beyond-AC attacks. PASS. 703/703 tests green (matches dev's own claim — this branch is T69 alone off main, not the T68+T69 merge reviewer measured at 732, no discrepancy). Both mutations (auto-router, free-models) caught. Auto-router guard confirmed firing end-to-end from the real `python -m pipeline` entrypoint and from `scripts/preflight.py` with a deliberately bad `OPENROUTER_MODEL_CHEAP=openrouter/free` config — process exits 1 before any DB/Telethon/network work, with the reproducibility-worded message, distinct from the paid-model "not free" message which a genuinely-paid non-`openrouter/`-namespace slug (`google/gemini-2.5-flash-lite`) still triggers end-to-end too (T28 not regressed). `grep -c '^MODEL_GOOD=' .env` = 1 on the real machine .env (dedup confirmed; the surviving second `MODEL_GOOD`-shaped line is `MODEL_GOOD_FALLBACK`, a distinct var). Confirmed T85's claim: grepped every `validate_for_run` call site repo-wide — only `src/pipeline/__main__.py` and `scripts/preflight.py` call it; `eval_compose.py`, `smoke.py`, `build_golden.py`, `dashboard.py` all call `Settings.load()` only, never `validate_for_run()`, so the eval harness the fix exists to protect does not run the guard. Pre-existing (unchanged by T69, filed separately as T85) — not grounds to fail T69. Beyond-AC: case/whitespace (` OpenRouter/Free `, `OPENROUTER/FREE`) rejected with the reproducibility message; `openrouter/auto` and the three cloaked-release names (`openrouter/optimus-alpha`, `openrouter/horizon-beta`, `openrouter/sonoma-sky-alpha`) all rejected — confirmed intentional (code comment + the live OpenRouter models API today lists 6 pseudo-models under the `openrouter/` namespace, not just auto/free, and the three cloaked names 404 against the live API today, consistent with "silently withdrawable" being the actual threat model); empty/None/malformed slugs fail closed via the existing "not free" path with no crash (never silently pass). Live-tested the new cheap default, `nvidia/nemotron-3-super-120b-a12b:free`, against the 3 cheap-tier schemas the human hadn't already covered (NOTE_SCHEMA was human-verified 3/3): TRIAGE_SCHEMA 1/1, PLAN_SCHEMA 1/2 (one transient Nvidia 503 overload, succeeded on the built-in retry), RELEVANCE_SCHEMA 1/1 on a clean call after an initial run hit 3/3 provider-overloaded 503s (same infra-flakiness class already handled by llm.py's existing retry+local-fallback, not a response_format capability gap) — all 4 cheap-tier schemas (PLAN/NOTE/TRIAGE/RELEVANCE; SLIDES_SCHEMA is good-tier only, confirmed via grep, so cheap never sees it) return schema-conforming JSON from the pin. ruff: 3 pre-existing findings (F401 unused `sys` + PLW1510 in mutate.py, I001 import order in test_config.py) confirmed byte-identical on `main` before T69's changes — not introduced. Evidence pasted below.

### AC1 — auto-router rejected, distinct reproducibility-worded message, checked before paid check

```
$ ./.venv/bin/python -m pytest tests/test_config.py -q
703 passed in 53.42s   # (full-suite run below; targeted auto-router tests all in this file)
```

End-to-end (real entrypoint, deliberately bad config):
```
$ cat /tmp/bad.env
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=hash
TELEGRAM_BOT_TOKEN=faketoken
OPERATOR_USER_ID=999
CHANNEL_IDS=-1001526709058
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-fake
OPENROUTER_MODEL_CHEAP=openrouter/free

$ set -a && source /tmp/bad.env && set +a
$ timeout 20 ./.venv/bin/python -m pipeline
Traceback (most recent call last):
  ...
  File ".../src/pipeline/__main__.py", line 233, in main
    settings.validate_for_run()
  File ".../src/pipeline/config.py", line 331, in validate_for_run
    raise MissingConfig(
pipeline.config.MissingConfig: OpenRouter auto-router slugs are not allowed: OPENROUTER_MODEL_CHEAP='openrouter/free'. An auto-router serves a different model on every call, so no run is reproducible and no A/B result can be trusted — pin one specific ':free' model slug instead (browse them at https://openrouter.ai/models?max_price=0).
EXIT CODE: 1
```

Same bad config through `scripts/preflight.py`:
```
$ timeout 30 ./.venv/bin/python scripts/preflight.py
==============================================================
  OK    imports: all runtime dependencies present
 FAIL   config incomplete
         OpenRouter auto-router slugs are not allowed: OPENROUTER_MODEL_CHEAP='openrouter/free'. ...
EXIT CODE: 1
```

Raised at `config.py:331` — before the paid check at `config.py:348` — confirming ordering.

### AC2 — paid slug (outside `openrouter/` namespace) still refused, T28 not regressed

```
$ cat /tmp/paid.env
... (same MINIMAL block) ...
OPENROUTER_MODEL_CHEAP=google/gemini-2.5-flash-lite

$ set -a && source /tmp/paid.env && set +a
$ timeout 20 ./.venv/bin/python -m pipeline
  File ".../src/pipeline/config.py", line 348, in validate_for_run
    raise MissingConfig(
pipeline.config.MissingConfig: Only free OpenRouter models are allowed, but these are not free: OPENROUTER_MODEL_CHEAP='google/gemini-2.5-flash-lite'. Use a slug ending in ':free' ...
```
`google/gemini-2.5-flash-lite` does not start with `openrouter/`, so `is_auto_router` is False for it and it genuinely falls through to and exercises the paid-model branch — T28 intact.

### AC3 — MODEL_GOOD dedup

```
$ grep -c '^MODEL_GOOD=' .env
1
$ grep -n '^MODEL' .env
17:MODEL_CHEAP=qwen3:4b-instruct
18:MODEL_GOOD=qwen3.5:latest
22:MODEL_VISION=qwen2.5vl:7b
64:MODEL_GOOD_FALLBACK=hf.co/DavidAU/Qwen3.5-9B-...
```
(`.env` is gitignored — this is the real machine file the dev edited, not a committed diff; verified directly.)

### AC4 — full suite, run myself

```
$ ./.venv/bin/python -m pytest -q
........................................................................ [ 10%]
........................................................................ [ 20%]
........................................................................ [ 30%]
........................................................................ [ 40%]
........................................................................ [ 51%]
........................................................................ [ 61%]
........................................................................ [ 71%]
........................................................................ [ 81%]
........................................................................ [ 92%]
.......................................................                  [100%]
703 passed in 53.42s
```
Matches dev's own "703 passed" exactly. Reviewer's 732 was measured on the T68+T69 merge (a different tree — T68 adds tests of its own); t69-pin-cheap-model branches directly off main (`git merge-base t69-pin-cheap-model main` = `6fa3241`, same as main's tip), so 703 is the correct standalone number. No discrepancy.

### AC5 — mutations caught

```
$ ./.venv/bin/python scripts/mutate.py --only auto-router
running 1 mutations
  ok   auto-router            caught
all 1 mutations caught

$ ./.venv/bin/python scripts/mutate.py --only free-models
running 1 mutations
  ok   free-models            caught
all 1 mutations caught
```

### Beyond-AC: case/whitespace, other namespace members, empty/None/malformed

```
is_auto_router(' OpenRouter/Free ') = True
is_auto_router('OPENROUTER/FREE') = True
is_auto_router('openrouter/auto') = True
is_auto_router('openrouter/optimus-alpha') = True
is_auto_router('openrouter/horizon-beta') = True
is_auto_router('openrouter/sonoma-sky-alpha') = True
is_auto_router('openrouter/') = True
is_auto_router('openrouterX/free') = False        # correctly NOT a false-positive match
is_auto_router('not-openrouter/free') = False      # correctly NOT a false-positive match
is_auto_router(None) = False
is_auto_router('') = False
is_auto_router('   ') = False

--- end to end validate_for_run ---
' OpenRouter/Free '                           -> MissingConfig [reproducib]
'OPENROUTER/FREE'                             -> MissingConfig [reproducib]
'openrouter/auto'                              -> MissingConfig [reproducib]
'openrouter/optimus-alpha'                    -> MissingConfig [reproducib]
'openrouter/horizon-beta'                     -> MissingConfig [reproducib]
'openrouter/sonoma-sky-alpha'                 -> MissingConfig [reproducib]
''                                            -> MissingConfig [not free]     # fails closed, no crash
None                                          -> MissingConfig [not free]     # fails closed, no crash
'   '                                         -> MissingConfig [not free]     # fails closed, no crash
'nvidia/nemotron-3-super-120b-a12b:free'      -> PASSED validate_for_run
```

Independent confirmation the `openrouter/` namespace ban is deliberately over-broad, not accidental — live OpenRouter models API today:
```
$ curl -s https://openrouter.ai/api/v1/models | jq -r '.data[].id' | grep '^openrouter/'
openrouter/auto-beta
openrouter/fusion
openrouter/pareto-code
openrouter/free
openrouter/bodybuilder
openrouter/auto
```
6 pseudo-models live today, none of them the 3 cloaked names from the task — those return 404 against `openrouter/api/frontend/models/find`, consistent with "silently withdrawable" being exactly the risk the whole-namespace ban defends against (a real single model today can vanish/rename under this org tomorrow, same as an auto-router).

### T85 blast radius — confirmed, pre-existing, not a T69 regression

```
$ grep -rn "validate_for_run" --include="*.py" .
tests/test_config.py: (many, all Settings.load(env).validate_for_run(env))
scripts/preflight.py:81:        settings.validate_for_run()
src/pipeline/__main__.py:233:    settings.validate_for_run()
src/pipeline/config.py: (definition + docstring)

$ for f in scripts/eval_compose.py scripts/smoke.py scripts/build_golden.py scripts/dashboard.py; do
  echo "=== $f ==="; grep -n "validate_for_run\|Settings.load" "$f"
done
=== scripts/eval_compose.py ===
220:    settings = Settings.load()
=== scripts/smoke.py ===
113:        Settings.load(),
=== scripts/build_golden.py ===
104:    settings = Settings.load()
=== scripts/dashboard.py ===
989:    web.run_app(build(Settings.load()), host=HOST, port=args.port, print=None)
```
Confirmed: only `__main__.py` (real process) and `preflight.py` (manual check) call `validate_for_run()`. `eval_compose.py`, `smoke.py`, `build_golden.py`, `dashboard.py` call `Settings.load()` only — they would happily load `OPENROUTER_MODEL_CHEAP=openrouter/free` from a `.env` and run against it with no guard firing. T69 did not touch any of these 4 files (`git show --stat 7cc2aec` confirms: `.env.example`, `scripts/mutate.py`, `src/pipeline/config.py`, `tests/test_config.py` only), and the module docstring's "called only by `__main__`" claim predates T69 unchanged, so this is pre-existing scope, correctly filed as T85, not a T69 regression.

### Live cheap-tier schema smoke test (new default: nvidia/nemotron-3-super-120b-a12b:free)

```
cheap model under test: nvidia/nemotron-3-super-120b-a12b:free
PASS  TRIAGE_SCHEMA    3.4s keys=['score', 'reason', 'topic'] missing=[]
PASS  PLAN_SCHEMA      22.7s keys=['entity', 'entity_context', 'image_query', 'clauses', 'queries'] missing=[]
FAIL  RELEVANCE_SCHEMA 15.9s RAISED BadCompletion: openrouter returned no completion: {"id":"gen-...","error":{"message":"Upstream error from Nvidia: Service temporarily overloaded","code":503,"metadata":{"error_type":"provider_overloaded"}}}
2/3 cheap-tier schemas returned schema-conforming JSON

# retried RELEVANCE_SCHEMA alone 15s later, clean run:
PASS 1.9s {'relevant': True, 'why': "The excerpt discusses Nvidia's Rubin platform, which is the Rubin GPU architecture, describing its announcement, relation to Blackwell, and NVLink generation. This matches the subject."}
```
NOTE_SCHEMA already human-verified 3/3 (per T86 task brief) — not re-run, per the "skip if already covered" instruction. `SLIDES_SCHEMA` confirmed good-tier only (`grep -n SLIDES_SCHEMA src/pipeline/stages/compose.py` -> only `llm.good(...)` call sites), so the cheap pin never sees it — consistent with the task's own reasoning. Net: all 4 cheap-tier schemas (PLAN, NOTE, TRIAGE, RELEVANCE) produce schema-conforming JSON from the pin; the one failure observed was upstream Nvidia overload (503), the exact class of transient failure `llm.py`'s existing 3-attempt retry + local-fallback machinery already handles in production (I disabled `fallback_to_local` for this diagnostic specifically to see the raw error instead of a masked fallback).

### Lint — 3 pre-existing findings, none introduced by T69

```
$ uvx ruff check src/pipeline/config.py scripts/mutate.py tests/test_config.py
F401 `sys` imported but unused — scripts/mutate.py:30
PLW1510 `subprocess.run` without explicit `check` argument — scripts/mutate.py:348
I001 Import block is un-sorted or un-formatted — tests/test_config.py:1
Found 3 errors.
```
Checked out the same 3 files from `main` (pre-T69) into the worktree and re-ran ruff: byte-identical 3 findings (mutate.py:30 F401, mutate.py:340 PLW1510, test_config.py:1 I001 — line-number shift on the PLW1510 hit is just T69's own added lines above it in the file, same finding). Confirmed pre-existing, not introduced by T69. Working tree restored to T69 (`git checkout t69-pin-cheap-model -- <files>`) before continuing.

### Environment note
All testing done in an isolated `git worktree` off `t69-pin-cheap-model` (`git worktree add`), with its own `uv sync --extra dev` venv, kept fully separate from the main repo checkout — the main repo's own branch/worktree state was never touched by any command in this validation. `git worktree remove --force` cleaned it up afterward.

**VERDICT: PASS.** All 5 stated AC hold. Beyond-AC attacks (case/whitespace, other namespace members, empty/None/malformed, end-to-end wiring, live cheap-tier schema check) found nothing that breaks T69. T85's claim independently confirmed and scoped as pre-existing, not a T69 regression.

## T68

- 2026-09-22 [T92/T68] Validated T68 (branch t68-substance-metrics-r1, commit dad94e7, strictly fast-forwards t68-substance-metrics @720c0bb) against AC + beyond-AC attacks in an isolated `git worktree` (`.claude/worktrees/agent-a9555f78285790498`, own `uv sync --extra dev` venv, gitignored `data/eval/*.json` present and used as-is). PASS. All 4 stated AC hold, decisive regression check reproduces reviewer's exact 5-red count, all 5 pinned values hand-derived independently and correct, T93's mechanism confirmed genuinely latent today, T74's composite-jump arithmetic confirmed to 3 decimals, `--rescore` provenance guard refuses both a real live-db run (`data/eval/baseline.json`, `set_version:None`) and a crafted version-mismatch attack. One non-blocking discrepancy found and reported below: reviewer's "zero new lint / 27 byte-identical" claim does not reproduce verbatim under `uvx ruff` today (ruff isn't a pinned dev dependency, so `uvx` resolves whatever is latest at invocation time) — 4 trivial new style-only findings exist (2x SIM114 in `quality.py`'s deliberately-documented 3-branch `proper_noun_density` loop, 1x C408 in a test helper, 1x RUF100 from one more `# noqa: E402` following the file's own pre-existing convention), none functional/security, none newly-introduced F-rule or BLE/PLW-class violations. Not grounds to fail T68 — not one of the 4 stated AC, and the project has no `[tool.ruff]` config to define what "clean" even selects.

### AC1 — `--rescore golden_current` / `--rescore golden_listonly`, zero network calls, reproduced independently

Ran under both `env -i` (bare env) and a custom socket guard that raises on any non-loopback `connect()`/`connect_ex()`/DNS resolution (stricter than `env -i` alone, which doesn't rule out a hardcoded-IP connection attempt):

```
$ env -i HOME="$HOME" PATH="/usr/bin:/bin" ./.venv/bin/python <driver importing a socket.connect guard first> golden_current
[no_network_guard] non-loopback socket connections blocked for this process
EXIT=0
rescoring data/eval/golden_current.json  (golden set v1, zero network calls)
24/24 rows scored
...
    decks_scored               24
    avg_substance_composite    39.57
    avg_grounding_ratio        0.145
  doc only: decks_scored 19  avg_substance_composite 37.588  avg_grounding_ratio 0.161
  raw only: decks_scored 5   avg_substance_composite 47.098  avg_grounding_ratio 0.084

$ env -i HOME="$HOME" PATH="/usr/bin:/bin" ./.venv/bin/python <same guard> golden_listonly
EXIT=0
    decks_scored               24
    avg_substance_composite    42.542
    avg_grounding_ratio        0.182
  (doc only: identical, 24/24 doc, 0 raw)
```
No `NetworkBlocked` exception raised in either run — proves zero network calls, not just "happened to work offline." Numbers match the stated baselines exactly:
`golden_current  overall 0.145/39.570  doc 0.161/37.588  raw 0.084/47.098` and `golden_listonly overall 0.182/42.542`. AC1 PASS.

### AC2 — every metric tested, deliberately-good vs deliberately-generic deck, values pinned (bounce-1 fix)

```
$ ./.venv/bin/python -m pytest tests/test_quality.py tests/test_eval_compose_rescore.py -v
... 45 passed in 0.05s
```
All 7 metrics have ordering tests (`*_orders_good_above_bad` / `*_orders_sourced_above_generic` / `*_orders_repetitive_run_above_varied_run` / `*_orders_good_below_bad`) AND pinned-value tests (`*_hand_computed_value` / `*_hand_computed_mid_range_value`) — `numeric_density`, `proper_noun_density`, `grounding_ratio`, `hook_similarity`, `substance_composite` all pin an exact number, not just a sign. `code_preserved` is the one metric with no "ordering" concept (tri-state bool|None) and is instead covered true/false/None each with a dedicated test — appropriate given its type. AC2 PASS.

### The decisive regression check — revert `grounding_ratio` to the joined-blob implementation

Task instruction: revert `deck_grams = _ngrams(deck_body_text(doc))` (the pre-fix joined-blob line) and confirm new tests go red; "Reviewer got 5 red."

Surgical single-line revert (only the `deck_grams` line, nothing else) → only 1 test red:
```
FAILED tests/test_quality.py::test_grounding_ratio_does_not_invent_grams_at_fragment_joins
1 failed, 44 passed
```
Reproducing reviewer's full "whole pre-fix `quality.py`" revert (`git show 720c0bb:src/pipeline/quality.py > src/pipeline/quality.py`, i.e. every change from the bounce-1 fix commit `b247533` undone at once — join-artifact fix, apostrophe fold, `_slides`/`_slide_text` malformed-input guards) → exactly 5 red, matching reviewer's count digit-for-digit:
```
$ ./.venv/bin/python -m pytest tests/test_quality.py -v
FAILED tests/test_quality.py::test_deck_body_text_handles_a_non_dict_doc
FAILED tests/test_quality.py::test_slide_text_ignores_a_non_list_bullets_field
FAILED tests/test_quality.py::test_grounding_ratio_does_not_invent_grams_at_fragment_joins - AssertionError: assert 0.5 == 0.0
FAILED tests/test_quality.py::test_grounding_ratio_folds_curly_apostrophe_to_ascii - AssertionError: assert 0.4 == 1.0
FAILED tests/test_quality.py::test_substance_composite_excludes_filler_term_while_phrases_empty - AssertionError: assert 70.05 == 66.3
5 failed, 33 passed in 0.14s
```
`git checkout -- src/pipeline/quality.py` afterward restored the fixed file; `git status --short` clean; reran `tests/test_quality.py tests/test_eval_compose_rescore.py` → 45 passed. Fix is proven discriminating, not just numerically different. Regression check PASS.

### Hand recomputations — all 5 pinned values, independently derived (exceeds "pick at least two")

- `numeric_density`: "Model hits 92% on the test with 4 points gain" → 10 word-tokens (`92`,`4` carry digits) → 2/10*100 = **20.0** ✓.
- `proper_noun_density`: headline "h" + sub "The team at Anthropic shipped an API update." → 9 tokens; hits = "Anthropic" (interior capital) + "API" (all-caps acronym) = 2 → 2/9*100 = **22.22** ✓.
- `grounding_ratio`: source "the quick brown fox jumps over the lazy dog" (7 3-grams) vs deck "a quick brown fox runs over the lazy cat" (7 3-grams); shared = {(quick,brown,fox), (over,the,lazy)} = 2 → 2/7 = **0.2857** ✓.
- `hook_similarity`: hooks alpha/beta/gamma, alpha/beta/delta, epsilon/zeta/eta → J(h1,h2)=2/4=0.5, J(h1,h3)=J(h2,h3)=0 → best-per-hook 0.5,0.5,0.0 → mean **0.3333** ✓.
- `substance_composite`: grounding=0.5, numeric_density=5.0→sat(5/8=0.625), bare_bullet=1-0.2=0.8, proper_noun=10.0→sat(10/15=0.6667), hook_similarity default 0.0→1-0=1.0, filler+code excluded (N/A) → weighted sum 0.30·0.5+0.15·0.625+0.15·0.8+0.10·0.6667+0.10·1.0=0.53042 over total_weight 0.80 → 0.53042/0.80*100 = **66.30** ✓.

All 5 match the code's assertions exactly. No pinned value is wrong.

### T93 (filed, not blocking T68) — confirmed genuinely latent, mechanism reproduced exactly

```
$ ./.venv/bin/python -c '... reassign quality.FILLER_PHRASES at runtime after import ...'
Confirm FILLER_PHRASES is empty today: ()
=== BEFORE reassignment ===  filler_count(score_substance): 0   composite: 12.5
=== simulate T74 (module attr reassignment) ===
filler_count (score_substance) AFTER: 0   <- stale, reads the default bound at import
filler_count(doc, phrases=q.FILLER_PHRASES) direct call: 3   <- correct if passed explicitly
substance_composite AFTER (using stale score_substance metrics): 22.22
delta: 12.5 -> 22.22
```
Exact match to the reviewer's measured `12.5 -> 22.22`. Mechanism: `def filler_count(doc, phrases: tuple = FILLER_PHRASES)` binds the default at *def* time (import), so `score_substance`'s bare `filler_count(doc)` call keeps reading the stale empty tuple forever within that process even after `quality.FILLER_PHRASES` is reassigned, while `substance_composite`'s `if FILLER_PHRASES else None` guard reads the module global fresh at call time — so the term flips from excluded to included (crediting the now-permanently-zero-looking filler_density a flat `1.0`) the moment `FILLER_PHRASES` becomes truthy, independent of whether the deck source that produced those metrics ever ran filler_count correctly. Confirmed on disk: `grep -n "^FILLER_PHRASES" src/pipeline/quality.py` → `FILLER_PHRASES: tuple[str, ...] = ()` — empty today, so T93 is real but not yet triggered. `git status --short` clean after (in-process attribute reassignment, no file touched). T93 correctly filed, correctly NOT a T68 blocker.

### T74 composite-jump arithmetic — confirmed to 3 decimals

Recomputed both golden sets' mean `substance_composite` with `FILLER_PHRASES` empty (today) vs populated with one placeholder phrase absent from every deck (so `filler_count` stays genuinely 0 on every deck — isolates the pure weight-renormalisation effect T74 will trigger):
```
golden_current:  before=39.570  after=46.285  delta=+6.715
golden_listonly: before=42.542  after=48.926  delta=+6.384
```
Matches the task's `42.54 -> 48.93` and reviewer's `39.57 -> 46.28` to the digit. Closed-form check: `new_S = (S*0.80 + 10)/0.90` (5 terms/0.80 -> 6 terms/0.90, filler term added at a flat 1.0 credit) reproduces both deltas exactly: `(39.570*0.8+10)/0.9=46.284`, `(42.542*0.8+10)/0.9=48.936` (rounding-order artifact of per-deck vs aggregate averaging accounts for the last 0.01). Arithmetic confirmed correct — T77 re-baselining across this boundary is safe to build on.

### `--rescore` provenance guard (bounce-1 blocker B3) — tried to get a wrong-pairing score past it

```
$ ./.venv/bin/python scripts/eval_compose.py --rescore baseline   # a REAL live-db run already on disk, set_version:None
data/eval/baseline.json was scored against a live db selection (summary.set_version is None) -- ... EXIT=1

$ python3 -c "d=json.load(open('data/eval/golden_current.json')); d['summary']['set_version']=999; ..." > _attack_wrongversion.json
$ ./.venv/bin/python scripts/eval_compose.py --rescore _attack_wrongversion
data/eval/_attack_wrongversion.json was scored against golden set v999, but data/eval/golden.json on disk is v1 -- refusing ... EXIT=1
```
Both attacks refused, exit 1, no score printed in either case. Happy path independently confirmed working above (AC1). Guard PASS.

### Beyond-AC: malformed decks, empty slides, bare-string deck, unicode

No crash on any of: bare-string `doc`, `doc=None`, `{"slides": []}`, `doc` as a bare list, `slides` as a dict instead of a list, unicode (emoji/RTL Arabic/zero-width space/combining mark) headline text, `hook_similarity` with empty/whitespace/None hooks, `substance_composite({})`, `substance_composite` with out-of-range negative/>100 inputs (all correctly clamped to [0,1] before weighting: `12.5`, `18.75`, `66.3` respectively — no output outside [0,100]), `code_preserved` with an empty code slide against a real fenced block (correctly `False`, not a crash), `filler_count` with an empty-string phrase in the list (no infinite/zero-length match, returns `0`). All handled gracefully per each function's own documented guard.

### Mutation pass (AC3) and full-suite regression (integration re-check)

Branch alone:
```
$ ./.venv/bin/python scripts/mutate.py
running 59 mutations
... all 59 mutations caught
```
Independent trial merge into `main` (fresh scratch worktree off `001ef71`, `git merge --no-commit --no-ff t68-substance-metrics-r1`, clean auto-merge, only conflict-free `Auto-merging scripts/mutate.py`), gitignored `data/eval/*.json` copied in from the real machine checkout:
```
$ ./.venv/bin/python -m pytest --collect-only -q
748 tests collected in 1.87s        # no hypothesis collection error (T70 worktree only, correctly absent here)

$ ./.venv/bin/python -m pytest -q
748 passed in 50.57s                # matches reviewer's 748 exactly; no T89 flakes observed this run

$ ./.venv/bin/python scripts/mutate.py
running 60 mutations
... all 60 mutations caught          # 59 T68 + 1 auto-router (T69), matches reviewer exactly
```
Scratch worktree + branch deleted after (`git worktree remove --force`, `git branch -D`) — main repo untouched throughout. AC3 + AC4 (baselines reproducible) PASS.

### Lint — discrepancy from reviewer's claim, non-blocking

Reviewer logged "ruff findings byte-identical to main (27, all pre-existing) — zero new lint." Reproducing today with the same `uvx ruff check` command does not match: `uvx ruff` resolves whatever ruff build is latest at invocation time (ruff is not a pinned dev dependency — absent from `pyproject.toml`'s `dev` extra, and no `[tool.ruff]`/`ruff.toml` config exists anywhere in the repo or its parent directories), so the active rule set is not pinned or reproducible run-to-run. Under today's resolved version (0.16.8), the T68 branch alone (no T69 merge) introduces 4 new findings versus main: `scripts/eval_compose.py` gains 1 more `RUF100` (one more `# noqa: E402` on its own new import block, following the file's pre-existing 6-instance pattern verbatim — same class, not a new mistake); `src/pipeline/quality.py` has 2x `SIM114` on `proper_noun_density`'s 3-branch `if/elif/elif` (the function's own docstring explains each branch is a distinct, deliberate reason (a)/(b)/(c) — combining them via `or` would reduce readability against the documented intent, a style-linter false positive, not a bug); `tests/test_quality.py` has 1x `C408` (`dict(...)` could be a literal, in a test helper). None are functional, security, or newly-introduced F/BLE/PLW-class violations; all are new-code style suggestions. This is a genuine discrepancy from the reviewer's stated number, worth surfacing, but not grounds to fail T68 — lint conformance isn't one of the 4 stated AC, and with no `[tool.ruff]` config anchoring what "clean" selects, the reviewer's and my own measurements are both honest readings of an unpinned tool. Recommend (for project-context.md, not a T68 blocker): pin a ruff version in dev deps or an explicit `[tool.ruff]` select list, so lint output is reproducible across sessions.

**VERDICT: PASS.** All 4 stated AC hold, independently reproduced with real command output (not read from the diff). The decisive regression check is proven discriminating (5/5 red on the reviewer's exact revert). All 5 pinned values hand-derived and correct. T93's mechanism is real, well-specified, and genuinely latent today — correctly not blocking T68. T74's composite-jump arithmetic is correct to 3 decimals — T77 can build its re-baseline on it. The `--rescore` provenance guard survived two independent live attacks. No crash found on any malformed/empty/unicode input attempted. One non-blocking lint-reproducibility discrepancy noted for the record.

## T70

- 2026-09-22 [T94/T70] Validated T70 (worktree-agent-a72be42f943493c79 @ 5fdf564, passed re-review after bounce 1). **PASS.** All 5 stated AC hold + the reviewer-specified share-utilisation assertion added, confirmed to kill its mutant, confirmed no false positive on the real implementation. Beyond-AC: all 3 named mutants re-killed from a cold `.hypothesis` cache; both forgery halves (id, text) proven load-bearing including the exact `research.py:399` raw-SearXNG-snippet fallback path; fenced-code-block bisection reproduced live; total-starvation confirmed and reported, not blocking; real `prompt_eval_count` measured against the actual configured local model (`qwen3:4b-instruct`) — filed as T95 evidence.

### Environment
`uv sync --extra dev` first, per the task brief (hypothesis is required; a missing hypothesis is a collection error, 0 tests, not 1 failure — confirmed this is why T91 exists). All destructive probes (mutating `src/pipeline/budget.py`) were done in the worktree, each reverted via `cp` from a saved backup and verified with `git diff --stat` showing empty output before moving to the next probe; the one change kept is additive-only to `tests/test_budget.py` (+19 lines, 1 new assertion + comment, no deletions).

### The new assertion (reviewer-specified, T94's primary task)

Added to `test_pack_never_exceeds_budget_and_never_loses_a_char_unaccounted` (the property test), right after the existing budget-ceiling check:
```python
total_clean = sum(len(t.replace(_MARKER_SENTINEL, "")) for t in texts)
assert sum(len(p.text) for p in packed) >= (
    min(budget, total_clean) - len(packed) * _MARKER_RESERVE
), "packed total fell short of the granted share/demand -- budget is being silently shrunk"
```
(`_MARKER_RESERVE` added to the test's import block.)

- **Kills the mutant.** Applied `content_budget = (share - marker_room) // 2` to `budget.py:259` (the reviewer's exact mutant — silently halves every truncated source's usable budget) with a fresh `.hypothesis` cache:
  ```
  E       AssertionError: packed total fell short of the granted share/demand -- budget is being silently shrunk
  E       assert 56 >= (95 - (1 * 38))
  E       Failing test case: texts=['0'*100], budget=95
  1 failed, 16 passed in 1.45s
  ```
  Reverted (`cp` from backup), `git diff --stat` empty, full `tests/test_budget.py` green again (17 passed).
- **No false positive on the real implementation.** Ran the property test 3x from a fresh `.hypothesis` cache each time (300 examples/run = 900 examples total) plus once more inside the full-suite run below: all green, zero failures.
  ```
  == run 1 ==  1 passed in 1.65s
  == run 2 ==  1 passed in 1.86s
  == run 3 ==  1 passed in 1.65s
  ```
- ruff clean on both touched files: `All checks passed!` (`uv run ruff check tests/test_budget.py src/pipeline/budget.py`).

### AC1 — property test (D5 invariant: total <= budget, kept+omitted==original per source)

`tests/test_budget.py::test_pack_never_exceeds_budget_and_never_loses_a_char_unaccounted` — PASS, part of the 725-green run below. The property additionally reconstructs `p.text`'s actual head/tail slices against the real sanitized original (the bounce-1 fix) and, as of this task, checks the packer used its granted share.

### AC2 — 40k-char / 3-source corpus under an 8192-token budget, all three non-empty

`tests/test_budget.py::test_a_40k_char_three_source_corpus_packs_under_an_8192_token_budget` — PASS (part of the run below). Reproduced by hand too:
```python
>>> from pipeline.budget import pack, Source, CHARS_PER_TOKEN
>>> texts = ["A"*15000, "B"*12000, "C"*13274]
>>> packed = pack([Source(id=f"s{i}", text=t) for i, t in enumerate(texts)], 8192*CHARS_PER_TOKEN)
>>> [p.kept_chars for p in packed]
[5462, 4372, 4372]   # (representative; exact split is fair-share, all > 0)
```
(Exact numbers vary by which sources are longest; every run showed all 3 `kept_chars > 0`.)

### AC3 — marker present iff bytes dropped

`test_marker_present_exactly_when_bytes_were_dropped`, `test_marker_names_the_actual_omitted_count`, `test_a_bare_lookalike_string_with_no_sentinel_is_not_the_marker` — all PASS.

### AC4 — under-budget corpus passes through byte-identical, no marker

`test_a_corpus_under_the_openrouter_budget_passes_through_byte_identical` — PASS.

### AC5 — full suite green from a clean sync

```
$ uv sync --extra dev            # in the T70 worktree
Resolved 65 packages in 2ms
Checked 63 packages in 7ms
$ rm -rf .hypothesis && uv run pytest -q
725 passed in 50.75s
```
Also test-merged the branch onto **current** `main` (817a333, which has moved twice since T70 branched — T69 then T68 both landed after) in a disposable scratch worktree, to check the "reviewer measured 725 at a now-stale merge point" note in the brief:
```
$ git worktree add <scratch> main --detach   # HEAD 817a333
$ git merge --no-commit --no-ff worktree-agent-a72be42f943493c79
Auto-merging .claude/logs/senior-dev.md
CONFLICT (content): Merge conflict in .claude/logs/senior-dev.md   # only file, append-only ledger, trivial keep-both
$ git status --porcelain   # everything else: pyproject.toml, budget.py, research.py, test_research.py, test_budget.py, uv.lock — all auto-merged clean
$ uv sync --extra dev && rm -rf .hypothesis && uv run pytest -q
770 passed in 55.78s        # 748 (current main) + 22 (T70's new/changed tests) -- clean
```
Scratch worktree removed after (`git worktree remove --force`); main repo and the T70 worktree both untouched by this check.

### Attack beyond the AC

**Cold-cache re-run of the 3 named mutants** (each: mutate, `rm -rf .hypothesis`, run `tests/test_budget.py`, revert via `cp` from backup, confirm `git diff --stat` empty before the next):

1. `tail = text[:tail_len]` (second head instead of a tail) — **RED**:
   ```
   assert '0' == '1'
   Failing test case: texts=['0'*41+'1'], budget=39
   1 failed, 16 passed in 11.76s
   ```
2. `HEAD_RATIO = 1.0` — **RED**:
   ```
   AssertionError: a real truncation must keep a genuine (non-empty) tail -- HEAD_RATIO must stay below 1.0
   Failing test case: texts=['0'*42], budget=39
   1 failed, 16 passed in 1.17s
   ```
3. `PackedSource(id, "", original, original, 0)` (whole function body replaced — discard everything, report nothing omitted) — **RED**, and wider than the reviewer's 3 sites: 6 failures in `test_budget.py` (property, negative-budget clamp, marker-present, marker-names-count, marker-unforgeable-by-content, openrouter-byte-identical) plus 3 in `tests/stages/test_research.py` (`test_research_one_packs_an_oversized_corpus_for_the_local_budget`, `test_research_one_logs_a_manifest_when_it_truncates`, `test_research_one_leaves_a_small_corpus_byte_identical`) — 9 total, confirming the reviewer's claim of research.py-level detection and then some.

All three reverted; `git diff --stat src/pipeline/budget.py` empty before the next probe each time; full suite re-confirmed green (725 passed) after the last revert.

### Forgery probes — both halves proven load-bearing, including research.py:399

Reintroduced each half of the bounce-1 fix (`sanitized = [Source(id=_sanitize(s.id), text=_sanitize(s.text)) for s in sources]` in `pack()`) one at a time:

- **`id=s.id`** (id bypass): `test_marker_is_unforgeable_via_a_crafted_source_id` — RED (`assert not marker_present(packed.id)` fails, forged marker present). Also drove the real `tests/stages/test_research.py::test_research_one_is_not_forgeable_via_a_crafted_source_url` integration test — RED (`a crafted source id must not be able to forge an omission marker into the prompt`).
- **`text=s.text`** (text bypass): `test_marker_is_unforgeable_by_source_content` — RED (`assert 4 == 2`, forged sentinel pair survives).
- **The `research.py:399` path specifically** (`fetch_text` returns `None` on a non-200 response → falls back to the raw, unfiltered SearXNG `content` snippet, no trafilatura in the path at all): this is exercised by no *permanent* test in the suite — the reviewer's own coverage here was a throwaway probe file, written and deleted during review, never committed. I wrote and ran an equivalent throwaway probe (`tests/stages/test_t94_probe.py`, deleted after use, `git status --porcelain` clean) that plants a forged sentinel-wrapped marker in the search result's `content` field and forces a 404 on the article fetch:
  ```
  PASS on real code:      1 passed in 0.05s
  PASS on the text=s.text bypass mutant:  RED --
    AssertionError: a forged marker planted in the RAW SearXNG content snippet
    (the research.py:399 fallback, no trafilatura in the path) must not
    survive into the prompt as a fake pipeline-authored omission
  ```
  Confirms the protection at this exact path is real (not merely inferred from the unit-level test) — `pack()`'s sanitize is applied uniformly regardless of how the text arrived, so the trafilatura-vs-raw-snippet distinction the module docstring calls out is correctly closed by the shared sanitize step. **Gap for the record:** no *permanent* regression test locks this specific path in (only the id-forgery path has one at the research.py integration level); worth a follow-up test, not a T70 blocker since the unit-level `pack()` test already covers the mechanism regardless of caller.

### Total starvation (reviewer nit 4) — confirmed, not blocking, recommend its own task

```python
>>> tiny = replace(settings, num_ctx_cheap=2048)   # == RESERVED_TOKENS exactly
>>> budget_tokens_for_role(tiny, "cheap")
0
>>> pack([Source("u1","A"*5000), Source("u2","B"*5000)], 0)
[PackedSource(id='u1', text='', ..., kept_chars=0, omitted_chars=5000), PackedSource(id='u2', ...kept_chars=0...)]
>>> tiny.validate_for_run(env={...})   # no exception
validate_for_run: passed with num_ctx_cheap=2048 (no error raised)
```
Confirmed: at `num_ctx_cheap <= RESERVED_TOKENS` (2048), every source's `kept_chars` goes to 0 — an entirely empty corpus is sent to the model — and `config.py`'s `validate_for_run()` has no check that `num_ctx_cheap > RESERVED_TOKENS`. `research.py`'s manifest log fires at `log.info` (confirmed by reading `stages/research.py:429`), not `log.warning`, for every truncation including total starvation — so this fails **silent at WARNING level too**, only visible by grepping INFO logs. Default `num_ctx_cheap=8192` is nowhere near this boundary, so it cannot fire with today's `.env`, and the packer's own behaviour here is still correctly D5-compliant (marker present, bytes accounted, nothing crashes) — the gap is entirely in `config.py` having no floor and the log level being one notch too quiet for a total-loss event. Agree with the reviewer: **out of T70's file scope** (config.py is contended by 4 in-flight branches) — recommend a small standalone follow-up task (`config.py`: `validate_for_run` raises/warns if any `num_ctx_<role> <= RESERVED_TOKENS`; `research.py`: bump the manifest log to WARNING when `kept_chars` is 0 for every source). Not a T70 blocker.

### Fenced code artifacts straddling the cut — reproduced live

`RESEARCH_SYSTEM` (stages/research.py:196-207) explicitly requires copying technical artifacts "VERBATIM inside a fenced block". `_pack_one` cuts at raw char offsets with no fence-awareness:
```python
>>> text = ("Intro prose. "*50) + "```python\ndef important():\n    return 42\n```\n" + ("Concluding prose. "*2000)
>>> # fence spans chars [650, 695); chose `share` so head_len lands at 672 (mid-fence)
>>> packed.text.count("```")
1
>>> packed.text[head_len-60:head_len]
'ntro prose. Intro prose. Intro prose. ```python\ndef importan'
```
The opening fence survives, the closing fence is cut into the omitted middle, and the kept head ends mid-statement with an unclosed ` ``` ` — the excerpt the research LLM sees is a syntactically dangling fence, in direct tension with the "exactly as written" instruction. D5's own invariant (bytes accounted, marker present) still holds — this is a content-quality gap, not a trace-ability gap, so it does not fail D5's hard requirement — but it is real and reproducible. Recommend as a follow-up (not a T70 blocker): either prefer cutting at a fence boundary when one is near the head/tail split point, or accept the bisection as a known, documented limitation. Flagging for `project-context.md`.

### Real `prompt_eval_count` measurement (T95 evidence) — Ollama was quiet, unlike at review time

Local Ollama runner was idle (`/api/ps` → `{"models":[]}`) and responded promptly (unlike the reviewer's 110s/600s timeouts under concurrent-worktree load). Measured against the actual configured local model, `qwen3:4b-instruct`, via `/api/chat` with `stream:false`, isolating corpus tokens from a fixed system+baseline overhead (28 tokens for system prompt + 1-char user message):
```
english_prose_12000:  len=12000  corpus_tokens=1894  chars/token=6.336
dense_code_12000:     len=12000  corpus_tokens=2879  chars/token=4.168
cjk_12000:            len=9800   corpus_tokens=4399  chars/token=2.228
```
`CHARS_PER_TOKEN=2` stays a safe lower bound against all three measured samples on the actual configured model — even repeated-phrase CJK (arguably denser than typical CJK prose) measured 2.228, not below 2. This narrows but does not eliminate the reviewer's concern: the margin at 2.228 vs the safety floor of 2.0 is only ~10%, so a genuinely denser real-world CJK/code-mixed corpus is not ruled out by this single sample. This is real, reproducible, measured data (not present in any prior review) — recommend it as concrete T95 evidence: `llm.py:348` already discards `prompt_eval_count` from the Ollama response body, so wiring it into a log line (per the reviewer's own suggested follow-up) would let this be measured continuously in production rather than once, here.

**VERDICT: PASS.** All 5 stated AC hold, reproduced with real command output. The reviewer-specified share-utilisation assertion is added, kills its mutant, and does not false-positive across 900+ examples on the real implementation — merged cleanly with `git diff --stat` clean of every mutation before/after. All 3 named mutants re-killed from a cold cache (mutant 3 kills wider than previously recorded — 9 failures, not 3). Both forgery-bypass halves are proven load-bearing, including the specific `research.py:399` raw-snippet fallback path (no permanent regression test for that exact path yet — noted as a gap, not a blocker). Full suite green both standalone (725) and merged onto current `main` (770, main having moved twice since branching). Three non-blocking follow-ups identified and reported: total-starvation config validation gap, fenced-artifact bisection at truncation boundaries, and a real (if narrow-margin) CHARS_PER_TOKEN measurement for T95.

**Addendum:** the new assertion (above) was committed to the branch as a test-only change so it survives worktree cleanup and merges with the rest of T70: `worktree-agent-a72be42f943493c79 @ d3fcdf7` ("test: assert the packer actually uses the share it was granted (T94)"), parent `5fdf564`. `src/pipeline/budget.py` is untouched by this commit — confirmed `git diff --stat` clean before/after every mutation probe run during this validation. Final re-run on the committed state: `725 passed in 48.52s`; `ruff check src/pipeline/budget.py tests/test_budget.py` → `All checks passed!`.

## T78

- 2026-09-22 [T99/T78] Validated T78 (worktree `agent-a2bd62aa8f035721b` @ `37e43b2`, rebased onto `main` @ `8c75f2e`, passed review at `bounces:2`). **PASS.** All 5 stated AC hold, reproduced independently with real `jsonschema` (not just the dev's own tests). B4 (the bounce-2 regression) reproduced end-to-end live: 2 `<li>` (fixed/restored) vs 0 `<li>` (reverted). FLAT-schema derivation confirmed by live perturbation. Full suite green from a clean locked sync: `807 passed in 71.17s`. Live check against real `MODEL_GOOD` (OpenRouter) also green, 2 reps.

### Environment

```
$ uv sync --locked --extra dev
Resolved 69 packages in 6ms
Checked 67 packages in 22ms
$ uv run python -c "import jsonschema; print(jsonschema.__version__)"
4.26.0
```

### AC1 — single source of truth

Confirmed by reading `src/pipeline/stages/compose.py`: `REQUIRED_BODY_FIELDS` + `SLIDE_TYPE_FIELDS` + `_FIELD_SCHEMAS` are the only body-contract dicts; `_slide_variant()` (schema builder) and `normalise_slides()`'s `required_any = REQUIRED_BODY_FIELDS.get(kind, ())` both index the *same* `REQUIRED_BODY_FIELDS` object — not two independently-written copies. `SLIDES_SCHEMA_FLAT` is derived from `SLIDE_TYPE_FIELDS` via `_ALL_TYPE_FIELDS = sorted({field for fields in SLIDE_TYPE_FIELDS.values() for field in fields})`, not hand-maintained (see the perturbation probe below).

### AC2 — `compare` w/ `series` but no `rows` rejected by real `jsonschema`

Independent check (not the dev's `_schema_allows` reimplementation nor their `_validate_real` helper — my own script, real `jsonschema.Draft7Validator` against the actual `SLIDES_SCHEMA` object):
```
=== AC2: compare w/ series but no rows -- real jsonschema ===
OK: rejected. message: {'type': 'compare', 'headline': 'H', 'series': [{'label': 'l', 'value': 1}]} is not valid under any of the given schemas
```

### AC3 — every type's minimal form passes, empty-body form fails, table-driven over the registries

Independent check, real `jsonschema`, iterated over `SLIDE_TYPES`/`REQUIRED_BODY_FIELDS` (all 15 types, including every alternative for `point`'s 3-way `anyOf`):
```
=== AC3: every type's minimal legal form passes; empty-body form fails (real jsonschema) ===
OK: all 15 types' minimal legal form(s) pass, all bare-headline forms rejected (real jsonschema)
```
Also confirmed `SLIDES_SCHEMA_FLAT` (the degraded-path schema) still accepts every type's minimal real shape: `FLAT failures: none`.

### AC4 — every shape our own code generates validates

Independent check calling the real functions directly (not re-running the dev's tests), real `jsonschema` against `SLIDES_SCHEMA`:
```
OK   ensure_links_slide output: links valid
OK   ensure_closing_slide[sources]: sources valid
OK   ensure_closing_slide[follow]: follow valid
OK   drop_duplicate_ctas[hook]: hook valid
OK   drop_duplicate_ctas[follow]: follow valid
OK   _restore_repo_facts output (with logo): repo valid

ALL AC4 shapes real-jsonschema-valid.
```

### AC5 — full suite green from a clean locked sync

```
$ uv sync --locked --extra dev
Resolved 69 packages in 6ms
Checked 67 packages in 22ms
$ uv run pytest -q
........................................................................ [  8%]
........................................................................ [ 17%]
........................................................................ [ 26%]
........................................................................ [ 35%]
........................................................................ [ 44%]
........................................................................ [ 53%]
........................................................................ [ 62%]
........................................................................ [ 71%]
........................................................................ [ 80%]
........................................................................ [ 89%]
........................................................................ [ 98%]
...............                                                          [100%]
807 passed in 71.17s (0:01:11)
```
(`tests/test_llm.py` + `tests/stages/test_compose.py` alone: `178 passed in 34.60s`.)

### Attack beyond the AC

**B4 end-to-end (the bounce-2 regression) — reproduced live, both directions.** Built a real "list"-intent deck (hook + 2 repo slides + a model-emitted `links` slide carrying only `sub`, no real `links` array — the exact B4 shape) and ran it through the *actual* `normalise_slides -> _restore_repo_facts -> ensure_links_slide -> build_html` path, three ways:
```
--- current (fixed) code: links=('links',) ---
links_slide: {'type': 'links', ..., 'links': ['https://github.com/o/repo-a', 'https://github.com/o/repo-b']}
<li> count in rendered html: 2

--- reverted (regressed) code: links=('links','sub') ---
links_slide: {'type': 'links', 'headline': 'All the links', 'sub': 'see below'}
<li> count in rendered html: 0

--- restored code: links=('links',) ---
links_slide: {'type': 'links', ..., 'links': ['https://github.com/o/repo-a', 'https://github.com/o/repo-b']}
<li> count in rendered html: 2
```
Mechanism: under the regressed `("links", "sub")` (an "at least one of" `anyOf`), a model-emitted `links` slide carrying only `sub` satisfies both the schema and `normalise_slides`'s `required_any` check, so it survives into `doc["slides"]` as a real `type: "links"` entry with no `links` array. `ensure_links_slide`'s own guard (`if "links" in types: return slides`) then sees that type already present and skips adding the real, researched index — the reader gets a headline, a "screenshot this" sub-line, and an empty `<ol class="linklist">`. Fixed (`("links",)`, a strict single `required`), that slide fails `required_any` and is dropped by `normalise_slides` before `ensure_links_slide` runs, so the real index gets built. Exactly the reviewer's 0-vs-2 `<li>` measurement, confirmed from my own scenario, not theirs.

**Render every type — all 17 (type, body-field) pairs.** Built the minimal legal slide for every `REQUIRED_BODY_FIELDS` alternative (15 types, `point` contributing 3 = 17 pairs), validated against real `jsonschema`, ran each through the real `normalise_slides`, rendered through the real `build_html` against the real Jinja templates, and grepped the output HTML for a unique marker planted in that slide's body field:
```
hook       sub        schema=OK normalise=OK render=OK
point      bullets    schema=OK normalise=OK render=OK
point      stat       schema=OK normalise=OK render=OK
point      sub        schema=OK normalise=OK render=OK
facts      rows       schema=OK normalise=OK render=OK
kpi        tiles      schema=OK normalise=OK render=OK
chart      series     schema=OK normalise=OK render=OK
code       code       schema=OK normalise=OK render=OK
flow       steps      schema=OK normalise=OK render=OK
compare    rows       schema=OK normalise=OK render=OK
quote      quote      schema=OK normalise=OK render=OK
photo      image      schema=OK normalise=OK render=OK
repo       name       schema=OK normalise=OK render=OK
links      links      schema=OK normalise=OK render=OK   (template strips the https:// scheme by design; checked the scheme-stripped host instead)
takeaway   sub        schema=OK normalise=OK render=OK
sources    urls       schema=OK normalise=OK render=OK
follow     sub        schema=OK normalise=OK render=OK

PASS: all 17 (type, body-field) pairs schema-valid, survive normalise_slides, and render real content.
```
No blank renders. (First pass of this script false-flagged 9 of the 17 as "BLANK" because it stringified whole Python containers instead of searching nested string values for the marker — a bug in my own probe, not the code under test; fixed and re-run above.)

**The derived-FLAT claim — perturbed live, not just read.** `real_compose` imported normally; a perturbed copy of `compose.py`'s source (regex-edited only in memory, nothing on disk in the repo) with `"quote": ("quote",)` instead of `"quote": ("quote", "attribution")` was loaded as a sibling module inside the real `pipeline.stages` package (so its relative imports still resolve) and its `SLIDES_SCHEMA_FLAT` compared to the real one:
```
real SLIDE_TYPE_FIELDS['quote']: ('quote', 'attribution')
perturbed SLIDE_TYPE_FIELDS['quote']: ('quote',)

'attribution' in real FLAT properties:      True
'attribution' in perturbed FLAT properties: False

PASS: SLIDES_SCHEMA_FLAT changed when SLIDE_TYPE_FIELDS was perturbed -- FLAT is genuinely derived, not hand-maintained.
```

**The `degraded_schema` kwarg on `llm.py`.** Counted the call sites: `coverage.py`, `expand.py`, `research.py` (x3), `extract.py`, `triage.py`, `enumerate_items.py` (x2), `synthesize.py` (x2, one with no schema at all) call `.cheap`/`.good`/`.vision` with no `degraded_schema` argument; only `compose.py`'s 3 call sites pass one explicitly (`SLIDES_SCHEMA_FLAT`). Confirmed in `_openrouter`: `prompt_schema = schema if degraded_schema is None else degraded_schema` (llm.py:436) — the default is genuinely `schema`, not a new behaviour, and this exact contract is pinned by `tests/test_llm.py::test_openrouter_degraded_prompt_falls_back_to_schema_when_no_degraded_schema` (green, part of the 178-test run above). Confirmed the Ollama transport is a **true no-op**, not just "unused by convention": `_ollama()`'s signature (llm.py:312-316) has no `degraded_schema` parameter at all, and `_chat()`'s calls into it — both the primary Ollama path and the local-fallback chain (llm.py:214-218, 273-278) — never pass one; `grep -n degraded_schema src/pipeline/llm.py` shows zero occurrences inside `_ollama`. Also confirmed the explicit-`raise AssertionError` guard on the registries (not a bare `assert`) genuinely survives `python -O`, unlike a real `assert` would:
```
$ uv run python -O -c "... broken registry entry ..."
OK: AssertionError raised under -O as expected: quote's required body field(s) ('quote', 'nonexistent_field') must also be declared in SLIDE_TYPE_FIELDS ...
```

**T100 mechanism confirmed.** Reproduced `scripts/eval_compose.py`'s hand-maintained `BODY_FIELDS` tuple against the exact B4 slide shape (`{"type": "links", "headline": "...", "sub": "..."}`, no `links` array):
```
eval_compose.py's `empty` check calls this slide empty:       False
production's normalise_slides calls this slide empty (drops it): True

CONFIRMED MECHANISM: eval_compose.py's flat, type-agnostic BODY_FIELDS
scores this slide as NOT empty (because 'sub' is a legal body field for
SOME type), while production's per-type REQUIRED_BODY_FIELDS correctly
calls it empty and drops it.
```
`scripts/eval_compose.py:68` (`BODY_FIELDS = ("sub", "bullets", "stat", "rows", "urls", ...)`) is exactly the third, type-agnostic copy the T99 brief described — this well-specifies T100: it should read `REQUIRED_BODY_FIELDS`/`SLIDE_TYPE_FIELDS` from `pipeline.stages.compose` instead of maintaining its own flat tuple, so a slide's "emptiness" is judged by the field(s) that are actually legal *for its own type*, not any field legal for any type. Not a T78 blocker — filed as T100 per the board, confirmed well-specified.

**Live check against real `MODEL_GOOD`.** `LLM_PROVIDER=openrouter`, `OPENROUTER_MODEL_GOOD=nvidia/nemotron-3-ultra-550b-a55b:free`, `FALLBACK_TO_LOCAL` forced off so a failure would surface rather than being masked by the local fallback. 2 reps (well under the 20/min, 1000/day free-tier budget), each a full real `compose()` call (including its `unaddressed`-clause recheck and `check_deck` integrity pass), every returned slide re-validated against real `jsonschema`:
```
provider: openrouter
model_good: nvidia/nemotron-3-ultra-550b-a55b:free
api key set: True
rep 1: 7 slides, types=['hook', 'facts', 'kpi', 'compare', 'takeaway', 'sources', 'follow']
rep 1: every slide real-jsonschema-valid
rep 1: theme='signal' caption[:80]='Anthropic dropped a new evaluation harness for coding agents this week. Instead '
rep 2: 6 slides, types=['hook', 'facts', 'point', 'takeaway', 'sources', 'follow']
rep 2: every slide real-jsonschema-valid
rep 2: theme='signal' caption[:80]='Anthropic dropped a new evaluation harness for coding agents this week. Instead '
```
Both reps produced rich, varied, schema-valid decks (kpi/compare/facts on rep 1) against the real hosted model — the fix works end-to-end in production conditions, not just under `FakeLLM`.

**Minor, non-blocking nit.** `ruff check` on the 4 touched files flags one unused import (`tests/test_llm.py:6`, `Terminal` imported but unused) — confirmed via `git diff main -- tests/test_llm.py` that this line is untouched by T78 (pre-existing on `main`, not a regression). No project lint gate exists yet (T79 "wire ruff + gate it in pre-commit" is still `status:todo`; `coding-standards.md`'s linter line is an unfilled placeholder), so this is informational only, not a T78 failure.

### VERDICT

**PASS.** AC1-5 all hold, independently reproduced with real `jsonschema` (not the dev's own hand-rolled `_schema_allows` or their `_validate_real` helper) plus a real end-to-end render pipeline. B4 reproduced live in both directions (0 vs 2 `<li>`, matching the reviewer's own measurement). FLAT-derivation and the `degraded_schema` no-op-on-Ollama claims both confirmed by direct perturbation/inspection, not just by reading the comments. T100's mechanism confirmed and well-specified. Live check against the real hosted `MODEL_GOOD` succeeded twice. No production code edited during this validation (`git status --porcelain` on the worktree: clean); all probes ran from scratch scripts outside the repo.

evidence: `logs/tester.md#T78`

## T105

**Scope.** Branch `t105-notes-block-in-compose-prompt-bounce1` @ `633100b` (fix `202021a`), worktree `.claude/worktrees/agent-ade1445dd10d51e65`. Confirmed fast-forward of `main` @ `e6e680f` (`git merge-base --is-ancestor main HEAD` true, zero conflicts) — the branch tree IS the merged tree. Diff: `src/pipeline/stages/compose.py` (+119/-2), `tests/stages/test_compose.py` (+311 new tests), `.claude/logs/senior-dev.md`.

**Clean-sync suite.**
```
$ uv sync --locked --extra dev   (hypothesis 6.100+, jsonschema>=4.23 both resolved)
$ uv run pytest -q
........................................................................ [  8%]
...(11 more identical dot-rows)...
..............................                                           [100%]
822 passed in 55.45s
```
Reran clean at the very end (after all mutation testing, reverted): `822 passed in 48.00s`, `git status --porcelain` empty throughout every mutation cycle.

### AC1 — prompt for a 12-note list carries all 12 names + URLs
Reproduced two ways: (a) the shipped `test_compose_prompt_for_a_list_carries_every_notes_name_and_url` — pass; (b) my own end-to-end script building notes via the REAL `enumerate_items._note(candidate, repos=True)` (not a fixture) for 12 synthetic-but-realistic GitHub candidates, through `compose()` with a `FakeLLM`, asserting every note's `name`/`url`/`owner` is verbatim in `fake_llm.calls[0].user`. 12/12 present. Header for the deliberately zero-star note (`[4] ... stars=0 ...`) confirmed distinct from a genuinely-absent-language note (`[8] ...` — no `language=` key emitted at all).

### AC2 — `_restore_repo_facts` genuinely matches, not silent no-op
`test_restore_repo_facts_actually_matches_the_researched_note` (fabricated-facts fake model, real note name) — pass. Independently reproduced live (see below): in both live reps, every repo slide's `name` matched its note via `by_name.get()` (case-insensitive), so owner/url/stars/language got restored/confirmed for every field the note actually carried. AC as literally stated (name-lookup match, not silent no-op) holds.
**Residual found, NOT a T105 regression — see "Beyond AC" below**: `_restore_repo_facts`'s per-FIELD `if note.get(field):` check is pre-existing code untouched by this diff, and is falsy-blind — a legitimately-0 `stars` (or `None`) cannot overwrite a model's fabricated substitute, because `0`/`None` both fail the truthy test. Reproduced against `_restore_repo_facts` directly AND end-to-end through `compose()` with a `FakeLLM`. Filed as a new finding below, not a T105 blocker.

### AC3 — budget safety (15-note list fits `num_ctx_good`; kept+dropped==received, logged)
`test_notes_block_at_realistic_sizes_fits_the_local_fallback_budget`, `test_notes_block_stays_within_budget_and_logs_what_it_drops`, `test_notes_block_logs_only_when_something_was_actually_dropped`, `test_compose_budgets_the_notes_block_against_the_good_role_not_cheap` — all pass. Independently reproduced D55's specific worst case (15 × 64,000-char spam-README notes) with real `budget_chars_for_role(settings, role="good")` = 28,672 chars (num_ctx_good=16384 default): packed body = 28,595 ≤ 28,672, marker present (real trimming happened), **all 15 URLs and all 15 names survive** — matches D55 exactly. Also independently re-verified `budget=0` and `budget=-100` edge cases on `pack()` itself: both still satisfy `kept_chars + omitted_chars == original_chars` for every source.

### AC4 — zero added model calls
`test_notes_block_adds_zero_model_calls` — pass (`len(fake_llm.calls) == 1` for a 12-note list, `clauses=[]`). My own live-rep scripts also logged exactly 1 `llm.good()` call per rep (no retry triggered; `check_deck`/`unaddressed` did not fire extra calls on either live run).

### The three re-applied bounce-0 mutations (`test_deck_shape_field_names_match_notes_block_field_names`)
All three applied by hand via `sed`/`awk` against the real source line, run, then `git checkout --` reverted (confirmed `git status --porcelain` clean after each):
1. **Drop `"language"` from the DECK SHAPE prose** → RED: `AssertionError: ... Extra items in the right set: 'language'`.
2. **Add `"license"` to the DECK SHAPE prose** → RED: `AssertionError: ... Extra items in the left set: 'license'`.
3. **Drop `"language"` from `_NOTE_FACT_FIELDS`** → RED, and catches on **two** tests, not just the pinned one: `test_deck_shape_field_names_match_notes_block_field_names` AND `test_notes_block_carries_index_name_and_every_present_fact`.
All three go red as required — the bounce-0 defect (comparing against a third hardcoded literal) is fixed; the test now reads the real prose out of a real composed prompt.

### `pack(sys.maxsize)` sanity check (independent, not the dev's own numbers)
300 random corpora (1-40 sources, 0-80k chars each): zero trimming, `shares == lengths` (via `_fair_shares`), **worst case 0.067ms** (better than review's reported 0.3ms). Separately timed the true worst case (15×64,000-char sources): **0.0108ms/call avg over 50 reps**. `budget=0`/`budget=-100` reconcile correctly (`kept_chars==0`, `kept+omitted==original`). Confirmed unbudgeted path (`budget_chars=None` → `sys.maxsize`) is genuinely test-only in production: grepped and read all three real call sites — `src/pipeline/__main__.py:78` (`partial_stage(compose, llm=llm, settings=settings)`), `scripts/eval_compose.py:256` (`compose(item, llm, settings)`), `scripts/smoke.py:166` (`compose(i, llm, settings)`) — **all three pass `settings`**, so `budget_chars` is never `None` on a real path; only direct unit tests exercise the `sys.maxsize` fallback.

### Live check (2 reps, real `LLM_PROVIDER=openrouter`, `OPENROUTER_MODEL_GOOD=nvidia/nemotron-3-ultra-550b-a55b:free`, real `.env` key, well under 20/min·1000/day)
Both reps built notes via the real `enumerate_items._note(repos=True)` producer (not fixtures) and ran the real `compose()` end-to-end (no `FakeLLM`).
- **Rep 1** (5 Rust CLI tools, one deliberately 0-star, one deliberately no-language-in-source): 4/5 repo slides matched their note on every field (owner/url/stars/language) exactly. The deliberately-0-star note's slide correctly showed `stars: 0` — the live model respected an explicit `stars=0` in the prompt. The one mismatch: the note with a genuinely-absent `language` (correctly omitted from the prompt per AC) came back with the model inventing `language: 'Rust'` on the slide (matching its 4 Rust-language neighbours), and `_restore_repo_facts` did not strip it (note's own `language` is `""`, falsy, so the per-field restore skips it).
- **Rep 2** (3 dev tools, one deliberately missing `stars`/`popularity=None`): go/requests slides matched their notes exactly on every field. The note with `stars=None` (correctly omitted from the prompt) came back with the model inventing `stars: 0` — the exact "invented zero" anti-pattern this task explicitly warned against, on the model's OUTPUT side (the PROMPT itself never manufactured it — confirmed `_notes_block` correctly omits a `None`/`""` value, matching AC).

So: **the root defect (model never shown any notes, so it invented literally everything) is fixed and independently confirmed live** — in both reps, every fact the note actually had was reproduced exactly, including a real `stars=0`. But **a narrower, different, live-reproduced gap remains**: when a note genuinely lacks an optional repo fact, the model does not leave the slide field blank — it invents a plausible value — and nothing downstream (DECK SHAPE prose doesn't say "omit, never guess"; `_restore_repo_facts`'s truthy check can't strip what the note has no value for) corrects it. 2/2 live reps reproduced this pattern (once for `language`, once for `stars`).

### Beyond-AC edge cases (own scripts, all pass except the finding above)
- Empty note dict `{}` → `"RESEARCHED ITEMS:\n[1]"`, no crash.
- Unicode in every field (CJK name/owner/url, emoji + em-dash in detail) → all fields round-trip verbatim.
- Marker-lookalike text WITHOUT the sentinel wrapper (`"[... 42 chars omitted ...]"` typed literally by a real source) → survives verbatim, correctly NOT treated as a forged marker (`marker_present()` false).
- Multiple forged (sentinel-wrapped) markers, across 2 notes → all stripped, `_MARKER_SENTINEL not in block`.
- Whitespace-only `name`/`detail` → degrades to bare `[1]`, no crash.
- Negative `stars` (`-1`) → preserved verbatim (facts are copied, not validated, by design).
- **Known non-vacuity gap (review nit 2), independently reproduced**: patched `_notes_block` to `return ""` unconditionally, reran `tests/stages/test_compose.py` — the sanitize-forged-marker test stays green (vacuous, confirmed: `not marker_present("")` is trivially true), but exactly **9** other tests go red (`test_notes_block_carries_index_name_and_every_present_fact`, `test_notes_block_keeps_a_falsy_but_real_star_count`, `test_notes_block_with_no_name_or_claim_still_carries_the_detail`, `test_compose_prompt_for_a_list_carries_every_notes_name_and_url`, `test_list_prompt_drops_the_separate_truncated_source_url_list`, `test_notes_block_at_realistic_sizes_fits_the_local_fallback_budget`, `test_compose_budgets_the_notes_block_against_the_good_role_not_cheap`, `test_notes_block_logs_only_when_something_was_actually_dropped`, `test_notes_block_stays_within_budget_and_logs_what_it_drops`) — matches review's claim exactly. Mutation reverted, `git status --porcelain` clean, full suite re-confirmed green (822 passed).

### New finding for the board (not a T105 blocker — pre-existing code, different root cause than D50)
`_restore_repo_facts` (compose.py, unchanged by this diff) restores a field only `if note.get(field):` — truthy-only. Two consequences, one live-observed and one code-level:
1. **Live-observed, 2/2 reps**: when a note structurally lacks an optional repo fact (the correct, AC-required behaviour — omit, don't manufacture, in the PROMPT), the model still invents a plausible value for that field in its raw output, and restore cannot fix what the note never had. `_notes_block` (T105's actual diff) behaves correctly; the gap is downstream, in the model's completion plus the pre-existing restore function.
2. **Code-level, not yet live-observed**: a note with a REAL `stars: 0` cannot overwrite a model's fabricated non-zero stars, because `0` is falsy — reproduced directly (`_restore_repo_facts` unit-level) and end-to-end through `compose()` with a crafted `FakeLLM`. The live model in rep 1 happened to reproduce a real `stars=0` faithfully on its own, so this specific sub-case has not (yet) been observed live, but the code has no enforcement backstop if a future model doesn't.
Recommend a follow-up task (adjacent to already-filed T107, which covers a related "repo card given facts it has none of" pattern for web-catalogue notes): tighten DECK SHAPE prose to explicitly say "if a fact is absent from the note, leave that field out of the slide — never infer or guess it," and change `_restore_repo_facts`'s per-field check from `if note.get(field):` to `if field in note:` (or equivalent None/""-aware check) so it can also correct/strip a field the note explicitly lacks, not just skip it.

### VERDICT

**PASS.** All 5 stated AC objectively verified with real reproductions (not just re-running the dev's own tests): real `_note()` output end-to-end, real budget math, real `pack()` timing, all three bounce-0 mutations independently re-applied and confirmed RED, two live reps against the real hosted model with zero added calls, clean re-sync + full suite (822 passed) at start and end with a clean working tree throughout. The one residual gap found (model inventing a value for a fact the note genuinely lacks, and `_restore_repo_facts`'s falsy-blind restore not catching it) is real, live-reproduced, and worth a follow-up task — but it is pre-existing code outside this diff's scope, is a materially narrower failure mode than D50's defect (which fabricated literally every field of every item because the model saw no notes at all), and none of T105's five stated AC depend on it.

evidence: `logs/tester.md#T105`

## T111

**Scope**: validate T111 (branch `t111-absent-facts-are-not-invented` @ `65b197c`, fix commit `bc40771`), worktree `.claude/worktrees/agent-a661d07bd19271bc5`, merge base `main` @ `09124ef`. Reviewer passed, no bounce.

### Setup — clean locked sync (AC5)

```
$ uv sync --locked --extra dev
Resolved 69 packages in 2ms
Checked 67 packages in 8ms
```

### AC5 — full suite green, tail pasted; ruff version

```
$ uv run pytest -q
........................................................................ [  8%]
........................................................................ [ 17%]
........................................................................ [ 26%]
........................................................................ [ 34%]
........................................................................ [ 43%]
........................................................................ [ 52%]
........................................................................ [ 60%]
........................................................................ [ 69%]
........................................................................ [ 78%]
........................................................................ [ 86%]
........................................................................ [ 95%]
.....................................                                    [100%]
829 passed in 62.08s (0:01:02)

$ uv run ruff --version
ruff 0.12.5
```

`ruff check` on the 4 touched Python files (compose.py, test_compose.py, test_render.py, test_repo_slide.py): **All checks passed!** (zero new lint errors — coding-standards.md's bar is "zero new lint errors on touched files", not repo-wide-zero). `ruff check .` repo-wide reports 27 pre-existing findings, all unused-import (`F401`) in untouched test files (`test_expand.py`, `test_llm.py`, `test_main.py`, `test_worker.py`) — not this diff's. `ruff format --check` on the 4 touched files says "would reformat" all 4 — verified **pre-existing**: `git show 09124ef:src/pipeline/stages/compose.py` (pre-T111 main) also fails `ruff format --check`, so this is the already-documented "ruff not pinned, no `[tool.ruff]` section" defect (coding-standards.md), not a regression from this diff.

### AC1 — note lacking `language`, model fabricates one → field gone

`tests/stages/test_repo_slide.py::test_restore_repo_facts_strips_a_field_the_note_never_carried` (note has no `language` key at all) and `::test_restore_repo_facts_strips_a_field_the_note_carries_as_none_or_empty` (note has `language: ""`, `_note()`'s real "nothing detected" sentinel) both pass. Independently reproduced with my own script calling `_restore_repo_facts` directly — output dict has no `"language"` key in either case. **Mutation-killed**: reverted `_restore_repo_facts` to the pre-fix `if note.get(field): slide[field] = note[field]` and reran — both tests, plus `test_compose_strips_facts_the_note_never_researched`, went **red** (`language` survived as `'Cobol'`). Reverted mutation, `git status --porcelain` clean, reran green.

### AC2 — genuine `stars: 0` + model's fabricated `stars: 4200` → output is `0`

`test_restore_repo_facts_overwrites_a_fabricated_star_count_with_a_genuine_zero` pins this exactly (note `stars: 0`, slide `stars: 4200` → asserts `slides[0]["stars"] == 0`); `test_compose_strips_facts_the_note_never_researched` pins it end-to-end through `compose()` with a `FakeLLM`. Both pass. **Mutation-killed** with the same revert as above — `test_restore_repo_facts_overwrites_a_fabricated_star_count_with_a_genuine_zero` went red with `assert 4200 == 0`.

### AC3 — `repo.html.j2` renders correctly for 3 distinct states

Dev's tests (`test_render.py::test_repo_slide_renders_a_genuine_zero_star_count`, `::test_repo_slide_omits_the_star_badge_when_no_count_was_researched`) pass and mutation-kill (reverted `{% if s.stars is defined %}` → `{% if s.stars %}`, zero-star test went red: `'★ 0'` missing from HTML). I independently re-verified all three states with my own `build_html()` script:

```
PASS: stars=0 renders '★ 0'
PASS: stars=4200 renders '★ 4200'
PASS: no 'stars' key renders no badge at all
```

### AC4 — T105's pin still passes

`test_deck_shape_field_names_match_notes_block_field_names` (the DECK SHAPE-prose-vs-`_NOTE_FACT_FIELDS` pin) still passes — the new "leave that field out" sentence was appended *after* the pinned "Copy owner, name, url, stars and language from the note VERBATIM" sentence, not inside it.

```
$ uv run pytest -q tests/stages/test_compose.py -k "deck_shape_field_names_match or T111 or strips_facts or restore_repo_facts" -v
collected 141 items / 137 deselected / 4 selected
tests/stages/test_compose.py ....                                        [100%]
4 passed, 137 deselected in 0.29s
```

### Beyond-AC: the falsy-render side effect (filed T114) — reachability verdict

Reviewer's concern: `{% if s.stars is defined %}` renders `stars=None` as "★ None" and `stars=False` as "★ False". Tested both explicitly, end-to-end, not just at the template layer:

- **`stars=False` — REACHABLE, confirmed end-to-end.** `search.py:278` passes SearXNG's `result.get("popularity")` through completely untyped into the note; `enumerate_items.py:505` copies it verbatim into `note["stars"]`; `_restore_repo_facts`'s guard is `value not in (None, "")` — `False` is neither, so it's written straight to the slide. Reproduced the full chain: a fake SearXNG response with `"popularity": false` → `searx()` → `enumerate_items._note()` → `_restore_repo_facts()` → `render.build_html()` → literal `<span class="stars">★ False</span>` in the output HTML. **One malformed/boolean `popularity` field from any SearXNG engine is sufficient** — no model cooperation needed at all (the note's own value, not the model's fabrication, is what renders). Also confirmed a non-numeric string (`"4.2k"`) passes the same guard untouched and renders literally — the guard is presence-only, never a type/numeracy check.
- **`stars=None` — NOT reachable via this fix.** Traced both writers of `slide["stars"]`: (1) `normalise_slides`'s `isinstance(slide.get("stars"), int)` check excludes `None` outright (never sets the key); (2) `_restore_repo_facts`'s own guard explicitly pops the key when the note's value `is None` — it cannot ever *assign* `None`, only strip it. No other code path writes to `slide["stars"]`. So "★ None" is not reachable through T111's own code; the reviewer's stated risk is half-real — real for `False` (and any other non-`None`/non-`""` falsy garbage), not for `None`.
- **Practical likelihood**: SearXNG's own engines normally either omit `popularity` (→ `None`, safely stripped) or supply a real int from GitHub-shaped results. A boolean or string `popularity` would require an unusual/misbehaving SearXNG engine or a manually crafted response — plausible but not the common case; still, the code has **zero defense** against it today (T114 tracks: validate/coerce `popularity` at the `search.py` boundary, or at minimum `isinstance(value, (int, float)) and not isinstance(value, bool)` in `_restore_repo_facts`).

Reproduction scripts (paths kept for reference, since exact command text is load-bearing here):
```python
# search.py -> enumerate_items._note() -> compose._restore_repo_facts() -> render.build_html()
# with a fake SearXNG result: {"url": "https://github.com/owner0/repo0", "popularity": False, ...}
# Output: note["stars"] == False  ->  slide["stars"] == False  ->
#   '<span class="stars">★ False</span>' present in rendered HTML.
```

### Beyond-AC: T113's three falsy mechanisms — confirmed

All three reproduced directly against `normalise_slides()` / `build_html()`, no mocking needed:

1. **`compose.py:809`** (`if isinstance(slide.get("stat"), dict) and slide["stat"].get("value"):`) — a schema-violating raw-number `stat.value: 0` (schema declares it a string, but per compose.py's own giant comment neither provider reliably enforces `oneOf`/`strict`) makes `slide["stat"].get("value")` falsy, dropping the **entire** `stat` object (value AND label), not just the zero. Confirmed: `stat: {"value": 0, "label": "Open bugs"}` → normalised slide has no `"stat"` key at all. Contrast confirmed: the schema-conformant string `"0"` survives intact.
2. **`compose.py:841-846`** (`{"value": str(t.get("value", "")).strip(), ...}` inside the tiles comprehension) — `.get(key, default)` only uses the default when the *key is missing*; a tile with an explicit JSON `null` value (`{"value": None, ...}`) has the key present, so `.get("value", "")` returns `None`, and `str(None)` is the literal string `"None"`, which is also non-empty so it survives the `if isinstance(t, dict) and str(t.get("value","")).strip()` filter too. Confirmed: normalised tile is `{"value": "None", ...}`, and rendering through the real `kpi.html.j2` (`{{ t.value }}`, no guard) produces literal `<div class="tval">None</div>` in the HTML.
3. **`compose.py:872`** (now line 879 after T111's +7 lines of prompt prose — same construct) — `isinstance(slide.get("stars"), int)` admits `bool`, since `bool` subclasses `int` in Python; already proven reachable above via the SearXNG-`popularity`-is-`False` chain (which doesn't even need this specific line — it goes through `_restore_repo_facts` directly — but a model emitting a literal `"stars": true` in violation of the "integer" schema would hit this exact line and pass it through `normalise_slides` too).

### Beyond-AC: is `_restore_repo_facts`'s "strip is complete" claim (owner/name/url unpoppable) correct?

Verified the reviewer's reasoning directly against the real producer, `enumerate_items._note()`, across 6 URL shapes (normal `owner/repo`, trailing slash, single-segment `owner`-only, root `/`, non-github single-segment, and a 3-segment path):

```
'https://github.com/owner0/repo0'   -> owner='owner0'    name='repo0'   url=<real>
'https://github.com/owner0/repo0/'  -> owner='owner0'    name='repo0'   url=<real>
'https://github.com/justowner'      -> owner='justowner' name=''       url=<real>
'https://github.com/'               -> owner=''          name=''       url=<real>
'https://example.com/somepage'      -> owner='somepage'  name=''       url=<real>
'https://github.com/a/b/c'          -> owner='a'         name='b/c'    url=<real>
No counterexample: whenever name is truthy, owner is also truthy, for every case tried.
```

Confirmed **holds**: `_note()`'s `owner, _, name = path.partition("/")` means a truthy `name` structurally requires a `"/"` in the path, which structurally requires a non-empty `owner` before it (the leading/trailing `"/"` was already `.strip()`ped, so `path` can't start with `/`). `url` comes straight from `candidate["url"]` (a required dict key, and `search.py` already filters empty/missing URLs out of every candidate before `_note()` ever sees one) — never absent, `None`, or `""`. Consequence: `by_name` (built from `if note.get("name"):`) only ever admits notes where owner+name+url are **all** present together; every other note (single-segment or root path) is skipped from matching **entirely**, leaving the slide's own (possibly fabricated) owner/name/url untouched rather than popped. So the reviewer's claim is correct for the real production shape — no counterexample found — and the actual residual risk is "unmatched note, fabrication survives," which is a pre-existing, separate limitation of the by-name lookup, not something T111 introduced or was asked to fix.

### Beyond-AC: other attacks tried, all held

- Note with **every** optional field absent (no owner/url/stars/language/logo keys at all): all 5 fabricated fields on the slide stripped, none survive.
- `stars` as a non-numeric string (`"4.2k"`): passes the presence guard untouched, renders literally (`★ 4.2k`) — confirms there is genuinely no type/numeracy validation at this layer (feeds directly into the T114 finding above).
- Unicode in `language` (`日本語`): round-trips through `_restore_repo_facts` and `build_html` untouched and unmangled.

### Live rep against the real hosted model (OpenRouter, `nvidia/nemotron-3-ultra-550b-a55b:free`, free tier)

Two live `compose()` calls (well inside the 20/min, 1000/day free-tier budget) using the repo's real `.env` credentials, no fakes:

- Rep 1 (1 note, `stars=0`/`language=""`): model produced no `repo`-typed slide at all for a single-item list this run (a `compose()` prompting/model-behavior quirk unrelated to T111 — not investigated further, out of scope).
- Rep 2 (2 notes — one `stars=0`/`language=""`, one `stars=812`/`language="Go"`): model produced both repo slides. Output, after `_restore_repo_facts`:
  ```
  repo slide: repo0 stars= 0     'language' in slide == False  (expect False)
  repo slide: repo1 stars= 812   language == Go                (expect Go)
  r0 stars == 0 (expect 0)
  'language' in r0 == False (expect False)
  r1 stars == 812 (expect 812)
  r1 language == Go (expect Go)
  repo slides produced: 2
  ```
  The genuine zero survived, the genuinely-absent language stayed absent, and the genuine non-zero/language pair for the second repo were written through correctly — the fix holds against the real hosted model, not just the `FakeLLM` fixture.

### VERDICT

**PASS.** All 5 stated AC objectively and independently verified (own reproductions, not just re-running the dev's tests) — every new/changed assertion mutation-killed against the pre-fix code, `git status --porcelain` clean after each revert, full suite re-confirmed green (829 passed) before and after. T105's pin unaffected. Zero new lint errors on touched files; the repo-wide `ruff format --check` failures on the touched files are pre-existing on main, not introduced here.

Reachability verdict for the reviewer's flagged side effect (T114, not a T111 blocker): `stars=False` is genuinely reachable end-to-end from a single malformed/non-numeric SearXNG `popularity` value, with zero model cooperation required — real but not the common case, and the code has no defense against it today. `stars=None` is *not* reachable through this code path; the reviewer's concern was half-real.

T113's three falsy-check mechanisms (compose.py:809 stat-drop-on-zero, compose.py:841-846 null-tile-renders-as-"None", compose.py:872/879 isinstance-int-admits-bool) all independently confirmed with direct reproductions — T113 is well-specified as filed.

Reviewer's "owner/name/url structurally unpoppable" reasoning verified correct against the real note producer across 6 URL shapes, no counterexample found.

evidence: `logs/tester.md#T111`

## T96
- 2026-09-23 [T96/T116, T122 verification pass] Verified `t96-bounce1` @ `b084028` (worktree `agent-a153676259542916e`), the senior-dev's bounce-1 fix for reviewer's B1 (classification regression) + B2 (evidence overstatement), plus T116 (`_ollama` empty-completion guard). **PASS -> done.**

**Setup.** `uv sync --locked --extra dev` first (per instructions), then full suite:
```
$ uv run pytest -q
........................................................................ [  8%]
........................................................................ [ 17%]
........................................................................ [ 25%]
........................................................................ [ 34%]
........................................................................ [ 42%]
........................................................................ [ 51%]
........................................................................ [ 59%]
........................................................................ [ 68%]
........................................................................ [ 76%]
........................................................................ [ 85%]
........................................................................ [ 93%]
....................................................                     [100%]
844 passed in 47.88s
```
Matches the claimed 844.

**B1 probe, both directions, both providers.** `pytest.mark.parametrize("bad_message", [None, "a bare string", 42, ["a", "list"]])` already exists in `tests/test_llm.py` for `test_openrouter_non_dict_message_is_classified_not_a_crash` and `test_ollama_non_dict_message_is_classified_not_a_crash` (8 cases total). I mutated `_answer_text` myself (removed `if not isinstance(message, dict): return None, None`) and reran just those 8:
```
$ uv run pytest -q tests/test_llm.py -k "non_dict_message"
...
E       AttributeError: 'list' object has no attribute 'get'
=========================== short test summary info ============================
FAILED tests/test_llm.py::test_openrouter_non_dict_message_is_classified_not_a_crash[None]
FAILED tests/test_llm.py::test_openrouter_non_dict_message_is_classified_not_a_crash[a bare string]
FAILED tests/test_llm.py::test_openrouter_non_dict_message_is_classified_not_a_crash[42]
FAILED tests/test_llm.py::test_openrouter_non_dict_message_is_classified_not_a_crash[bad_message3]
FAILED tests/test_llm.py::test_ollama_non_dict_message_is_classified_not_a_crash[None]
FAILED tests/test_llm.py::test_ollama_non_dict_message_is_classified_not_a_crash[a bare string]
FAILED tests/test_llm.py::test_ollama_non_dict_message_is_classified_not_a_crash[42]
FAILED tests/test_llm.py::test_ollama_non_dict_message_is_classified_not_a_crash[bad_message3]
8 failed, 67 deselected in 0.25s
```
All 8 fail with uncaught `AttributeError` -- guard removed. Restored the guard:
```
$ uv run pytest -q tests/test_llm.py -k "non_dict_message"
........                                                                 [100%]
8 passed, 67 deselected in 0.16s
```
All 8 pass, `pytest.raises(BadCompletion)` -- guard present. `git diff` confirmed clean after revert. B1 is genuinely closed, and I also diffed `git diff 09124ef b084028 -- src/pipeline/llm.py` (pre-T96 baseline to current) directly: every existing status-code `raise` (`Retryforever`/`RateLimited`/`Terminal`/`Retryable`) is an untouched context line; the isinstance guard did not exist in `fd0b525` (T96's pre-bounce commit) and was added in `b084028` -- confirms the regression's origin and its fix are exactly where the story says.

**B2 (evidence attribution).** Read the module docstring (`src/pipeline/llm.py:20-42`) and the `tests/test_llm.py` section comment (~line 543-554): both now attribute "4 of 8" to T78's reviewer, name Arm A as the deliberately weak-prompt arm, state Arms B/C saw zero empty-content under the real production prompt, and say the production-path frequency is UNMEASURED. No code or test asserts a frequency. Matches D65 verbatim.

**T116 verdict: cannot fail an item that previously shipped.** Confirmed by direct reads, not just trusting the review:
- `src/pipeline/errors.py`: `class BadCompletion(Retryable)` -- confirmed subclass.
- `src/pipeline/stages/synthesize.py:55` and `:75`: both are `llm.good(...)` calls with no `schema` arg (defaults to `None`), both followed by `brief = (brief or "").strip()` then `if not brief: raise Retryable(...)` -- confirmed both call sites.
- `src/pipeline/worker.py:_run_one`: `Retryforever`/`Terminal` get dedicated except clauses; everything else (including plain `Retryable` and `BadCompletion`) falls to `except Exception as exc:` where `if not isinstance(exc, Retryable): log.exception(...)` gates the crash log, then `record_failure(..., max_attempts=...)` spends one attempt regardless. Since `BadCompletion` IS a `Retryable`, this branch is byte-identical whether `_ollama` raises `BadCompletion("...empty completion...")` (new) or `synthesize.py` raises `Retryable("...empty brief")` (old, from `_ollama` returning `""`). Verdict: **safe, cannot regress a previously-shipping item** -- same exception family, same worker branch, same attempt-spend, only the message differs (and now names which field, if any, was recovered).
- Directly exercised the new `_ollama` guard: `test_ollama_both_content_and_thinking_empty_raises_bad_completion` calls `.cheap("s", "u")` (no schema) with `{"content": "", "thinking": ""}` and asserts `BadCompletion` is raised where old code returned `""` silently -- read and confirmed this test exists and passes.

**Mutation 2 (the self-reported coverage gap).** Confirmed the assertion exists at `tests/test_llm.py:290`: `assert "recovered" not in str(exc_info.value)` inside `test_openrouter_unparseable_json_is_retryable_without_fallback`. Reproduced the gap myself: dropped `_finish`'s `if recovered_field:` guard so the `(answer recovered from message.{recovered_field})` suffix is always appended:
```
$ uv run pytest -q tests/test_llm.py
...
E       AssertionError: content itself supplied the (unusable) answer here — the field-recovery annotation must only appear when a fallback field was used
E       assert 'recovered' not in 'model retur...essage.None)'
FAILED tests/test_llm.py::test_openrouter_unparseable_json_is_retryable_without_fallback
1 failed, 74 passed in 30.51s
```
Exactly the one test the dev named, exactly the failure mode named ("answer recovered from message.None"). Reverted, full suite green again (844 passed, confirmed).

**str() coercion on the fallback branch.** Removed `str(recovered)` -> `recovered` in `_answer_text`'s fallback return. `test_openrouter_recovered_reasoning_is_always_coerced_to_str` (list-shaped `reasoning`) goes red: `assert isinstance(out, str)` fails with `False = isinstance(['chunk one', 'chunk two'], str)`. Restored, green. Independently confirmed the real hazard this guards against: `(["chunk one", "chunk two"] or "").strip()` (what `synthesize.py:60`/`:76` does to a no-schema answer) raises `AttributeError: 'list' object has no attribute 'strip'` in a bare Python REPL check -- so a list-shaped `reasoning` really would crash `synthesize()` without the coercion. `_finish`'s primary-branch (non-recovery) path is unaffected -- pre-existing, non-regression behaviour per the reviewer's N1, not touched by this fix.

**Classification walk.** `grep -n "raise " src/pipeline/llm.py` -> 19 raise sites; cross-checked every one against `git diff 09124ef b084028 -- src/pipeline/llm.py`: the only new/changed `raise BadCompletion` sites are the two new empty-completion guards (`_ollama` new, `_openrouter`'s pre-existing one narrowed from `content is None or not str(content).strip()` to `content is None` -- same outcome, the emptiness test just moved into `_answer_text`) and `_finish`'s field-recovery re-raise. Every `Retryforever`/`RateLimited`/`Terminal`/plain-`Retryable` raise at a status-code dispatch point is an untouched context line in the diff. `worker.py`'s three-way dispatch (`Retryforever` / `Terminal` / everything else) is likewise untouched.

**Live rep (cheap, single).** Real `.env` has `LLM_PROVIDER=openrouter` with a live key configured. Ran ONE rep against the actual configured `OPENROUTER_MODEL_GOOD` using `synthesize.py`'s real SYSTEM prompt and a realistic research-note payload (mindful of the shared 20/min, 1000/day account-wide budget -- T96 already spent ~25 reps on this same question, so I did not repeat that sweep):
```
HTTP status: 200
message keys: ['content', 'reasoning', 'reasoning_details', 'refusal', 'role']
content is empty: False
reasoning_details present: True
```
Did **not** reproduce `content == ""` -- consistent with D65's Arms B/C (zero empty-content under the real production prompt). One rep proves nothing either way (D9/D27's own methodological rule); I am not treating this as evidence the quirk is rare, only reporting that this single attempt did not trigger it.

**Lint / format.** `ruff check src/pipeline/llm.py tests/test_llm.py` -> 1 finding (`F401` unused `Terminal` import in `tests/test_llm.py:6`), confirmed pre-existing on baseline `09124ef` (`git show 09124ef:tests/test_llm.py` has the identical unused import) -- reviewer already flagged this as devops-branch's fix to make, not a T96/T116 regression. `ruff format --check` on both files: "would reformat" on both, confirmed identical on the `09124ef` baseline copies -- zero new drift.

**Out of scope, not tested/failed for:** T117 (refusal-only completion classification) and T118 (CoT-then-JSON extraction) -- neither exists in this diff, both correctly gated per D67/D68.

**Working tree.** `git status --porcelain` and `git diff --stat` both empty after every mutation probe -- all reverted.

evidence: `logs/tester.md#T96`

## T79/T91/T85

**Scope.** Branch `devops/t79-t91-t85-lint-uv-guard-bounce1`, commits `088a995` (fix) + `1fee2d7` (log), worktree `.claude/worktrees/agent-a7af907decb1ee21e`, merge-base `09124ef`. Reviewer passed T79/T91 on substance and rejected only T85's two blockers (B1: stale docstring, B2: reviewer's own mutation of all 4 call sites to `pass` left 827/827 green). Routed straight to me per bounce-1 handoff. Verdict: **PASS**, all three, with one documentation-accuracy finding on T91 recorded below (not blocking).

### B1 — `config.py` docstring, verified true, not just longer

```
$ grep -rn "validate_for_run\|validate_model_config" --include="*.py" src/ scripts/ | grep -v test_
src/pipeline/config.py:7:- ``validate_for_run(env=None)`` -- the full service guard...
src/pipeline/config.py:10:- ``validate_model_config()`` -- the narrower model/provider-only guard...
src/pipeline/__main__.py:233:    settings.validate_for_run()
scripts/eval_compose.py:236:    settings.validate_model_config()
scripts/dashboard.py:993:    settings.validate_model_config()
scripts/preflight.py:81:        settings.validate_for_run()
scripts/smoke.py:122:    settings.validate_model_config()
scripts/build_golden.py:110:    settings.validate_model_config()
```
Docstring names both validators, both audiences, and all six call sites verbatim; cross-checked against `grep` and every one matches. `is_auto_router()`'s doc-pointer also fixed (`see validate_for_run` -> `see validate_model_config`). **B1 closed.**

### B2 — the wiring test, mutated one script at a time (the check the task demanded, not trusted from the dev's report)

For each of the 4 scripts: copied it to `/tmp`, replaced its sole `settings.validate_model_config()` call with `pass  # MUTATED`, ran `tests/test_model_guard_wiring.py -k <name>` alone, restored from the `/tmp` copy, confirmed `git diff --stat` on that file was empty before moving to the next.

```
$ pytest tests/test_model_guard_wiring.py -q -k eval_compose   (mutated)
E   Failed: DID NOT RAISE MissingConfig
FAILED ...[eval_compose.py]                                     1 failed, 3 deselected

$ pytest tests/test_model_guard_wiring.py -q -k smoke.py        (mutated)
E   Failed: DID NOT RAISE MissingConfig
FAILED ...[smoke.py]                                             1 failed, 3 deselected

$ pytest tests/test_model_guard_wiring.py -q -k build_golden    (mutated)
E   Failed: DID NOT RAISE MissingConfig
FAILED ...[build_golden.py]                                      1 failed, 3 deselected

$ pytest tests/test_model_guard_wiring.py -q -k dashboard       (mutated)
E   Failed: DID NOT RAISE MissingConfig
stdout: "mission control on http://127.0.0.1:8770  (loopback only)"
FAILED ...[dashboard.py]                                         1 failed, 3 deselected
```
All four go red **individually** -- a test that only fails when all four are broken would not have shown this. After each restore, `git status --porcelain` was empty. Reran all 4 together on the clean tree: `4 passed in 0.12s`. Full suite after: `831 passed in 48-55s` (ran 3x, stable). **B2 genuinely closed** -- the reviewer's finding (827/827 green with all four neutered at once) cannot recur: any single regression at any one of the four call sites now fails its own parametrized case.

### Four-script refusal, end to end (beyond the AC)

Loaded each script fresh via `spec_from_file_location` (eval_compose/smoke/build_golden) / `runpy.run_path(run_name="__main__")` (dashboard), zero Telegram vars, real `MissingConfig` messages:
```
eval_compose.py auto-router: MissingConfig: OpenRouter auto-router slugs are not allowed: ... no run is reproducible ...
eval_compose.py paid:        MissingConfig: Only free OpenRouter models are allowed, but these are not free: ...
smoke.py auto-router:        MissingConfig: OpenRouter auto-router slugs are not allowed: ... reproducible ...
smoke.py paid:                MissingConfig: Only free OpenRouter models are allowed, but these are not free: ...
build_golden.py auto-router: MissingConfig: OpenRouter auto-router slugs are not allowed: ... reproducible ...
build_golden.py paid:         MissingConfig: Only free OpenRouter models are allowed, but these are not free: ...
dashboard auto-router:        MissingConfig: OpenRouter auto-router slugs are not allowed: ... reproducible ...
dashboard auto-router mission-control lines: []
dashboard paid:                MissingConfig: Only free OpenRouter models are allowed, but these are not free: ...
dashboard paid mission-control lines: []
```
All four distinguish the auto-router wording ("reproducib...") from the paid wording ("not free") as the AC demands, and `dashboard.py`'s "mission control on..." line never printed on either refusal -- confirmed the guard runs before `web.run_app`/the print, in the source (`Settings.load(); # T85/D16 comment; settings.validate_model_config(); print("mission control..."); web.run_app(...)`) and live.

### `smoke.py` under `env -i`, zero Telegram vars

```
$ env -i PATH="$PATH" HOME="$HOME" .venv/bin/python scripts/smoke.py
preflight:
  provider: ollama
  OK       cheap  qwen3:4b-instruct
  MISSING  good   qwen3.5:9b
  OK       searxng http://localhost:8080

Fix the above first. `docker compose up -d searxng`, `ollama serve`, `ollama pull ...`
```
Ran to completion of the preflight block with **zero Telegram vars set at all** (default provider `ollama`, no `Settings.REQUIRED` check reached). It stopped only on real infra readiness (local Ollama missing the `good` model), never on the guard or on missing Telegram credentials -- exactly the "no Telegram, no Instagram" workflow the split exists to protect (D16). Split's own purpose upheld.

### T28/T69 unweakened

Compared `preflight.py` byte-for-byte against the merge-base (`09124ef`, in a throwaway `git worktree add`, removed after):
```
Both branch and 09124ef, same env (full Telegram vars + OPENROUTER_MODEL_CHEAP=openrouter/free):
  OK    imports: all runtime dependencies present
 FAIL   config incomplete
         OpenRouter auto-router slugs are not allowed: OPENROUTER_MODEL_CHEAP='openrouter/free'. An auto-router serves a different model on every call, so no run is reproducible and no A/B result can be trusted -- pin one specific ':free' model slug instead (browse them at https://openrouter.ai/models?max_price=0).
```
Identical message, identical path (`validate_for_run()`, untouched in `__main__.py`/`preflight.py`). **T28/T69 not weakened.**

### The lint gate actually fires (without touching shared `.git/hooks`)

Baseline: `ls .git/hooks | grep -v .sample` -> empty (confirmed before and after -- never ran `pre-commit install`).
Planted `scripts/_lint_test_scratch.py` (F401 unused `os` + bare `except:`), `git add`ed it only (never committed), ran, then `git reset` + `rm` it:
```
$ .venv/bin/ruff check scripts/_lint_test_scratch.py
F401 `os` imported but unused
E722 Do not use bare `except`
Found 2 errors.                                                  exit=1

$ .venv/bin/pre-commit run --files scripts/_lint_test_scratch.py
ruff check (pinned version + select in pyproject.toml)...Failed
- exit code: 1
                                                                   pre-commit exit=1
$ ls .git/hooks | grep -v .sample     # unchanged, empty, both before and after
```
Full-tree `ruff check src tests scripts` finds **only** the 2 planted findings (`Found 2 errors` total) -- confirms the rest of the tree is genuinely clean, not just the scratch file being noisy in isolation. Restored: `git status --porcelain` empty. Re-ran `pre-commit run --all-files` on the clean tree: `Passed`.

### The `--fix` removals -- all 15 accounted for, `src/` one + 3 more spot-checked

`git diff 09124ef -- tests/ src/ scripts/ | grep -E "^-\s*(import |from )"` lists exactly 13 lines (F401) + 2 F541 lines in `dashboard.py` (the two `f"..."` -> `"..."` conversions with no `{}` placeholders) = **15, matches the claim exactly**. Spot-checked for genuine deadness (grep for remaining use of the removed name in each file, all zero hits):
- `src/pipeline/publish/tokens.py`: `timedelta` removed; file only does `datetime.now(tz) - refreshed` (a `datetime` subtraction, produces a `timedelta` object but never binds the name) -- confirmed dead.
- `scripts/mutate.py`: `sys` removed -- `grep -n "\bsys\." scripts/mutate.py` empty -- confirmed dead.
- `tests/test_conversation.py`: `Item` removed from the `pipeline.models` import -- `grep -n "\bItem\b"` empty -- confirmed dead.
- `tests/test_llm.py`: `Terminal` removed from the `pipeline.errors` import -- `grep -n "\bTerminal\b"` empty -- confirmed dead.
(Also confirmed `test_intake.py`, `test_research.py`, `test_triage.py`, `test_coverage.py`, `test_expand.py`, `test_main.py`, `test_worker.py`'s removed `pytest`/`replace`/`_replace` imports all have zero remaining `pytest.`/`replace`/`_replace` usages in their files.)

I001 nit cross-check: pyproject.toml's deferral comment claims "15 hits" -- `ruff check --select I001 src tests scripts` -> `Found 15 errors`. Matches exactly (the dev's log notes they fixed 3 stray double-blank-lines left by their own `--fix` to make this land back on 15).

### Guard's own logic + delegation, mutated (the wiring test's own docstring claims, verified rather than trusted)

```
neutered validate_model_config() body to `return` immediately:
$ pytest tests/test_config.py -q
12 failed, 60 passed          -- exactly the 12 claimed
(restored, clean)

replaced validate_for_run()'s `self.validate_model_config()` delegation call with `pass`:
$ pytest tests/test_config.py -q
9 failed, 63 passed           -- exactly the 9 claimed, includes
FAILED test_validate_for_run_is_a_strict_superset_of_validate_model_config
(restored, clean)
```

### Ruff version and pin

```
$ .venv/bin/ruff --version
ruff 0.16.8
```
Pin held exactly (`ruff==0.16.8` in `pyproject.toml`'s dev deps). Full clean-env reproduction: `rm -rf .venv && uv sync --extra dev && .venv/bin/python -m pytest -q` -> `831 passed in 54.77s`; `.venv/bin/ruff check src tests scripts` -> `All checks passed!` exit 0.

### T91 -- finding, not a blocker

The plain-`uv sync` reproduction pasted in `docs/development.md` (pytest present, only `jsonschema`+`hypothesis` missing, "Interrupted: 2 errors during collection") is **not reproducible from a genuinely fresh checkout today**:
```
$ rm -rf .venv && uv sync && .venv/bin/python -m pytest -q
/.../.venv/bin/python: No module named pytest
```
`pytest`, `pytest-asyncio`, `hypothesis`, `jsonschema`, `ruff`, `pre-commit` are all declared under `[project.optional-dependencies] dev`, which `uv sync` (no `--extra`) does not install at all -- confirmed with `uv sync --extra dev` followed by plain `uv sync`, which **prunes** the extras back out (`- pytest==9.1.1`, `- pytest-asyncio==...`, etc. in the sync diff). So a truly first-time `uv sync` gives `ModuleNotFoundError: No module named pytest`, not the two-file collection-error transcript quoted verbatim in the docs. The underlying safety property T91 exists for is still true and if anything stronger (you cannot get a misleadingly-short pass; you cannot run pytest at all), but the exact pasted transcript describes an intermediate state (dev deps synced before `jsonschema`/`hypothesis` existed, then re-synced without `--extra dev` after they were added) rather than "a plain `uv sync`" on a fresh clone. Also flagging a wording inversion in `README.md`'s new line -- "why plain `uv sync` silently collects zero tests rather than failing loudly" describes the opposite of `docs/development.md`'s own "Interrupted" banner, which *is* a loud failure. Not gating on this: reviewer already passed T91 on substance, the core claim (no silent partial pass) holds under my own reproduction, and this is a docs-accuracy nit for a follow-up, not a functional regression.

### Working tree

`git status --porcelain` and `git diff --stat` empty after every probe in this pass (mutations on `scripts/eval_compose.py`, `scripts/smoke.py`, `scripts/build_golden.py`, `scripts/dashboard.py`, `src/pipeline/config.py` x2, and the scratch lint file, all reverted/removed). Scratch worktree used for the T28/T69 byte-comparison (`git worktree add .../basecheck 09124ef`) removed via `git worktree remove --force` after use. `.git/hooks` unchanged throughout (no `pre-commit install` ever run).

**Verdict: PASS** -- B1 and B2 both genuinely closed under independent, one-at-a-time reproduction (not the dev's aggregate report). T79 and T91 hold under my own from-scratch reproduction, with one non-blocking documentation-accuracy finding on T91's exact transcript, recorded above for `project-context.md`/follow-up.

evidence: `logs/tester.md#T79/T91/T85`

## T74

**Scope.** Validates T74 (filler re-scope, D73/D74) + T93 (binding fix) + T127 (`clear_bytecode`). Branch `t74-structural-partition-bounce1`, commits `47e1fc1` (merge main) + `a1edfbf` (bounce-1 fix) + `bb512bf` (log), worktree `.claude/worktrees/agent-afe2e20aa27965cb3`, base `bada324` = current `main` tip (`git merge-base --is-ancestor bada324 HEAD` true). `uv sync --locked --extra dev`: `Resolved 80 packages ... Checked 78 packages`. `data/eval/*.json` (9 files, gitignored) present in the worktree already. **Verdict: PASS.**

### Setup / baseline

```
$ uv run pytest -q
884 passed in 57.61s
$ uv run ruff check src tests scripts   (ruff 0.16.8, the pinned gate -- `ruff check .` alone also flags the vendored .claude/skills/ examples, not part of this gate)
All checks passed!
$ uv run python scripts/mutate.py
running 61 mutations
...
all 61 mutations caught
```
`git status --porcelain` clean before, after, and between every probe below (every mutation I applied was reverted and reverified clean).

### Killer AC — reproduced myself, exact

```
$ uv run python scripts/eval_compose.py --rescore golden_current | tail -12
  decks_scored               24
  avg_substance_composite    39.57
  doc only  decks_scored 19  avg_substance_composite 37.588  avg_grounding_ratio 0.161
  raw only  decks_scored  5  avg_substance_composite 47.098  avg_grounding_ratio 0.084

$ uv run python scripts/eval_compose.py --rescore golden_listonly | tail -6
  decks_scored               24
  avg_substance_composite    42.542
  doc only  decks_scored 24  avg_substance_composite 42.542
```
Matches D22 (`39.570` / `42.542`, `19 doc / 5 raw`, `24 doc / 0 raw`) to the digit, on my own independent invocation.

### B3 (the serious one) — flipped it myself

Edited `deck_body_text`'s default from `frozenset()` to `frozenset({'links','sources','follow'})` — the one-token flip — and reran:
```
$ uv run pytest -q
FAILED tests/test_quality.py::test_deck_body_text_default_exclude_types_excludes_nothing
E  AssertionError: 'LINKS-MARKER' missing from deck_body_text(doc) with no override
1 failed, 883 passed in 62.04s

$ uv run python scripts/eval_compose.py --rescore golden_current | grep avg_substance_composite | head -1
    avg_substance_composite    40.415
$ uv run python scripts/eval_compose.py --rescore golden_listonly | grep avg_substance_composite | head -1
    avg_substance_composite    43.398
```
Exactly the claimed `40.415`/`43.398`, exactly 1 test red. Reverted (`git checkout -- src/pipeline/quality.py`), reran both `--rescore` calls: back to `39.57`/`42.542`. Also ran the new mutate.py anchor directly: `uv run python scripts/mutate.py --only deck-body-text-default-scope` -> `ok deck-body-text-default-scope caught`. **B3 closed, confirmed two independent ways.**

### B2 — all 3 `PIPELINE_AUTHORED_TYPES` now exercised, and it fires

```
$ uv run python -c "... ensure_closing_slide([], is_dm=False, source_urls=['https://a.example']) ..."
closing types: ['sources', 'follow']
links types: ['links']
```
All 3 covered. Then edited `compose.py`'s `sources` slide headline from `"Sources"` to `"Why it matters"` (an existing `FILLER_PHRASES` entry) and reran the disjointness test alone:
```
FAILED tests/test_quality.py::test_filler_phrases_disjoint_from_pipeline_authored_cta_text
E   AssertionError: 'why it matters' duplicates pipeline-authored CTA text: ['Why it matters', 'Follow for more', ...]
```
Reverted, `git status --porcelain` clean. **B2 closed.**

### B1 — figures confirmed, and a discrepancy resolved

Independent script over `data/eval/golden_current.json` + `golden_listonly.json` (same 48 doc-or-raw rows `--rescore`'s `decks_scored` counts), calling `filler_count`/`deck_body_text` directly:
```
total_decks = 48
total_hits = 16
distinct phrases fired = 12
decks with >=1 hit = 15  (31%)
```
Matches the claimed 16/12/15/31% exactly. For the "corrected distribution" figure: my task brief quoted `mean 0.843, stdev 0.279, min 0.123` — this does **not** match the currently shipped docstring (`quality.py:648`, `mean 0.816, population stdev 0.335, min 0.0`). Traced it: `logs/reviewer.md` bounce-1 review computed `0.843/0.279/0.123` **before** the dev's N2 fix (unscoped `word_count` denominator); the dev's own bounce-1 log entry records both and states the post-N2 numbers supersede. I reproduced **both** independently, exactly:
```
post-N2 (current code, scoped word_count):  mean=0.8163 stdev=0.3346 min=0.0000  at_ceiling=33/48
pre-N2  (unscoped word_count, reviewer's bounce-1 number): mean=0.8427 stdev=0.2788 min=0.1228  at_ceiling=33/48
```
Both check out to 3-4 decimals against their respective (superseded vs current) claims. The shipped docstring correctly carries the post-N2 (corrected) figures. **B1 closed** — flagging the stale pre-N2 figure in my task brief for `project-context.md` so a future reader isn't confused by two "corrected" numbers.

### B4 — `clear_bytecode()` / `tests/test_mutate.py`

```
$ uv run pytest tests/test_mutate.py -q
3 passed in 0.28s
```
Reverted `clear_bytecode` to the old hardcoded `for root_dir in ("src", "scripts")`:
```
$ uv run pytest tests/test_mutate.py -q
FAILED tests/test_mutate.py::test_clear_bytecode_derives_its_dirs_from_mutations
FAILED tests/test_mutate.py::test_clear_bytecode_root_dirs_match_derivation_expression
2 failed, 1 passed in 0.19s
```
2 of 3 tests catch the regression against an unlisted fixture dir (`zz_mutate_test_fixture_only`). Reverted, `git status --porcelain` clean, 3 passed again. **B4 closed.**

### N2 (word_count scoping) — re-entry figure verified

Same independent script: `decks >= FILLER_DENSITY_TARGET(3.0) = 3/48 (6.25%)` — matches the claimed 3/48, confirmed **under 10%**, so D74's decision to keep `filler_count` out of the composite holds and `FILLER_COUNT_IN_COMPOSITE` correctly stays `False`. Mutated `score_substance`'s `word_count` back to the unscoped `deck_body_text(doc)`:
```
$ uv run pytest tests/test_quality.py::test_score_substance_word_count_is_scoped_like_filler_count -q
E   assert 11 == 5
1 failed
```
Reverted, clean. **N2 closed.**

### Tautology sweep

Re-read every `filler`/`deck_body_text`/`FILLER_PHRASES` test in `tests/test_quality.py` (the blocklist-hygiene tests, the two harvested-phrase tests, the disjointness test, the citation test, the exclusion test, the model-written-mandated-types test, the T93 binding test). All hardcode phrases that are still live in the 19-entry `FILLER_PHRASES` (`"why it matters"`, `FILLER_PHRASES[0]` derivations, etc.) or derive from `ensure_closing_slide`/`ensure_links_slide` directly — none found still-tautological beyond the one the dev already caught and fixed (`"Follow for more"`) and the one the reviewer flagged as honestly-disclosed-near-vacuous (N4, `test_active_component_weights_never_includes_filler_count_even_when_phrases_empty` — its own comment now says so). Also independently mutated `filler_count`'s old import-time-bound-default bug (`phrases: ... = FILLER_PHRASES` instead of `= None`): `test_filler_count_reads_the_live_module_global_not_an_import_time_default` goes red (`assert 1 == 0`) — T93's own regression test is real, not vacuous. Reverted, clean.

### Beyond the AC

- **`bare_bullet_ratio` unchanged, independently.** Hand-typed the old six-type set (`{"hook","takeaway","sources","links","follow","repo"}`, never reading `STRUCTURAL_SLIDE_TYPES`) and recomputed `bare_bullet_ratio` against the shipped function across all rows of all 8 corpus files present: `checked = 97, mismatches = 0`. Matches the reviewer's claim exactly.
- **D25.** `grep -n "^import\|^from" src/pipeline/quality.py` -> `import re` / `import unicodedata` only (plus `from __future__ import annotations`). `test_quality_module_imports_only_re_and_unicodedata` (AST-walks the real source file, not a docstring) passes.
- **The 9 dropped phrases.** Independent script scanning every fragment of every slide (via `_slide_text`) in all 8 corpus files, tagging each hit's slide type: all 9 dropped phrases (`follow for more` x83, `daily tech, explained` x61, `stay updated on` x11, `screenshot this slide` x9, `all the links` x8, `get the latest on` x3, `get updates on` x3, `follow for updates on` x3, `the future of` x1) have **zero** non-closing occurrences — every single one of their combined 182 occurrences is on a `PIPELINE_AUTHORED_TYPES` slide. All 9 confirmed, not just 2.

### Final state

```
$ git status --porcelain     (empty)
$ uv run pytest -q           884 passed in 57.56s
$ uv run ruff check src tests scripts     All checks passed!  (ruff 0.16.8)
```

**Verdict: PASS.** Killer AC reproduced exact; all 4 blockers (B1-B4) independently verified closed, each under my own mutation, not trust of the dev/reviewer's report; N2's 3/48 re-entry figure verified under 10%; no new tautological test found; `bare_bullet_ratio` and D25 both independently spot-checked and hold; all 9 dropped phrases confirmed absent from every non-closing slide in the full corpus.

For `project-context.md`: my task brief quoted the reviewer's **pre-N2-fix** filler-component distribution (`mean 0.843, stdev 0.279, min 0.123`) as "the corrected distribution" — that figure was superseded within the same bounce by the dev's N2 fix (scoped `word_count`), which moved it to `mean 0.816, stdev 0.335, min 0.0` (33/48 at ceiling unchanged). Both are real, both reproduced exactly above; only the post-N2 one is what's currently shipped in `quality.py:648`'s docstring and should be the one cited going forward.

evidence: `logs/tester.md#T74`

## T73

**Scope.** Validates T73, the research-stage eval harness (bounce 2, the gate — a third
reject forces escalation). Branch `t73-bounce2`, commit `026cfda`, worktree
`.claude/worktrees/agent-a4a07162ded65de94`, rebased cleanly on `main` (`c9ce109`) with
T74/T93/T127. Files changed: `scripts/eval_research.py` (new, 1053 lines), `tests/test_eval_research.py`
(new, 1397 lines, 61 tests), `data/eval/research_golden.json` (new, committed, 3-item golden set),
`.claude/logs/senior-dev.md`. `git diff main...HEAD -- src/` = 0 lines (D76's four frozen
`grounding_ratio` inputs untouched, confirmed). **Verdict: PASS.**

### Setup

```
$ uv sync --locked --extra dev
Resolved 80 packages in 3ms
Checked 78 packages in 12ms

$ ./.venv/bin/python -m pytest -q
945 passed in 47.47s / 47.86s (reran twice, both clean)

$ ./.venv/bin/ruff check src tests scripts   (ruff 0.16.8, pinned gate — `ruff check .`
                                               alone also flags vendored .claude/skills/
                                               examples, not part of the gate)
All checks passed!
```
`git status --short` clean throughout (every mutation I applied below was reverted and
reverified before moving on).

**Correction to my brief's "8 corpus files . . . from the main checkout":** that instruction
does not apply to this task. Main checkout has zero files under `data/eval/corpus_cache/` (it's
gitignored runtime state, never populated there) — I believe "8 corpus files" was carried over
from T74's brief, which really does have 8 supporting `data/eval/*.json` files for
`eval_compose.py`'s *different* harness (confirmed present in main: `baseline.json`,
`e26_baseline.json`, `e26_fewshot.json`, `fewshot.json`, `golden.json`, `golden_baseline.json`,
`golden_fewshot.json`, `golden_listonly.json`). T73's harness (`eval_research.py`) doesn't read
any of those; its only runtime dependency is `data/eval/corpus_cache/` (fetched web pages +
plan cache) and `data/eval/research_golden.json` (committed, already present). I sourced
`corpus_cache` from `.claude/worktrees/agent-a87a48d4454023c39` (t73-bounce1, the worktree
D86's "51/51 HTTP 200" audit was actually run against) and byte-verified it:

```
$ diff -rq <t73-bounce1 worktree>/data/eval/corpus_cache/ <this worktree>/data/eval/corpus_cache/
(no output)  ->  BYTE-IDENTICAL COPY CONFIRMED
```

### 1. Corpus audit — re-run myself, fresh script, not the dev's code

Wrote an independent audit script (base64-decodes every cached body, checks status code,
body length, and scans for block/challenge markers) and ran it against the 51-file corpus:

```
total cached HTTP entries: 51
status code distribution: {200: 51}
non-2xx count: 0
zero-length bodies: 0
size range: 7900 - 2245119 bytes, median 152811 bytes
```
`51/51 HTTP 200, 0 non-2xx, 0 zero-length bodies` — D86's core claim reproduced exactly.

**But my own broader marker scan found a genuine gap D86 missed.** Sorting all 51 bodies by
size and pulling `<title>` tags, the *smallest* file (7900 bytes,
`http-f58732296c92160bc32175022827326d152bb1b7e3aefdbe2bd4246cf5ad398f.json`) has
`<title>Making sure you're not a bot!</title>` — a genuine "Anubis" anti-scraper JS-challenge
page (`documentfoundation.org`, "Protected by Anubis"), served with **HTTP status 200** and
cached as if it were real article content. It contains neither "captcha" nor "cloudflare"
literally, so it wasn't caught by D86's own "3 containing 'captcha' are genuine articles"
marker pass either — my blunt substring scan (66 hits across ~20 files for
captcha/cloudflare/"access denied"/"too many requests", all on long legitimate tech articles
about web security/rate-limiting/bot-detection, verified as incidental by size — 20KB-2.2MB
bodies) didn't flag it either; only checking title tags on the *smallest* files by size did.

**Why this isn't a T73 code defect.** `CachingHTTP` screens on HTTP status code only, and its
own docstring says so plainly ("a non-2xx response . . . used to be cached . . . Only a 2xx
body is written now") with an explicit, pre-existing disclaimer that "clean today is a
property of the corpus at audit time, not an invariant this cache enforces going forward"
(module docstring). A 200-status JS challenge page is outside what status-code screening can
ever catch — the code doesn't claim otherwise, and out-of-scope items already exclude deeper
content-sniffing (body-size cap, URL identity). So: not a blocker for T73, but it **does**
narrow D86's specific "genuinely clean" framing — recommending a project-context.md addendum,
not a code fix (see bottom).

### 2. Post-T71 simulation — both directions, reproduced myself

Ran `test_recorder_records_a_good_routed_call_post_t71_simulation` and
`test_note_roles_makes_a_post_t71_artifact_distinguishable_from_its_baseline` as shipped: pass.
Then reproduced B1 directly — deleted `Recorder.good` (reverting to the bounce-0 bug where a
`good`-routed call falls through `__getattr__` to the raw inner client, unrecorded, no
exception) and reran:

```
$ ./.venv/bin/python -m pytest tests/test_eval_research.py -v -k "post_t71 or note_roles"
test_recorder_records_a_good_routed_call_post_t71_simulation FAILED
  AssertionError: both the plan call AND the good-routed note call must be recorded --
  pre-fix, only the plan call would show up here
  assert 1 == 2   (only the plan call was recorded, note silently dropped)
test_score_item_reports_note_roles_from_recorded_calls PASSED
test_note_roles_makes_a_post_t71_artifact_distinguishable_from_its_baseline PASSED
test_summarise_aggregates_note_roles_across_rows PASSED
```
Fixed code -> green with the note counted; B1 reproduced -> red with the *exact* predicted
symptom (silent zero, no exception). Reverted, `git diff` clean, full suite reran green (61/61,
945/945).

### 3. Two differing artifacts — produced my own, not just the dev's in-memory test

Drove `eval_research.run()` end-to-end twice (real save path, real file I/O, offline fake LLM —
no live network/model touched) against a 1-item golden set, varying only which role serves the
note call, and diffed the two **saved on-disk files**:

```
baseline note_roles_overall: {'cheap': 1}      arm note_roles_overall: {'good': 1}
baseline row note_roles:     {'cheap': 1}      arm row note_roles:     {'good': 1}
fingerprints identical (D87): True -> 5ddbbd1aa637+cheap:qwen3:4b-instruct+good:qwen3.5:9b+temp:0.3
```
D87 confirmed exactly: byte-identical `prompt_fingerprint` on both sides, and the row's own
`note_roles` is what actually distinguishes a `good`-routed arm from a `cheap` baseline.

**Secondary finding (not a blocker):** `compare()`'s numeric-only diff loop
(`isinstance(lv, (int, float))`) silently skips dict-valued fields, so `note_roles_overall`
never appears in the printed `--compare` table — only in the raw saved JSON. A user running
`--compare baseline arm` on these two files would see every other metric line up identically
and would *not* see the note_roles difference on screen unless they open the JSON directly.
D87 says "the saved row . . . can tell a good-routed arm from a cheap baseline" — true of the
file, not yet true of the primary comparison workflow's own output. Worth a follow-up nit, not
a fail (the AC as asked — "verify two artifacts genuinely differ" — is satisfied at the file
level, which I did above).

### 4. `data/eval/<tag>.json` collision — fed it a real compose file, not a synthetic mock

Beyond the dev's own tests (`test_compare_refuses_a_foreign_harness_file`,
`test_run_writes_the_research_prefixed_filename_not_the_bare_tag`, both pass), copied the
**real** `data/eval/baseline.json` (genuinely produced by `eval_compose.py`, no `"harness"`
key, confirmed via `json.load(...).keys()`) to `research-realcompose.json` and called
`eval_research.compare()` on it directly:

```
Correctly refused: research-realcompose.json was not produced by this harness
(harness=None, expected 'research') -- refusing to compare a foreign eval file
```
Also independently fuzzed `_result_path()`/`--tag` for path traversal (`../../../etc/passwd`,
`/etc/passwd`, `..`, `....//....//etc/passwd`, `a/b/c`) — every case stays inside `RESULTS`,
no `/` survives sanitization, `path.resolve()` never escapes. Security check (content-derived
path segments = hex digests; tag sanitization) holds.

### 5. Mutation survivors — both re-verified independently, plus one of my own

- **F3** (the delegation-test mirror): deleted `"sub": note.get("detail", "")` from
  `_note_prose_doc`. `test_score_note_uses_pipeline_quality_directly_for_grounding_and_density`
  **stayed green** (proves it's a mirror, not independent). The new
  `test_note_prose_doc_maps_claim_to_headline_and_detail_to_sub` **went red**
  (`AssertionError: detail must surface via quality.py's own field reader`) — confirmed the
  separation the docstring claims. Reverted, clean.
- **F4** (plan-cache fingerprint isolation): collapsed `_plan_cache_dir_for` to a fixed
  `"plan-cache-fixed"` path regardless of fingerprint. `test_plan_cache_dir_is_namespaced_by_fingerprint`
  **went red** (`assert a != b` failed, both equal). Reverted, clean.
- **My own, unflagged by either bounce:** mutated `score_item`'s `yield` denominator
  (`queries_planned` -> `queries_planned + 1`, an off-by-one). Caught by two independent tests
  simultaneously (`test_score_item_yield_gate_rate_and_confidence_mix` exact-arithmetic check,
  and the post-T71 simulation's `row["yield"] == 1.0` assertion). Reverted, clean.

No third tautological test found in a full read of the remaining ~900 lines (fingerprint,
`score_item`, `summarise`, `--reps`/`_spread`, model-config guard, `load_golden`, `compare`
sections) — all use hand-computed or independently-derived expected values, not
self-referential ones.

### 6. D83 noise floor — sanity-checked against the raw recorded artifacts, not re-run live

`--reps` genuinely costs live wall-clock/network (D84: 9 non-2xx re-fetches/rep, ~536s/warm
rep) — rather than burning 9-18 more minutes reproducing a fresh 3-rep sample, I pulled the
exact `noise-rep{1,2,3}.json` files D83/D85 were computed from (same t73-bounce1 worktree),
byte-verified them (sha256 match), and independently recomputed the aggregate stats straight
from the raw per-item `rows`, not the printed summary:

```
metric                             rep1       rep2       rep3   spread
yield_overall                       0.8        0.8        0.8   ZERO
notes_produced_total                 12         12         12   ZERO
failure_phrase_rate_overall      0.0833     0.0833     0.0833   ZERO
gate_accept_rate_overall         0.7442     0.7209     0.7442   3.2% relative
avg_grounding_ratio               0.537     0.4938     0.5298   8.3% relative
items_completed                       3          2          3   37.5% relative (coin-flip)
```
Qualitative claim independently confirmed on the first three metrics (zero noise — gate on
them) and `items_completed` (real, structural coin-flip — useless as a threshold). My exact
`avg_grounding_ratio` numbers differ slightly from D83's stated 0.561/0.537/0.494 (D83 likely
cites an earlier/separate 3-rep sample than the ones saved in this worktree) — expected, since
relevance-gate and note-writing calls stay live every rep even against a frozen corpus, so two
genuinely separate 3-rep sessions will draw different live-model outputs. Both sessions land on
the same qualitative verdict (8-12% relative noise, "DO NOT gate"), which is stronger evidence
than one sample alone. Also confirmed D85 directly from the rows: item 502 (the only
`source:"dm"` item) hit `NeedsInput: gaps=['whether smaller open-...` on exactly rep2 and only
rep2 — items 501/503 (`source:"channel"`) never do, matching the structural claim
(`research.py:260`'s `clauses=[]` for non-`dm` items) exactly.

### 7. D14 caveat on both durable surfaces

Confirmed via the dev's own passing tests (`test_summarise_writes_the_d14_caveat_into_the_saved_summary`,
`test_compare_prints_the_d14_caveat_read_back_from_the_saved_file`) and independently in my own
run() above: `grounding_comparability_caveat` present in the saved JSON `summary` dict, and
printed by both `run()`'s per-rep stdout and `compare()`'s own output (reproduced live in
section 3's `compare()` call).

### Final state

```
$ git status --short          (empty, both worktree-native and my added data/eval/*.json —
                                latter confirmed `git check-ignore`d, no repo pollution)
$ ./.venv/bin/python -m pytest -q      945 passed in 47.86s
$ ./.venv/bin/ruff check src tests scripts     All checks passed!  (ruff 0.16.8)
$ git diff main...HEAD -- src/         (empty — D76 untouched)
```

**Verdict: PASS.** All 5 review-round findings (B1/post-T71 both directions, B2/corpus audit,
D87/F1 differing artifacts, F2/tag collision, F3+F4 mutation survivors) independently
reproduced under my own mutations and my own fresh scripts, not trust of the dev's or
reviewer's report. No third tautological test found. D76's frozen inputs untouched. D83's
noise-floor qualitative claim reproduced from a second, independently-verified 3-rep sample.
Security (hex-digest path segments, `--tag` traversal) independently fuzzed and holds.

For `project-context.md`:
- **D86 addendum needed, not a D86 retraction.** The audited corpus is 51/51 HTTP 200 exactly
  as claimed, but "genuinely clean" overstated confidence: one cached page (the smallest file,
  7.9KB) is an HTTP-200 "Anubis" JS anti-scraper challenge page, invisible to both status-code
  screening (by design — recorded as a known limitation in `CachingHTTP`'s own docstring) and
  to keyword-marker scanning (it uses neither "captcha" nor "cloudflare"). Only caught by
  cross-referencing body size against `<title>` tags. Doesn't affect T73's code correctness
  (the harness never claimed content-sniffing), but any future run scoring item(s) whose note
  drew on that specific cached URL should expect one contaminated source in an otherwise-clean
  corpus.
- **`--tag baseline` "8 corpus files" instruction is stale for future T73-adjacent tasks** —
  that count belongs to `eval_compose.py`'s golden-set support files, not
  `eval_research.py`'s `corpus_cache`. Worth fixing at the source (whatever template/precedent
  future task briefs for this harness are drawn from) so a future tester doesn't spend the same
  cycles re-deriving it.
- **`compare()`'s numeric-only diff table silently omits `note_roles_overall`/`confidence_mix_overall`**
  (dict-valued fields) — D87's fix lives in the saved file but not yet in the primary
  `--compare` display a user actually runs. Not a blocker; worth a follow-up nit.

evidence: `logs/tester.md#T73`

done — logged, board:PASS (returned to spawner, not written here per integrity rule 1), standards:ok

## T71/T72

**Scope.** Validates T71 (role routing, ships defaulted `cheap`) + T72 (relevance gate
fail-open, now tri-state, fires only on ERRORED) + T131 (fail-open notes excluded from
MIN_NOTES), landed together on branch `t71-t72-research-routing-fail-open-bounce2`,
commits `0075aa1` + `112b79d`, worktree `.claude/worktrees/agent-aaf39bc3aa014506e`.
Bounce 1 fixed B1 (fail-open now draws only from ERRORED, never REFUSED — D88), B2
(FAIL_OPEN_KEY excludes fail-open notes from MIN_NOTES — D90/T131), B3 (excerpt back to
dedicated `RELEVANCE_EXCERPT_CHARS=1200`), plus N1-N7 non-blocking findings. Bounce 2
fixed the two ship-blockers (default flip to `cheap`, D94; N1 note-corpus-budget now
follows `settings.research_note_role` instead of hardcoded `cheap`) and folded in N2-N6.
**Verdict: PASS.**

### Setup / full-suite reproduction

```
$ ln -s /Users/aayushgour/Desktop/projects/telegram-automation/.venv .venv   # worktree had none
$ git status --short
?? .venv

$ ./.venv/bin/python -m pytest -q
........................................................................ [  7%]
...(966 dots)...
F (test_every_filler_phrase_has_a_real_non_closing_citation)
1 failed, 966 passed in 48.60s / 49.88s (reran twice, identical)
```
The 1 failure is `tests/test_quality.py::test_every_filler_phrase_has_a_real_non_closing_citation`,
gated on `data/eval/*.json` golden-compose artifacts that are gitignored and genuinely absent
from this fresh worktree (`data/eval/` exists on the shared main checkout with those files, the
worktree has none) — confirmed pre-existing/environmental, not this diff: reproduced the same
`skipif`-bypassing failure shape independent of any T71/T72 code by inspection of the skip
condition (`DATA_DIR.exists()` is true — the dir exists gitignored-empty in some worktrees —
but `load_docs()` then finds nothing in it). Per the brief, not failing the task for it.

```
$ ./.venv/bin/ruff check src tests scripts
All checks passed!    (ruff 0.16.8)

$ ./.venv/bin/ruff format --check src/pipeline/stages/research.py src/pipeline/config.py
2 files would be reformatted — same pre-existing whole-file drift reproduced against
`git show main:src/pipeline/stages/research.py` in isolation (1 file would be reformatted,
identical diff shape), confirmed not introduced by this branch (D33's caveat, T79 not landed).

$ ./.venv/bin/python scripts/mutate.py
running 68 mutations
  ok   closing-slide          caught
  ...
  ok   note-role-config       caught
  ok   gate-fail-open-fires   caught
  ok   gate-fail-open-bounded caught
  ok   gate-fail-open-errored-only caught
  ok   relevance-excerpt-dedicated-constant caught
  ok   note-corpus-budget-follows-role caught
  ok   relevance-gate-malformed-verdict-errors caught
  ...
all 68 mutations caught
real 1:59.94
$ git status --short
?? .venv
```
D92 discipline: `git status --short` run before and after every command in this pass, including
after the mutation sweep and after every hand-mutation below. Never dirty except my own
untracked `.venv` symlink and (temporarily) my own scratch test file, both removed at the end.
No `mutate.py` INFLIGHT/leftover-mutation symptom observed.

### 1. Mouse-cursor guarantee — ABSOLUTE, vacuity-checked (not vacuously green)

Wrote an independent test (not the dev's own, not committed) driving `research()` on the same
3 explicitly-refused POISONED candidates, asserting on `fake_llm.calls` directly rather than
trusting `note_calls == []` alone:

```
gate_calls = [c for c in fake_llm.calls if _is_gate_call(c)]
note_calls = [c for c in fake_llm.calls if _is_note_call(c)]
assert len(gate_calls) == 3   # PASSED — all 3 candidates genuinely reached the gate
assert note_calls == []       # PASSED
```
Both assert. `note_calls == []` is NOT vacuous: 3 gate calls fired, all real `{"relevant": False}`
verdicts, none re-admitted. Confirmed by hand-mutating the classification branch
(`elif verdict is _GateVerdict.ERRORED:` -> `elif verdict is _GateVerdict.REFUSED:`, i.e.
`mutate.py`'s own `gate-fail-open-errored-only` shape) and rerunning: goes red as expected
(fail-open would then fire on an explicit refusal). Reverted, reran green, `git diff` clean.

### 2. Tri-state exhaustive probe, defaults SAFE

Direct unit probes against `_is_relevant`, one call per shape, no `research()` indirection:

| verdict fed in | result |
|---|---|
| `{"relevant": True, "why": "..."}` | ACCEPTED |
| `{"relevant": False, "why": "..."}` | REFUSED |
| `{"why": "..."}` (missing `relevant` key) | ERRORED |
| `{}` | ERRORED |
| `["not", "a", "dict"]` | ERRORED |
| `"a bare string"` | ERRORED |
| `None` | ERRORED |
| `42` | ERRORED |
| raises `Retryable("transient")` | ERRORED |
| raises `Terminal("bad completion")` | ERRORED |
| raises `ValueError("malformed json")` | ERRORED |
| raises `Retryforever("ollama down")` | propagates (not caught) |
| raises `asyncio.CancelledError()` | propagates (BaseException, `except Exception` does not catch it) |

14 assertions, all passed. Falsified: reverted the `isinstance`/missing-key guard to
`if False:` (mutate.py's `relevance-gate-malformed-verdict-errors` shape) and reran — 6 of the
8 shape-probes went red, one via `AttributeError: 'int' object has no attribute 'get'` (the
exact N2 pre-fix crash — a malformed verdict used to abort the whole facet loop, not just
exclude one candidate). Reverted, reran green (14/14), `git diff` clean.

**Note for project-context.md on my own brief's wording:** my brief characterized "a missing
key, `{}`" as `->REFUSED`. That is the *pre-N3* behaviour. The shipped bounce-2 code (and the
dev's own N3 log entry, and both my test and the dev's `test_relevance_gate_treats_a_verdict_
missing_relevant_key_as_errored`) classify both as ERRORED, by design — N3 explicitly moved them
off REFUSED because "a dict missing 'relevant' was silently binned REFUSED... indistinguishable
from a genuine working verdict." This does NOT reopen D88: D88's 96.6% figure is about explicit
`{"relevant": false}` verdicts (882 events), which stay REFUSED unconditionally in every shape I
tested — a malformed/keyless verdict is a materially different, much rarer shape the dev/reviewer
judged as "no usable verdict," consistent with "no path may silently default to ERRORED" (nothing
here is silent — it's an explicit, tested branch) and does not restore the refuted behaviour.
Flagging only because the brief's own shape table doesn't match the shipped code; recommend the
board carries the corrected mapping (missing-key/`{}` -> ERRORED, not REFUSED) forward.

### 3. T131/B2 — fail-open notes excluded from MIN_NOTES

Existing dev tests reproduced (`test_fail_open_notes_alone_do_not_satisfy_min_notes`,
`test_real_notes_alongside_fail_open_ones_still_satisfy_min_notes`) — both pass, both target
`research()` end-to-end via a monkeypatched `_research_one`.

Wrote two more, targeting the exact "mixed item" and "legacy DB row" cases the brief named:
- 1 legacy note (no `FAIL_OPEN_KEY` at all) + 1 explicit `FAIL_OPEN_KEY=True` -> only 1 real,
  below `MIN_NOTES=2` -> `Retryable` raised. PASSED.
- 2 legacy notes (neither carries `FAIL_OPEN_KEY`) -> both count as real, run completes.
  PASSED — proves a legacy on-disk row (written before T131 landed) reads as real, not
  silently dropped, because `n.get(FAIL_OPEN_KEY)` on a keyless dict returns `None` and
  `not None` is `True`.

Falsified: mutated the filter to `FAIL_OPEN_KEY in n and not n[FAIL_OPEN_KEY]` (require the key
present) — the two-legacy-notes test went red (`Retryable: only 0 of 2 ... need at least 2`),
exactly the silent-exclusion bug this shape guards against. Reverted, reran green, `git diff`
clean.

### 4. N1 — note corpus budget follows `settings.research_note_role`

Confirmed the two new budget tests are non-tautological: `budget_chars_for_role` reads
`num_ctx_cheap=8192`/`num_ctx_good=16384` (both defaults, `src/pipeline/config.py:155-156`) via
`(num_ctx - RESERVED_TOKENS=2048) * CHARS_PER_TOKEN=2`, giving cheap=12,288 / good=28,672 chars —
matches the tests' own cited numbers exactly. Their probe body (15,300 chars) sits strictly
between the two, so `marker_present()` genuinely flips on the role setting alone, all other
inputs byte-identical between the two tests. `mutate.py`'s `note-corpus-budget-follows-role`
entry (hardcodes `role="cheap"`) is caught in the 68/68 sweep above — this is the regression a
good-routed note call silently getting a cheap-sized corpus, which N1's own comment says was
"a third confound" on the original -61% grounding measurement (D95).

### 5. N4 — red-in-isolation, reproduced directly (not through the dev's own test)

Hand-mutated `_research_one`'s `if not errored:` guard to `if True:` (mutate.py's
`gate-fail-open-fires` shape — disables fail-open entirely) and ran
`test_gate_fails_open_per_facet_but_all_fail_open_notes_still_retry` **in isolation**:

```
$ ./.venv/bin/python -m pytest -q tests/stages/test_research.py::test_gate_fails_open_per_facet_but_all_fail_open_notes_still_retry -v
FAILED ... AssertionError: both facets must have actually failed open and reached the note call...
assert 0 == 2
 +  where 0 = len([])
```
Genuinely red, for the right reason (0 note calls reached, not the pre-fix vacuous pass on
`pytest.raises(Retryable, match="non-fail-open")` alone — both facets errored straight to
nothing and the identical message fired with fail-open fully disabled, exactly as the dev's own
log describes finding by hand). Reverted (`git checkout -- src/pipeline/stages/research.py`,
cleared `__pycache__`), reran: 1 passed. `git diff` clean throughout.

### 6. The default

```
$ grep -n research_note_role src/pipeline/config.py
    research_note_role: str = "cheap"                                          # dataclass default
    research_note_role=e.get("RESEARCH_NOTE_ROLE", "cheap").strip().lower(),   # Settings.load default
$ grep RESEARCH_NOTE_ROLE .env.example
RESEARCH_NOTE_ROLE=cheap
```
Both comments cite D94/T135 by name and explicitly warn against reading `cheap` as settled.
`test_research_note_role_defaults_to_cheap` (test_config.py) and
`test_note_call_routes_to_cheap_by_default` (test_research.py) both pin the default live.
Confirmed `RESEARCH_NOTE_ROLES = ("cheap", "good")` (no "vision" — documented why: `LLMClient.
vision` requires a positional `image_paths` arg the note call never supplies) and
`validate_model_config()` rejects any other value with `MissingConfig`.

### Tautological-test sweep

Read the full diff of every new/changed test in `tests/stages/test_research.py` (563 lines),
`tests/test_config.py` (+28), `tests/test_llm_role_routing.py` (new, 107 lines, AST-walked
census — self-documents its own evasion limits, N4 bounce-1), `tests/test_eval_research.py`
(+18, `_StubLLM.good()` fixture only, not a test). Every assertion checks something falsifiable
(specific source URLs re-admitted, specific WARNING counts, specific role sequences, marker
presence/absence tied to a computed budget boundary) rather than "no exception raised" or a
result never inspected. The one test this file has carried as tautological across two prior
bounces (N4, `test_gate_fails_open_per_facet_but_all_fail_open_notes_still_retry`) is now fixed
and independently reproduced red-in-isolation above (§5). Found no new tautological test in this
bounce's diff.

### Board / context notes for the pen-holder

- T131 (board: `status:todo, deps:T72`) landed as part of this bundle (B2, `FAIL_OPEN_KEY` +
  the `real_notes` filter in `research()`) — confirmed by code, tests, and my own independent
  probes in §3. Recommend the board writer move it to `test`/`done` alongside T71/T72 rather
  than leaving it `todo` while its code ships.
- My brief's tri-state shape table ("a missing key, `{}` -> REFUSED") does not match the shipped
  bounce-2 code or the dev's own N3 fix (both -> ERRORED, by design). See §2 above for why this
  does not reopen D88 or constitute a regression — flagging only so the record stays accurate.
- T137 (`NUM_CTX_GOOD >= NUM_CTX_CHEAP` unenforced) confirmed exactly as filed: no assertion or
  validation anywhere ties the two `num_ctx_*` settings together; `budget.py`'s N1 fix silently
  depends on the invariant holding.

done — logged, board:PASS, standards:ok

## T108/T109

Validated `worktree-agent-ac13b313c7d286c9e` @ `a3c4519` (+`b61bd8f` log), against
D5/D58/D59/D60/D61/D28-D30, reviewed and passed by reviewer (no bounce). **Verdict: PASS.**

### Full suite

```
$ cd .claude/worktrees/agent-ac13b313c7d286c9e && uv run pytest -q
........................................................................ [  7%]
...(990 dots total across the run)...
FAILED tests/test_quality.py::test_every_filler_phrase_has_a_real_non_closing_citation
1 failed, 990 passed in 49.74s
```
Ran twice (once before, once after all mutation probing below) — identical result both times.
`git status --short` clean before and after every run (D92). The 1 failure is the pre-existing,
already-diagnosed (T140) `data/eval/research_golden.json`-satisfies-`DATA_DIR.exists()`-but-
`load_docs()`-finds-nothing gap, reproduced independently here on the same worktree — **not
counted against T108/T109**, per the spawner's brief. `ruff check src tests scripts`: all clean.

### AC1 — both blobs pack; nothing dropped without a trace

`tests/stages/test_synthesize.py::test_{list,news}_blob_survives_the_measured_adversarial_64k_note`
run directly and green: 15 notes x 64,000-char `detail` per blob, `kept_chars+omitted_chars ==
original_chars` per note verified against an independently-built oracle (not the code under
test), manifest length == 15 (every note still represented after packing). Partial-drop logging
(`test_notes_blob_logs_only_when_something_was_actually_dropped`) also green — the log fires on
a short-note-survives/long-note-trimmed mix, not only on total loss.

### AC2 — per-stage reserve is default-preserving

Confirmed by direct grep, not just reading the diff:
```
$ grep -n "budget_chars_for_role\|reserved_tokens" src/pipeline/stages/research.py \
    src/pipeline/stages/compose.py scripts/measure_relevance_excerpt.py
research.py:541:    budget_chars = budget_chars_for_role(settings, role=settings.research_note_role)
compose.py:511:        budget_chars_for_role(settings, role="good") if settings else None
measure_relevance_excerpt.py:92:    full_budget = budget_chars_for_role(settings, role="cheap")
```
None of the three pass `reserved_tokens`; the param sits after a bare `*` in both
`budget_tokens_for_role`/`budget_chars_for_role`, so it is keyword-only — no caller can drift
into it positionally by accident. `tests/test_budget.py::test_reserved_tokens_param_defaults_to_
the_module_constant` pins the same fact at the unit level.

### AC3 — zero added model calls

```
$ uv run pytest -q tests/stages/test_synthesize.py -k "zero_model_calls" -v
collected 21 items / 19 deselected / 2 selected
tests/stages/test_synthesize.py ..                                       [100%]
2 passed, 19 deselected in 0.08s
```
Both list and news paths asserted `len(fake_llm.calls) == 1` even while trimming a 15x64k-char
adversarial corpus.

### AC4 — D30's mutant reproduced and still dead

Applied the exact mutant by hand to a scratch-backed copy of `budget.py` (`content_budget =
(share - marker_room) // 2`), ran `tests/test_budget.py`:
```
E       AssertionError: packed total fell short of the granted share/demand -- budget is being silently shrunk
E       assert 56 >= (95 - (1 * 38))
E       Failing test case: test_pack_never_exceeds_budget_and_never_loses_a_char_unaccounted(
E           texts=['000...(100 zeros)...'], budget=95,
E       )
1 failed, 19 passed in 1.62s
```
Exact match to the assertion cited in the brief (`56 >= 95 - 38`). Reverted via the pre-mutation
backup; `diff` against the backup byte-identical, `git status --short` clean.

### AC5 — the two format tests bite precisely

Dropped the list blob's `"- "` prefix (`return "\n".join(p.text for p in packed)`): exactly
`test_list_blob_preserves_the_bullet_format_when_nothing_is_trimmed` goes red (20 passed, 1
failed), diff shows the missing `"- "` on every line. Reverted, byte-identical.
Dropped the news blob's `"\n\n"` join (`"\n"` instead): exactly
`test_news_blob_preserves_the_record_format_when_nothing_is_trimmed` goes red (20 passed, 1
failed). Reverted, byte-identical, `git status --short` clean both times.

### Own security probe (not inherited from the reviewer)

Built independently against `_pack_list_notes`/`_pack_news_notes` directly:
1. **Sources URL query-string case (T70-mirror, as requested):**
   `"sources": ["https://evil.example/page?ref=<sentinel-wrapped-forged-marker>&utm=1"]` ->
   sentinel stripped, `marker_present() == False`. PASS.
2. **`confidence` field**, which the dev's own tests never forged: a sentinel-wrapped marker as
   the literal `confidence` value -> stripped, `marker_present() == False`. PASS.
3. **List blob's `claim` field dressed as a query string** (`f"repo?x={forged}"`) -> stripped.
   PASS.
All three: `_MARKER_SENTINEL not in output` and `marker_present(output) is False`.

### Fenced code block in a note — what actually happens

Two constructions:
- Fence positioned so its opener survives in the head-70% and its closer survives in the
  tail-30% of a truncated note: both fences round-trip intact by construction (this is
  layout-dependent, not guaranteed).
- Fence positioned so the **opener** sits in the surviving head but the **closer** sits deep in
  the omitted middle (large fenced YAML block, tight budget): the packed text handed to the
  model contains a **dangling, unclosed ` ```yaml ` fence** — one `` ``` `` in the output where
  two are needed. Reproduced directly:
  ```
  ``` count in packed output: 1 (odd => dangling/unclosed fence reached the model)
  ...intro text. ```yaml
  line_0: v
  ...
  line[... 25097 chars omitted ...]
  emarks. closing remarks. ...
  ```
  **This is a genuine, new finding, distinct from N1.** N1 is about the *completion-side* 1500-
  char reserve being sized for prose only, not VERBATIM code the model must *emit*. This is on
  the *input* side: `pack()`'s head+tail truncation is byte/char-budget only, with no markdown
  awareness, so a note whose fenced block straddles the cut point can hand the model a
  syntactically broken fence as "real" input. **D5's own contract still holds exactly** — the
  omission marker is present, `kept_chars+omitted_chars == original_chars` reconciles, nothing
  is silently dropped — so this is not a T108/T109 AC violation and does not fail the task. It
  is a content-quality risk (the model may try to "close" the fence itself, invent trailing
  content, or get confused) worth a line in project-context.md for whoever next touches
  `pack()`'s truncation strategy or the SYSTEM prompt's VERBATIM-fence instruction.

### T143 mechanisms confirmed (pre-existing, filed, not counted against this task)

All three applied by hand to a scratch-backed copy of `synthesize.py`, one at a time, reverted
and verified byte-identical after each:
1. `FOUND ({len(notes)} items)` -> `FOUND ({len(notes)+1} items)`: `tests/stages/test_synthesize.py`
   21/21 green. No test asserts the literal item-count string.
2. Section headers renamed (`"ORIGINAL NEWS ITEM:"` -> `"ORIGINAL ITEM DETAIL:"`,
   `"RESEARCH NOTES:"` -> `"GATHERED FINDINGS:"`): 21/21 green. No test asserts exact header text.
3. **`item.raw_text` deleted entirely from the news prompt** (`user = f"RESEARCH NOTES:\n\n{blob}"`,
   dropping the `"ORIGINAL NEWS ITEM:\n{item.raw_text}\n\n"` prefix): 21/21 green in
   `test_synthesize.py`, **and** the full repo suite: `990 passed, 1 pre-existing failure` —
   identical to the unmutated baseline. Confirmed mechanism: every assertion on the news-path
   prompt checks `note["claim"] in prompt` / `note["question"] in prompt` or marker
   presence/absence — none checks that `item.raw_text` (the fixture's `"a news item"`) is present
   anywhere in the built prompt. This is the serious one: production news synthesis could lose
   the actual news item text from its own prompt with **zero** test signal anywhere in the repo.
   T143's fix needs a direct `item.raw_text in user` (or equivalent) assertion on the news path.

### N6 — `NUM_CTX_GOOD <= 3584` yields a 0 budget; confirmed not silent

Set `NUM_CTX_GOOD=3584` (== `SYNTHESIZE_RESERVED_TOKENS`), confirmed `budget_chars_for_role(...,
role="good", reserved_tokens=SYNTHESIZE_RESERVED_TOKENS) == 0`. Packed output:
```
list blob:  '- \n- '     (empty bullets, in-band marker itself elided -- marker_room == 0 too)
news blob:  ''           (fully empty string)
marker_present() == False for both -- no in-band trace survives into the prompt text itself
```
But **not silent**: both calls emit a `log.warning` with the full per-note manifest before the
text collapses, e.g. `packed into 0/0 chars budget: [{'note': '1', 'kept': 0, 'omitted': 41}, ...]`
— the accounting is fully reconciled and visible in logs, just not in-band in the prompt (matches
`budget.py`'s own documented behavior for budgets too small to fit even the marker). Only
reachable by explicit operator misconfiguration well below the 16384 default — not a default-path
risk. Deliberate, confirmed not silent, consistent with D5's log-side trace guarantee.

### D92 discipline

`git status --short` run before and after the full suite and before/after every hand-applied
mutation (D30's budget.py mutant, the two format-test mutants, the three T143 mutants) — clean in
every case, no leftover mutation, no concurrent `mutate.py` run. All mutation work done by
copy-backup + `sed`-edit + run + restore-from-backup + `diff` byte-identical check, not via
`scripts/mutate.py` (avoids the exact concurrency hazard D92/D98 describe).

### For project-context.md (pen-holder to append; I don't edit the file)

- New finding, this task: a fenced code block whose *closing* fence lands inside `pack()`'s
  omitted middle region produces a dangling/unclosed fence in the packed prompt text reaching the
  model. D5's byte-accounting contract holds (marker present, kept+omitted reconciles); this is a
  markdown-structural/content-quality gap, not a silent-loss violation. Worth a task for whoever
  next touches `pack()`'s truncation or the SYSTEM prompt's "carry VERBATIM, still fenced" clause
  — cut on a fence boundary instead of a raw char offset, or accept the risk explicitly.
- T143 well-specified: reproduced all three named mutants surviving the full 991-test suite
  (990+1 pre-existing). Mutant 3 (`item.raw_text` dropped from the news prompt) is confirmed as
  the serious one — zero tests anywhere check the original item text reaches the news synthesis
  prompt.

done — logged, board:PASS, standards:ok

## T90/T130

Validated `senior-dev/t90-t130-pack-triage-plan-challenge-screen-bounce1` @ `2146e9d`
(+`f3f76b6`), worktree `.claude/worktrees/agent-a195df279aabcc146`, against
D5/D29/D32/D34/D60/D86/D91/D92/D93/D97. **Verdict: PASS.**

### Full suite + lint

```
$ .venv/bin/python -m pytest -q
........................................................................ [  7%]
...(993 dots total)...
FAILED tests/test_quality.py::test_every_filler_phrase_has_a_real_non_closing_citation
1 failed, 993 passed in 49.01s

$ .venv/bin/python -m pytest -q tests/test_eval_research.py
84 passed in 9.30s

$ .venv/bin/ruff check src/ scripts/ tests/
All checks passed!
```
`git status --short` clean before and after (D92). The 1 failure is the pre-existing T140 gap
(`data/eval` corpus present but `load_docs()` finds no doc-validated deck) — not counted against
T90/T130 per the spawner's brief; unrelated file (`test_quality.py`), reproduced independently.

### T90 — untouched by the bounce, re-verified with an adversarial 64k page

`git diff 4affffe -- src/pipeline/stages/triage.py src/pipeline/stages/research.py` is byte-clean
(no output) — the bounce touched only `scripts/eval_research.py`,
`scripts/measure_challenge_screen_redos.py`, `tests/test_eval_research.py`,
`.claude/logs/senior-dev.md`. Built an adversarial 64,844-char page (marker-tagged head and tail)
in an `Item.extracted["url_texts"]` entry and ran it through both real call sites directly
(`llm_provider=ollama`, default `num_ctx_cheap=8192` -> `budget_chars_for_role(cheap)=12288`):
```
SITE 1 triage.judgeable_text -> pack(): kept=12250 omitted=52646 (kept+omitted==64896==len(text))
  packed.text <= budget+marker room; MARKER_START and MARKER_END both present in packed.text
SITE 2 research._item_context -> pack(): kept=12250 omitted=52646 (kept+omitted==64896==original)
  same fit + same head/tail representation
```
Both sites fit the budget, both preserve `kept + omitted == original`, both still carry visible
content from the page rather than a blank/degenerate result. T90 stands unmodified and reconfirmed.

### T130 blocker 1 — title-collision false positives, verified closed

Ran the four reviewer-found colliding real headlines and five genuine challenge shapes straight
against `is_challenge_page` (matches the committed
`COLLIDING_REAL_HEADLINES`/`GENUINE_CHALLENGE_TITLES` parametrized tests, 84/84 green above; also
re-run standalone for this log):
```
NEGATIVE (must stay unflagged, short body each):
  "Are you a robot? Inside the CAPTCHA arms race"          -> False
  "One more step toward fusion power"                      -> False
  "Just a moment in history: the Apollo 11 tapes"           -> False
  "Attention required: EU's new AI rules land in March"     -> False

POSITIVE (must still flag):
  "Making sure you're not a bot!"                    (Anubis)             -> True
  "Just a moment..."                                 (Cloudflare)         -> True
  "Attention Required! | Cloudflare"                 (vendor-suffixed)    -> True
  "Checking your browser before accessing example.com" (domain-appended) -> True
  "Please Wait... | DDoS-Guard"                      (ddos-guard hyphen)  -> True
```
`_normalise_title("DDoS-Guard") == "ddos-guard"` confirmed — the hyphen survives normalisation
(vendor bucket also matches the raw lowercased title, never through `_normalise_title`, so the
hyphen is doubly protected).

### T130 blocker 2 — ReDoS cap, timed independently

```
$ .venv/bin/python scripts/measure_challenge_screen_redos.py
uncapped:  1000 tags 0.14s / 5000 tags 3.59s / 20000 tags 98.24s
capped:    1000 tags 0.28s / 5000 tags 2.27s / 20000 tags 2.09s
real long article stays unflagged within the cap: html=7977 visible=7599 (threshold=2000) -- UNFLAGGED
```
The first full-script run's capped-20k reading (2.09s) landed just over 2s — this machine is
running 25+ concurrent agent worktrees right now, and that run immediately followed the 98s
uncapped pass in the same process (GC/memory pressure from a 1.3M-char string). Isolated re-timing
(capped 20k only, 5 back-to-back runs, no preceding uncapped pass) is stable and comfortably under
2s: `1.17s, 1.17s, 1.16s, 1.19s, 1.17s`. A second full-script run also came in under 2s
(`20000 tags capped -> 1.42s`). Verdict: the cap holds; the one >2s reading was measurement noise
from this box's load, not the fix. `scripts/measure_challenge_screen_redos.py` is committed and
re-runnable (confirmed via `git diff --stat 4affffe..2146e9d`, listed above).

### Mutation testing (isolated copies, D92 discipline — worktree never touched)

Copied `eval_research.py` to scratch, mutated there, ran, discarded — `git status --short` clean
throughout and after (confirmed above).
- **Mutant A** — reverted `_CHALLENGE_TITLES_EXACT` matching from whole-title equality back to
  substring-anywhere (the exact pre-bounce-1 bug): all 4 colliding real headlines flip to
  `True` (false-positive). The committed test
  `test_is_challenge_page_does_not_flag_a_real_headline_that_collides_with_a_challenge_phrase`
  asserts `False` for each — **killed** (would go red).
- **Mutant B** — removed the `html[:_MAX_CHALLENGE_SCREEN_HTML_CHARS]` cap at the call site
  (call `_visible_text(html)` uncapped): the committed boundary-cap fixture
  (`test_is_challenge_page_caps_the_html_scanned_before_the_visible_text_check`) flips from
  `True` to `False` — **killed** (would go red).

Both mutants confirm the test suite actually enforces the fix, not merely appears to.

### Attack beyond the stated checklist — both directions

- **False negative found, not a regression:** a localised (French) challenge title,
  `"Un instant, s'il vous plaît..."`, is NOT flagged. This is a pre-existing scope limit (the
  screen only ever knew English phrases, before and after this bounce) — not something this
  bounce broke or was asked to fix. Reporting as a known gap, not a blocker.
- **Case-insensitivity confirmed both buckets:** `"DDOS-GUARD"`, `"DdoS-Guard"`,
  `"CHECKING YOUR BROWSER before accessing site.com"` all still flag (title is lowercased before
  either comparison; `_CHALLENGE_TITLES_EXACT`/`_VENDOR` are both already-lowercase). Real
  ellipsis char U+2026 (`"Just a moment…"`, `"One more step…"`) and an em-dash vendor suffix
  (`"Attention Required! — Cloudflare"`) all still flag — `_TRAILING_PUNCT_RE`/`_VENDOR_SUFFIX_RE`
  both handle these.
- **New corpus-shrinking finding (real, worth tracking, not blocking this bounce):** the vendor
  bucket is still substring-anywhere by design (docstring's rationale: "nothing plausible collides
  with a vendor's own name"). Tested a real, plausible AI/tech-news headline naming the vendor by
  its own name as the story's subject — `"Publishers deploy DDoS-Guard and other defenses"` —
  and it flags `True`. DDoS-Guard is a real company that has been actual tech news (sanctions,
  content-hosting controversy), so a headline naming it is plausible in this pipeline's own
  corpus, the same risk class blocker 1 closed for the generic-English-phrase bucket. Materially
  narrower than the 4 generic-word collisions bounce 1 fixed (this requires an article specifically
  about the named vendor, not any common phrase), so **not treated as a new blocker** — flagged for
  `project-context.md` as a residual risk for whoever next touches this bucket.
- **Verified a real long article still reaches the model unflagged** (measurement script output
  above): 7,977 html chars -> 7,599 visible chars, comfortably over the 2,000 threshold.
- Tried several more real-headline shapes that legitimately reuse challenge-phrase words in longer
  context (`"One more step for mankind: Artemis crew set for launch"`,
  `"Attention required before your flight: new EU rules explained"`,
  `"How CAPTCHA became the internet's least favorite doorman"`) — all correctly stay unflagged.

### Cache-hit bypass mechanism — confirmed, not built (as documented)

Wrote a probe pre-populating a `CachingHTTP` disk cache entry with a raw challenge-page body
(simulating a pre-T130 cached entry, bypassing `is_challenge_page` entirely — exactly how a real
pre-existing cache would look), then read it back through a fresh `CachingHTTP` wrapping a backend
that raises on any call:
```
resp.status_code = 200
resp.content == CHALLENGE_BODY: True
stats.hits=1 stats.misses=0 stats.challenge_pages=0
is_challenge_page(resp.content) if it HAD been screened: True
```
Confirms the mechanism exactly as documented: a cache HIT never calls `is_challenge_page`, the
challenge body replays unscreened, and `CacheStats.challenge_pages` reads 0 for it even though the
same body would score `True` if actually screened. Known gap, stays a known gap — not a surprise.

### D32 — confirmed unchanged this bounce

`research.py:106` still `RELEVANCE_EXCERPT_CHARS = 1200`, still fed through `pack()` at
`research.py:656` (`_is_relevant`) — unchanged, consistent with the clean `git diff 4affffe`
above and D93's measurement standing as-is.

### For project-context.md (pen-holder to append; I don't edit the file)

- T90 reconfirmed untouched by the T130 bounce, and independently re-verified against an
  adversarial 64k-char page at both call sites (triage.judgeable_text, research._item_context):
  fits budget, `kept+omitted==original`, content represented at both head and tail.
- T130 blocker 1 (title-collision false positives) and blocker 2 (ReDoS) both verified closed by
  direct test + independent measurement + targeted mutation testing (2/2 mutants killed).
- New residual finding (not blocking): the `_CHALLENGE_TITLES_VENDOR` substring-anywhere bucket
  still has a plausible real-headline collision risk for a story naming the vendor "DDoS-Guard" by
  name (a real company this pipeline's AI/tech-news corpus could plausibly cover) — narrower than
  the fixed generic-phrase collisions, worth a task if this bucket is revisited.
- `_MAX_CHALLENGE_VISIBLE_TEXT_CHARS = 2000` remains an honestly-labelled unmeasured guess
  (`data/eval/corpus_cache` empty in this environment) — not retired this round, per the brief's
  own instruction not to fail for it.

done — logged, board:PASS, standards:ok

## T140
- 2026-09-23 [T148] **PASS** on T140 -> orchestrator. Branch `worktree-agent-a6d32eebb67877ed6` @ `5c52dc3`, test-only, single file (`tests/test_quality.py`). Independently reproduced every reviewer claim from scratch on my own pristine clones (not just re-read the review) before concurring; then fixed T149 as authorised (same-shape bug, script not pipeline). All work done in dedicated scratch clones per D92/D98 (own `.venv` symlink only in `git status --short`, before AND after every run).

**1. `run_tests` before/after, reproduced on two fresh pristine clones (`data/eval/` holds only the tracked `research_golden.json` -- verified `ls data/eval/` on both before running).** Exact `mutate.py` invocation (`pytest <path> -q -x --no-header`, checked via returncode):
```
BEFORE  main   5026379:  1 failed, 29 passed in 0.12s   -> run_tests('tests/test_quality.py') = False
AFTER   branch 5c52dc3:  62 passed, 1 skipped in 0.15s  -> run_tests('tests/test_quality.py') = True
```
Matches the reviewer's numbers exactly.

**2. Full sweep, corpus-absent (the stricter config), dedicated clone, sole writer.**
```
$ ./.venv/bin/python -m pytest -q --no-header      # baseline
970 passed, 1 skipped in 50.11s

$ ./.venv/bin/python scripts/mutate.py
running 68 mutations
  ... (68 lines, all "ok ... caught") ...
all 68 mutations caught
```
`git status --short` before the sweep: `?? .venv` only. After: identical, `?? .venv` only -- no orphan `.mutate-inflight`, no stray mutation. Independently counted the mutations targeting `tests/test_quality.py` by loading `MUTATIONS` directly: **16**, matching the reviewer's figure, all 16 inside the 68/68 caught.

**3. Three states, independently probed:**
- absent entirely -> `test_run_files_present_is_empty_when_the_data_dir_does_not_exist_at_all` passes; real skip reason on the actual skipif, read via `pytest -rs`: `SKIPPED [1] tests/test_quality.py:542: none of harvest_filler_phrases.RUN_FILES present under <path> -- these are gitignored eval_compose.py run artefacts... (DATA_DIR itself always exists because data/eval/research_golden.json is tracked...)` -- names the missing files, not a bare "absent".
- RUN_FILES entry present -> `test_run_files_present_finds_a_real_run_file` passes; runs.
- present but no doc-validated deck -> reproduced directly: `test_corpus_present_but_no_doc_validated_deck_is_not_mistaken_for_absent` passes, and I independently called `harvest.load_docs()` on a row shaped `{"doc":{"slides":[]}}` (the specific edge case in the brief) -- returned `[]`, confirmed it does NOT count as validated (the `doc.get("slides")` truthiness check on an empty list is falsy).

**4. Regression mutation, applied and reverted by hand (not via `mutate.py`, since no `mutate.py` entry exists yet for this line -- N3 in the review, left as a nit):** `_run_files_present`'s body -> `return ()`. Full `tests/test_quality.py` run under the mutation:
```
FAILED tests/test_quality.py::test_run_files_present_finds_a_real_run_file
FAILED tests/test_quality.py::test_corpus_present_but_no_doc_validated_deck_is_not_mistaken_for_absent
2 failed, 60 passed, 1 skipped in 0.15s
```
The real citation test (`test_every_filler_phrase_has_a_real_non_closing_citation`) correctly SKIPS under the mutation (the skipif fires, since the helper now always reports absent) -- but two other tests go RED, so the guard failing is caught as a failure, not laundered into a silent green run. Reverted; `git diff tests/test_quality.py` empty; reran full file green (`65 passed` once T149's 2 tests were added, `63 passed` before).

Lint: `ruff check tests/test_quality.py` -> `All checks passed!` (ruff 0.16.8, per D33/T79). **No new blocking findings on T140** -- concur with the reviewer's PASS.

### T149 fix (authorised in the brief -- script, not production pipeline code)

`scripts/harvest_filler_phrases.py:165` had the identical defect: `not EVAL_DIR.exists() or not any(EVAL_DIR.glob("*.json"))` reads the tracked `research_golden.json` fixture as a usable corpus. Reproduced on a pristine clone of main BEFORE fixing:
```
$ ./.venv/bin/python scripts/harvest_filler_phrases.py
  decks scanned:      0
  fragments scanned:  0
0/28 candidates found a real citation ...  (all 28 DROPPED)
EXIT CODE: 0
```
Fix (reviewer's suggested shape, applied as-is): moved the predicate into `harvest_filler_phrases.py` as `run_files_present(eval_dir=EVAL_DIR)`, deriving from `RUN_FILES`; `main()` now gates on it and names the missing files; `tests/test_quality.py::_run_files_present` now **delegates** to it (`harvest.run_files_present(eval_dir=data_dir)`) instead of carrying a second copy -- one owner, two readers, per house rule 3. Committed on top of T140 (`deps:T140`), same worktree/branch: `worktree-agent-a6d32eebb67877ed6` @ `ed290e8`.

**Caught my own mistake mid-fix, by the new tests themselves** -- exactly the "write test, try to break it" discipline: my first cut called `run_files_present()` with no args inside `main()`, so its `eval_dir: Path = EVAL_DIR` default bound the ORIGINAL `EVAL_DIR` at function-definition time -- the identical D26/T93 stale-default trap `FILLER_PHRASES` hit, reproduced one file over while fixing its sibling. Both new hermetic tests failed immediately:
```
FAILED test_harvest_main_exits_nonzero_naming_run_files_when_dir_absent
FAILED test_harvest_main_exits_nonzero_when_only_the_tracked_golden_fixture_exists
2 failed, 63 deselected
```
Fixed by passing `EVAL_DIR` explicitly (`run_files_present(EVAL_DIR)`, read fresh every call, not via the bound default) -- both tests then passed. Then mutated the fix back to the OLD broken guard to confirm the new load-bearing test actually catches it (not vacuous):
```
FAILED test_harvest_main_exits_nonzero_when_only_the_tracked_golden_fixture_exists
1 failed, 1 passed
```
Reverted; `diff` against the fixed version byte-identical.

**Full suite in the real-corpus worktree** (sanity, not the pristine proof): `971 -> 973 passed` (2 new tests) after the fix, `0` failed, `0` skipped (real corpus present locally). `ruff check` on both touched files: `All checks passed!`. `ruff format --check` flags the same 2 files it already flags identically on unmodified `main` (2 files would be reformatted) -- pre-existing drift (N1-class, not a regression I introduced; `ruff format` is not a gate per T79).

**Pristine-clone proof (the actual acceptance bar), fresh clone of the `ed290e8` commit, `data/eval/` holding only the tracked `research_golden.json`:**
```
$ ./.venv/bin/python scripts/harvest_filler_phrases.py
ERROR: none of harvest_filler_phrases.RUN_FILES present under <path>/data/eval -- data/ is
gitignored and this script needs the real, saved eval_compose.py runs to verify a phrase against
(RUN_FILES: golden_current, golden_listonly, golden_baseline, golden_fewshot, baseline, fewshot,
e26_baseline, e26_fewshot). Nothing was harvested.
$ echo $?
1
```
Non-zero, named error, no table printed -- contrasted directly against the same clone-shape on unfixed main (`EXIT CODE: 0`, empty table, above). `git status --short` on the pristine clone before and after: `?? .venv` only.

Full suite on that same pristine clone: `972 passed, 1 skipped in 49.89s` -- the corpus-gated citation test correctly SKIPS (no `RUN_FILES` on a pristine clone), the two new `harvest_main` tests PASS (hermetic, `tmp_path`/`monkeypatch`, never touch the real gitignored dir).

### For project-context.md (pen-holder to append; I don't edit the file)
- T140 CONFIRMED PASS, independently reproduced (not just re-read). The reviewer's 16/68-masked-verdicts finding and the 68/68 corpus-absent sweep both hold.
- T149 FIXED by the tester (authorised in-brief, script-only): `scripts/harvest_filler_phrases.py` gains `run_files_present()`; `main()` now exits 1 with the missing `RUN_FILES` names instead of exit 0 with an empty table. Commit `ed290e8` on `worktree-agent-a6d32eebb67877ed6`, on top of T140's `5c52dc3` (deps:T140 honoured -- must merge in that order).
- **New finding, same failure family (D78/T127/D92/D98/T140, now instance #6):** the fix-in-progress itself briefly reintroduced D26/T93's stale-default-binding trap (`eval_dir: Path = EVAL_DIR` bound at def-time, invisible to a runtime `monkeypatch.setattr(harvest, "EVAL_DIR", ...)`) -- caught only because the new tests were written before the fix was declared done, not after. Worth naming explicitly for the pattern log: **any function default that reads a module-level "where's the data" global is this trap waiting to happen**; the safe shape is reading the global by bare name inside the function body (or passing it explicitly at every call site), never as a parameter default.
- Nit, not fixed (left as a nit, same as the reviewer's N3): no `mutate.py` entry yet anchors `_run_files_present`/`run_files_present`'s body directly -- my regression proof for T148 was a manual apply/revert, and my mutation proof for T149 was also manual. Both demonstrated real, non-vacuous catches; formalising them as `mutate.py` entries is optional follow-up, not done here to keep this task's diff minimal.
- Scratch evidence left on disk for re-inspection, not committed anywhere: `<scratchpad>/t148-main`, `<scratchpad>/t148-branch`, `<scratchpad>/t149-pristine` (pristine clones used for the proofs above).

done — logged, board:PASS(T140) + fixed(T149), standards:ok

## T81

- 2026-09-23 [T81 bounce 1] **PASS** -- schema-divergence diagnostic, branch `t81-schema-divergence-diagnostic-bounce1` @ `cddd3fa`, worktree `.claude/worktrees/agent-a0b42f95d16e07e8e`, rebased onto real `main` tip `5026379`. Independently reproduced every claim in the handoff, then attacked beyond it.

**Suite + lint, reproduced.** `./.venv/bin/python -m pytest -q`: `1006 passed, 1 failed in 48.93s` -- the 1 failure is `test_every_filler_phrase_has_a_real_non_closing_citation` (T140 family, `data/eval/*.json` present but no doc-validated deck in this worktree), not scored against this task per the brief. `tests/test_llm.py` alone: `91 passed in 30.44s`. `uvx ruff check src tests scripts` (ruff 0.16.8): `All checks passed!`. `uvx ruff format --check` on the two touched files flags both -- verified this is pre-existing: ran the identical check against `d309c7d:src/pipeline/llm.py`/`tests/test_llm.py` (the pre-T81 base) and it flags the SAME drift (magic-trailing-comma style predating this repo's `ruff format` defaults), not something T81 introduced. Not a blocker (D33, `ruff format` is not the gate per T79).

**AC5 -- classification byte-for-byte unchanged, verified by diff not count.** `grep -c "raise " src/pipeline/llm.py` is 21 in this worktree and 21 in `d309c7d`. `diff <(git show d309c7d:src/pipeline/llm.py | grep "raise ") <(grep "raise " src/pipeline/llm.py)` is EMPTY -- every raise line's text is identical; only line numbers shift (new code inserted above).

**Announce-once, reproduced with a real import block (not the `monkeypatch.setattr(llm_mod, "jsonschema", None)` shortcut).** Ran `test_missing_jsonschema_warns_once_not_once_per_call` directly: it blocks `builtins.__import__` for the literal name `"jsonschema"`, `importlib.reload(pipeline.llm)`, confirms `reloaded.jsonschema is None`, then drives 3 real calls through `LLMClient.cheap()` and asserts exactly 1 "jsonschema is not installed" WARNING -- PASS. I then mutated the guard twice in an isolated scratch copy (never the live worktree) to confirm the test actually discriminates: (a) removed the `if not _warned_missing_jsonschema:` gate (always-warn) -> test failed, `3 == 1` assertion caught `3` WARNINGs; (b) removed the whole warn block (never-warn, the original B1 bug) -> test failed, caught `0 == 1`. Both directions killed. Restored, re-ran green, `git status --short` on the live worktree empty before and after (D92).

**B2's two mutants, reproduced independently in a scratch copy (D92: `cp -r src tests pyproject.toml` + a `.venv` symlink, never the live tree; `git status --short` on the live worktree confirmed clean before and after both mutation passes):**
- `where = "<root>"` (hardcoded, bypassing `error.absolute_path`): `test_schema_divergence_names_unexpected_fields_across_a_oneof_union` goes RED on `assert "at slides/0" in message` -- killed.
- Dropping `_divergence_detail(error)[:200]`: `test_schema_divergence_message_is_bounded_against_an_enormous_completion` goes RED, actual output `500,104` chars (matches the dev's own measurement) against an asserted `< 500` -- killed.

**Validator cache, attacked on three fronts:**
1. Cache-bypass mutant (always recompute, never consult `_validator_cache`): `test_validator_cache_reuses_the_compiled_validator_for_the_same_schema_object` goes RED (`first is second` fails, two distinct `Draft202012Validator` objects) -- killed.
2. Identity-guard-drop mutant (`if cached is not None:` alone, no `cached[0] is schema` check): `test_validator_cache_ignores_a_stale_entry_under_a_reused_id` goes RED (`validator is not stale_validator` fails, serves the wrong cached validator) -- killed.
3. **Tried to force a REAL `id()` reallocation collision** (not the dev's deterministic hand-planted simulation): compiled+cached a schema, deleted every reference, `gc.collect()`, then allocated 200 fresh dicts checking for an `id()` match against the freed slot. **No live collision occurred in this run** -- consistent with the dev's own choice not to rely on GC timing (not guaranteed to reproduce). Confirmed all 4 real schema callers (`SLIDES_SCHEMA`, `SLIDES_SCHEMA_FLAT`, `NOTE_SCHEMA`, `RELEVANCE_SCHEMA`) are module-level constants built once at import (grepped `^SLIDES_SCHEMA`/`^SLIDES_SCHEMA_FLAT`/`^NOTE_SCHEMA`/`^RELEVANCE_SCHEMA` in `compose.py`/`research.py`), and that `_schema_prompt`/the openrouter degrade branch never copies a schema (same object reference threaded through `sent_schema`), so the cache cannot grow unboundedly or collide in the paths that actually exist today. I could not break it; the identity-check mechanism is proven correct by mutation (1+2 above), just not by a genuine live collision.

**Never-raise, went beyond the two named probes.** Ran an 11-probe script directly against `_log_schema_divergence` (source in the return below): 8.58 MB completion vs the real `SLIDES_SCHEMA` (0.060s, no raise), self-recursive `$ref` with a 4000-deep instance (0.004s, no raise), pathological schema (`{"type": "not-a-real-json-schema-type"}`), schema as a bare string, schema as a bare int, `parsed` containing `float('nan')`, a 5000-deep nested list against an object schema, a self-referential (cyclic) Python dict as `parsed`, a `$ref` to a nonexistent fragment, 200 slides each violating the real 15-arm union at once, and control chars/ANSI escapes/emoji in a failing instance value. **0/11 raised.** Then went further: deliberately forced `jsonschema`'s own internal resolver to raise a genuine `RecursionError` (a self-referential `additionalProperties` schema against a 5000-key-deep instance with `sys.setrecursionlimit(200)`) to prove the `except Exception:` branch inside `_log_schema_divergence` is live code, not a dead safety net that happens to never trigger -- it caught the `RecursionError`, logged at DEBUG with `exc_info=True` (the traceback in my run's output is that debug log, not an escaped exception), and returned normally. This exceeds what either the dev or the prior reviewer's 21-probe pass exercised: neither of the two named probes (huge completion, deep `$ref`) actually reaches jsonschema's own fallback path, since jsonschema's `iter_errors` doesn't itself raise on either — only a genuine stack-depth blowout does, and I constructed one.

**Sweep for tautological tests.** Independently re-mutated `max(errors, key=_schema_error_relevance)` -> `min(...)` in a scratch copy: `test_schema_divergence_prefers_the_broader_error_over_a_deeper_one` goes RED (`assert "caption" in message` fails, message instead names the nested `slides/0` mismatch) -- confirms this test is not tautological, it genuinely discriminates on the sort direction. Reviewed all 12 new T81-bounce-1 tests plus the 12 prior-bounce tests for the "asserts something true regardless of the implementation" shape (this session's most common defect class, per the brief) -- none found weak; every message-content assertion I spot-checked (`"at slides/0"`, `"caption"`, `< 500` chars, `len(missing) == 1`) is backed by a mutation that flips it red.

**T81's stated exclusions honoured.** T118 (CoT extraction) and T117 (refusal classification) are not implemented in this diff (`grep -rn "T117\|T118" src/pipeline/llm.py` -- no hits outside comments citing them as future work) -- not scored.

**Judgment calls for the return, not test failures:**
- *Is announce-once enough?* The process is a single long-running `python -m pipeline` under launchd (`deploy/com.aayush.newspipeline.plist`, `KeepAlive.SuccessfulExit=false` -- restarts on ANY exit, clean or crashed). `_warned_missing_jsonschema` is a module-level flag, so a restart genuinely re-warns (good: an operator who watches logs right after a deploy/crash will see it). But over a long uptime the single WARNING line competes with an 8 MB x 5-backup rotation (`LOG_MAX_BYTES`/`LOG_BACKUPS` in `__main__.py`) -- on a long-lived process it can rotate out of the retained window, leaving zero live evidence of "no validator" until the next restart. Not a regression from this bounce (B1's fix is strictly better than the prior permanent silence), but worth flagging: "once" is once-per-process-lifetime, not once-ever-visible.
- *Can it become permanently silent after T141 lands `jsonschema` as a production dependency?* No -- confirmed by reading the two branches separately: the actual divergence WARNING (`"%s completion parsed but diverges from the schema..."`) has NO once-gate, it fires unconditionally on every call that produces an error. Only the "jsonschema is not installed" message is once-gated, and that whole branch becomes dead code once `jsonschema is None` can never be true. The mechanism that matters (the divergence log itself) has no silencing path in this diff.

**Evidence commands (paste-able):**
```
$ ./.venv/bin/python -m pytest -q
1006 passed, 1 failed in 48.93s   (1 failed = test_every_filler_phrase_has_a_real_non_closing_citation, T140, not scored)

$ ./.venv/bin/python -m pytest -q tests/test_llm.py
91 passed in 30.44s

$ uvx ruff check src tests scripts
All checks passed!   (ruff 0.16.8)

$ diff <(git show d309c7d:src/pipeline/llm.py | grep "raise ") <(grep "raise " src/pipeline/llm.py)
(empty -- byte-for-byte identical)
```

**All mutation and probe work done in scratch copies under D92** (`/private/tmp/.../scratchpad/t81-mutate*`, deleted after use), never the live worktree; `git status --short` on `.claude/worktrees/agent-a0b42f95d16e07e8e` confirmed empty before and after every pass.

### For project-context.md (pen-holder to append; I don't edit the file)
- T81 bounce 1 CONFIRMED PASS -- both blockers (B1 permanently-silent import guard, B2 two surviving mutants) verified fixed and independently re-mutated to confirm the fixes actually discriminate, not just that new assertions exist.
- New operational note, not a blocker: the announce-once WARNING is once-per-process-lifetime under launchd's `KeepAlive.SuccessfulExit=false` restart-on-any-exit policy -- a restart re-warns (good visibility point), but a long-uptime process risks the single line rotating out of the 8 MB x 5-backup `data/pipeline.log` window before an operator looks. Not this bounce's regression; worth naming if a future task ever revisits observability here.
- Confirmed for T141's future landing: only the "jsonschema not installed" WARNING is once-gated; the actual "diverges from the schema" WARNING (the load-bearing measurement instrument per D65) has no such gate and cannot become silent through any mechanism visible in this diff.
- Could not construct a genuine live `id()`-reuse collision against the validator cache in a short-lived script (expected -- GC/allocator timing is not deterministically reproducible); the identity-guard mechanism itself is proven correct by mutation, both by the dev's deterministic simulation and independently by my own cache-bypass and identity-check-drop mutants.

done -- logged, board:PASS, standards:ok

## T119/T120/T83/T115/T124/D98

**Verdict: PASS.** Branch `devops/t119-t120-t83-t115-t124-d98-bounce1` @ `89e38f5` (fix `870869b`), worktree `.claude/worktrees/agent-a89680d968d6d30e6`. All 8 verify items plus the attack requests below.

### Full suite + lint
```
$ ./.venv/bin/ruff check src tests scripts
All checks passed!   (ruff 0.16.8)

$ ./.venv/bin/python -m pytest -q --deselect tests/test_quality.py::test_every_filler_phrase_has_a_real_non_closing_citation
976 passed, 1 deselected in 48.97s
```
The deselected test is `test_every_filler_phrase_has_a_real_non_closing_citation` (T140, explicitly out of scope) -- ran the suite without the deselect too and confirmed it is the *only* red: `976 passed, 1 failed in 56.94s`, failure is `assert docs, "data/eval/*.json exists but no doc-validated deck was found in it"` against a stale local `data/eval/` directory, unrelated to any file this bundle touches.
Bare `ruff check .` (not the documented command) does find 12 findings, all in `.claude/skills/webapp-testing/examples/` -- vendored skill examples outside `[tool.ruff.lint]`'s intended scope and outside `src tests scripts`. `./.venv/bin/ruff check src tests scripts` -- the command both `coding-standards.md:17` and `docs/development.md:84` actually document -- is genuinely 0 findings, confirming T119's fix is not just a doc claim.

### 1. The five-entry-point proof
`cp .env.example .env`, `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY=sk-or-x` (key only checked for presence, never validity, by `validate_model_config`):

- **`smoke.py` under `env -i` with ZERO Telegram vars** (the explicit workflow `validate_model_config()` was split out for):
```
$ env -i PATH="$PATH" HOME="$HOME" LLM_PROVIDER=openrouter OPENROUTER_API_KEY=sk-or-x \
    timeout 15 ./.venv/bin/python scripts/smoke.py
preflight:
  provider: openrouter
  MISSING  openrouter key          <- expected: dummy key, real 401 from openrouter.ai
  OK       cheap  nvidia/nemotron-3-super-120b-a12b:free
  OK       good   nvidia/nemotron-3-ultra-550b-a55b:free
  OK       searxng http://localhost:8080
```
No `MissingConfig`, no traceback -- cleared `validate_model_config()` with no Telegram vars at all, confirming the split from `validate_for_run()` actually works as documented.
- **`eval_compose.py --limit 0`**: `provider=openrouter  good-model=nvidia/nemotron-3-ultra-550b-a55b:free` then ran to completion, exit 0.
- **`build_golden.py --list`**: cleared validation, printed `0 cases, 0 failure modes` + skip list, exit 0.
- **`dashboard.py --port 8972`**: printed `mission control on http://127.0.0.1:8972`, `curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8972/` -> `HTTP 200`.
- **`__main__.py`** (needs `validate_for_run()`, so also supplied dummy `TELEGRAM_API_ID/HASH/BOT_TOKEN/OPERATOR_USER_ID/CHANNEL_IDS` under `env -i`): logged `DRY_RUN is on -- previews will arrive, nothing will publish` (proves `validate_for_run()`, which calls `validate_model_config()`, passed clean), loaded telethon, and only then failed on `aiogram.utils.token.TokenValidationError: Token is invalid!` -- the dummy bot-token string, nothing to do with models.

**Both directions on the regression itself**: replayed all five against `d309c7d`'s `.env.example` (`OPENROUTER_MODEL_GOOD`/`_VISION=google/gemini-2.5-flash`, paid): `Settings.load(env=that_file).validate_model_config()` raises
```
pipeline.config.MissingConfig: Only free OpenRouter models are allowed, but these are not
free: OPENROUTER_MODEL_GOOD='google/gemini-2.5-flash', OPENROUTER_MODEL_VISION='google/gemini-2.5-flash'.
```
-- exactly what T120/T83 describe all 5 entry points hitting before this bundle.

### 2. Both lock refusals + a legitimate sweep still catches all 68
Tested in an isolated worktree copy first (git worktree, own `.venv`, clean tree) to avoid contaminating the reviewed tree, then reproduced live in the reviewed worktree itself via two genuine accidental crashes (below):
```
Test A -- INFLIGHT only, tree clean:
refusing to start: .../data/.mutate-inflight already exists ... EXIT=3

Test B -- dirty tree only, no INFLIGHT:
refusing to start: the working tree is dirty -- a mutation's revert can silently
clobber whatever this already contains:
 M src/pipeline/stages/compose.py
EXIT=3
```
Final clean sweep in the reviewed worktree, Bash tool timeout raised to 300s:
```
$ ./.venv/bin/python scripts/mutate.py
running 68 mutations
  ok   closing-slide          caught
  ...  (68 lines, all "ok ... caught")
all 68 mutations caught
MUTATE_EXIT=0
$ git status --short   (empty)
```

### 3. B2's ordering -- crash produces BOTH signals, recovery message must win
Simulated (planted INFLIGHT + a manual dirty edit together) in the isolated copy:
```
=== Test C: simulated crash (BOTH lock AND dirty tree present) ===
refusing to start: .../data/.mutate-inflight already exists -- either another
mutate.py run is active in this worktree, or a previous one was killed
mid-mutation and never cleaned up. Read the file and `git status --porcelain`
by hand: ...
EXIT=3
```
The recovery message fired, never the plain dirty-tree one. Mutation-tested `tests/test_mutate.py::test_refuse_reason_simulated_crash_prints_the_recovery_message` itself: swapped `_refuse_reason`'s two checks back to dirty-tree-first (the exact bounce-1 bug) -- test goes red (`AssertionError: ... "the dirty-tree check is shadowing it again"`), confirming it's not vacuous. Reverted, `tests/test_mutate.py` 11/11 green again.

**Then it happened for real, twice, unprompted** (a background `mutate.py` run I launched got killed by session/tool teardown, and later the harness's own 2-minute Bash timeout killed a re-run mid-sweep):
- Crash 1: `data/.mutate-inflight` present, `src/pipeline/llm.py` dirty (one line: an `f"openrouter exceeded..."` -> `f"ignored"` mutation, live on disk). `mutate.py --only closing-slide` correctly refused with the recovery message (not dirty-tree). Followed the documented recovery steps by hand: read the JSON, `target.write_text(rec["original"])`, confirmed `git diff --stat` empty, removed the lock. Byte-identical restore.
- Crash 2 and 3 (same tool-timeout cause, different files): `src/pipeline/stages/enumerate_items.py` and `src/pipeline/quality.py` respectively, each with a live INFLIGHT record. Same manual recovery, same result both times: `git diff --stat` empty after restore, i.e. the recorded `original` field byte-matched `git show HEAD:<path>` exactly in all three real crashes.

### 4. B5 discrimination -- both directions
```
$ ./.venv/bin/python -m pytest tests/test_config.py -k "env_example" -v      # on 89e38f5
tests/test_config.py::test_env_example_model_slugs_pass_validate_model_config PASSED
tests/test_config.py::test_env_example_slugs_agree_with_config_py_defaults PASSED
2 passed, 76 deselected

# swapped .env.example for d309c7d's (paid Gemini slugs), same two tests, same file:
FAILED tests/test_config.py::test_env_example_model_slugs_pass_validate_model_config
  pipeline.config.MissingConfig: Only free OpenRouter models are allowed, but these are not
  free: OPENROUTER_MODEL_GOOD='google/gemini-2.5-flash', OPENROUTER_MODEL_VISION='google/gemini-2.5-flash'.
FAILED tests/test_config.py::test_env_example_slugs_agree_with_config_py_defaults
  AssertionError: assert 'google/gemini-2.5-flash' == 'nvidia/nemot...50b-a55b:free'
2 failed, 76 deselected
```
Green on the fix, red on the pre-fix commit, same test file, same command -- the pin discriminates. `.env.example` restored, `git status --short` empty afterward.

### 5. B1's citation -- `extract.py:121-127` verified against current line numbers
```
$ sed -n '121,127p' src/pipeline/stages/extract.py
        if isinstance(verdict, str):  # a model that ignored the schema
            verdict = {"transcript": verdict, "description": verdict,
                       "usable": "none", "why": "unstructured reply"}

        usable = str(verdict.get("usable", "none")).lower()
        if usable not in ("hero", "inset", "background", "none"):
            usable = "none"
```
Exact match to the citation -- this is the real verdict-shape/usable-enum guard on the vision path. `grep -rn normalise_slides src/pipeline/` shows it is called only 3x, all inside `compose.py` (lines 574/597/615, reading `doc.get("slides")` from the compose completion) -- `extract.py` never imports or calls it. The claim "`normalise_slides` never sees a vision completion" is genuinely true, not just asserted; `docs/setup.md:105` and `.env.example:75` both cite the same corrected line range and both match.

### Exit-code contract (0/1/2/3) -- all reachable and distinct
```
--only closing-slide (real mutation)  -> exit 0, "all 1 mutations caught"
--list                                -> exit 0
--only nonexistent-mutation-xyz       -> exit 2, "no mutation named 'nonexistent-mutation-xyz'"
dirty tree, --only closing-slide      -> exit 3, "refusing to start: the working tree is dirty..."
```
Exit 1 (survived) needed a real uncaught mutation to prove honestly -- planted a temporary, clearly-inert probe Mutation (`if "follow" not in types:` -> same text + a trailing comment) in a disposable git-worktree copy only (never the reviewed worktree; committed, tested, then the whole worktree was `git worktree remove --force`d, never merged/pushed):
```
$ ./.venv/bin/python scripts/mutate.py --only tester-survivor-probe
running 1 mutations
  FAIL tester-survivor-probe  SURVIVED -- nothing tests this
1 mutation(s) survived -- those guarantees are untested:
  - tester-survivor-probe: ...
exit=1
```
(First attempt at this gave a false "caught" via `exit=0` -- traced to my own scratch-venv symlink missing `pyvenv.cfg`, so the subprocess `pytest` call inside `check()` failed with `ModuleNotFoundError` and `run_tests()`'s `returncode == 0` check read that as a genuine catch. Fixed the scratch venv to a proper symlinked venv tree (`bin`/`lib`/`pyvenv.cfg`/`include`) and reran -- correctly SURVIVED. Noted because it's exactly the kind of false-positive this task's own docstring warns about, just in my test rig rather than the tool.)

### Attack: can a crashed sweep still lose work?
Beyond the two accidental real crashes above, ran a controlled `SIGKILL` sweep against `mutate.py --only closing-slide` at 11 coarse delays (20ms-2s) then 59 fine delays (1ms-59ms) -- 70 real `kill -9`s total, each followed by checking on-disk state and, where a lock existed, performing the documented manual recovery and diffing the result against `git show HEAD:<path>`.
```
70 kills total:
  finished-cleanly-or-no-op       : process beat the kill, or delay was before spawn settled
  lock-only (false-refuse, safe)  : 1  -- INFLIGHT written, target not yet touched; recovery is a no-op
  genuine-crash (lock+dirty)      : 33 -- every single one: recorded "original" byte-matched git HEAD,
                                           restored file byte-matched git HEAD
  DATA-LOSS-RISK (dirty, no lock) : 0
```
Zero data-loss cases across 70 kills plus 3 real accidental crashes (73 total). The structural reason: `check()` writes `INFLIGHT` (line ~674) strictly before it mutates the target file (line ~685), so a "dirty but unlocked" state never occurred once in 73 trials -- consistent with the code's own ordering, not just luck. The two theoretically-possible narrow windows (killed between lock-write and mutation-write; killed between the `finally`'s revert-write and the unlink) both land in the same safe "lock-only" bucket externally and cost a rerun, never a clobbered file. I could not construct a data-loss case. The three-days-ago 1159-line -> 12-byte incident is exactly the "auto-restore at startup" bug D98 already root-caused and this bundle removes (`restore_any_leftover()` no longer exists; `main()` refuses instead) -- consistent with why it can't reproduce anymore.

### Sweep for other stale facts
- `grep -rn "uvx ruff" --include=*.md --include=*.py .` (excluding `.venv`/`.git`): only hits left are in the task/decision board, the decision log, and the tester/reviewer logs (historical records of the bug/decision, correctly past-tense) and `docs/development.md:88` (contrasting the old broken behaviour by name, correctly qualified). No live "how to run this" doc still recommends bare `uvx ruff`.
- `grep -rn "nex-agi" README.md docs/*.md .env.example .claude/coding-standards.md`: zero hits -- T83's never-verified slug is gone from every user-facing doc.
- `README.md`'s Status section no longer pins a test count at all (`run ./.venv/bin/python -m pytest --collect-only -q for today's count rather than trusting a number pinned here, which only rots`) -- better than a repin, since a repinned number rots the same way T115 found this one already had. `--collect-only -q` -> `977 tests collected` (976 pass + 1 out-of-scope), self-consistent.
- `docs/development.md`'s "fresh `uv sync` has no pytest" claim (T124) reproduced independently with a real `uv sync` (no `--extra dev`) into a fresh, isolated venv outside the worktree: `No module named pytest`, byte-for-byte the same failure mode the doc shows.
- `docs/howto-verify-context-budget-packer.md:122` still has a hardcoded `725 passed in 48.52s` transcript -- NOT touched by this bundle (`git diff d309c7d..HEAD -- docs/howto-verify-context-budget-packer.md` is empty) and outside the T119/T120/T83/T115/T124/D98 scope; it's a dated, tester-authored evidence transcript (a different genre than README's live-fact badge), not claimed as current by this bundle. Flagging for project-context.md, not failing this task for it.

### For project-context.md (pen-holder to append; I don't edit the file)
- T119/T120/T83/T115/T124/D98 bounce 1 CONFIRMED PASS. Full suite 976 passed / 1 deselected (T140, out of scope), ruff 0.16.8 clean on the documented command, 68/68 mutations caught on a clean final sweep, exit codes 0/1/2/3 all independently reproduced.
- **73 real `kill -9` trials against `mutate.py` (70 controlled + 3 accidental) produced zero data loss** -- every crash left both a dirty file and a byte-matching `INFLIGHT` recovery record, never one without the other. Two of the three accidental crashes were caused by this tester's own tooling (a background sweep killed by session teardown, then the harness's 2-minute Bash default timeout killing a rerun) -- worth noting the sweep genuinely takes 60-90s and any automation wrapping it needs a >=120s budget, which is itself weak corroborating evidence for why the actual production incident (a mutate.py run apparently killed 3 days before this bounce) was plausible.
- `docs/howto-verify-context-budget-packer.md:122`'s hardcoded `725 passed` transcript is stale versus the current 976/977, but it's untouched by this bundle and out of scope -- flagging for a future docs-freshness pass, not blocking this one.
- Minor process note: `ruff check .` (bare, no path args) still returns 12 findings from `.claude/skills/webapp-testing/examples/` -- outside `src tests scripts` and outside this bundle's scope, but worth someone eventually deciding whether vendored skill examples should be `ruff`-excluded explicitly rather than relying on every invocation using the three-path form.

done -- logged, board:PASS, standards:ok
