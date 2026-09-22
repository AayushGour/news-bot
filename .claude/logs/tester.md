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
