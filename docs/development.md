# Development

## Layout

```
src/pipeline/
  __main__.py        process entrypoint: listener + bot + worker + tunnel
  worker.py          the two loops; claims items and applies stages
  models.py          Item, Status, WORKER_HALTS
  db.py              SQLite access, migrations, JSON columns
  errors.py          the failure taxonomy the worker dispatches on
  config.py          Settings.load() — the only place env is read
  llm.py             role → model resolution, retry ladder, local fallback
  search.py          SearXNG client, dedupe, engine-outage detection
  logredact.py       credential redaction, installed on every log handler
  stages/            one module per stage; each returns field updates
  publish/           instagram · media_host · tunnel · tokens
  intake/            channel · bot_intake · manual
  approval/          the Telegram approval bot and its auth boundary
templates/           base.html.j2 + one partial per slide type
scripts/             operational tools (see below)
tests/               599 tests, mirroring the src layout
```

## Running the tests

```bash
./.venv/bin/python -m pytest -q                    # all 599
./.venv/bin/python -m pytest tests/publish -q      # one area
./.venv/bin/python -m pytest -q -p no:cacheprovider
```

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
not a test.

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

**Equivalent mutants exist.** A mutation that cannot change behaviour for any
real input is not a gap — say so rather than bending a test to kill it.

## Conventions

- **The prompt is a hint; the code is a guarantee.** If something must hold,
  enforce it after the model returns. Several bugs here were "the prompt already
  asks for that" — and the model simply did not.
- **Comments explain *why*, and name the failure that motivated them.** The
  codebase is full of `# item 84 …` notes. They are the reason nobody
  re-introduces the bug.
- **Never truncate content to fit.** Shrink the type, widen the window, page
  the query.
- **An outage is not a result.** Never record infrastructure failure as a
  finding about the content.
- Lint clean on touched files: `uvx ruff check <file>`. Compare against
  `git show HEAD:<file>` before claiming a finding is yours.

## Adding a stage

1. Write `stages/yourstage.py` — a function taking `item` plus injected
   dependencies, returning a dict of field updates. It must not call other
   stages.
2. Add the status to `models.Status`.
3. Register it in `build_stage_registry()`. `missing_statuses()` raises at
   startup if a non-halt status has no stage, so you cannot forget.
4. Add its outputs to `requeue.STAGE_OUTPUTS` — what a requeue to that stage
   must clear. Note the subtlety: a status names *finished* work and the worker
   keys the *next* stage off it, so you clear what the stage running **from**
   that status produces.
5. Test the stage, then mutate it.

## Adding a slide type

1. `templates/slides/<type>.html.j2` — the partial. CSS lives in
   `base.html.j2`.
2. Add the name to `SLIDE_TYPES` in `stages/compose.py`.
3. Add its required body field to `required_any` in `normalise_slides` — a
   slide with only a headline renders as text on an empty field.
4. Describe it in the compose prompt.
5. Render it for real before believing it:

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
| `preflight.py` | verify every dependency and credential before a first run |
| `check_instagram.py` | token, account and publishing scope — prints nothing secret |
| `smoke.py` | end-to-end against real Ollama and SearXNG; no Telegram, no Instagram |
| `dashboard.py` | the operator UI |
| `requeue.py` | send an item back to a stage |
| `queue_item.py` | add an item from the CLI |
| `tunnel.py` | run the media tunnel standalone |
| `eval_compose.py` | score compose against real items, so a prompt change is testable |
| `build_golden.py` | snapshot an evaluation set from real items, tagged by failure mode |
| `mutate.py` | break the code and check the tests notice |
| `cleanup.py` | report reclaimable disk; deletes only what this project owns |

## Debugging the live system

```bash
tail -f data/pipeline.log
sqlite3 data/app.db "SELECT id,status,last_error FROM items ORDER BY id DESC LIMIT 10;"
sqlite3 data/app.db "SELECT * FROM events WHERE item_id=100 ORDER BY id DESC LIMIT 10;"
```

The `events` table is the transition history — `from_status`, `to_status`, `at`,
and a `detail` naming what caused it. It answers "how did this item get here"
faster than the log does.

When a process looks hung, sample it before theorising:

```bash
sample $(pgrep -f "python -m pipeline") 3 -f /tmp/sample.txt
ps -o pid,stat,etime,%cpu,time -p $(pgrep -f "python -m pipeline")
```

Elapsed time far exceeding CPU time means it is throttled or blocked, not busy
— that is how the launchd `ProcessType` throttle was found.
