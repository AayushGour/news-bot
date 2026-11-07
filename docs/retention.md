# Storage retention: how-to

Verified 2026-11-01 (tester, evidence: `.claude/logs/tester.md#T241`). Every step
below was run; the deck pipeline's database rows are never deleted, only files.

## What it does

The worker runs a cleanup job about 60 s after boot and then every
`CLEANUP_INTERVAL_S` (default 6 h).

| Item state | Files deleted when | Notes |
|---|---|---|
| `awaiting_approval` | waiting strictly more than `AWAITING_APPROVAL_MAX_DAYS` (120) | auto-rejected first (`last_error` = "auto-rejected: awaiting approval > 120 days", an events row is written), then cleaned. Exactly 120 d is kept. `0` disables the rule. |
| `rejected`, `dropped` | `TERMINAL_MEDIA_GRACE_DAYS` (1) after reaching that state | |
| `published` | same grace, and only if `ig_post_id` is set | `CLEANUP_PUBLISHED_MEDIA=false` keeps local files |
| `failed` | `FAILED_MEDIA_GRACE_DAYS` (30) | kept longer so a requeue can re-read the download |

Bucket objects under `items/<id>/` go too when `CLEANUP_MINIO=true`.

Never touched: `approved`, `publishing`, `needs_input`, any in-flight stage
(ingested..extracted etc.), published items without `ig_post_id`, and anything
inside its grace period. The DB row, caption, slides JSON, `text_hash` and
publish history are always kept (dedupe survives).

An item approved or requeued while a sweep is running is skipped and keeps its
files (reported as "raced").

## Steps

1. Set the knobs in `.env` (all documented in `.env.example`, defaults in
   `src/pipeline/config.py`). To preview only, put
   `RETENTION_ENABLED=true` and `RETENTION_DRY_RUN=true`.
   Note: the live `.env` currently has `RETENTION_ENABLED=false`.
2. Preview by hand (changes nothing):
   `./.venv/bin/python scripts/cleanup.py`
   It prints one line per item (`item 24 [failed] failed 56d >= grace 30d: 1 file(s) 14.6M, ...`),
   a totals line (`retention: would auto-reject 0; would delete 883 file(s) ...`),
   the awaiting_approval count and oldest age, then "report only - pass --apply to do this".
3. Apply by hand: `./.venv/bin/python scripts/cleanup.py --apply` (same policy as the worker).
4. Automatic: set `RETENTION_ENABLED=true`, `RETENTION_DRY_RUN=false`, restart
   the worker. With `RETENTION_ENABLED=false` the job does nothing; with
   `RETENTION_DRY_RUN=true` it only logs what it would do. A Telegram summary
   goes to the operator after each run that acted.
5. A second run finds nothing (idempotent).

Tips: `CLEANUP_INTERVAL_S` has a 60 s floor. Docker, Ollama and browser caches
are only reported, never pruned.
