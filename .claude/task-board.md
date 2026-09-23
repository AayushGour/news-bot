# Task board — <project>    size:<S|M|L>   (PM sets size; architect may bump)

Owner: architect creates/assigns. **Single-writer rule:** a spawned worker never edits this file — it returns status to its spawner, who writes it here (CLAUDE.md integrity rule 1). `done` is authorized by a **tester** PASS + evidence and recorded by the board writer as `status:done  evidence:<ref>` (rule 2); the board-lint hook blocks evidence-less done lines.

Format:
`- [ ] T<id> [owner] <title>  prio:<P0|P1|P2|P3>  status:<todo|wip|review|test|done|blocked>  deps:<ids|->`
done lines add: `evidence:<ref>` (tester log anchor; the deliverable itself for non-code tasks) — required.
owners: architect | product-engineer | ux-designer | senior-dev | junior-dev | devops | reviewer | tester
prio (PM sets): P0 critical · P1 high · P2 medium · P3 low
size (PM sets): S small/obvious · M normal full-loop · L big/complex + parallel

## Plan mode  (done before dev mode)
- [x] T0 [business-analyst] Requirements → project-context.md  prio:P1  status:done  evidence:project-context.md
- [ ] T1 [architect] Design + standards + task split  prio:P1  status:wip
- [ ] T1a [ux-designer] Flows + design system + a11y AC → design.md  prio:P2  status:todo  deps:T1
- [ ] T1b [product-engineer] Feasibility + spike unknowns  prio:P2  status:todo  deps:T1

## Dev mode
- [ ] T20 [senior-dev] Compose eval harness + few-shot examples (scripts/eval_compose.py, stages/examples.py)  prio:P2  status:test  deps:-  bounces:0
- [ ] T21 [senior-dev] Pin OpenRouter good-model; run baseline + fewshot eval  prio:P1  status:test  deps:T20  bounces:0
- [ ] T22 [devops] Host disk — resolved on its own (now 77%, 211Gi free); cleanup script added  prio:P3  status:test  deps:-  bounces:0
- [ ] T26 [senior-dev] Two-stage local fallback chain (MODEL_*_FALLBACK)  prio:P1  status:test  deps:-  bounces:0
- [ ] T27 [senior-dev] Mission control dashboard (loopback-only aiohttp)  prio:P2  status:test  deps:-  bounces:0
- [ ] T59 [senior-dev] Dashboard: job lanes, per-item log + timeline, chat per item  prio:P2  status:test  deps:T27  bounces:0
- [ ] T60 [senior-dev] Removed every content cap; platform limits kept and documented  prio:P1  status:test  deps:-  bounces:0
- [ ] T61 [senior-dev] num_ctx_good=16384 will overflow on uncapped docs via the local fallback — superseded by T70  prio:P1  status:blocked  deps:T70  bounces:0
- [ ] T28 [senior-dev] Free models only — enforced at startup + preflight price check  prio:P1  status:test  deps:-  bounces:0
- [ ] T29 [senior-dev] Requeue stage table cleared the wrong stage's output  prio:P1  status:test  deps:-  bounces:0
- [ ] T30 [senior-dev] Caption edit sent raw tuples as reply_markup — ValidationError every time  prio:P0  status:test  deps:-  bounces:0
- [ ] T31 [senior-dev] Pending prompt swallows the next unrelated message as caption text  prio:P1  status:test  deps:T30  bounces:0
- [ ] T23 [senior-dev] Fix eval scorer: vacuous tail check + sources-key URLs; add enumeration_ok, persist decks  prio:P1  status:test  deps:T21  bounces:0
- [ ] T24 [senior-dev] 5 of 30 decks composed with no closing slide — guarantee it in code  prio:P1  status:test  deps:T23  bounces:0
- [ ] T25 [senior-dev] Re-research item 26 (notes predate e7ed4ca, lack url/owner/logo) then rerun both eval arms  prio:P2  status:blocked  deps:T23  bounces:0
- [ ] T35 [senior-dev] Pinned golden eval set (24 cases, 9 failure modes) + prompt fingerprint  prio:P1  status:test  deps:-  bounces:0
- [ ] T36 [senior-dev] FEW_SHOT_EXAMPLES defaults to "list" (24/24, 0 failures)  prio:P2  status:test  deps:T35  bounces:0
- [ ] T42 [senior-dev] Links index guaranteed in code (was 1/4 enumerations)  prio:P2  status:test  deps:T36  bounces:0
- [ ] T46 [senior-dev] Query expansion + parallel search + catalogue routing (repos|web)  prio:P1  status:test  deps:-  bounces:0
- [ ] T50 [senior-dev] Web enumerations extract THINGS from documents, not list sources  prio:P1  status:test  deps:T46  bounces:0
- [ ] T51 [senior-dev] _score_web ranks now — finer gradients + host authority  prio:P2  status:test  deps:T46  bounces:0
- [ ] T52 [senior-dev] Clause gate runs on the enumeration path too  prio:P1  status:test  deps:T43,T44  bounces:0
- [ ] T53 [senior-dev] scripts/mutate.py — 24 mutations, anchor-checked, bytecode-safe  prio:P2  status:test  deps:-  bounces:0
- [ ] T54 [senior-dev] Provider 404 gets no retry and no local fallback, unlike a rate limit  prio:P1  status:test  deps:-  bounces:0
- [ ] T58 [senior-dev] Answers route by swipe-reply; lower id no longer eats every reply  prio:P1  status:test  deps:-  bounces:0
- [ ] T55 [tester] Quality check vs golden_listonly — on par; caught+fixed the links/closing trim clash  prio:P1  status:test  deps:T50,T51,T52  bounces:0
- [ ] T56 [senior-dev] Compound clause reported as wholly uncovered; say which half was found  prio:P2  status:todo  deps:T52  bounces:0
- [ ] T57 [senior-dev] NeedsInput discards the research/extraction already paid for  prio:P2  status:todo  deps:T52  bounces:0
- [ ] T37 [senior-dev] Hashtag cap ignored inline tags — 2 captions would fail at publish  prio:P0  status:test  deps:-  bounces:0
- [ ] T38 [senior-dev] Eval records the raw completion on failure  prio:P2  status:test  deps:T35  bounces:0
- [ ] T39 [senior-dev] Relevance gate matches whole tokens, negating prefixes excluded  prio:P1  status:test  deps:-  bounces:0
- [ ] T40 [senior-dev] CALL_DEADLINE_S — wall-clock cap on every model call  prio:P2  status:test  deps:-  bounces:0
- [ ] T2 [senior-dev] <hard task>  prio:P1  status:todo  deps:T1
- [ ] T3 [junior-dev] <easy task>  prio:P2  status:todo  deps:T1
- [ ] T4 [devops] <deploy/CI task>  prio:P2  status:todo  deps:T2
- [ ] T5 [reviewer] Review T2+T3 code + integration  prio:P1  status:todo  deps:T2,T3
- [ ] T6 [tester] Validate T2+T3 vs AC  prio:P1  status:todo  deps:T5
- [x] T62 [orchestrator] launchd ProcessType Background throttled the service to ~1% CPU  prio:P0  status:done  evidence:logs/orchestrator.md#T62  deps:-  bounces:0
- [x] T63 [orchestrator] Instagram access token written to logs in cleartext — redact at every handler  prio:P0  status:done  evidence:tests/test_logredact.py  deps:-  bounces:0
- [x] T64 [orchestrator] /logs 500 — rotation glob parsed the archive file's suffix as an int  prio:P1  status:done  evidence:tests/test_dashboard_logs.py  deps:-  bounces:0
- [x] T65 [orchestrator] Restore item 84 — live on Instagram but requeue had wiped its publish record  prio:P0  status:done  evidence:logs/orchestrator.md#T65  deps:-  bounces:0
- [x] T66 [orchestrator] publish_log survives requeue so a published item can't be silently re-approved  prio:P0  status:done  evidence:tests/test_publish_history.py  deps:-  bounces:0
- [x] T67 [orchestrator] Requeue via clickable stage timeline instead of a status dropdown  prio:P2  status:done  evidence:tests/test_dashboard_logs.py  deps:-  bounces:0

## Post quality — free models produce bad posts    size:L   (architect, 2026-09-22)

Phase 0 measure · Phase 1 pin · Phase 2 research harness · Phase 3 research bundle (one arm) · Phase 4 compose arms.
Acceptance criteria per task: `.claude/project-context.md` ## Post-quality programme.

- [x] T68 [senior-dev] Mechanical substance metrics + --rescore from saved decks  prio:P0  status:done  evidence:logs/tester.md#T68  deps:-  bounces:1
- [x] T92 [tester] Validate T68 vs AC — PASS, socket-guard proof + 5 hand recomputations  prio:P0  status:done  evidence:logs/tester.md#T68  deps:T68  bounces:0
- [x] T93 [senior-dev] filler_count binds FILLER_PHRASES at import — stale default vs composite crediting 1.0  prio:P1  status:done  evidence:logs/tester.md#T74  deps:T68  bounces:0
- [ ] T89 [tester] 5 async tests flake under full-suite load (test_render x3, test_main x2)  prio:P2  status:todo  deps:-  bounces:0
- [ ] T84 [reviewer] Review T68 + T69 — T69 PASS, T68 REJECT (3 blocking)  prio:P0  status:test  deps:T68,T69  bounces:0
- [x] T85 [devops] validate_for_run() never runs in eval/smoke/build_golden/dashboard — split out Settings.validate_model_config()  prio:P1  status:done  evidence:logs/tester.md#T85  deps:-  bounces:1
- [x] T121 [tester] Validate the devops trio — PASS, all 4 guard sites mutated independently  prio:P1  status:done  evidence:logs/tester.md#T85  deps:T85  bounces:0
- [x] T79 [devops] Pin ruff 0.16.8 + [tool.ruff] select; pre-commit gate; 15 pre-existing violations fixed  prio:P2  status:done  evidence:logs/tester.md#T85  deps:-  bounces:0
- [x] T91 [devops] uv sync --extra dev is mandatory — a missing dev dep collects ZERO tests  prio:P1  status:done  evidence:logs/tester.md#T85  deps:-  bounces:0
- [ ] T124 [devops] T91's pasted transcript is an intermediate state — a fresh uv sync has NO pytest; uv sync also prunes extras  prio:P3  status:todo  deps:T91  bounces:0
- [ ] T119 [architect] coding-standards.md:17 goes FALSE on T79 merge — it names unpinned `uvx ruff` which bypasses the new pin  prio:P1  status:todo  deps:T79  bounces:0
- [ ] T120 [devops] .env.example ships paid Gemini slugs; T85 widens the blast radius 1 -> 5 scripts that refuse to start  prio:P2  status:todo  deps:T85  bounces:0
- [ ] T115 [architect] README says 599 tests (actual 827); coding-standards still says T79 is pending  prio:P3  status:todo  deps:T79  bounces:0
- [x] T86 [tester] Validate T69 vs AC — PASS, plus live catalogue check of the openrouter/ namespace  prio:P0  status:done  evidence:logs/tester.md#T69  deps:T69  bounces:0
- [x] T69 [senior-dev] Pin OPENROUTER_MODEL_CHEAP; reject auto-router slugs  prio:P0  status:done  evidence:logs/tester.md#T69  deps:-  bounces:0
- [ ] T80 [senior-dev] A/B MODEL_GOOD vs dots-3-note-preview:free — incumbent is not broken, so measure  prio:P3  status:todo  deps:T68,T77  bounces:0
- [ ] T81 [senior-dev] parse_json accepts any valid JSON — validate the completion against the schema it sent  prio:P0  status:todo  deps:T78  bounces:0
- [ ] T82 [senior-dev] preflight: probe schema ENFORCEMENT per slug AND the Ollama local path; test oneOf + anyOf  prio:P1  status:todo  deps:T69  bounces:0
- [ ] T83 [architect] docs/setup.md recommends unverified slugs; .env.example still ships paid Gemini slugs  prio:P2  status:todo  deps:T69  bounces:0
- [x] T73 [senior-dev] Research-stage eval harness with frozen document cache — gates T71/T72  prio:P0  status:done  evidence:logs/tester.md#T73  deps:T68  bounces:2
- [x] T129 [tester] Validate T73 — PASS; found an HTTP-200 anti-bot page invisible to status+keyword screening  prio:P0  status:done  evidence:logs/tester.md#T73  deps:T73  bounces:0
- [ ] T130 [senior-dev] Corpus screening misses HTTP-200 JS challenge pages (Anubis) — status codes and keyword markers both blind  prio:P2  status:todo  deps:T73  bounces:0
- [ ] T123 [senior-dev] Grow the research golden set to 8-10 items; 3 cannot arbitrate T72 (fail-open effective n=2)  prio:P1  status:todo  deps:T73  bounces:0
- [x] T74 [senior-dev] Re-scope filler_count to non-closing slides; drop it from the composite (METRIC_SET_VERSION 3) so v3 == v1 arithmetic  prio:P1  status:done  evidence:logs/tester.md#T74  deps:T93  bounces:1
- [x] T128 [tester] Validate T74 + T93 + T127 — PASS, killer AC reproduced independently  prio:P1  status:done  evidence:logs/tester.md#T74  deps:T74  bounces:0
- [x] T127 [senior-dev] mutate.py clear_bytecode() never cleared scripts/__pycache__ — a stale .pyc failed 3 tests on an unmutated tree; now derives dirs from MUTATIONS  prio:P2  status:done  evidence:logs/tester.md#T74  deps:T74  bounces:0
- [x] T125 [senior-dev] MEASURE ONLY — grounding provenance; ALL THREE pre-registered predictions FAILED; content slides ground at 22-27%, pipeline CTA at 0%  prio:P1  status:done  evidence:scripts/measure_grounding_provenance.py  deps:-  bounces:0
- [ ] T126 [senior-dev] Regenerate the golden snapshots — they predate _restore_repo_facts' sub fallback (0/49 repo slides carry a sub), so D75's inflation is UNMEASURED  prio:P1  status:todo  deps:T125  bounces:0
- [ ] T77 [senior-dev] Re-baseline golden set; close the phantom 21% regression  prio:P1  status:todo  deps:T69,T80  bounces:0
- [x] T70 [senior-dev] Context budget packer — local fallback truncates 10k tokens into num_ctx 8192  prio:P1  status:done  evidence:logs/tester.md#T70  deps:T69  bounces:1
- [x] T94 [tester] Validate T70 vs AC; added the share-utilisation assertion  prio:P1  status:done  evidence:logs/tester.md#T70  deps:T70  bounces:0
- [ ] T97 [senior-dev] config.py has no floor validating num_ctx_<role> > RESERVED_TOKENS; starvation logs at INFO  prio:P2  status:todo  deps:T70  bounces:0
- [ ] T98 [senior-dev] head/tail cut bisects fenced artifacts mid-fence, against RESEARCH_SYSTEM's verbatim-carry rule  prio:P2  status:todo  deps:T70  bounces:0
- [ ] T87 [architect] llm.py mid-call fallback resends the OpenRouter-sized prompt; also extract the _num_ctx/_NUM_CTX_FIELD mapping and surface prompt_eval_count  prio:P1  status:todo  deps:T70  bounces:0
- [ ] T95 [senior-dev] CHARS_PER_TOKEN is unmeasured — derive it from Ollama's prompt_eval_count  prio:P2  status:todo  deps:T87  bounces:0
- [ ] T90 [senior-dev] T61's other two instances: triage.py:109 and research.py:239 feed uncapped text to cheap at num_ctx 8192  prio:P1  status:todo  deps:T70  bounces:0
(T91 duplicate — see the devops trio line above, done)
- [ ] T71 [senior-dev] Route the research note to good; role routing in config  prio:P1  status:todo  deps:T70  bounces:0
- [ ] T72 [senior-dev] Relevance gate: wider excerpt + fail-open on total rejection  prio:P1  status:todo  deps:T71  bounces:0
- [x] T78 [senior-dev] Per-type slide schema — SLIDES_SCHEMA permits the broken slide it then discards  prio:P0  status:done  evidence:logs/tester.md#T78  deps:-  bounces:2
- [x] T99 [tester] Validate T78 vs AC — PASS, B4 verified end-to-end at the render layer  prio:P0  status:done  evidence:logs/tester.md#T78  deps:T78  bounces:0
- [x] T96 [senior-dev] MODEL_GOOD sometimes puts the whole answer in message.reasoning; llm.py reads only content — production frequency UNMEASURED (Arm A only)  prio:P1  status:done  evidence:logs/tester.md#T96  deps:-  bounces:1
- [x] T122 [tester] Validate T96 + T116 vs AC — PASS, 19 raise sites byte-identical  prio:P1  status:done  evidence:logs/tester.md#T96  deps:T96  bounces:0
- [ ] T117 [senior-dev] A refusal-only completion burns 3 attempts + a local call; classify it, never consume it — gated on live evidence  prio:P3  status:todo  deps:T96  bounces:0
- [ ] T118 [senior-dev] reasoning carrying CoT-then-JSON fails parse_json; extraction heuristic must land AFTER T81 or it widens silent wrong-shape flow  prio:P2  status:todo  deps:T81  bounces:0
- [x] T116 [senior-dev] _ollama had NO empty-completion guard — silent "" for a no-schema call  prio:P2  status:done  evidence:logs/tester.md#T96  deps:T96  bounces:0
- [ ] T100 [senior-dev] eval_compose BODY_FIELDS is a third hand-maintained body contract — scores the B4 slide as non-empty  prio:P1  status:todo  deps:T78  bounces:0

### Headline promises a count the deck does not deliver  (user report, P1, size M)
- [x] T101 [architect] Design headline/body-count reconciliation + delivered-count redesign (D34-D52)  prio:P1  status:done  evidence:project-context.md  deps:-  bounces:0
- [x] T105 [senior-dev] compose() never passes item.research — list decks copy owner/stars/language "VERBATIM" from notes never in the prompt, so the model invents them  prio:P0  status:done  evidence:logs/tester.md#T105  deps:-  bounces:1
- [x] T110 [tester] Validate T105 vs AC — PASS, live-verified 2 reps; found the absent-fact residual  prio:P0  status:done  evidence:logs/tester.md#T105  deps:T105  bounces:0
- [x] T111 [senior-dev] Absent facts are still invented: _restore_repo_facts' truthy `if note.get(field)` cannot strip a fabricated value nor write a real stars=0  prio:P0  status:done  evidence:logs/tester.md#T111  deps:T105  bounces:0
- [x] T112 [tester] Validate T111 vs AC — PASS, live-verified; scoped T114 to stars=False (reachable) not None (not)  prio:P0  status:done  evidence:logs/tester.md#T111  deps:T111  bounces:0
- [ ] T113 [senior-dev] Falsy-vs-presence bug class, 3 more instances: stat value 0 drops the stat; a null KPI tile renders "None"; isinstance(stars,int) admits bools  prio:P1  status:todo  deps:T111  bounces:0
- [ ] T114 [senior-dev] stars type is unvalidated at the boundary — enumerate_items._note passes SearXNG popularity through untyped, so None/False now render as "star None"  prio:P1  status:todo  deps:T111  bounces:0
- [ ] T108 [senior-dev] synthesize.py budgets nothing — full note detail (~64k chars measured) into llm.good with no pack, no cap, no trace  prio:P1  status:todo  deps:-  bounces:0
- [ ] T109 [senior-dev] RESERVED_TOKENS=2048 was sized for research; compose's own scaffold is 6737 tokens — needs a per-stage reserve  prio:P1  status:todo  deps:T108  bounces:0
- [ ] T102 [senior-dev] Measured per-(type,field) body caps + deterministic headline reconciler; capacity measured as a surface, script-only evidence (D44/D51)  prio:P1  status:todo  deps:T101  bounces:0
- [ ] T103 [senior-dev] Deliver the requested count: one list_deck_plan, densify past rich capacity, MAX_ITEMS becomes editorial (D43/D45/D46)  prio:P1  status:todo  deps:T102,T105  bounces:0
- [ ] T104 [junior-dev] eval_compose score_deck: headline_count_mismatch via integrity.py's detector; report raw, do NOT touch COMPOSITE_WEIGHTS  prio:P1  status:todo  deps:T100,T102  bounces:0
- [ ] T106 [senior-dev] PARKED — split an over-ceiling list across a 1/2, 2/2 post series (D43 mechanism 1); L-sized, touches publish/approval/db  prio:P3  status:todo  deps:T103  bounces:0
- [ ] T107 [senior-dev] A web enumeration is composed as "repo" cards it has no owner/stars/language for  prio:P3  status:todo  deps:T105  bounces:0
- [x] T88 [devops] Add jsonschema>=4.23 as a dev dep  prio:P1  status:done  evidence:pyproject.toml  deps:T78  bounces:0
- [ ] T75 [senior-dev] Best-of-N compose selected by substance score  prio:P1  status:todo  deps:T68,T69  bounces:0
- [ ] T76 [junior-dev] Compose temperature to one constant + A/B the value  prio:P2  status:todo  deps:T75  bounces:0
(T79 duplicate — see the devops trio line above, done)
