"""Mission control — every job, its result, its log, and a chat per item.

Telegram is a good surface for approving one item and a bad one for seeing the
queue: what is waiting on you, what failed and why, what a deck actually looks
like, and what was said about it. This is that view, and it writes into the
same conversation the bot uses, so a question asked in Telegram can be answered
here and vice versa.

Actions go through the same ``pipeline`` functions as the bot and the CLI, so
there is one implementation of each rather than a second that drifts.

SECURITY — read before changing the bind address. Anything that can reach this
server can approve a post, and approval is the gate to publishing. There is no
login. It binds to 127.0.0.1 only, which is why it is safe on a shared network
and would stop being safe the moment it listened on 0.0.0.0.

    ./.venv/bin/python scripts/dashboard.py          # http://127.0.0.1:8770
    ./.venv/bin/python scripts/dashboard.py --port 9000
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiohttp import web  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline.config import Settings  # noqa: E402
from pipeline.db import Database  # noqa: E402
from pipeline.intake.manual import TooThin, queue_request  # noqa: E402
from pipeline.models import TERMINAL, Status  # noqa: E402
from pipeline.requeue import TARGETS, requeue  # noqa: E402

#: Bound to loopback because approving an item publishes it. See module docstring.
HOST = "127.0.0.1"
DEFAULT_PORT = 8770

#: Uncapped. Reading half a log to work out why an item failed is the same
#: problem as researching half a request.
LOG_TAIL_BYTES = 0
ITEM_LOG_LINES = 0

LOG_PATH = ROOT / "data" / "pipeline.log"

#: Rows before the board offers the rest. The queue runs to hundreds, and a
#: single scroll of them is not navigation.
PAGE_SIZE = 25

#: Seconds between refreshes, and only while something is mid-flight. A page
#: that reloads under you while you are reading a failed item is hostile.
REFRESH_S = 20

#: Lane key, label, tooltip, and the statuses in it — ordered by how much of
#: your attention each deserves.
LANES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("attn", "waiting on you", "needs a decision from you",
     (Status.AWAITING_APPROVAL.value, Status.NEEDS_INPUT.value)),
    ("work", "running", "the pipeline is working on these",
     (Status.INGESTED.value, Status.EXTRACTED.value, Status.TRIAGED.value,
      Status.RESEARCHED.value, Status.SYNTHESIZED.value, Status.COMPOSED.value,
      Status.RENDERED.value, Status.APPROVED.value, Status.PUBLISHING.value)),
    ("good", "published", "live on Instagram", (Status.PUBLISHED.value,)),
    ("bad", "stopped", "failed, rejected or dropped",
     (Status.FAILED.value, Status.REJECTED.value, Status.DROPPED.value)),
]

LANE_BY_KEY = {key: (label, blurb, statuses) for key, label, blurb, statuses in LANES}
LANE_OF = {s: key for key, _, _, statuses in LANES for s in statuses}
RUNNING = set(LANE_BY_KEY["work"][2])

CSS = """
*{box-sizing:border-box;margin:0}
:root{
  --bg:#0d1014; --raise:#141920; --sink:#0a0d11; --line:#1f2630;
  --ink:#e8ecf2; --dim:#7d8899; --faint:#4c5563;
  --attn:#e0932f; --work:#5b9ad6; --good:#46a877; --bad:#cf5f56;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
  --r:9px;
}
@media (prefers-color-scheme: light){
  :root:not([data-theme="dark"]){
    --bg:#f4f6f8; --raise:#fff; --sink:#eceff3; --line:#dfe4ea;
    --ink:#11151b; --dim:#5a6472; --faint:#98a1ad;
  }
}
body{background:var(--bg);color:var(--ink);font:15px/1.55 var(--sans);
     -webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}

header{position:sticky;top:0;z-index:20;border-bottom:1px solid var(--line);
  background:var(--bg)}
.bar{max-width:1140px;margin:0 auto;padding:11px 20px;display:flex;gap:14px;
  align-items:center}
.brand{font-weight:650;letter-spacing:-.015em;white-space:nowrap}
.brand span{color:var(--dim);font-weight:400}
.grow{flex:1}
.search{display:flex;gap:6px}
.search input{width:200px}
nav{max-width:1140px;margin:0 auto;padding:0 20px 10px;display:flex;gap:6px;
  flex-wrap:wrap}
.tab{display:flex;align-items:center;gap:7px;padding:5px 11px;border-radius:99px;
  border:1px solid var(--line);font-size:13px;color:var(--dim);white-space:nowrap}
.tab:hover{border-color:var(--faint);color:var(--ink)}
.tab.on{background:var(--raise);border-color:var(--faint);color:var(--ink);
  font-weight:550}
.tab b{font-variant-numeric:tabular-nums;font-weight:650}
.dot{width:7px;height:7px;border-radius:99px;flex:none}
.d-attn{background:var(--attn)} .d-work{background:var(--work)}
.d-good{background:var(--good)} .d-bad{background:var(--bad)}

main{max-width:1140px;margin:0 auto;padding:22px 20px 80px}

input,textarea,select,button{font:inherit;color:var(--ink);border-radius:7px;
  border:1px solid var(--line);background:var(--sink);padding:7px 10px}
textarea{resize:vertical;width:100%;line-height:1.5}
input:focus,textarea:focus,select:focus,button:focus-visible{
  outline:2px solid var(--work);outline-offset:-1px}
button{background:var(--raise);cursor:pointer;font-size:13.5px;padding:7px 13px;
  white-space:nowrap}
button:hover{border-color:var(--faint)}
button.go{background:var(--good);border-color:transparent;color:#fff;font-weight:550}
button.no{background:transparent;border-color:var(--bad);color:var(--bad)}
button.no:hover{background:var(--bad);color:#fff}
form{display:contents}

.card{background:var(--raise);border:1px solid var(--line);border-radius:var(--r);
  padding:16px 18px;margin-bottom:14px}
.card>h2{font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--dim);font-weight:600;margin-bottom:11px}
.compose{display:flex;gap:9px;align-items:flex-start}
.compose textarea{min-height:52px}
.warn{color:var(--attn);font-size:13.5px;margin-top:9px}

.rows{display:flex;flex-direction:column;gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.row{display:grid;grid-template-columns:auto 54px 1fr auto;gap:13px;
  align-items:center;padding:11px 15px;background:var(--raise)}
.row:hover{background:var(--sink)}
.rid{font:600 12.5px/1 var(--mono);color:var(--dim);
  font-variant-numeric:tabular-nums}
.txt{min-width:0}
.title{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta{font-size:12px;color:var(--dim);margin-top:3px;display:flex;gap:9px;
  flex-wrap:wrap;align-items:center}
.acts{display:flex;gap:6px;opacity:.5;align-items:center}
.bump{display:flex;gap:4px;align-items:center}
.pos{font:650 13px/1 var(--mono);color:var(--dim);min-width:22px;text-align:right;
  font-variant-numeric:tabular-nums}
.bump button{padding:1px 7px;font-size:11px;line-height:1.35;border-radius:5px}
.prio{font:600 11px/1 var(--mono);color:var(--attn);border:1px solid var(--attn);
  border-radius:99px;padding:2px 7px}
.row:hover .acts,.acts:focus-within{opacity:1}
/* ---- stage timeline (click a stage to requeue there) ---- */
.tl-head{margin:15px 0 9px;font:600 11px/1 var(--sans);letter-spacing:.09em;
  text-transform:uppercase;color:var(--dim)}
.timeline{display:flex;flex-wrap:wrap;gap:0;align-items:flex-start;
  overflow-x:auto;padding-bottom:3px}
.tl-step{display:flex;margin:0;position:relative;flex:0 0 auto}
/* The connecting rail. Each step draws a half-segment to its left and one to
   its right, meeting at the dot centres — a single full-width segment per step
   overshoots the last dot by half a label, because the dot is centred and the
   labels set the step widths. */
.tl-step::before,.tl-step::after{content:"";position:absolute;top:9px;
  height:2px;width:50%;background:var(--line);z-index:0}
.tl-step::before{left:0}
.tl-step::after{left:50%}
.tl-step:first-child::before,.tl-step:last-child::after{display:none}
.tl-node{display:flex;flex-direction:column;align-items:center;gap:6px;
  background:none;border:0;padding:0 13px;cursor:pointer;position:relative;
  z-index:1;color:var(--faint);border-radius:7px}
.tl-node.fixed{cursor:default}
.tl-dot{width:13px;height:13px;border-radius:50%;background:var(--bg);
  border:2px solid var(--line);transition:transform .12s,border-color .12s}
.tl-lbl{font:500 10.5px/1.2 var(--sans);letter-spacing:.03em;white-space:nowrap}
.tl-node.done{color:var(--dim)}
.tl-node.done .tl-dot{background:var(--good);border-color:var(--good)}
.tl-node.now{color:var(--ink)}
.tl-node.now .tl-lbl{font-weight:700}
.tl-node.now .tl-dot{background:var(--work);border-color:var(--work);
  box-shadow:0 0 0 4px color-mix(in srgb,var(--work) 22%,transparent)}
.tl-node.todo .tl-dot{background:var(--bg);border-color:var(--line)}
.tl-node.off .tl-dot{background:var(--bg);border-color:var(--faint)}
button.tl-node:hover{color:var(--ink);background:var(--sink)}
button.tl-node:hover .tl-dot{transform:scale(1.32);border-color:var(--attn)}
button.tl-node:focus-visible{outline:2px solid var(--work);outline-offset:2px}
/* ---- already-on-the-account warning ---- */
.warn-live{display:flex;flex-direction:column;gap:3px;margin-bottom:11px;
  padding:9px 12px;border-radius:7px;border:1px solid var(--attn);
  background:color-mix(in srgb,var(--attn) 12%,transparent)}
.warn-live b{color:var(--attn);font:600 12.5px/1.4 var(--sans)}
.warn-live span{color:var(--dim);font:12px/1.45 var(--sans)}
.warn-live code{font:12px/1 var(--mono);color:var(--ink)}
@media (prefers-reduced-motion:reduce){
  .tl-dot{transition:none}
  button.tl-node:hover .tl-dot{transform:none}
}
.err{color:var(--bad);font:12px/1.45 var(--mono);margin-top:4px;
  white-space:pre-wrap}
.thumb{width:32px;height:40px;object-fit:cover;border-radius:4px;
  border:1px solid var(--line);background:var(--sink);flex:none;display:block}
.more{display:block;text-align:center;padding:11px;color:var(--dim);
  font-size:13.5px;background:var(--raise)}
.more:hover{color:var(--ink)}
.empty{padding:26px;text-align:center;color:var(--dim);background:var(--raise);
  border:1px dashed var(--line);border-radius:var(--r)}

.crumb{display:flex;align-items:center;gap:12px;margin-bottom:14px;
  font-size:13px;color:var(--dim);flex-wrap:wrap}
.crumb a:hover{color:var(--ink)}
.head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:10px}
.head h1{font-size:19px;font-weight:600;letter-spacing:-.015em}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;
  color:var(--dim);border:1px solid var(--line);border-radius:99px;padding:2px 9px}
.req{font-size:15px;line-height:1.6;white-space:pre-wrap}
.bar-acts{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.slides{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));
  gap:10px}
.slides img{width:100%;border-radius:6px;border:1px solid var(--line);
  background:#000;display:block;transition:transform .12s,border-color .12s}
.slides a:hover img{transform:scale(1.03);border-color:var(--faint)}
pre{font:12px/1.55 var(--mono);white-space:pre-wrap;word-break:break-word;
  background:var(--sink);border:1px solid var(--line);border-radius:7px;
  padding:12px;max-height:65vh;overflow:auto}
summary{cursor:pointer;font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--dim);font-weight:600}
summary:hover{color:var(--ink)}
details[open]>summary{margin-bottom:11px}

.chat{display:flex;flex-direction:column;gap:9px;margin-bottom:13px}
.msg{max-width:74%;padding:9px 13px;border-radius:13px;font-size:14px;
  white-space:pre-wrap;word-break:break-word;line-height:1.5}
.msg .who{display:block;font:600 10px/1 var(--sans);letter-spacing:.1em;
  text-transform:uppercase;opacity:.6;margin-bottom:5px}
.from-pipeline{align-self:flex-start;background:var(--sink);
  border:1px solid var(--line);border-bottom-left-radius:4px}
.from-operator{align-self:flex-end;background:var(--work);color:#fff;
  border-bottom-right-radius:4px}
.hint{color:var(--dim);font-size:13px;margin-top:9px}

@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:640px){
  .row{grid-template-columns:auto 1fr;row-gap:8px}
  .acts{grid-column:1/-1;opacity:1}
  .search input{width:120px}
}
"""


# --------------------------------------------------------------- helpers


def esc(value, limit: int = 0) -> str:
    text = "" if value is None else str(value)
    if limit and len(text) > limit:
        text = text[:limit] + "…"
    return html.escape(text)


def ago(iso: str | None) -> str:
    """Relative time — "which of these is stale" is the question being asked."""
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    for cutoff, div, unit in ((90, 1, "s"), (5400, 60, "m"), (172800, 3600, "h")):
        if seconds < cutoff:
            return f"{max(0, int(seconds // div))}{unit} ago"
    return f"{int(seconds // 86400)}d ago"


def page(title: str, body: str, counts: dict, active: str = "",
         query: str = "", refresh: bool = False) -> web.Response:
    tabs = [
        f"<a class='tab{' on' if active == 'queue' else ''}' href='/queue'>"
        f"<span class=dot style='background:var(--work)'></span>run order</a>",
        f"<a class='tab{' on' if not active else ''}' href='/'>"
        f"<span class=dot style='background:var(--faint)'></span>all"
        f"<b>{sum(counts.values())}</b></a>"
    ]
    for key, label, blurb, statuses in LANES:
        tabs.append(
            f"<a class='tab{' on' if active == key else ''}' href='/?lane={key}'"
            f" title='{esc(blurb)}'><span class='dot d-{key}'></span>{esc(label)}"
            f"<b>{sum(counts.get(s, 0) for s in statuses)}</b></a>"
        )
    meta = f"<meta http-equiv=refresh content={REFRESH_S}>" if refresh else ""
    return web.Response(content_type="text/html", text=(
        f"<!doctype html><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>{meta}"
        f"<title>{esc(title)}</title><style>{CSS}</style>"
        f"<header><div class=bar>"
        f"<a class=brand href='/'>Mission control<span> · pipeline</span></a>"
        f"<span class=grow></span>"
        f"<form class=search method=get action='/'>"
        f"<input name=q value='{esc(query)}' placeholder='search or #id'"
        f" aria-label=search><button>find</button></form>"
        f"<a class=tab href='/logs'>logs</a>"
        f"</div><nav>{''.join(tabs)}</nav></header><main>{body}</main>"
    ))


def action(item_id: int, verb: str, label: str, cls: str = "") -> str:
    return (f"<form method=post action='/item/{item_id}/{verb}'>"
            f"<button class='{cls}'>{label}</button></form>")


def status_badge(status: str) -> str:
    return (f"<span class=badge><span class='dot d-{LANE_OF.get(status, '')}'>"
            f"</span>{esc(status)}</span>")


def thumb(item_id: int, paths: list) -> str:
    if not paths:
        return "<span class=thumb></span>"
    name = Path(str(paths[0])).name
    return (f"<img class=thumb src='/media/{item_id}/{esc(name)}' alt=''"
            f" loading=lazy>")


# ----------------------------------------------------------------- board


async def index(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    counts = await db.status_counts()
    lane = request.query.get("lane", "")
    query = (request.query.get("q") or "").strip()

    # "#91" and "91" jump straight to the item rather than searching for it.
    if query.lstrip("#").isdigit():
        raise web.HTTPFound(f"/item/{int(query.lstrip('#'))}")

    sql = ("SELECT id, source, status, intent, raw_text, last_error,"
           " status_updated_at, rendered_paths, priority FROM items")
    where, params = [], []
    if lane in LANE_BY_KEY:
        statuses = LANE_BY_KEY[lane][2]
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params += list(statuses)
    if query:
        where.append("raw_text LIKE ?")
        params.append(f"%{query}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = await db.conn.execute_fetchall(sql + " ORDER BY id DESC", params)

    notice = request.query.get("thin")
    body = [
        "<div class=card><h2>new post</h2>"
        "<form method=post action='/new' class=compose>"
        "<textarea name=text required placeholder='A headline, a link, a "
        "paragraph, or a request like &quot;10 github repos for rust&quot;…'"
        "></textarea><button class=go>research it</button></form>"
        + (f"<p class=warn>{esc(notice)}</p>" if notice else "")
        + "</div>"
    ]

    if query or lane:
        label = LANE_BY_KEY[lane][0] if lane in LANE_BY_KEY else "all"
        line = f"<b>{len(rows)}</b> in {esc(label)}"
        if query:
            line += f" matching “{esc(query)}”"
        body.append(f"<p class=crumb>{line}<span class=grow></span>"
                    f"<a href='/'>clear</a></p>")

    if not rows:
        body.append("<div class=empty>nothing here</div>")
        return page("Mission control", "".join(body), counts, lane, query)

    shown = rows if request.query.get("all") == "1" else rows[:PAGE_SIZE]
    cards = []
    for r in shown:
        acts = []
        if r["status"] == Status.AWAITING_APPROVAL.value:
            acts.append(action(r["id"], "approve", "approve", "go"))
            acts.append(action(r["id"], "reject", "reject", "no"))
        if r["status"] not in {s.value for s in TERMINAL}:
            acts.append(action(r["id"], "drop", "drop"))
            if r["status"] in RUNNING:
                # Ordering lives in one place — the run order view, where the
                # positions are visible. Two ways to reorder invites them to
                # disagree.
                acts.append(f"<a class=tab href='/queue'>order</a>")

        try:
            paths = json.loads(r["rendered_paths"] or "[]")
        except (TypeError, json.JSONDecodeError):
            paths = []

        meta = [status_badge(r["status"]), esc(r["source"])]
        if r["priority"]:
            # Only when set, so an untouched queue stays uncluttered.
            meta.insert(0, f"<span class=prio>prio {r['priority']:+d}</span>")
        if r["intent"]:
            meta.append(esc(r["intent"]))
        if paths:
            meta.append(f"{len(paths)} slides")
        when = ago(r["status_updated_at"])
        if when:
            meta.append(when)

        title = (r["raw_text"] or "").strip().replace("\n", " ") or "(no text)"
        err = f"<div class=err>{esc(r['last_error'])}</div>" if r["last_error"] else ""
        cards.append(
            f"<div class=row>{thumb(r['id'], paths)}"
            f"<a class=rid href='/item/{r['id']}'>#{r['id']}</a>"
            f"<div class=txt><a class=title href='/item/{r['id']}'>{esc(title)}</a>"
            f"<div class=meta>{''.join(f'<span>{m}</span>' for m in meta)}</div>"
            f"{err}</div><div class=acts>{''.join(acts)}</div></div>"
        )

    if len(rows) > len(shown):
        params = {k: v for k, v in request.query.items() if k != "all"}
        params["all"] = "1"
        cards.append(f"<a class=more href='/?{urlencode(params)}'>"
                     f"show all {len(rows)}</a>")

    body.append(f"<div class=rows>{''.join(cards)}</div>")
    live = any(r["status"] in RUNNING for r in rows)
    return page("Mission control", "".join(body), counts, lane, query, refresh=live)


async def queue(request: web.Request) -> web.Response:
    """The claimable items, in the order the worker will take them."""
    db: Database = request.app["db"]
    counts = await db.status_counts()
    items = await db.queue_order(sorted(RUNNING))

    if not items:
        return page("Run order", "<div class=empty>nothing queued — the "
                    "pipeline is idle</div>", counts, "queue")

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for position, item in enumerate(items):
        held = bool(item.next_attempt_at and item.next_attempt_at > now)

        moves = []
        if position > 0:
            moves.append(f"<form method=post action='/queue/{item.id}/up'>"
                         f"<button title='run sooner'>▲</button></form>")
        if position < len(items) - 1:
            moves.append(f"<form method=post action='/queue/{item.id}/down'>"
                         f"<button title='run later'>▼</button></form>")
        if position > 0:
            moves.append(f"<form method=post action='/queue/{item.id}/top'>"
                         f"<button title='run next'>top</button></form>")

        meta = [status_badge(item.status), esc(item.source)]
        if held:
            # Otherwise the list claims an order the worker will not follow.
            meta.append("<span class=prio>backing off</span>")
        when = ago(item.status_updated_at)
        if when:
            meta.append(when)

        title = (item.raw_text or "").strip().replace("\n", " ") or "(no text)"
        rows.append(
            f"<div class=row><span class=pos>{position + 1}</span>"
            f"<a class=rid href='/item/{item.id}'>#{item.id}</a>"
            f"<div class=txt><a class=title href='/item/{item.id}'>{esc(title)}"
            f"</a><div class=meta>"
            f"{''.join(f'<span>{m}</span>' for m in meta)}</div></div>"
            f"<div class=acts><span class=bump>{''.join(moves)}</span></div></div>"
        )

    body = (f"<p class=crumb><b>{len(items)}</b> queued, next first"
            f"<span class=grow></span>"
            f"<span>reordering rewrites the run order immediately</span></p>"
            f"<div class=rows>{''.join(rows)}</div>")
    return page("Run order", body, counts, "queue", refresh=True)


async def move(request: web.Request) -> web.Response:
    """Move one item within the run order."""
    db: Database = request.app["db"]
    item_id = int(request.match_info["id"])
    where = request.match_info["where"]

    order = [i.id for i in await db.queue_order(sorted(RUNNING))]
    if item_id not in order:
        raise web.HTTPFound("/queue")

    at = order.index(item_id)
    order.pop(at)
    if where == "up":
        order.insert(max(0, at - 1), item_id)
    elif where == "down":
        order.insert(min(len(order), at + 1), item_id)
    elif where == "top":
        order.insert(0, item_id)
    else:
        raise web.HTTPBadRequest(text=f"unknown move {where}")

    await db.reorder(order)
    raise web.HTTPFound("/queue")


# ---------------------------------------------------------------- detail


def _log_files() -> list[Path]:
    """The live log plus its rotated backups, oldest first.

    RotatingFileHandler names them pipeline.log.1 … .5, where .1 is the most
    recent backup — so reverse-sorting the suffixes puts them in time order.

    Only a purely numeric suffix is one of those backups. The glob also catches
    anything else parked beside the log — pipeline.log.archive-20260913, say —
    and feeding that to int() took the whole page down with a 500 rather than
    skipping one file.
    """
    backups = sorted(
        (p for p in LOG_PATH.parent.glob(f"{LOG_PATH.name}.*")
         if p.suffix[1:].isdigit()),
        key=lambda p: int(p.suffix[1:]), reverse=True,
    )
    return [p for p in [*backups, LOG_PATH] if p.exists()]


def _log_tail() -> str:
    text = "".join(
        p.read_text(encoding="utf-8", errors="replace") for p in _log_files()
    )
    if LOG_TAIL_BYTES:
        text = text[-LOG_TAIL_BYTES:]
    return text


def _newest_first(text: str) -> str:
    """Reverse the log for display.

    The question this page answers is "what just happened", and 25,000 lines
    oldest-first buried it — a DRY_RUN warning from eight days earlier read as
    the current state.
    """
    return "\n".join(reversed(text.splitlines()))


def item_log(item_id: int) -> str:
    if not _log_files():
        return ""
    needle = re.compile(rf"\bitem {item_id}\b")
    lines = [ln for ln in _log_tail().splitlines() if needle.search(ln)]
    if ITEM_LOG_LINES:
        lines = lines[-ITEM_LOG_LINES:]
    # Newest first here too, so the most recent attempt is what you read.
    return "\n".join(reversed(lines))


#: The pipeline drawn as a straight line. Includes the two statuses that are
#: NOT requeue targets — the human gate and the terminal state — because a
#: timeline that omitted them would not match the path the item actually took.
#: Only the stages in TARGETS are clickable; the other two render as markers.
PIPELINE = [
    "ingested", "extracted", "triaged", "researched", "synthesized",
    "composed", "rendered", "awaiting_approval", "approved", "publishing",
    "published",
]


def timeline_control(item) -> str:
    """The pipeline as clickable stages — click one to requeue there.

    Replaces a dropdown of stage names. The dropdown gave no sense of where the
    item was or what "requeue to composed" would actually throw away; the line
    shows both, because every stage after the one you click is work that gets
    cleared.
    """
    here = PIPELINE.index(item.status) if item.status in PIPELINE else None
    nodes = []
    for index, stage in enumerate(PIPELINE):
        if here is None:
            state = "off"          # dropped/failed/needs_input — not on the line
        elif index < here:
            state = "done"
        elif index == here:
            state = "now"
        else:
            state = "todo"
        label = stage.replace("_", " ")
        if stage in TARGETS:
            hint = (f"requeue to {stage} — clears {stage} onward"
                    if state != "now" else f"re-run from {stage}")
            nodes.append(
                f"<form method=post action='/item/{item.id}/requeue' class=tl-step>"
                f"<input type=hidden name=to value='{stage}'>"
                f"<button class='tl-node {state}' title='{esc(hint)}'>"
                f"<span class=tl-dot></span><span class=tl-lbl>{esc(label)}</span>"
                f"</button></form>")
        else:
            nodes.append(
                f"<div class=tl-step><div class='tl-node {state} fixed' "
                f"title='{esc(label)} — not a requeue target'>"
                f"<span class=tl-dot></span><span class=tl-lbl>{esc(label)}</span>"
                f"</div></div>")
    return f"<div class=timeline>{''.join(nodes)}</div>"


def published_warning(item) -> str:
    """Say plainly when an item is already on the account.

    ig_post_id is cleared by a requeue (it is the double-post guard), so
    without this the dashboard shows a requeued item as though it had never
    been posted — which is how a second carousel gets approved onto a live
    account by someone with no way to know.
    """
    history = item.publish_log or []
    if not history:
        return ""
    last = history[-1]
    when = str(last.get("at", ""))[:19].replace("T", " ")
    times = (f"{len(history)} times" if len(history) > 1 else "once")
    return (f"<div class=warn-live><b>already published {times}</b>"
            f"<span>last as <code>{esc(str(last.get('ig_post_id', '?')))}</code>"
            f" at {esc(when)} UTC — approving again puts another carousel "
            f"on the account</span></div>")


def chat_html(messages: list[dict], item) -> str:
    bubbles = []
    for message in messages:
        role = message["role"]
        who = "pipeline" if role == "pipeline" else "you"
        surface = f" · {message['surface']}" if message.get("surface") else ""
        stamp = ago(message.get("at"))
        bubbles.append(
            f"<div class='msg from-{esc(role)}'><span class=who>"
            f"{esc(who)}{esc(surface)}{f' · {esc(stamp)}' if stamp else ''}"
            f"</span>{esc(message['text'])}</div>"
        )
    if not bubbles:
        bubbles.append("<p class=hint>no messages yet</p>")

    if item.status == Status.NEEDS_INPUT.value:
        hint = "Waiting on you — your message becomes the answer."
        placeholder = "Answer the question…"
    elif item.status == Status.AWAITING_APPROVAL.value:
        hint = "Your message becomes a revision note and the deck is recomposed."
        placeholder = "What should change…"
    else:
        hint = ("Nothing is waiting on you here. A message is recorded on the "
                "thread but changes nothing until it is waiting again.")
        placeholder = "Leave a note…"

    return (
        f"<div class=chat>{''.join(bubbles)}</div>"
        f"<form method=post action='/item/{item.id}/say' class=compose>"
        f"<textarea name=text placeholder='{esc(placeholder)}' required></textarea>"
        f"<button class=go>send</button></form>"
        f"<p class=hint>{esc(hint)}</p>"
    )


async def detail(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    counts = await db.status_counts()
    item_id = int(request.match_info["id"])
    item = await db.get_item(item_id)
    if item is None:
        raise web.HTTPNotFound(text="no such item")

    rows = await db.conn.execute_fetchall(
        "SELECT (SELECT MAX(id) FROM items WHERE id < ?) AS prev,"
        "       (SELECT MIN(id) FROM items WHERE id > ?) AS next",
        (item_id, item_id))
    prev, nxt = rows[0]["prev"], rows[0]["next"]
    crumb = ["<a href='/'>← board</a><span class=grow></span>"]
    crumb.append(f"<a href='/item/{prev}'>← #{prev}</a>" if prev
                 else "<span style='opacity:.35'>← prev</span>")
    crumb.append(f"<a href='/item/{nxt}'>#{nxt} →</a>" if nxt
                 else "<span style='opacity:.35'>next →</span>")
    parts = [f"<p class=crumb>{''.join(crumb)}</p>"]

    meta = [status_badge(item.status),
            f"<span class=badge>{esc(item.source)}</span>",
            f"<span class=badge>{esc(item.intent or 'news')}</span>"]
    if item.theme:
        meta.append(f"<span class=badge>{esc(item.theme)}</span>")
    if item.priority:
        meta.append(f"<span class=prio>priority {item.priority:+d}</span>")
    when = ago(item.status_updated_at)
    if when:
        meta.append(f"<span class=badge>{esc(when)}</span>")
    parts.append(
        f"<div class=card><div class=head><h1>Item {item.id}</h1>"
        f"{''.join(meta)}</div><p class=req>{esc(item.raw_text)}</p>"
        + (f"<pre class=err style='margin-top:11px'>{esc(item.last_error)}</pre>"
           if item.last_error else "") + "</div>"
    )

    if item.rendered_paths:
        imgs = "".join(
            f"<a href='/media/{item.id}/{esc(Path(p).name)}' target=_blank>"
            f"<img src='/media/{item.id}/{esc(Path(p).name)}' alt='slide {n}'"
            f" loading=lazy></a>"
            for n, p in enumerate(item.rendered_paths, start=1))
        parts.append(f"<div class=card><h2>result · {len(item.rendered_paths)}"
                     f" slides</h2><div class=slides>{imgs}</div></div>")

    parts.append(f"<div class=card><h2>conversation</h2>"
                 f"{chat_html(await db.messages_for(item.id), item)}</div>")

    controls = []
    if item.status == Status.AWAITING_APPROVAL.value:
        controls.append(action(item.id, "approve", "approve", "go"))
        controls.append(action(item.id, "reject", "reject", "no"))
    if item.status in RUNNING:
        controls.append("<a class=tab href='/queue'>▲ run order</a>")
    controls.append(action(item.id, "drop", "drop", "no"))
    parts.append(f"<div class=card><h2>actions</h2>"
                 f"{published_warning(item)}"
                 f"<div class=bar-acts>{''.join(controls)}</div>"
                 f"<h2 class=tl-head>stage · click to requeue</h2>"
                 f"{timeline_control(item)}</div>")

    if item.caption:
        parts.append(f"<div class=card><h2>caption</h2>"
                     f"<pre>{esc(item.caption)}</pre></div>")

    events = await db.events_for(item.id)
    if events:
        timeline = "\n".join(
            f"{e['at'][:19].replace('T', ' ')}  {e['from_status'] or '—':>18}"
            f" → {e['to_status']}"
            + (f"   ({e['detail']})" if e.get("detail") else "")
            for e in events)
        parts.append(f"<div class=card><details><summary>timeline · "
                     f"{len(events)} transitions</summary>"
                     f"<pre>{esc(timeline)}</pre></details></div>")

    log = item_log(item.id)
    if log:
        parts.append(f"<div class=card><details><summary>log · "
                     f"{len(log.splitlines())} lines</summary>"
                     f"<pre>{esc(log)}</pre></details></div>")

    for label, value in (("research", item.research), ("slide json", item.slides),
                         ("clauses", item.clauses)):
        if value:
            parts.append(
                f"<div class=card><details><summary>{label} · {len(value)}"
                f" entries</summary><pre>"
                + esc(json.dumps(value, indent=1)) + "</pre></details></div>")
    if item.brief:
        parts.append(f"<div class=card><details><summary>brief</summary>"
                     f"<pre>{esc(item.brief)}</pre></details></div>")

    return page(f"Item {item.id}", "".join(parts), counts,
                LANE_OF.get(item.status, ""), refresh=item.status in RUNNING)


# --------------------------------------------------------------- actions


async def new(request: web.Request) -> web.Response:
    """Queue a request typed here, exactly as a DM would arrive."""
    db: Database = request.app["db"]
    settings: Settings = request.app["settings"]

    form = await request.post()
    try:
        item_id = await queue_request(db, settings, str(form.get("text") or ""))
    except TooThin as exc:
        # Back to the board with the reason rather than a bare error page —
        # the same rule the bot applies, worded the same way.
        raise web.HTTPFound(f"/?thin={quote(str(exc))}")

    raise web.HTTPFound(f"/item/{item_id}")


async def say(request: web.Request) -> web.Response:
    """Send a message to an item, doing whatever that item is waiting for."""
    db: Database = request.app["db"]
    item_id = int(request.match_info["id"])
    item = await db.get_item(item_id)
    if item is None:
        raise web.HTTPNotFound(text="no such item")

    form = await request.post()
    text = str(form.get("text") or "").strip()
    if not text:
        raise web.HTTPFound(f"/item/{item_id}")

    await db.add_message(item_id, "operator", text, "dashboard")

    if item.status == Status.NEEDS_INPUT.value:
        # Same effect as replying in Telegram: the answer resumes the item at
        # the stage that asked.
        resume = Status(item.resume_status or Status.TRIAGED)
        await db.transition(item_id, resume, {"answer": text, "question": None},
                            detail="dashboard answer")
    elif item.status == Status.AWAITING_APPROVAL.value:
        # SYNTHESIZED re-runs compose; COMPOSED would only re-render the slides
        # that are already there.
        await db.transition(item_id, Status.SYNTHESIZED, {"regen_note": text},
                            detail="dashboard revision")
    # Any other status: recorded on the thread, nothing to act on.

    raise web.HTTPFound(f"/item/{item_id}")


async def act(request: web.Request) -> web.Response:
    """Approve, reject, drop or requeue — mirroring the bot's own guards."""
    db: Database = request.app["db"]
    item_id = int(request.match_info["id"])
    verb = request.match_info["action"]

    item = await db.get_item(item_id)
    if item is None:
        raise web.HTTPNotFound(text="no such item")

    if verb == "requeue":
        form = await request.post()
        target = str(form.get("to") or Status.TRIAGED.value)
        if target not in TARGETS:
            raise web.HTTPBadRequest(text=f"{target} is not a requeueable stage")
        await requeue(db, item_id, Status(target), detail="dashboard requeue")
    elif verb in ("approve", "reject"):
        # The guard the bot applies: acting on an item that has moved on would
        # silently re-open a decision someone else already made.
        if item.status != Status.AWAITING_APPROVAL.value:
            raise web.HTTPConflict(
                text=f"item {item_id} is '{item.status}', not awaiting approval")
        await db.transition(
            item_id,
            Status.APPROVED if verb == "approve" else Status.REJECTED,
            detail="dashboard")
    elif verb in ("bump", "lower"):
        # Relative, not absolute: pressing it twice should move it twice, and
        # the operator is comparing this item against the queue, not naming a
        # number.
        step = 1 if verb == "bump" else -1
        await db.set_priority(item_id, item.priority + step)
    elif verb == "drop":
        await db.transition(item_id, Status.DROPPED, detail="dashboard")
    else:
        raise web.HTTPBadRequest(text=f"unknown action {verb}")

    raise web.HTTPFound(request.headers.get("Referer") or "/")


async def media(request: web.Request) -> web.FileResponse:
    """Serve a rendered slide, refusing anything outside the media directory."""
    settings: Settings = request.app["settings"]
    root = Path(settings.media_dir).resolve()
    target = (root / request.match_info["id"] / request.match_info["name"]).resolve()
    # A crafted name must not walk out of media_dir into the rest of the disk.
    if not target.is_relative_to(root) or not target.is_file():
        raise web.HTTPNotFound(text="no such file")
    return web.FileResponse(target)


async def logs(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    counts = await db.status_counts()
    if not _log_files():
        return page("logs", "<div class=empty>no log file yet</div>", counts)
    tail = _log_tail()
    files = ", ".join(p.name for p in _log_files())
    return page("logs", f"<div class=card><h2>newest first · "
                        f"{len(tail.splitlines())} lines · {files}</h2>"
                        f"<pre>{esc(_newest_first(tail))}</pre></div>", counts)


async def build(settings: Settings) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["db"] = await Database(settings.db_path).connect()
    app.add_routes([
        web.get("/", index),
        web.post("/new", new),
        web.get("/queue", queue),
        web.post(r"/queue/{id:\d+}/{where}", move),
        web.get("/logs", logs),
        web.get(r"/item/{id:\d+}", detail),
        web.post(r"/item/{id:\d+}/say", say),
        web.post(r"/item/{id:\d+}/{action}", act),
        web.get(r"/media/{id:\d+}/{name}", media),
    ])

    async def close_db(app: web.Application) -> None:
        await app["db"].close()

    app.on_cleanup.append(close_db)
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    print(f"mission control on http://{HOST}:{args.port}  (loopback only)")
    web.run_app(build(Settings.load()), host=HOST, port=args.port, print=None)
