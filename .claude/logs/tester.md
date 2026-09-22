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
