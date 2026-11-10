# Design note: everything in Docker (T346)

Overview: [architecture § Running in Docker](../architecture.md#running-in-docker).
Rules: **U12** (user: "i want everything running in docker"; supersedes U11's
read-only dashboard), **D298** (a live SQLite WAL DB never crosses a kernel
boundary). This note is the build contract and cutover runbook for T347–T356.

## 0. Facts this design rests on (verified in the repo, 2026-11-08)

| Fact | Where |
|---|---|
| `Database.connect` mkdirs, sets `journal_mode=WAL`, runs schema + migrate on every open — a host process pointed at a missing `data/app.db` silently creates an empty one | `src/pipeline/db.py:191-200` |
| Telethon session = `str(settings.session_path)`, default `secrets/telegram.session` — a SQLite file Telethon writes | `__main__.py:319-320`, `config.py:475` |
| `ig_token.json` lives at `settings.db_path.parent` — it moves with `DB_PATH` | `__main__.py:301` |
| Log, tunnel origin, themes resolve from `Path(__file__).parents[...]` → `/app/...` in the image because `PYTHONPATH=/app/src` shadows the pip-installed copy | `__main__.py:43`, `publish/tunnel.py:31`, `stages/render.py:30`, `Dockerfile:19` |
| cloudflared is spawned by name (`cloudflared tunnel --url LOCAL_TARGET`), `LOCAL_TARGET = "http://localhost:9000"` hard-coded; not installed in the image | `publish/tunnel.py:77,176-177`, `Dockerfile` |
| MinIO is a standalone `docker run` container `pipeline-minio` publishing 9000/9001, not a compose service; its data location is not recorded | `docs/setup.md:196-202` |
| Ollama / SearXNG defaults are `localhost`; compose already overrides to `host.docker.internal:11434` / `http://searxng:8080` | `config.py:480,507`, `docker-compose.yml` |
| Image is `python:3.13-slim` + `playwright install --with-deps chromium`; the "jammy never built" note refers to the *old* base, replaced in T206 — whether today's image builds and renders is unverified | `Dockerfile:1-17` |
| Every theme's font stack is Apple system fonts (`-apple-system`, `Helvetica Neue`, `SF Mono`, `Menlo`, `Georgia`, `Iowan Old Style`); none exist in a Linux image, and they cannot be legally copied in | `config/themes/*.json`, `templates/base.html.j2:13-14` |
| Snapshot code from T340 exists (`write_snapshot`: backup API → DELETE journal → quick_check → fsync → `os.replace`) and settings `snapshot_path` / `snapshot_interval_s` / `dashboard_readonly` | `src/pipeline/snapshot.py`, `config.py:374-380` |
| Digest/token/retention jobs are interval-based, not wall-clock | `__main__.py:47,331-345` |

**WAL across containers on a named volume.** A named volume is a directory on
the Docker Desktop VM's ext4 disk. All containers share that one Linux kernel,
so `fcntl` locks and the `-shm` mmap (same inode, same page cache) behave as for
two processes on one Linux host — the condition SQLite's WAL documentation
requires. A bind mount is virtiofs between macOS and the VM, which shares
neither, hence T338. **Status: reasoned, not yet measured — T347 is the gate.**
If T347 fails, the fallback shape is the dashboard served from inside the worker
process (one container, one connection pool); see §8.

## 1. Compose (T349)

```yaml
volumes:
  pipeline-state:
    external: true            # created by the runbook; `down -v` cannot delete it

x-pipeline: &pipeline
  build: .
  env_file: .env
  environment: &pipeline-env
    DB_PATH: /app/state/app.db
    TELEGRAM_SESSION_PATH: /app/state/telegram.session
    OLLAMA_HOST: http://host.docker.internal:11434
    SEARXNG_URL: http://searxng:8080
    S3_ENDPOINT: http://host.docker.internal:9000
    TUNNEL_TARGET: http://host.docker.internal:9000
    TZ: ${TZ:-UTC}
  extra_hosts: ["host.docker.internal:host-gateway"]
  depends_on: [searxng]
  restart: unless-stopped

services:
  searxng: { ...unchanged... }
  app:
    <<: *pipeline
    container_name: pipeline-app
    volumes:
      - pipeline-state:/app/state
      - ./data:/app/data
      - ./secrets:/app/secrets:ro
      - ./config:/app/config:ro
      - ./templates:/app/templates:ro
    stop_grace_period: 60s    # lets the lease release and an in-flight publish finish
    healthcheck: { test: ["CMD", "python", "-m", "pipeline.lease", "--check"], interval: 60s }
  dashboard:
    <<: *pipeline
    container_name: pipeline-dashboard
    command: ["python", "scripts/dashboard.py"]
    environment:
      <<: *pipeline-env
      DASHBOARD_HOST: 0.0.0.0   # keep the existing comment block
    volumes:
      - pipeline-state:/app/state
      - ./data:/app/data:ro
      - ./config:/app/config:ro
      - ./templates:/app/templates:ro
    ports: ["127.0.0.1:8770:8770"]   # loopback-restricted publish — unchanged
```

- Drop `profiles: ["container"]`: `docker compose up -d` starts everything.
- `environment:` overrides `.env` values (e.g. `.env`'s `S3_ENDPOINT=http://localhost:9000`), so `.env` stays valid for a bare-host rollback.
- MinIO is **not** moved into compose during cutover (fewer moving parts; its data location is unknown). Follow-up T357 adopts it with its existing volume declared `external`.

## 2. Image (T349)

- `ENV PIPELINE_IN_CONTAINER=1`.
- cloudflared: pinned release `.deb` for `${TARGETARCH}` (arm64 on Apple Silicon) with a checksum check.
- `sqlite3` CLI (the runbook and ops use it inside the container).
- Fonts per the user's decision (§7 Q1): bundle open fonts and append them to each theme's stack *after* the Apple names, so host rendering is unchanged and the container falls through to them.
- Confirm the build and a real render inside the image (T347), then rewrite the Dockerfile's T206 comment to state the current fact.

## 3. Code (T348, senior-dev)

| Piece | Spec |
|---|---|
| `TUNNEL_TARGET` | new `Settings.tunnel_target` (default `http://localhost:9000`); `tunnel.py` uses it instead of the `LOCAL_TARGET` constant |
| storage guard | `config.in_container()` (exists or add: env `PIPELINE_IN_CONTAINER=="1"` or `/.dockerenv`). `fs_type(path)` reads `/proc/self/mountinfo`, picks the longest mount point containing the path's directory. In a container, `Database.connect` (rw) and the Telethon start refuse unless `fs_type` ∈ `LOCAL_FS = {"ext4", "xfs", "btrfs"}`, with a message naming D298 |
| lease | `src/pipeline/lease.py`: `WorkerLease(path=ROOT/"data"/"worker.lease")`, JSON `{owner_kind: host|container, instance_id, hostname, pid, heartbeat_at}`, written temp-then-rename. `acquire()` before Telethon/bot: fresh lease (< `LEASE_STALE_S`=120) held by another instance → exit non-zero, logged. Heartbeat `_periodic` every `LEASE_HEARTBEAT_S`=30: if the file names another instance → set `stop` (fencing). Release on clean shutdown. `python -m pipeline.lease --check` for the healthcheck |
| host fence | not in a container + a fresh lease with `owner_kind=container` → rw `Database.connect` refuses ("the pipeline runs in Docker; use `docker compose exec app …`"). Covers the host worker, host dashboard and every `scripts/*.py` |
| backups | repurpose T340's `snapshot.py`: `backup_job` = `write_snapshot(db_path, backup_dir/f"app-{UTC:%Y%m%dT%H%M}.db")` + keep newest `BACKUP_KEEP`=48. Settings `backup_dir` (`ROOT/data/backups`), `backup_interval_s` (3600; 0 disables). Remove `snapshot_path`, `dashboard_readonly`, the dashboard-snapshot wording. Writing a DELETE-journal file onto the bind mount is fine: one writer, nothing opens it across the boundary while live |
| constants | all names/numbers above as module constants; config only in `config.py` |

Tests: `fs_type` on a fake mountinfo (ext4 ok; virtiofs, fakeowner, fuse.grpcfuse, overlay refused; longest-prefix wins); lease acquire/refuse/stale-steal/fencing/release with a fake clock; host fence; backup prune keeps exactly N; `tunnel_target` reaches the spawn args. Mutation-test each guard.

## 4. Cutover runbook (T353, devops; needs the user's own go)

Expected downtime: a few minutes. Messages in that window are not lost:
Telegram queues bot updates for 24 h, and Telethon backfills up to
`BACKFILL_LIMIT` missed channel posts.

**Pre-flight (no downtime).**
1. T347–T352 are PASS, and the user has answered §7.
2. `docker compose build`; `docker volume create pipeline-state`.
3. Reachability from a throwaway container: `curl -s host.docker.internal:11434/api/tags`, `curl -sI host.docker.internal:9000/minio/health/live`, `curl -s searxng:8080`.
4. Host safety copy: `sqlite3 data/app.db ".backup data/backups/pre-docker-$(date +%Y%m%dT%H%M).db"`.
5. Pick a quiet moment: `sqlite3 data/app.db "select count(*) from items where status in ('approved','publishing')"` → 0.

**Cutover.**
1. Stop the host worker so launchd cannot restart it: `launchctl bootout gui/$(id -u)/com.aayush.newspipeline`. Stop any host dashboard. `pgrep -fl "pipeline|dashboard.py"` → nothing.
2. Freeze and measure: `sqlite3 data/app.db "PRAGMA wal_checkpoint(TRUNCATE); PRAGMA integrity_check; select count(*), max(id) from items; select count(*) from events; select count(*) from messages;"` → `ok`. Record the counts. Do the same `integrity_check` on `secrets/telegram.session`.
3. Consistent copies on the host (native locks): `mkdir -p data/cutover`, then `sqlite3 data/app.db ".backup data/cutover/app.db"` and `sqlite3 secrets/telegram.session ".backup data/cutover/telegram.session"`. Set `PRAGMA journal_mode=DELETE` on both copies and run `integrity_check`. `cp data/ig_token.json data/cutover/` if it exists. No `-wal`/`-shm` may exist in `data/cutover/`.
4. Seed the volume:
   `docker compose run --rm --no-deps -v "$PWD/data/cutover:/seed:ro" --entrypoint sh app -c 'cp /seed/* /app/state/ && sqlite3 /app/state/app.db "PRAGMA integrity_check; select count(*), max(id) from items; select count(*) from events; select count(*) from messages;"'`
   The counts must equal step 2. Compare `sha256sum` of the seed and the copy.
5. Fence the host copies, keeping them for rollback:
   - `mv data/app.db data/app.db.pre-docker-<ts>` (and any `-wal`/`-shm`);
   - `mv secrets/telegram.session secrets/telegram.session.pre-docker-<ts>`;
   - `mv ~/Library/LaunchAgents/com.aayush.newspipeline.plist ~/Library/LaunchAgents.disabled/`. A plist left in LaunchAgents is loaded again at the next login, and `bootout` does not persist.
6. `docker compose up -d`.
7. Verify (T354):
   - logs show the lease acquired by the container, Telethon connected with no login prompt, bot polling, and `tunnel origin published`;
   - a DM test post reaches `awaiting_approval`;
   - the dashboard on 127.0.0.1:8770 loads and an action works;
   - `docker compose exec app sqlite3 /app/state/app.db "PRAGMA integrity_check"` → ok;
   - a backup appears in `data/backups/` within the hour;
   - `.venv/bin/python scripts/requeue.py …` on the host refuses with one line and exit 1, no traceback: `the pipeline runs in Docker — use: docker compose exec app python scripts/requeue.py …` (every script that opens the DB does the same, via `lease.run_host_script`).

**Restart and recreate behaviour (the lease).**
- A graceful stop (`docker compose stop`, `down`) releases the lease, so the next `up` starts immediately.
- `docker compose restart app` and `docker start pipeline-app` take over at once: the new process sees a fresh lease from the same kind, hostname and pid namespace, which is its own predecessor.
- A `docker compose up --force-recreate` (new container, new hostname) within 120 s of an *unclean* death (OOM, `kill -9`, Docker Desktop crash) is refused until the old lease goes stale. The container exits non-zero and `restart: unless-stopped` retries, so it self-heals within about two minutes; do not delete the lease to speed it up while anything might still be running.
- Host-side liveness is clock-skew safe: the lease counts as live if either its `heartbeat_at` (VM clock) or the file's mtime (host side of the bind mount) is within 120 s. After a Mac sleep the VM clock can lag; the mtime keeps a live container fenced. A heartbeat more than 300 s in the future is treated as live and logged (`clock skew`); remove the file by hand only once you are sure the owner is gone.
- Keep the `*.pre-docker-*` fence files (`data/app.db.pre-docker-<ts>`, `secrets/telegram.session.pre-docker-<ts>`) until T355 retires launchd; they are the rollback path.

**Rollback (any time).**
1. `docker compose stop app dashboard`, then wait for `pipeline-app` to exit. The lease is released, or stale within 120 s.
2. Export:
   `docker compose run --rm --no-deps -v "$PWD/data/rollback:/out" --entrypoint sh app -c 'sqlite3 /app/state/app.db ".backup /out/app.db" && sqlite3 /app/state/telegram.session ".backup /out/telegram.session" && cp /app/state/ig_token.json /out/ 2>/dev/null; true'`
   This keeps every item made in Docker.
3. On the host:
   - run `integrity_check` on both exported files;
   - `mv data/rollback/app.db data/app.db`, `mv data/rollback/telegram.session secrets/telegram.session`, and put `ig_token.json` back in `data/`;
   - rewrite media paths back to the host location (the cutover rewrote them to `/app/`, D316): `sqlite3 data/app.db "UPDATE items SET raw_media_paths=replace(raw_media_paths,'/app/','/Users/aayushgour/Desktop/projects/telegram-automation/'), extracted=replace(extracted,'/app/','/Users/aayushgour/Desktop/projects/telegram-automation/'), research=replace(research,'/app/','/Users/aayushgour/Desktop/projects/telegram-automation/'), slides=replace(slides,'/app/','/Users/aayushgour/Desktop/projects/telegram-automation/'), rendered_paths=replace(rendered_paths,'/app/','/Users/aayushgour/Desktop/projects/telegram-automation/'); PRAGMA integrity_check;"` (unnecessary once T361 stores relative paths);
   - `rm data/worker.lease` only if it is still there and still fresh (a stale lease is ignored on its own; a leftover fresh one would make the host worker refuse to start).
4. `mv ~/Library/LaunchAgents.disabled/com.aayush.newspipeline.plist ~/Library/LaunchAgents/`, then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aayush.newspipeline.plist`.
5. If the volume itself is damaged: restore the newest `data/backups/app-*.db`, or `pre-docker-*`. Items after that backup are lost, which is why the backups are hourly.

**Retire launchd (T355), after 7 stable days.** Move the plist and `install-launchd.sh` to `deploy/legacy/` and update setup/usage/README. Delete the `*.pre-docker-*` files only on the user's say-so.

## 5. Telethon and bot exclusivity — how it is guaranteed

- **Physically:** after cutover step 5 the only session file is in the volume, which no host process can open.
- **Logically:** the lease is acquired before Telethon and the bot start, by any worker, host or container.
- **Rollback order:** the container stops before the session is exported, and the host only starts after that.
- **Testing:** never start a container worker with a copy of the live session, or the live bot token, while the host worker runs. Two clients on one auth key risk Telegram revoking it, and two bot pollers fight over `getUpdates`. Pre-cutover tests run with a scratch env (no real session or token) or the dashboard only.

## 6. Acceptance criteria

- **T347 (spike):**
  - two containers on one named volume: a writer at ~50 tx/s plus a reader, 10 min, with `--force-recreate` of the reader 20× mid-run → `integrity_check` ok and no SQLITE_CORRUPT or IOERR;
  - the same with the dashboard image doing real page loads;
  - `/proc/self/mountinfo` fs type recorded for a named volume (expect ext4) and a bind mount (expect virtiofs/fakeowner);
  - a container reaches host Ollama bound to 127.0.0.1 and MinIO :9000;
  - image builds on arm64; `cloudflared --version`; one real deck renders in the image (PNG attached).
- **T350 (render parity):** 3 real decks (sans, serif and mono-heavy themes) rendered on the host vs in the image. Side-by-side PNGs, shrink/overflow differences, any Recompose difference.
- **T352 (pre-cutover):**
  - guards refuse: DB on a bind mount, DB in the overlay, a host rw connect while a container lease is fresh, and a second worker container;
  - a stale lease is taken over;
  - concurrent dashboard approve and worker transitions stay consistent;
  - backup rotation works;
  - full `pytest` and `ruff` pass.
  - All of this on a scratch volume with a DB **copy** and no live Telegram credentials.
- **T354 (post-cutover):** §4 step 7, pasted output.

## 7. User decisions

1. **Fonts.** Slides rendered in Linux lose the Apple fonts every theme names. Choose one:
   - (a) bundle open fonts (e.g. Inter / Liberation / Gelasio / JetBrains Mono) and accept a visible look change on the public account, judged from T350's side-by-sides;
   - (b) a different font set you pick.
   Without a decision, the slides render in Chromium's default Linux fonts. Not recommended.
2. **Docker Desktop at login.** Turn on "Start Docker Desktop when you sign in". It replaces launchd's `RunAtLoad`. Give the VM enough memory for Chromium and the worker; ≥ 4 GB suggested.
3. **Go-ahead for the cutover window** (production downtime, plus moving the live DB and session). This must be your own message.
4. **TZ** for the container logs: set `TZ` in `.env`, otherwise logs are in UTC.

## 8. If T347 fails

Do not share the volume between two containers. Serve the dashboard from
inside the worker process: `build()` mounted on the worker's event loop as one
more task, port 8770 published from `app`, one `Database`. Cost: a dashboard
crash or restart takes the worker with it, and `scripts/dashboard.py` becomes a
module that `__main__` imports. Every other part of this design is unchanged.
