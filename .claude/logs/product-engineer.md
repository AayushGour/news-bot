- 2026-10-21 [T219] host spike on real 24GB host: qwen3.8:27b vision is functionally multi-image-capable (2 images, distinct correct answers) but measured at 181-450s per call (single tiny image: 181/271/372s warm; 2 images: 398.9s; 3 images: failed at 400s timeout; 6 images @512px: failed at 450s timeout) and routinely evicted mid-request by the live production worker's cheap/good calls on this memory-tight host (PhysMem 23G/24G used, near-zero free, even with ONLY the 27B loaded) — DESIGN-CHANGING: D198's "one call over all 6 candidates" is not completable in acceptable time on this host; recommend capping IMAGE_CANDIDATES to 2 and/or revisiting the model choice, or deferring T223's build pending a host/model change. Verifier (4B, num_ctx_good=16384) measured fine: p90 context 5839 chars = 1383 prompt tokens (huge headroom vs 16384), 3 reps 79.3s/175.3s/176.9s wall-clock including one 121.65s cold-reload from eviction; MAX_VERIFY_PASSES=4 is generously provisional-safe since even p90/max context fits in ONE pass by token math. Prevalence 59/100 recent items have a researched background. Human-judgment pass on 10 real items: ~6/10 of production's actual historical "first downloadable" picks are clear topic mismatches, confirming the user's complaint with evidence. IMAGE_MATCH_MIN: only 1 of 10 ranking calls completed before killing the loop (2-image call, scored 9 & 10, both correct) — false-accept/reject table at 5/6/7/8 KEPT PROVISIONAL because each ranking call costs 400-450s+ on this host (getting a real table costs hours, and the host-speed finding makes that low-value right now); recommend keeping provisional 7 pending a faster host/model. 27B explicitly unloaded (keep_alive:0) and confirmed via `ollama ps` before returning; no stray t219 processes left running. → architect, with host-viability as the primary blocker ahead of threshold-tuning.
- 2026-10-21 [T230-interim] (written after the orchestrator restart wiped scratchpad t230+t219; figures below are from my session transcript, host 2026-10-21 under heavy contention: swap 8.3-8.8G/9.2G used, a ~8.3G unrelated VM resident, PhysMem 23G/24G, load avg 3-31, live worker active). qwen3-vl:8b Q4_K_M, Ollama 0.34.4, 100% GPU, `ollama ps` SIZE 6.8G@ctx8192 / 8.1G@ctx16384. Cold load 4.3-5.8s. prompt_eval_count ~1090-1130 per 512px image (1131@1img, 2244@2, 4422@4, 6552@6 describe-prompt; ranking prompt 2371@2, 4535@4). Multi-image in one message WORKS and is distinct+correct at 2/4/6 (cat, car, mountain, Tao-stock-photo, phone/ChatGPT page, Musk portrait all described correctly, Musk named); describe-prompt elapsed 36.0s/54.7s/51.9s (n=2/4/6, warm, calm host). Ranking prompt (T218 schema, `think:false`): n=2 cold 80.9s (load 4.3 + prefill 36.6 @64.7 tok/s + decode 39.9s/451 tok @11.3 tok/s), n=2 repeat 43.1s/44.9s (identical prompt -> KV prefix-cache hit, prefill 0.06s: NOT representative, production prompts never repeat), n=4 repeat 79.3s (cache hit). FRESH-content ranking calls: 5/5 TIMED OUT at 240s (items 388 n=2, 387 n=6, 385 n=3, 383 n=6, 382 n=6) even with host calm and decode back at 9.5 tok/s; earlier fresh 388 n=2 timed out at 500s and 400s under heavy contention (decode 2.4 tok/s, 4B+2B resident, 8B evicted by the live worker's 4B call mid-run). DESIGN-CHANGING: `think:false` (and omitted, and "/no_think" suffix) does NOT suppress qwen3-vl:8b's chain-of-thought on Ollama 0.34.4: all three returned a non-empty message.thinking (~550 chars even for 1 image/1 candidate); `ollama show` says thinking default true, levels false,true. Thinking tokens scale with candidate count (item 383 n=6 was at n_gen=534 and climbing at 9.5 tok/s when the 240s timeout cut it). Prefill under swap collapsed 64 -> 18 tok/s. Observed 8B evicted by live 4B load; 8B+2B co-resident (10.2G) was observed and stable for minutes; 4B+2B co-resident (7.5G) as well. Production's own vision call to the 4B fallback failed: "image input is not supported ... provide the mmproj" (existing, unrelated). Ranking-quality table NOT obtained (0/10 completions). Resuming on 2026-10-31 with a rebuilt scratchpad.
- 2026-10-31 [T230] qwen3-vl:8b host spike, final. Header: Ollama 0.34.4, Q4_K_M, 100% GPU, 512px edge, JPEG q85, T218 schema via `format`, num_ctx 16384 (`ollama ps` 8.1G; 6.9G@8192), swap 5.2-7.3G of 6-7G, PhysMem 23G/24G, live worker BUSY on the local 4B (44 requeued items) the whole run, so every number is under real contention. Scratchpad was wiped by the restart; rebuilt (10 items, 6 fresh candidates each, so candidate sets differ from T219).
  THINK: `think:false`, omitted, `/no_think` all still produce chain-of-thought (message.thinking non-empty). 2 images, no `format`: false 334.6s/3877 tok/15388 think-chars; with `format` schema: false 64.5s/667 tok/2048 chars, omitted 52.2s/646 tok/2411 chars. `format` cuts runaway thinking ~6x; production's schema path is the right shape, but thinking is unavoidable (1468-1994 gen tokens at n=6).
  LATENCY n=6 @512 fresh content (prompt_eval_count 6551-6651 = ~1090/img): item 388 304.7s (load 75.7, prefill 91.1, decode 137.8/1468 tok) | 385 309.6s (load 31.3, prefill 95.2, decode 182.9/1652) | 382 441.0s (load 90.1, prefill 113.8, decode 236.9/1994) | 387 TIMEOUT 590s | 383 TIMEOUT 590s. 3/5 completed, median 310s. Earlier (2026-10-21, calmer, same host): n=2 cold 80.9s (load 4.3), n=2/n=4 identical-prompt repeats 43-79s are KV-cache hits and NOT representative; 6 distinct describe-images 51.9s/6552 tok. 3 reps NOT achieved at n=2/4/6 or 768: host never gave a window; cannot be bought without stopping production (not allowed).
  MEMORY/CO-RESIDENCY (poll.log, 211 samples/15s): 8B and 4B resident together in 0/211 samples; 8B alone 128, 4B alone 74, none 9. Scheduler log: "model predicted to exceed available memory, evicting" (5.8G needed, 5.5G free) then "waiting for pending requests to complete" - the 8B request queues behind the 4B's 2m41s/3m42s compose calls, each swap costs load 31-90s. OLLAMA_MAX_LOADED_MODELS unset: Ollama already behaves as max=1 here.
  RANKING (3 completed items of 10 attempted-5): item | production pick | model per-candidate scores | human | verdict: 388 | Kaiserschmarrn dessert (mismatch) | 0,0,0,0,0,0 (all candidates junk: screwdrivers, horses; retry_query "Terence Tao AGI race safety concerns") | all junk, correct | no false accept/reject. 385 | businesswoman (mismatch) | 0,0,0,0,0,0 for six AI-toy-robot photos ("no data breach shown") | human 6-7 acceptable on-topic stock | 6 FALSE REJECTS at any threshold. 382 | modern office building (mismatch) | 2,10,0,2,3,2 (10 = AI illustration, correct) | human agrees, #2 only | correct. Threshold 5/6/7/8 give IDENTICAL outcomes on this sample (scores cluster 0-3 and 10; nothing in 4-9) -> sample cannot choose a threshold; model is bimodal and literal-minded (scores "does it show the news event", not "is it a fitting hook"). Recommend IMAGE_MATCH_MIN stays 5-7 only after the prompt asks for topic/domain fit with an explicit mid-scale anchor; re-measure on >=10 items. Schema followed 3/3, descriptions distinct and accurate, indices matched, Musk portrait named correctly.
  UNLOAD: 8B stopped, `ollama ps` shows only the worker's 4B; poll.sh killed; pgrep for scratchpad/t230 processes: nothing left.

- 2026-11-01 [T248] News content-budget calibration (script: scripts/measure_material_budget.py <db-copy>; 181 news decks id>250 with slides, 180 channel + 1 dm; read-only copy, 0 model calls). RECOMMEND: formula A (distinct non-fail-open notes with non-empty normalised claim), low-confidence notes IN, budget=clamp(max(units, clauses-gaps),1,6); NO raw_text +1 (so no RAW_SUBSTANCE_CHARS constant; if B is chosen anyway use 800 = raw p50 and P2=7). PRE-REGISTERED P2 = median total slides 6 for news decks with <=2 notes (4 fixed + budget median 2; n=32, p25=p75=2, so stable; pass band 5-6, fail if >=7). Hand spot-check below. Raw output follows (drop list trimmed to 10).
decks: 181 (id>250, intent=news); by source: {'channel': 180, 'dm': 1}

== raw_text length (chars): n=181 min=53 p25=535 med=785 p75=953 max=2266 mean=776.43
   share >= 300: 88%
   share >= 400: 83%
   share >= 500: 80%
   share >= 600: 70%
   share >= 800: 48%
   share >= 1000: 22%

== 1. unit distributions (budget = clamp(max(units, clauses-gaps),1,6))
   current total slides  : n=181 min=5 p25=8 med=8 p75=9 max=10 mean=8.44
   current CONTENT slides: n=181 min=1 p25=3 med=4 p75=5 max=7 mean=4.04  (excl ['follow', 'hook', 'links', 'photo', 'sources', 'takeaway'])
   A  notes, low in      : n=181 min=1 p25=3 med=4 p75=5 max=5 mean=3.80
   A  notes, low out     : n=181 min=0 p25=2 med=3 p75=4 max=5 mean=2.87
   B  notes+raw, low in  : n=181 min=2 p25=3 med=5 p75=6 max=6 mean=4.50
   B  notes+raw, low out : n=181 min=0 p25=2 med=4 p75=5 max=6 mean=3.57
   clauses-gaps          : n=181 min=0 p25=0 med=0 p75=0 max=1 mean=0.01

   budget (clamped) and resulting new total = 4 + budget; vs content slides now:
   A  notes, low in      : n=181 min=1 p25=3 med=4 p75=5 max=5 mean=3.80 | decks whose current content > budget: 74/181 | mean slides cut 0.66
   A  notes, low out     : n=181 min=1 p25=2 med=3 p75=4 max=5 mean=2.92 | decks whose current content > budget: 118/181 | mean slides cut 1.31
   B  notes+raw, low in  : n=181 min=2 p25=3 med=5 p75=6 max=6 mean=4.50 | decks whose current content > budget: 48/181 | mean slides cut 0.38
   B  notes+raw, low out : n=181 min=1 p25=2 med=4 p75=5 max=6 mean=3.59 | decks whose current content > budget: 90/181 | mean slides cut 0.91

   units-bucket table (A low-in / B low-in): n, median total now, median content now
   A  notes, low in
     units=1  n=  1 total_med=7 content_med=2
     units=2  n= 37 total_med=8 content_med=4
     units=3  n= 29 total_med=8 content_med=3
     units=4  n= 44 total_med=9.0 content_med=4.0
     units=5  n= 70 total_med=9.0 content_med=4.0
   B  notes+raw, low in
     units=2  n= 15 total_med=7 content_med=3
     units=3  n= 32 total_med=8.0 content_med=3.5
     units=4  n= 35 total_med=8 content_med=4
     units=5  n= 46 total_med=9.0 content_med=4.0
     units=6  n= 53 total_med=9 content_med=4

   P2 population (news decks with <=2 notes):
     n=32  current total median=8.0
     A  notes, low in      : budget med=2.0  -> total med = 6.0  (dist n=32 min=1 p25=2 med=2.0 p75=2 max=2 mean=1.97)
     A  notes, low out     : budget med=1.0  -> total med = 5.0  (dist n=32 min=1 p25=1 med=1.0 p75=2 max=2 mean=1.31)
     B  notes+raw, low in  : budget med=3.0  -> total med = 7.0  (dist n=32 min=2 p25=2 med=3.0 p75=3 max=3 mean=2.62)
     B  notes+raw, low out : budget med=2.0  -> total med = 6.0  (dist n=32 min=1 p25=1 med=2.0 p75=2 max=3 mean=1.84)

== 2. content-slide traceability (script-local, NOT quality.py)
   traced (>= 60% of numbers+proper nouns, else words, in notes+raw): 571/731 = 78%
     chart    8/14 = 57%
     code     20/30 = 67%
     compare  47/67 = 70%
     facts    81/104 = 78%
     flow     64/93 = 69%
     kpi      39/46 = 85%
     point    220/271 = 81%
     quote    92/106 = 87%
   untraced content slides per deck: n=181 min=0 p25=0 med=1 p75=2 max=3 mean=0.88; decks with >=1: 98/181

== 3. evidence gate (D219) vs existing rich slides
   chart  slides= 14 dropped=  7 (50%)
   code   slides= 30 dropped= 27 (90%)
   kpi    slides= 46 dropped= 13 (28%)
   quote  slides=106 dropped= 70 (66%)
   (quote drops with fuzzy coverage >= 0.9: 14/70 - candidates for a tolerant rule)
   TOTAL  slides=196 dropped=117

   -- 10 drops hand-checked (from the script spot-check list; material read by grep) --
   264 kpi  ~60 days / 14-day window: absent from material -> CORRECT drop (invented)
   279 quote "Xi will take every data center..." : material says "He will take..." -> CORRECT (altered quote)
   354 code  C seed snippet: material has no code -> CORRECT (invented)
   358 chart 25/35/40% by 6/9/12 months: material only "six to 12 months" -> CORRECT (invented series)
   290 kpi  tile "2 prior incidents": absent (1 homicide tip, 20 visas present) -> CORRECT
   303 quote Mostaque: material quote reads "doesn't does need your labor" (upstream typo) -> FALSE drop (verbatim modulo typo; fuzzy cov 1.00)
   378 code  ascii architecture diagram: not in material -> CORRECT
   359 chart hours by department 424..: derived by model from token counts -> CORRECT (fabricated/derived)
   309 kpi  "5 years operating": absent -> CORRECT
   321 quote board statement: material paraphrases ("beauty of the land and its preservation") -> CORRECT (paraphrase presented as quote)
   Extra: 382 code JSON built from prose ($100M/GW, 5GW) -> CORRECT drop (structure invented; numbers real). 348 kpi "3 months/2 years" was a FALSE drop until number-WORDS ("three months") were added to the normaliser -> fixed (kpi 17->13 drops).
   Result: 1/10 false drop in the sample (10%); in the 70 quote drops a 12-sample showed most are model sentences phrased as quotes (correct drops); 14/70 have >=0.9 fuzzy coverage (near-verbatim: dropped "Because/The" lead-ins, quote spliced from 2 sentences) = candidates for a tolerant rule. Upper-bound false-drop rate = 15/196 rich slides (7.7%) = 15/117 drops (13%); lower bound (strict hand sample) ~10% of drops. Gate drops 117/196 existing rich slides (60%): mostly fabricated structure, which is the point of D219 - but expect rich-slide count to collapse (code 90%, quote 66%, chart 50%, kpi 28%).
   NORMALISER (as implemented): lowercase; strip commas inside digits; "$" ignored; match raw number AND scaled value for k/thousand, m/mn/million, b/bn/billion, t/trillion; "%"/"percent"/"x" keep the raw number; number-words zero..twenty,30,40,50,60,hundred -> digits; compare as float sets. Text: lowercase, punctuation->space, curly->straight apostrophes, collapse whitespace. Quote rule: split on "...", ellipsis char and sentence ends, EACH segment must be a normalised substring; recommended tolerant fallback = >=0.9 of quote tokens in >=3-token runs of the material (recovers 14/70 quote drops; would also pass 279 at 0.89? no: 0.89 < 0.9, stays dropped). Code rule: fenced ``` in material OR >=50% of code lines (>=8 chars) verbatim. Chart: >=2 series values (value or display) in material numbers. KPI: every tile value in material numbers (or normalised substring if non-numeric).
== 4. decks missing hook / takeaway / sources / follow
   channel  n=180: no-hook=0/180 (0%)  no-takeaway=38/180 (21%)  no-sources=0/180 (0%)  no-follow=0/180 (0%)
   dm       n=1: no-hook=0/1 (0%)  no-takeaway=0/1 (0%)  no-sources=1/1 (100%)  no-follow=0/1 (0%)
   decks at 10 slides: 43 / 181
- 2026-11-01 [T237] nvidia/nemotron-3.5-super-vl:free as hosted image gate: ENFORCES schema with images 6/6, ranks well with prompt B; recommend prompt B + IMAGE_MATCH_MIN 6 + IMAGE_CANDIDATES 6. Header: model nvidia/nemotron-3.5-super-vl:free via real LLMClient.vision (OPENROUTER_MODEL_VISION + FALLBACK_TO_LOCAL=false inline; printed effective slug = nvidia/nemotron-3.5-super-vl:free, provider openrouter, fallback False), 512px-edge JPEG q85, T218 schema (relevance 0-10, depicts<=160, issues enum x7, retry_query<=80, additionalProperties false), temp 0.2, 10 items (T230 candidate set), 3 reps each, 2026-11-01, scratchpad t237 (h.py, an.py, rank.jsonl). Human labels = MY judgement from contact sheets (F fit / B bad / ? borderline, excluded from FA/FR): 363 FFFF?F | 364 ?B?FBF | 367 ??B??? | 368 BB?BBF | 369 ??FFF? | 382 ?FB?F? | 383 FFFF?F | 385 FFFFFF | 387 F?FFFF | 388 BBBBBB. T219/T230 per-item judgements were not retrievable per candidate (only prod-pick summaries), so these are fresh and subjective.
  ENFORCEMENT (1 image, item 385): banana-enum x3 -> exactly {"answer":"banana"} 3/3 (6.0/3.3/14.4s); T218 schema x3 -> jsonschema-valid 3/3 (2.8/3.7/3.5s, relevance 8, correct depicts). 120/120 ranking calls also jsonschema-valid. VERDICT: schema IS enforced with images attached (unlike gemma-4-31b).
  QUOTA: 126 OpenRouter calls total (6 enforce + 120 ranking); 126 HTTP 200, 0x 429, 0 errors, 0 client retries (paced 3s between calls; ~20 RPM not approached). Items/day in data/app.db copy: 5-15 (median ~11) -> gate at 59% = ~6.5 calls/day, ~8 with one retry each: <1% of D8's 1000/day; even 50 items/day = ~30-40 calls.
  LATENCY (30 calls per cell, 10 items x 3 reps): n=6 prompt A median 9.9s mean 13.2 max 51.7 | n=6 B median 11.2 mean 15.7 max 109.3 | n=3 A median 7.0 mean 8.1 max 27.3 | n=3 B median 7.1 mean 9.2 max 37.0. Tail is the risk (109s once): keep CALL_DEADLINE well above, fail-closed D199 on timeout.
  FA/FR pooled over 3 reps (FA = score>=T on human B; FR = score<T on human F; n=6: 42 B, 87 F cells; n=3: 24 B, 39 F):
  | cfg | T=5 FA/FR | T=6 | T=7 | T=8 |
  | n6 A | 2/42, 25/87 | 2/42, 34/87 | 0/42, 44/87 | 0/42, 56/87 |
  | n6 B | 3/42, 3/87 | 3/42, 5/87 | 2/42, 9/87 | 0/42, 20/87 |
  | n3 A | 2/24, 13/39 | 2/24, 16/39 | 2/24, 17/39 | 0/24, 23/39 |
  | n3 B | 3/24, 0/39 | 3/24, 0/39 | 2/24, 1/39 | 0/24, 6/39 |
  Item-level (n6 B, 30 item-reps): T5 18 picked-F 9 picked-? 3 no-pick(all-bad items 388) | T6 same | T7 18 F, 8 ?, 1 wrongly-no-pick | T8 18 F, 6 ?, 3 wrongly-no-pick. n6 A T7: 18 F, 4 ?, 5 wrongly-no-pick.
  Score hist: A 0-3 heavy (56 zeros) with spread; B mass at 7-9 (80 eights), 0-1 for junk. Prompt A is unstable on generic items: item 363 (TikTok) scores 9,9,9 in rep0 then 2,2,2 in reps1-2 (same input); A argmax unstable across reps on 3/10 items, B on 2/10 (364, 369 = real ties among mediocre candidates).
  PER-ITEM n=6 prompt B (picks over 3 reps | median latency | production's historical pick | verdict): 388 none usable (all 0-1; retry_query "Terence Tao artificial general intelligence safety discussion") 5.7s | Kaiserschmarrn dessert (mismatch) | gate correctly rejects all 6, retry works. 387 #1 Terence Tao Wikipedia photo (10,10,10; named correctly) 10.7s | male teacher shirt | model finds the real Tao. 385 #1 (8,8,8; all 6 toy-robot photos 6-8) 10.0s | frustrated businesswoman (mismatch) | FIXES T230's 6 false rejects. 383 #1/#2 (9) 12.7s | "1515 Third Street" (mismatch) | good. 382 #2 (LLM illustration, 8-9) 11.8s | modern office building (mismatch) | good. 369 #2 (stock app 7-8; ties with Musk scrabble/SpaceX 7-8) 11.9s | Musk Terafab (fit) | ok, tie. 368 #6 (Jack Clark on stage with Claude logo, 9-10) 9.5s | man speaks on conference | right pick, others 0-3. 367 #1 portrait scored 10 -- see hazard. 364 picks 3,1,3 (barbed-wire AI heads 7-8 / men facing 5-7) 15.6s | SF Chronicle building (mismatch) | ties among mediocre, acceptable. 363 #1 (8, all TikTok 5-8) 8.7s | boy with lit match (mismatch) | good.
  DESIGN-RELEVANT FINDINGS: (1) Indices map correctly (spot-checked 385/364/367/369: `depicts` matches the title of the indexed image in all). (2) At n=3, 10/60 calls (17%) returned PHANTOM ratings for indices beyond n (4 or 1..10); 0/60 at n=6. Gate code must drop out-of-range indices and treat missing 1..n as bad completion. (3) HALLUCINATED IDENTITY: item 367 (Jack Clark quote) the model scored two generic stock portraits of a smiling man 10/10 with depicts "Jack Clark" (it adopts the article's name; the real Jack Clark photo wasn't among candidates) -> a person-story false accept the FA table does not count (I labelled them ?). Mitigation to test in T223: tell the model to name a person only if a caption/visible text shows it, else "unidentified man", and rate unidentified generic portraits <=6. (4) Prompt A is not fixable by threshold: its FR is 29-64% at n=6. (5) retry_query empty when a candidate fit, populated for item 388 only.
  RECOMMENDATION: prompt B (hook/topic-domain fit + 9-10/7-8/5-6/3-4/0-2 anchors), IMAGE_MATCH_MIN 6, IMAGE_CANDIDATES 6 (n=6 FR 5/87 vs n=3 0/39 are equal within noise; n=6 gives more chance of a good image, had no phantom indices, +~4s). T=7 buys 1 fewer FA for 4 more FR; T=8 is too strict (20/87 FR). Threshold stays provisional on 10 items/1 labeller; add the unidentified-person rule and re-check item 367.
  Left running: nothing (pgrep h.py empty). No production code, .env, worker, Ollama touched. DB copy only (not otherwise used).
- 2026-11-07 [T293] planner spike: local 4B flat plan fails bar as sketched (A 82.6/74.4, B 85.5/79.1) but GO conditional with design C/D + grounding validator (98.7 valid, 93.3 target, edit_slide 27/30 no collateral); report .claude/research/2026-11-07-t293-planner-spike.md -> architect
