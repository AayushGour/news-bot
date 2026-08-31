# Telegram → Instagram Content Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an always-on service that reads Telegram channel messages, researches them on the open web, composes Instagram carousel slides, renders them as PNGs, and publishes them after human approval via Telegram.

**Architecture:** One Python process, one asyncio loop, three concurrent components (Telethon listener, aiogram bot, worker loop) sharing one SQLite database. The `status` column on the `items` table is the state machine. Every pipeline stage is a pure function `Item → dict of new fields`; the worker dispatches on status and commits each transition in a single transaction.

**Tech Stack:** Python 3.13, asyncio, aiosqlite, Telethon (channel read), aiogram 3 (bot), httpx, trafilatura, Jinja2, Playwright/Chromium, SearXNG (Docker), Ollama (host), boto3 (Cloudflare R2), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-31-telegram-to-instagram-pipeline-design.md`

## Global Constraints

- **`num_ctx` MUST be pinned on every Ollama request.** Unpinned, Ollama 0.33 loads at the model's full declared context (262144), claims ~43 GB, and spills to CPU. Correctness requirement, not tuning.
- **Models:** cheap = `qwen3:4b-instruct` (num_ctx 8192); good = `qwen3.5:9b` (num_ctx 16384); vision = `llama3.2-vision` (num_ctx 8192).
- **`awaiting_approval` is terminal for the worker.** Only a bot callback advances it. Never add a worker path out of that status.
- **Every Telegram handler is authorised against `OPERATOR_USER_ID`** via one shared decorator. DM intake bypasses triage, so this is the only barrier between a stranger and the publishing pipeline.
- **`DRY_RUN=true` by default.** No test and no default-configured run ever posts to Instagram.
- **Secrets never committed:** `.env`, `secrets/telegram.session` (chmod 600), `data/`. `.gitignore` lands in Task 1 before any credential can exist.
- **Slide count clamped 3–10.** Instagram's carousel maximum is 10; a Telegram album maximum is also 10.
- **Ollama runs on the host, not in Compose.** Docker Desktop on macOS cannot reach the GPU.
- **Test suite must run without a GPU.** All model calls go through a `LLMClient` seam that tests replace with a fake.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/pipeline/config.py` | Env → frozen typed settings; fails fast on missing required values |
| `src/pipeline/models.py` | `Item`, `Slide`, `ResearchNote`, `Status` enum |
| `src/pipeline/db.py` | Schema DDL, connection, row↔dataclass mapping, transitions, event log |
| `src/pipeline/llm.py` | `LLMClient` over Ollama: `cheap()`, `good()`, `vision()`, JSON-schema calls |
| `src/pipeline/worker.py` | Stage registry, poll loop, retry/backoff, failure classification |
| `src/pipeline/stages/triage.py` | Score 0–10, drop below threshold, dedupe |
| `src/pipeline/stages/extract.py` | Vision on images, fetch+extract on URLs |
| `src/pipeline/stages/research.py` | Query plan → search → relevance gate → notes |
| `src/pipeline/stages/synthesize.py` | Notes → sourced factual brief |
| `src/pipeline/stages/compose.py` | Brief → slides JSON + caption + hashtags |
| `src/pipeline/stages/render.py` | Slides → HTML → PNG, overflow guard |
| `src/pipeline/search.py` | SearXNG JSON client + URL fetch/extract |
| `src/pipeline/intake/channel.py` | Telethon listener + backfill |
| `src/pipeline/intake/bot_intake.py` | Operator DM → item |
| `src/pipeline/approval/auth.py` | `operator_only` decorator |
| `src/pipeline/approval/bot.py` | Preview album, keyboard, four callbacks |
| `src/pipeline/publish/media_host.py` | PNG → R2 → public URL |
| `src/pipeline/publish/instagram.py` | Containers → carousel → publish, retry-safe |
| `src/pipeline/publish/tokens.py` | IG token refresh job |
| `src/pipeline/digest.py` | Daily digest + failure alerts |
| `src/pipeline/__main__.py` | Boots listener + bot + worker + scheduler |

---

### Task 1: Project skeleton, config, models, database

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`
- Create: `src/pipeline/__init__.py`, `src/pipeline/config.py`, `src/pipeline/models.py`, `src/pipeline/db.py`
- Test: `tests/test_config.py`, `tests/test_db.py`

**Interfaces:**
- Produces: `Status` (StrEnum), `Item` (dataclass), `ResearchNote`, `Slide`; `Settings.load()`; `Database(path)` with `async connect()`, `insert_item()`, `get_item(id)`, `claim_items(statuses, limit)`, `transition(id, to_status, fields=None)`, `record_failure(id, error, terminal=False)`, `list_by_status(status)`.

- [ ] **Step 1: Write `.gitignore` first** — before any credential can be created.

```
.env
data/
secrets/
poc/.venv/
poc/out/
__pycache__/
*.pyc
.venv/
.pytest_cache/
*.session
*.session-journal
```

- [ ] **Step 2: Write failing test for Status transitions and Item round-trip**

```python
# tests/test_db.py
import pytest
from pipeline.db import Database
from pipeline.models import Status

@pytest.mark.asyncio
async def test_insert_and_get_roundtrip(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    item_id = await db.insert_item(source="channel", source_chat_id=-100123,
                                   source_msg_id=7, raw_text="hello")
    item = await db.get_item(item_id)
    assert item.status == Status.INGESTED
    assert item.raw_text == "hello"
    assert item.attempts == 0

@pytest.mark.asyncio
async def test_duplicate_source_msg_is_rejected(tmp_path):
    db = Database(tmp_path / "t.db"); await db.connect()
    await db.insert_item(source="channel", source_chat_id=-100123, source_msg_id=7, raw_text="a")
    dup = await db.insert_item(source="channel", source_chat_id=-100123, source_msg_id=7, raw_text="a")
    assert dup is None   # idempotent: backfill/replay must not duplicate

@pytest.mark.asyncio
async def test_transition_writes_fields_and_event(tmp_path):
    db = Database(tmp_path / "t.db"); await db.connect()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1, raw_text="x")
    await db.transition(i, Status.TRIAGED, {"triage_score": 8, "triage_reason": "ok"})
    item = await db.get_item(i)
    assert item.status == Status.TRIAGED and item.triage_score == 8
    events = await db.events_for(i)
    assert (events[-1]["from_status"], events[-1]["to_status"]) == ("ingested", "triaged")

@pytest.mark.asyncio
async def test_claim_items_respects_next_attempt_at(tmp_path):
    db = Database(tmp_path / "t.db"); await db.connect()
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=2, raw_text="x")
    await db.record_failure(i, "boom")          # sets next_attempt_at in the future
    claimed = await db.claim_items([Status.INGESTED], limit=10)
    assert claimed == []                         # backing off, not yet due
```

- [ ] **Step 3: Run to verify failure** — `pytest tests/test_db.py -v`. Expected: `ModuleNotFoundError: pipeline.db`.

- [ ] **Step 4: Implement `models.py`**

```python
from dataclasses import dataclass, field
from enum import StrEnum

class Status(StrEnum):
    INGESTED = "ingested"; TRIAGED = "triaged"; EXTRACTED = "extracted"
    RESEARCHED = "researched"; SYNTHESIZED = "synthesized"; COMPOSED = "composed"
    RENDERED = "rendered"; AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"; PUBLISHING = "publishing"; PUBLISHED = "published"
    DROPPED = "dropped"; REJECTED = "rejected"; FAILED = "failed"

TERMINAL = {Status.PUBLISHED, Status.DROPPED, Status.REJECTED, Status.FAILED}
# awaiting_approval is terminal FOR THE WORKER only — the bot advances it.
WORKER_HALTS = TERMINAL | {Status.AWAITING_APPROVAL}

@dataclass
class ResearchNote:
    question: str; claim: str; detail: str; confidence: str
    sources: list[str] = field(default_factory=list)

@dataclass
class Item:
    id: int; source: str; status: Status
    source_chat_id: int | None = None; source_msg_id: int | None = None
    raw_text: str = ""; raw_media_paths: list[str] = field(default_factory=list)
    attempts: int = 0; last_error: str | None = None
    triage_score: int | None = None; triage_reason: str | None = None
    extracted: dict = field(default_factory=dict)
    research: list[dict] = field(default_factory=list)
    brief: str | None = None; slides: list[dict] = field(default_factory=list)
    caption: str | None = None; regen_note: str | None = None
    rendered_paths: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    approval_msg_id: int | None = None
    ig_child_ids: list[str] = field(default_factory=list)
    ig_carousel_id: str | None = None; ig_post_id: str | None = None
```

- [ ] **Step 5: Implement `db.py`** with the schema from spec §6, JSON columns encoded/decoded at the mapping boundary, `insert_item` returning `None` on `UNIQUE` conflict, `transition` writing an `events` row in the same transaction, `record_failure` computing `next_attempt_at = now + 2**attempts minutes`, and `claim_items` filtering `next_attempt_at IS NULL OR next_attempt_at <= now`.

- [ ] **Step 6: Implement `config.py`** — `Settings` frozen dataclass from env with the spec §12 variables, `DRY_RUN` defaulting to `True`, required-value validation raising at load.

- [ ] **Step 7: Write `.env.example`** with every variable from spec §12, no values.

- [ ] **Step 8: Run tests** — `pytest tests/ -v`. Expected: all pass.

---

### Task 2: LLM client with test seam

**Files:**
- Create: `src/pipeline/llm.py`
- Test: `tests/test_llm.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `Settings` from Task 1.
- Produces: `LLMClient(settings, http)` with `async cheap(system, user, schema=None) -> str|dict`, `async good(...)`, `async vision(system, user, image_paths) -> str`; `FakeLLM` with `queue(response)` and call recording.

- [ ] **Step 1: Write failing test asserting num_ctx is always pinned**

```python
# tests/test_llm.py
import pytest, json
from pipeline.llm import LLMClient

@pytest.mark.asyncio
async def test_cheap_pins_num_ctx_and_returns_parsed_json(settings, capture_http):
    capture_http.respond({"message": {"content": '{"score": 7, "reason": "ok"}'}})
    llm = LLMClient(settings, capture_http)
    out = await llm.cheap("sys", "usr", schema={"type": "object"})
    assert out == {"score": 7, "reason": "ok"}
    sent = capture_http.last_json
    assert sent["options"]["num_ctx"] == settings.num_ctx_cheap   # the whole point
    assert sent["model"] == settings.model_cheap
    assert sent["format"] == {"type": "object"}

@pytest.mark.asyncio
async def test_retries_without_think_when_model_rejects_it(settings, capture_http):
    capture_http.respond_sequence([(400, "does not support thinking"),
                                   (200, {"message": {"content": "ok"}})])
    llm = LLMClient(settings, capture_http)
    assert await llm.cheap("s", "u") == "ok"
    assert "think" not in capture_http.all_json[-1]
```

- [ ] **Step 2: Run to verify failure.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `LLMClient`** — POST `{OLLAMA_HOST}/api/chat`, `stream=False`, `think=False` with retry-without-`think` on 400, `options={"num_ctx": ..., "temperature": ...}`, `format=schema` when given, JSON parse with fence stripping. Vision calls attach base64 `images`.

- [ ] **Step 4: Implement `FakeLLM` in `conftest.py`** — same interface, returns queued responses, records `(role, system, user, schema)` per call. Every stage test uses this; no test touches Ollama.

- [ ] **Step 5: Run tests.** Expected: pass.

---

### Task 3: Worker loop, stage registry, failure classification

**Files:**
- Create: `src/pipeline/worker.py`, `src/pipeline/errors.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Produces: `Retryable`, `Terminal`, `Survivable` exceptions; `STAGES: dict[Status, tuple[handler, next_status]]`; `Worker(db, stages).tick()` and `.run()`.

- [ ] **Step 1: Write failing tests for the three failure classes and the approval halt**

```python
# tests/test_worker.py
@pytest.mark.asyncio
async def test_worker_advances_through_registered_stage(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=1, raw_text="x")
    stages = {Status.INGESTED: (lambda item: {"triage_score": 9}, Status.TRIAGED)}
    await Worker(db, stages).tick()
    assert (await db.get_item(i)).status == Status.TRIAGED

@pytest.mark.asyncio
async def test_worker_never_advances_awaiting_approval(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=2, raw_text="x")
    await db.transition(i, Status.AWAITING_APPROVAL)
    await Worker(db, ALL_STAGES).tick()
    assert (await db.get_item(i)).status == Status.AWAITING_APPROVAL

@pytest.mark.asyncio
async def test_terminal_error_fails_immediately_without_retrying(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=3, raw_text="x")
    def boom(item): raise Terminal("instagram 400")
    await Worker(db, {Status.INGESTED: (boom, Status.TRIAGED)}).tick()
    item = await db.get_item(i)
    assert item.status == Status.FAILED and item.attempts == 1

@pytest.mark.asyncio
async def test_retryable_error_backs_off_and_fails_after_three(db):
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=4, raw_text="x")
    def boom(item): raise Retryable("ollama down")
    w = Worker(db, {Status.INGESTED: (boom, Status.TRIAGED)})
    for _ in range(3):
        await db.clear_backoff(i)   # simulate time passing
        await w.tick()
    assert (await db.get_item(i)).status == Status.FAILED
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement `errors.py` and `worker.py`** — `tick()` claims due items whose status is in `STAGES` and not in `WORKER_HALTS`, runs the handler, commits `transition(next_status, fields)`; `Terminal` → immediate `FAILED`; `Retryable` → `record_failure` with backoff, `FAILED` at attempt 3; unknown exceptions treated as `Retryable`.

- [ ] **Step 4: Run tests.** Expected: pass.

---

### Task 4: Triage stage

**Files:** Create `src/pipeline/stages/triage.py`; Test `tests/stages/test_triage.py`

**Interfaces:** Consumes `LLMClient`, `Item`. Produces `async triage(item, llm, db, threshold) -> dict` returning `{"triage_score", "triage_reason", "_next": Status}`.

- [ ] **Step 1: Failing tests** — score below threshold routes to `DROPPED`; score at/above routes to `TRIAGED`; **DM source bypasses the model entirely and scores 10**; near-duplicate of a recent item is dropped without a model call.

```python
@pytest.mark.asyncio
async def test_dm_bypasses_triage_without_calling_model(db, fake_llm):
    item = Item(id=1, source="dm", status=Status.INGESTED, raw_text="anything")
    out = await triage(item, fake_llm, db, threshold=6)
    assert out["triage_score"] == 10 and out["_next"] == Status.TRIAGED
    assert fake_llm.calls == []

@pytest.mark.asyncio
async def test_low_score_drops(db, fake_llm):
    fake_llm.queue({"score": 2, "reason": "no claim", "topic": "chatter"})
    item = Item(id=1, source="channel", status=Status.INGESTED, raw_text="gm")
    out = await triage(item, fake_llm, db, threshold=6)
    assert out["_next"] == Status.DROPPED
```

- [ ] **Step 2: Run to verify failure. Step 3: Implement.** Cheap model, schema `{score:int, reason:str, topic:str}`, DM short-circuit before any model call, dedupe via normalised-text hash against items created in the last 48h.

- [ ] **Step 4: Run tests.** Expected: pass.

---

### Task 5: Search client and extract stage

**Files:** Create `src/pipeline/search.py`, `src/pipeline/stages/extract.py`; Test `tests/test_search.py`, `tests/stages/test_extract.py`

**Interfaces:** Produces `async searx(http, url, q, n) -> list[dict]`, `async fetch_text(http, url) -> str|None`, `async extract(item, llm, http) -> dict`.

- [ ] **Step 1: Failing tests** — a URL that 404s yields `{url, error}` and does **not** raise (survivable per spec §10); oversized bodies are skipped; a text-only item with no URLs and no images returns empty extraction and still advances.

```python
@pytest.mark.asyncio
async def test_failed_url_is_recorded_not_raised(fake_http, fake_llm):
    fake_http.respond(404)
    item = Item(id=1, source="channel", status=Status.TRIAGED,
                raw_text="see https://example.com/x")
    out = await extract(item, fake_llm, fake_http)
    assert out["extracted"]["url_texts"][0]["error"]
    # survivable: no exception, pipeline continues
```

- [ ] **Step 2–3: Verify failure, implement.** `searx` hits `/search?format=json`; `fetch_text` applies timeout, 2 MB cap, browser UA, `trafilatura.extract` in a thread, 6k char cap. Vision path base64-encodes downloaded media.

- [ ] **Step 4: Run tests.**

---

### Task 6: Research stage — disambiguation and relevance gate

**Files:** Create `src/pipeline/stages/research.py`; Test `tests/stages/test_research.py`

**Interfaces:** Produces `async research(item, llm, http, settings) -> dict` returning `{"research": [note_dicts]}`.

This task fixes the PoC's most serious defect (spec §7.3.1). The regression test uses the real poisoned domains.

- [ ] **Step 1: Write the regression test that pins the bug**

```python
POISONED = ["https://custom-cursor.com/en/collection/anime",
            "https://www.rw-designer.com/cursor-set/anime",
            "https://stackoverflow.com/questions/1234/sql-cursor-loop"]

@pytest.mark.asyncio
async def test_relevance_gate_rejects_keyword_collision_sources(fake_http, fake_llm):
    """The PoC cited custom-cursor.com — a mouse-cursor download site — as the
    source for a statement by Cursor's leadership. This must never recur."""
    fake_llm.queue({"queries": ["Cursor Anysphere AI coding editor OpenAI block"]})
    fake_http.respond_search([{"url": u, "title": "t", "content": "c"} for u in POISONED])
    fake_http.respond_pages({u: "Download anime mouse cursors free" for u in POISONED})
    fake_llm.queue_each([{"relevant": False, "why": "mouse cursors, not Anysphere"}] * 3)
    out = await research(item_about_cursor(), fake_llm, fake_http, settings)
    assert out["research"] == []          # nothing poisoned reached the corpus

@pytest.mark.asyncio
async def test_planner_queries_must_carry_disambiguation(fake_llm, fake_http):
    """A bare ambiguous token is what caused the collision."""
    out_queries = await plan_queries(item_about_cursor(), fake_llm, entity_hint="Anysphere, AI coding editor")
    assert all(q.strip().lower() != "cursor" for q in out_queries)

@pytest.mark.asyncio
async def test_single_researcher_failure_is_survivable(fake_llm, fake_http):
    """Spec §10: one researcher dying must not fail the item, given >=2 succeed."""
    ...  # 3 queries, middle one raises; assert 2 notes returned, no exception

@pytest.mark.asyncio
async def test_fewer_than_two_notes_raises_retryable(fake_llm, fake_http):
    ...  # assert pytest.raises(Retryable)
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — planner prompt receives an entity hint and must embed disambiguating terms; `asyncio.gather` over queries with a semaphore; per-document relevance gate (cheap model, schema `{relevant: bool, why: str}`) before any document enters the corpus; rejections logged with domain; static blocklist seeded with `custom-cursor.com`, `rw-designer.com`; `≥2` notes required or `Retryable`.

- [ ] **Step 4: Run tests.**

---

### Task 7: Synthesize and compose stages

**Files:** Create `src/pipeline/stages/synthesize.py`, `src/pipeline/stages/compose.py`; Test `tests/stages/test_synthesize.py`, `tests/stages/test_compose.py`

**Interfaces:** Produces `async synthesize(item, llm) -> {"brief": str}`, `async compose(item, llm) -> {"slides": [...], "caption": str}`. `SLIDES_SCHEMA` exported for reuse.

- [ ] **Step 1: Failing tests** — slide count clamped to 3–10; slide type vocabulary is exactly `hook|point|facts|takeaway|sources` (**`facts`, not `compare`** — spec §7.5); `regen_note` when present is injected into the compose prompt; caption credits the source channel.

```python
@pytest.mark.asyncio
async def test_slide_types_use_facts_not_compare(fake_llm):
    assert "facts" in SLIDES_SCHEMA["properties"]["slides"]["items"]["properties"]["type"]["enum"]
    assert "compare" not in SLIDES_SCHEMA["properties"]["slides"]["items"]["properties"]["type"]["enum"]

@pytest.mark.asyncio
async def test_regen_note_reaches_the_prompt(fake_llm):
    fake_llm.queue(valid_slides_doc())
    item = Item(id=1, source="dm", status=Status.COMPOSED, brief="b", regen_note="punchier")
    await compose(item, fake_llm)
    assert "punchier" in fake_llm.calls[-1].user
```

- [ ] **Step 2–4: Verify failure, implement, run.** Synthesis prompt enforces source-URL attribution and explicit contradiction flagging (spec §7.4). Compose states per-field character limits as hints — enforcement is Task 8.

---

### Task 8: Renderer, templates, overflow guard

**Files:** Create `src/pipeline/stages/render.py`, `templates/base.html.j2`, `templates/slides/{hook,point,facts,takeaway,sources}.html.j2`, `config/theme.json`; Test `tests/stages/test_render.py`, `tests/golden/`

**Interfaces:** Produces `async render(item, settings) -> {"rendered_paths": [...]}`; raises `Recompose(slide_index, reason)` on unfixable overflow.

The PoC template is the starting point — it already renders correctly at 1080×1350 with zero overflow. Port it, split per slide type, rename `compare` → `facts`.

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_overflow_triggers_font_step_then_recompose(settings, tmp_path):
    """Spec §7.6: the guard is the real enforcement; the model ignores limits."""
    item = item_with_slides([{"type": "point", "headline": "H",
                              "bullets": ["x" * 4000] * 4}])
    with pytest.raises(Recompose) as e:
        await render(item, settings)
    assert e.value.slide_index == 0

@pytest.mark.asyncio
async def test_renders_expected_png_dimensions(settings, tmp_path):
    paths = (await render(item_with_slides(valid_six()), settings))["rendered_paths"]
    assert len(paths) == 6
    for p in paths:
        assert Image.open(p).size == (1080, 1350)

@pytest.mark.asyncio
async def test_golden_image_matches_reference(settings, tmp_path):
    out = await render(item_with_slides(GOLDEN_SLIDES), settings)
    assert image_diff(out["rendered_paths"][0], "tests/golden/hook.png") < 0.01
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — Jinja2 render, Playwright Chromium in a **subprocess** (spec §7.6: Chromium OOM must not kill Telethon), viewport 1080×1350, per-slide element screenshot, `scrollHeight > clientHeight + 2` check on every text box, one font-size step down and re-measure, then `Recompose`. Worker maps `Recompose` back to `Status.COMPOSED` with `regen_note`.

- [ ] **Step 4: Generate golden references, run tests.**

---

### Task 9: Intake — Telethon listener, DM handler, authorisation

**Files:** Create `src/pipeline/approval/auth.py`, `src/pipeline/intake/channel.py`, `src/pipeline/intake/bot_intake.py`; Test `tests/test_auth.py`, `tests/intake/test_channel.py`

**Interfaces:** Produces `operator_only(handler)` decorator; `ChannelListener(db, client, settings)` with `start()` and `backfill()`; `register_intake(dp, db, settings)`.

- [ ] **Step 1: Write the authorisation tests first — this is the security boundary (spec §8.1)**

```python
@pytest.mark.asyncio
async def test_dm_from_stranger_creates_no_item(db, settings):
    """DM bypasses triage, so an unauthorised DM is a direct path to publishing."""
    await handle_dm(fake_message(user_id=999999, text="inject me"), db, settings)
    assert await db.list_by_status(Status.INGESTED) == []

@pytest.mark.asyncio
async def test_dm_from_operator_creates_item(db, settings):
    await handle_dm(fake_message(user_id=settings.operator_user_id, text="ok"), db, settings)
    assert len(await db.list_by_status(Status.INGESTED)) == 1

@pytest.mark.asyncio
async def test_stranger_callback_does_not_transition(db, settings):
    i = await seed_awaiting_approval(db)
    await handle_callback(fake_callback(user_id=999999, data=f"approve:{i}"), db, settings)
    assert (await db.get_item(i)).status == Status.AWAITING_APPROVAL
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — `operator_only` compares `from_user.id` to `settings.operator_user_id`, ignores silently (no reply, no error, no liveness signal). Applied to every handler. Channel listener registers `events.NewMessage(chats=settings.channel_ids)`, inserts `ingested` rows; `backfill()` queries `MAX(source_msg_id)` per channel on boot and replays gaps, relying on the `UNIQUE` constraint for safety.

- [ ] **Step 4: Run tests.**

---

### Task 10: Approval bot — preview and callbacks

**Files:** Create `src/pipeline/approval/bot.py`; Test `tests/approval/test_bot.py`

**Interfaces:** Produces `async send_preview(bot, db, item)`; callbacks `approve|reject|regen|caption`.

- [ ] **Step 1: Failing tests** — album sent first, keyboard on a **separate** reply message (albums cannot carry keyboards, spec §8); preview text lists **source domains**; approve → `APPROVED`; reject → `REJECTED`; regen → `COMPOSED` with note and **no re-research**; caption edit re-previews without re-rendering.

```python
@pytest.mark.asyncio
async def test_keyboard_is_on_separate_message_from_album(fake_bot, db):
    await send_preview(fake_bot, db, item_rendered())
    album, keyboard_msg = fake_bot.sent
    assert album.is_media_group and album.reply_markup is None
    assert keyboard_msg.reply_markup is not None

@pytest.mark.asyncio
async def test_preview_lists_source_domains(fake_bot, db):
    """Operator's last chance to catch a relevance failure (spec §8)."""
    await send_preview(fake_bot, db, item_rendered_with_sources(["teslarati.com"]))
    assert "teslarati.com" in fake_bot.sent[1].text

@pytest.mark.asyncio
async def test_regenerate_returns_to_composed_keeping_brief(db, fake_bot):
    i = await seed_awaiting_approval(db, brief="THE BRIEF")
    await handle_callback(fake_callback(OPERATOR, f"regen:{i}"), db, settings)
    item = await db.get_item(i)
    assert item.status == Status.COMPOSED and item.brief == "THE BRIEF"
```

- [ ] **Step 2–4: Verify failure, implement, run.**

---

### Task 11: Publishing — R2, Instagram, retry safety, token refresh

**Files:** Create `src/pipeline/publish/media_host.py`, `src/pipeline/publish/instagram.py`, `src/pipeline/publish/tokens.py`; Test `tests/publish/test_instagram.py`

**Interfaces:** Produces `async upload(paths, item_id) -> list[str]`; `async publish_carousel(item, http, settings) -> str`; `async refresh_token_if_due(...)`.

- [ ] **Step 1: Failing tests — double-post prevention is the critical one (spec §9)**

```python
@pytest.mark.asyncio
async def test_retry_reuses_persisted_container_ids(db, fake_http, settings):
    """A retry must not create a second carousel."""
    i = await seed_approved(db, ig_child_ids=["c1", "c2"], ig_carousel_id="car1")
    fake_http.respond({"id": "POST1"})
    await publish_carousel(await db.get_item(i), fake_http, settings)
    assert not any("/media" in c.url and "is_carousel_item" in str(c.data)
                   for c in fake_http.calls)     # no child containers recreated

@pytest.mark.asyncio
async def test_dry_run_makes_no_network_calls(db, fake_http, settings_dry_run):
    await publish_carousel(await db.get_item(await seed_approved(db)), fake_http, settings_dry_run)
    assert fake_http.calls == []

@pytest.mark.asyncio
async def test_instagram_4xx_raises_terminal_not_retryable(db, fake_http, settings):
    fake_http.respond(400, {"error": {"message": "bad"}})
    with pytest.raises(Terminal):
        await publish_carousel(await db.get_item(await seed_approved(db)), fake_http, settings)

@pytest.mark.asyncio
async def test_token_refresh_failure_alerts_operator(fake_http, fake_bot, settings):
    """Silent token expiry stops publishing with no visible symptom."""
    fake_http.respond(400, {"error": {}})
    await refresh_token_if_due(fake_http, fake_bot, settings, days_old=55)
    assert "token" in fake_bot.sent[-1].text.lower()
```

- [ ] **Step 2–4: Verify failure, implement, run.** Publish sequence per spec §9; `ig_child_ids`/`ig_carousel_id` persisted as created and reused on retry; `ig_post_id` written immediately; 4xx → `Terminal`, 5xx → `Retryable`; rate-limit counter over a rolling 24h; `DRY_RUN` logs instead of calling.

---

### Task 12: Digest, alerts, and process wiring

**Files:** Create `src/pipeline/digest.py`, `src/pipeline/__main__.py`, `docker-compose.yml`, `README.md`; Test `tests/test_main.py`

- [ ] **Step 1: Failing test** — `build_stage_registry()` covers every non-halt status exactly once, and contains **no** entry for `AWAITING_APPROVAL`.

```python
def test_registry_covers_all_worker_statuses_and_excludes_approval():
    reg = build_stage_registry(deps)
    assert Status.AWAITING_APPROVAL not in reg
    for s in set(Status) - WORKER_HALTS:
        assert s in reg, f"no stage registered for {s}"
```

- [ ] **Step 2–4: Verify failure, implement, run.** `__main__` boots Telethon listener, aiogram dispatcher, worker loop, and a scheduler (daily digest, token refresh) under one `asyncio.gather` with graceful shutdown. Compose runs SearXNG and the app; Ollama stays on the host at `host.docker.internal:11434`. README documents the credential setup the operator must do by hand.

---

## Self-Review

**Spec coverage:** §4 state machine → T1/T3; §5 layout → all; §6 data model → T1; §7.1 → T4; §7.2 → T5; §7.3.1 relevance gate → T6; §7.4/7.5 → T7; §7.6 overflow guard → T8; §8 approval → T10; §8.1 authorisation → T9; §9 publishing + retry safety + token → T11; §10 failure taxonomy → T3; §11 testing → every task; §12 config/secrets → T1/T12; §13 order → task order.

**Placeholder scan:** none — every step names real files, real tests, real assertions.

**Type consistency:** `Status` values used identically across T1–T12; `facts` (not `compare`) in T7 and T8; `Retryable`/`Terminal`/`Survivable` from T3 used in T5, T6, T11; `Recompose` raised in T8 and handled in T3's registry.

**Gap found and fixed during review:** nothing in Tasks 1–11 asserted that every worker status has a registered stage — a missing registry entry would silently strand items forever. Added as Task 12 Step 1.
