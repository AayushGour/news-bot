# Development

## Setup

```bash
uv sync --extra dev      # REQUIRED for tests — plain `uv sync` is not enough
```

`pytest` itself — along with `hypothesis` (property-based tests), `ruff` and
`pre-commit` — is in the `dev` extra; none of them ship with a plain `uv sync`. On a fresh checkout
without the extra there is no `pytest` to start a run:

```
$ ./.venv/bin/python -m pytest -q
/path/to/.venv/bin/python: No module named pytest
```

That is loud, not silent. But **`uv sync` (no `--extra dev`) also actively
prunes** dev extras that were already installed — so the same failure
reappears if a later step re-runs plain `uv sync`, even though `pytest` worked
a moment before. An immediate `No module named pytest` with no pass/fail
summary is the tell: run `uv sync --extra dev` and retry before assuming
something is broken.

## Layout

```
src/pipeline/
  __main__.py        process entrypoint: listener + bot + worker loops + tunnel + periodic jobs;
                     build_stage_registry() maps every status to its stage
  worker.py          the two loops; claims items, runs one stage, classifies failures
  models.py          Item, Status, TERMINAL, WORKER_HALTS
  db.py              SQLite access, migrations, JSON columns, transition/transition_if
  errors.py          the failure taxonomy the worker dispatches on
  config.py          Settings.load() — the only place env is read; editorial constants
  limits.py          platform limits (Telegram message/callback sizes); a leaf module
  llm.py             role → provider/model resolution, retry ladder, local chain, verify()
  budget.py          context budgeting and nonce-delimited data blocks for prompts
  expand.py          widens a search plan into more queries before it runs
  search.py          SearXNG client, dedupe, engine-outage detection, page/image fetch
  safe_download.py   SSRF-guarded fetch_public + pinned transport for untrusted URLs
  integrity.py       provenance checks: figures, URLs, repos must be backed by material
  quality.py         mechanical substance metrics for a composed deck (eval scoring)
  verify.py          advisory pre-render verifier (VERIFY)
  coverage.py        does the deck answer what was asked (also the revision judge)
  conversation.py    NeedsInput: ask the operator, park, resume
  deck_shape.py      slide types and slide-count limits (a leaf module, no imports back)
  render_worker.py   headless-Chromium screenshot worker, run as its own process by render
  requeue.py         send an item back to a stage, clearing what that stage produces
  retention.py       the file-cleanup policy (worker job and scripts/cleanup.py)
  faults.py          once-per-fault operator alerts (config faults, IG throttle)
  digest.py          the daily summary and failure alerts
  logredact.py       credential redaction, installed on every log handler
  stages/            one module per stage; each returns field updates
                     (extract, triage, research, enumerate_items, synthesize,
                      compose, render, revise)
  revision/          agentic Regenerate: model (action registry) · plan · execute ·
                     text_edit · fetch · structural · check · record · request · copy
  publish/           instagram · media_host · tunnel · tokens
  intake/            channel · bot_intake · manual
  approval/          the Telegram approval bot and its auth boundary
templates/           base.html.j2 + one partial per slide type
config/              themes/*.json, fonts/, theme.json
scripts/             operational tools (see below)
tests/               mirrors the src layout
docs/                user docs; docs/design/ holds feature design specs
```

The revision package has its own API reference: [revision-api](revision-api.md).

## Team process and the code brain

The project is built by a small team of AI agents. The rulebook is
`AGENTS.md` at the repo root (roles, the plan → build → review → test loop, the
task board in `.claude/task-board.md`, decisions in
`.claude/project-context.md`, coding rules in `.claude/coding-standards.md`).
On-demand skills (security review, TDD, data modeling, …) live in
`.agents/skills/<name>/SKILL.md`.

The **code brain** is a [graphify](https://github.com/Graphify-Labs/graphify)
knowledge graph of this codebase (functions, classes, calls, imports), built
locally by tree-sitter — no LLM, no API key, nothing leaves the machine. It
lives in `graphify-out/` (gitignored), is rebuilt at session start and after
source edits by hooks, and is queried through the `graphify` MCP server
(`.mcp.json`). To refresh it by hand, or to see a symbol's blast radius:

```bash
uvx --from 'graphifyy[mcp]<0.10' graphify update .
uvx --from 'graphifyy[mcp]<0.10' graphify affected "<symbol>"
```

## Running the tests

```bash
./.venv/bin/python -m pytest --collect-only -q     # current count, last line
./.venv/bin/python -m pytest -q                     # everything
./.venv/bin/python -m pytest tests/revision -q     # one area
./.venv/bin/python -m pytest -q -p no:cacheprovider
```

The count is deliberately not pinned in prose here — it only goes stale the
next time a test is added or removed. Ask the suite, not this doc.

`asyncio_mode = auto`, so async tests need no marker.

### Tests must be isolated from the machine

Two real bugs came from tests reading live state:

- `publish/tunnel.py` resolves the media base from `data/tunnel-origin.txt`,
  which exists whenever a tunnel is running. A test read it and started passing
  a live hostname instead of the loopback it had configured. An autouse fixture
  in `conftest.py` now points every test at a temp path.
- Pacing in `search_many` is real wall-clock sleeping. An autouse fixture sets
  the interval to zero; tests that assert *on* the pacing set their own.

If a test's result can change because of what is running on your laptop, it is
not a test. Anything that needs real data uses a **copy** of `data/app.db`,
never the live file.

## Linting

```bash
./.venv/bin/ruff check src tests scripts    # after `uv sync --extra dev`
```

`ruff` is pinned in `pyproject.toml`'s dev deps (not resolved as "whatever is
newest" via bare `uvx ruff`) and `[tool.ruff.lint] select` is explicit, so a
finding count means the same thing in every session (T79/D33) — see the
comment above `select` in `pyproject.toml` for what is enabled today and what
is deliberately deferred.

A local pre-commit hook (`.pre-commit-config.yaml`) runs this same check —
same pinned binary, same config — on every commit touching `src/`, `tests/` or
`scripts/`. One-time setup, per clone:

```bash
./.venv/bin/pre-commit install
```

(Deliberately not run for you by any script: `pre-commit install` writes to
`.git/hooks/`, which every git worktree of this repo shares, so it is an
explicit, one-time human step.)

## Mutation testing

A green suite is not evidence until you have tried to falsify it. Break the
code on purpose and confirm the tests notice:

```bash
./.venv/bin/python scripts/mutate.py
```

Or by hand, which is what most of this codebase's coverage was actually
verified with:

```bash
cp src/pipeline/x.py /tmp/x.orig
# flip a condition, shift a boundary, return a constant
./.venv/bin/python -m pytest tests/test_x.py -q     # MUST go red
cp /tmp/x.orig src/pipeline/x.py
```

### The mutation harness has lied four ways

Every one of these produced a false "SURVIVED", which reads as a test gap that
is not there — or worse, hides one that is:

1. **Shell quoting.** A `python -c` one-liner mangled a regex; the file was
   never modified and the tests passed against clean code. Use a script file.
2. **zsh does not word-split unquoted variables.** `pytest $files` passed one
   bogus path, pytest ran nothing, and "no tests ran" scored as a survivor.
   Treat *no tests ran* as its own outcome.
3. **Non-unique anchors.** `s.count(find) != 1` means the mutation did not
   apply. Assert on the count and report `BAD-ANCHOR`, do not fall through.
4. **Stale bytecode.** Clear `__pycache__` between mutants.

And one that cost an hour of debugging: `pkill` on the mutation script skips
`finally`, leaving `if False:` in a source file. Always verify the tree is
restored (`git diff`) before trusting a result.

**The harness refuses to start instead of racing (D92/D98).** Before
mutating anything, `mutate.py` checks for `data/.mutate-inflight` (a lock
file it writes for the duration of each mutation) and for a dirty working
tree, and exits without touching a file if either is true:

```
refusing to start: data/.mutate-inflight already exists — either another
mutate.py run is active in this worktree, or a previous one was killed
mid-mutation and never cleaned up. Read the file and `git status
--porcelain` by hand: if a mutation it describes is still applied, restore
that file's content yourself, then remove data/.mutate-inflight before
retrying.
```

Recovery from a genuine crash is a **manual step**, not something the next
invocation does for you — a prior version auto-deleted the lock at startup,
which is exactly what raced and lost the record in the first place (D98).
To recover:

1. `cat data/.mutate-inflight` — it names the mutated file and holds the
   original (pre-mutation) file content as JSON.
2. `git status --porcelain` that file — if it is dirty, overwrite it with
   the `original` field from the lock file (or `git checkout -- <path>` if
   the file was tracked and unmodified before the crash).
3. `git diff` to confirm the tree is clean again.
4. `rm data/.mutate-inflight`.
5. Re-run the sweep.

Never delete the lock file first and check the tree after — read it, then
restore, then remove it, in that order.

**Equivalent mutants exist.** A mutation that cannot change behaviour for any
real input is not a gap — say so rather than bending a test to kill it.

## Conventions

The full list is `.claude/coding-standards.md`. The ones that shaped the code
most:

- **The prompt is a hint; the code is a guarantee.** If something must hold,
  enforce it after the model returns. Several bugs here were "the prompt already
  asks for that" — and the model simply did not.
- **Comments explain *why*, and name the failure that motivated them.** The
  codebase is full of `# item 84 …` notes. They are the reason nobody
  re-introduces the bug.
- **Never truncate content to fit.** Shrink the type, widen the window, page
  the query.
- **An outage is not a result.** Never record infrastructure failure as a
  finding about the content; raise `Retryforever` (or a subclass) so the item
  defers without spending an attempt.
- **No exception text in operator-facing output** — closed codes and fixed
  sentences only. (The failure alert on the `failed` path still carries it;
  T318.)
- **Config in one place.** Read the environment only in `config.py`; new knobs
  go in `Settings`, `Settings.load()`, `.env.example` and the
  [configuration reference](setup.md#configuration-reference), together.
- Lint clean on touched files: `./.venv/bin/ruff check <file>`. Compare
  against `git show HEAD:<file>` before claiming a finding is yours.

## Adding a stage

1. Write `stages/yourstage.py` — a function taking `item` plus injected
   dependencies, returning a dict of field updates (optionally `_next`,
   `_detail`, `_after`; see [architecture](architecture.md#the-worker)). As a
   rule it should not call other stages: the worker sequences them. The two
   existing exceptions are deliberate — `stages/revise.py` runs `compose()` for
   `/skip`, render overflow, a deck with no slides and a confirmed rebuild, and
   `__main__.research_or_enumerate` runs `enumerate_items` and then `research`
   in one step. Add a third only with the same justification.
2. Add the status to `models.Status`. If it is a human gate — something only
   the operator may move — add it to `WORKER_HALTS` instead of registering a
   stage.
3. Register it in `build_stage_registry()` (`__main__.py`). `missing_statuses()`
   raises at startup if a non-halt status has no stage, so you cannot forget.
4. Add its outputs to `requeue.STAGE_OUTPUTS` — what a requeue to that stage
   must clear. Note the subtlety: a status names *finished* work and the worker
   keys the *next* stage off it, so you clear what the stage running **from**
   that status produces.
5. Add a DB column, if any, with a migration (`.agents/skills/data-modeling`).
6. Test the stage, then mutate it.

## Adding a slide type

1. `templates/slides/<type>.html.j2` — the partial. CSS lives in
   `base.html.j2`. Only read the fields you register in step 3 —
   `test_every_field_a_template_reads_is_declared_for_its_type`
   (`tests/stages/test_compose.py`) fails if the template reads a field the
   schema does not declare for this type.
2. Add the name to `SLIDE_TYPES` in `deck_shape.py`.
3. Register the type in BOTH `REQUIRED_BODY_FIELDS[type]` (the field(s) whose
   presence means "this slide has content") and `SLIDE_TYPE_FIELDS[type]`
   (every field the type may legally carry, a superset of the body fields), in
   `stages/compose.py`. `SLIDES_SCHEMA` builds its discriminated union from
   these two dicts at **module import time**, indexing each with `[type]` —
   skip either and `import pipeline.stages.compose` raises `KeyError`
   immediately rather than silently building a permissive arm (T78).
4. Describe it in the compose prompt (`SYSTEM` in `stages/compose.py`).
5. If revisions may add it, check `ADDABLE_TYPES` in `revision/text_edit.py`.
6. Render it for real before believing it:

```bash
./.venv/bin/python - <<'PY'
import asyncio, sys; sys.path.insert(0, "src")
from dotenv import load_dotenv; load_dotenv(".env")
from pipeline.stages.render import render
from pipeline.models import Item
from pipeline.config import Settings
slide = {"type": "yourtype", "headline": "Test", ...}
item = Item(id=9999, source="test", status="composed", raw_text="d",
            slides=[slide], theme="signal")
print(asyncio.run(render(item, settings=Settings.load()))["rendered_paths"])
PY
```

## Scripts

| Script | Purpose |
|---|---|
| `preflight.py` | verify every dependency and credential before a first run (`--send` DMs you a test) |
| `check_instagram.py` | token, account and publishing scope — prints nothing secret |
| `smoke.py` | end-to-end against real models and SearXNG; no Telegram, no Instagram |
| `dashboard.py` | the operator UI, `http://127.0.0.1:8770` |
| `requeue.py` | send an item back to a stage |
| `queue_item.py` | add an item from the CLI |
| `tunnel.py` | run the media tunnel standalone |
| `cleanup.py` | report reclaimable disk; `--apply` runs the retention policy by hand ([retention](retention.md)) |
| `mutate.py` | break the code and check the tests notice |
| `eval_compose.py` | score compose against real items, so a prompt change is testable |
| `eval_research.py` | score research against real items, with a frozen document cache |
| `build_golden.py` | snapshot an evaluation set from real items, tagged by failure mode |
| `build_t293_golden.py`, `build_t298_fixture.py` | rebuild the revision test fixtures |
| `measure_*.py`, `harvest_filler_phrases.py` | one-off measurements behind specific decisions; each docstring names its task |

## Debugging the live system

```bash
tail -f data/pipeline.log
q() { docker compose exec -T app sqlite3 -readonly /app/state/app.db "$1"; }
q "SELECT id,status,attempts,next_attempt_at FROM items ORDER BY id DESC LIMIT 10;"
q "SELECT * FROM events WHERE item_id=100 ORDER BY id DESC LIMIT 10;"
q "SELECT id,status FROM items WHERE status IN ('revision_requested','revision_confirm');"
```

The live DB lives on the `pipeline-state` volume. Query it read-only from the
container, or a copy from `data/backups/`; a host `sqlite3 data/app.db` would
create an empty database next to the real one.

The `events` table is the transition history — `from_status`, `to_status`, `at`,
and a `detail` naming what caused it. It answers "how did this item get here"
faster than the log does. A deferred item keeps `attempts` unchanged and
moves `next_attempt_at` forward; a rising `attempts` means the item itself is
failing.

When a bare-host process looks hung, sample it before theorising (in Docker,
`docker compose top app` and `docker stats` give the same first read):

```bash
sample $(pgrep -f "python -m pipeline") 3 -f /tmp/sample.txt
ps -o pid,stat,etime,%cpu,time -p $(pgrep -f "python -m pipeline")
```

Elapsed time far exceeding CPU time means it is throttled or blocked, not busy
— that is how the launchd `ProcessType` throttle was found.
