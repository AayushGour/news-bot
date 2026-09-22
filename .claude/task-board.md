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
- [ ] T93 [senior-dev] filler_count binds FILLER_PHRASES at import — stale default vs composite crediting 1.0  prio:P1  status:todo  deps:T68  bounces:0
- [ ] T89 [tester] 5 async tests flake under full-suite load (test_render x3, test_main x2)  prio:P2  status:todo  deps:-  bounces:0
- [ ] T84 [reviewer] Review T68 + T69 — T69 PASS, T68 REJECT (3 blocking)  prio:P0  status:test  deps:T68,T69  bounces:0
- [ ] T85 [senior-dev] validate_for_run() never runs in eval/smoke/build_golden/dashboard  prio:P1  status:todo  deps:-  bounces:0
- [x] T86 [tester] Validate T69 vs AC — PASS, plus live catalogue check of the openrouter/ namespace  prio:P0  status:done  evidence:logs/tester.md#T69  deps:T69  bounces:0
- [x] T69 [senior-dev] Pin OPENROUTER_MODEL_CHEAP; reject auto-router slugs  prio:P0  status:done  evidence:logs/tester.md#T69  deps:-  bounces:0
- [ ] T80 [senior-dev] A/B MODEL_GOOD vs dots-3-note-preview:free — incumbent is not broken, so measure  prio:P3  status:todo  deps:T68,T77  bounces:0
- [ ] T81 [senior-dev] parse_json accepts any valid JSON — validate the completion against the schema it sent  prio:P0  status:todo  deps:T78  bounces:0
- [ ] T82 [senior-dev] preflight: probe schema ENFORCEMENT per slug AND the Ollama local path; test oneOf + anyOf  prio:P1  status:todo  deps:T69  bounces:0
- [ ] T83 [architect] docs/setup.md recommends unverified slugs; .env.example still ships paid Gemini slugs  prio:P2  status:todo  deps:T69  bounces:0
- [ ] T73 [senior-dev] Research-stage eval harness with frozen document cache  prio:P0  status:todo  deps:T68  bounces:0
- [ ] T74 [junior-dev] Filler-phrase blocklist harvested from real decks  prio:P2  status:todo  deps:T68  bounces:0
- [ ] T77 [senior-dev] Re-baseline golden set; close the phantom 21% regression  prio:P1  status:todo  deps:T69,T80  bounces:0
- [ ] T70 [senior-dev] Context budget packer — local fallback truncates 10k tokens into num_ctx 8192  prio:P1  status:review  deps:T69  bounces:1
- [ ] T87 [architect] llm.py resends the OpenRouter-sized prompt to local on mid-call fallback — changes the shared LLMClient contract  prio:P1  status:todo  deps:T70  bounces:0
- [ ] T90 [senior-dev] T61's other two instances: triage.py:109 and research.py:239 feed uncapped text to cheap at num_ctx 8192  prio:P1  status:todo  deps:T70  bounces:0
- [ ] T91 [devops] uv sync --extra dev required after T70 — a missing hypothesis is a collection error, 0 of 723 tests run  prio:P1  status:todo  deps:T70  bounces:0
- [ ] T71 [senior-dev] Route the research note to good; role routing in config  prio:P1  status:todo  deps:T70  bounces:0
- [ ] T72 [senior-dev] Relevance gate: wider excerpt + fail-open on total rejection  prio:P1  status:todo  deps:T71  bounces:0
- [ ] T78 [senior-dev] Per-type slide schema — SLIDES_SCHEMA permits the broken slide it then discards  prio:P0  status:review  deps:-  bounces:1
- [x] T88 [devops] Add jsonschema>=4.23 as a dev dep  prio:P1  status:done  evidence:pyproject.toml  deps:-  bounces:0
- [ ] T75 [senior-dev] Best-of-N compose selected by substance score  prio:P1  status:todo  deps:T68,T69  bounces:0
- [ ] T76 [junior-dev] Compose temperature to one constant + A/B the value  prio:P2  status:todo  deps:T75  bounces:0
- [ ] T79 [devops] Wire ruff + gate it in pre-commit  prio:P2  status:todo  deps:-  bounces:0
