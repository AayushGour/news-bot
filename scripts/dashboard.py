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
login. It binds to 127.0.0.1 only by default (``settings.dashboard_host``,
``DASHBOARD_HOST`` env var), which is why it is safe on a shared network and
would stop being safe the moment it listened on 0.0.0.0 with no other guard.

The only legitimate override is a container's own internal bind: Docker
cannot route a published port to a process bound to the container's own
loopback interface, so the containerized `dashboard` service in
docker-compose.yml sets DASHBOARD_HOST=0.0.0.0 *inside* the container while
publishing the port back to the host as `127.0.0.1:8770:8770` — the
host-side restriction, which is the actual security boundary, is unchanged.
A bare host run with no env var set still binds 127.0.0.1, exactly as before.

    ./.venv/bin/python scripts/dashboard.py          # http://127.0.0.1:8770
    ./.venv/bin/python scripts/dashboard.py --port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import math
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiohttp import web  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline.config import IG_MIN_PUBLISH_GAP_S, Settings  # noqa: E402
from pipeline.db import Database  # noqa: E402
from pipeline.lease import run_host_script  # noqa: E402
from pipeline.intake.manual import TOO_THIN, TooThin, queue_request  # noqa: E402
from pipeline.models import TERMINAL, Status  # noqa: E402
from pipeline.requeue import TARGETS, requeue  # noqa: E402
from pipeline.revision import copy as rcopy  # noqa: E402
from pipeline.revision.record import latest_run  # noqa: E402
from pipeline.revision.request import (  # noqa: E402
    ConfirmResult,
    Origin,
    Surface,
    confirm_revision,
    pending_confirm,
    request_revision,
)
from pipeline.stages.render import available_themes  # noqa: E402

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
     (Status.AWAITING_APPROVAL.value, Status.NEEDS_INPUT.value,
      Status.REVISION_CONFIRM.value)),
    ("work", "running", "the pipeline is working on these",
     (Status.INGESTED.value, Status.EXTRACTED.value, Status.TRIAGED.value,
      Status.RESEARCHED.value, Status.SYNTHESIZED.value, Status.COMPOSED.value,
      Status.RENDERED.value, Status.REVISION_REQUESTED.value, Status.APPROVED.value,
      Status.PUBLISHING.value)),
    ("good", "published", "live on Instagram", (Status.PUBLISHED.value,)),
    ("bad", "stopped", "failed, rejected or dropped",
     (Status.FAILED.value, Status.REJECTED.value, Status.DROPPED.value)),
]

LANE_BY_KEY = {key: (label, blurb, statuses) for key, label, blurb, statuses in LANES}
LANE_OF = {s: key for key, _, _, statuses in LANES for s in statuses}
RUNNING = set(LANE_BY_KEY["work"][2])

#: The stylesheet and the shell script live in scripts/dashboard_static/ and are read ONCE here,
#: then inlined into every page — no static route, nothing to cache or to serve (design.md,
#: Architecture). A missing file raises at import, so a broken checkout fails at startup rather
#: than rendering an unstyled page.
STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"


def _asset(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


CSS = _asset("dashboard.css")
JS = _asset("dashboard.js")

#: Own 24x24 stroke paths (no icon font, no CDN). Referenced as ``icon("queue")``.
ICON_PATHS = {
    "logo": '<path d="M4 7l8-4 8 4-8 4z"/><path d="M4 12l8 4 8-4M4 17l8 4 8-4"/>',
    "overview": '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/>'
                '<rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5h13l3.5 7v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z"/>',
    "play": '<circle cx="12" cy="12" r="9"/><path d="M10 8l6 4-6 4z"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/>',
    "alert": '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    "order": '<path d="M7 4v16M7 20l-3-3M7 20l3-3M17 20V4M17 4l-3 3M17 4l3 3"/>',
    "file-text": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>'
                 '<path d="M14 3v6h6M8 13h8M8 17h6"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "chevron-left": '<path d="M15 6l-6 6 6 6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.9-3M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.9 3M20 20v-4h-4"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
}
#: Emitted once per page; every icon is a <use> of one of these.
ICONS = (
    "<svg width=0 height=0 style='position:absolute' aria-hidden=true focusable=false><defs>"
    + "".join(f"<symbol id='i-{name}' viewBox='0 0 24 24'>{path}</symbol>"
              for name, path in ICON_PATHS.items())
    + "</defs></svg>"
)

#: Plain words for the operator. The raw status stays in ``data-status`` / ``title`` (grep-friendly,
#: and what tests hook on). One dict: every package reads this, none re-spells a status.
STATUS_LABEL = {
    Status.AWAITING_APPROVAL.value: "Ready to review",
    Status.NEEDS_INPUT.value: "Needs your answer",
    Status.REVISION_CONFIRM.value: "Confirm change",
    Status.REVISION_REQUESTED.value: "Rewriting",
    **{s.value: f"Working: {s.value}" for s in (
        Status.INGESTED, Status.EXTRACTED, Status.TRIAGED, Status.RESEARCHED,
        Status.SYNTHESIZED, Status.COMPOSED, Status.RENDERED)},
    Status.APPROVED.value: "Approved",
    Status.PUBLISHING.value: "Publishing",
    Status.PUBLISHED.value: "Published",
    Status.FAILED.value: "Failed",
    Status.REJECTED.value: "Rejected",
    Status.DROPPED.value: "Dropped",
}
SOURCE_LABEL = {"dm": "DM", "channel": "Channel post"}

#: Sidebar wording for each lane (the LANES labels stay the list headings).
LANE_NAV_LABEL = {"attn": "Review", "work": "Running", "good": "Published", "bad": "Stopped"}
LANE_ICON = {"attn": "inbox", "work": "play", "good": "check", "bad": "alert"}
OVERVIEW_HREF = "/"
ITEMS_HREF = "/items"
#: Page keys for ``page(active=...)`` that are not lanes. ``""`` is "All items".
ACTIVE_HOME, ACTIVE_ALL, ACTIVE_QUEUE, ACTIVE_LOGS = "home", "", "queue", "logs"

#: ``page(notice=<code>)`` maps a code to (kind, text). The query string only ever supplies the
#: code, never the words, so a crafted link cannot put text on the page.
NOTICES: dict[str, tuple[str, str]] = {
    "saved": ("ok", "Saved."),
    "sent": ("ok", "Sent."),
    "queued": ("ok", "Queued - the pipeline will pick it up."),
    "moved": ("ok", "Run order updated."),
    # UX-6: one code per verb the item/list/queue forms post. Words are ours; the query only
    # ever carries the code.
    "approved": ("ok", "Approved - publishing."),
    "rejected": ("ok", "Rejected - it will not be published."),
    "dropped": ("ok", "Dropped - it left the queue."),
    "requeued": ("ok", "Retrying - the pipeline will pick it up."),
    "regen": ("ok", "Rewrite requested - a new version is on its way."),
    "answered": ("ok", "Answer sent - the pipeline is resuming."),
    "priority": ("ok", "Priority updated."),
    "revision_yes": ("ok", "Confirmed - applying the change."),
    "revision_no": ("ok", "Kept as it was."),
}
#: verb in /item/{id}/{verb} -> NOTICES code shown after it succeeds.
VERB_NOTICE = {
    "approve": "approved", "reject": "rejected", "drop": "dropped", "requeue": "requeued",
    "regen": "regen", "caption": "saved", "bump": "priority", "lower": "priority",
    "revise_yes": "revision_yes", "revise_no": "revision_no",
}
#: Redirect targets we will rebuild from a Referer: a plain absolute path, nothing else. This is
#: what stops a forged Referer from sending the browser off-site (`//host`, `/\host`, schemes).
SAFE_PATH = re.compile(r"/[A-Za-z0-9._~%/-]*")
BRAND = "Mission control"
#: Runs before first paint (in <head>): marks the page as scripted and restores the sidebar rail.
PRE_PAINT_JS = (
    "document.documentElement.classList.add('js');"
    "try{if(localStorage.getItem('mc-rail')==='1')document.documentElement.classList.add('rail')}"
    "catch(e){}"
)


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


def icon(name: str, cls: str = "") -> str:
    """An inline icon from the sprite. Decorative (``aria-hidden``): the label next to it, or the
    ``aria-label`` on the control holding it, is what assistive tech reads."""
    return (f"<svg class='ico{f' {esc(cls)}' if cls else ''}' aria-hidden=true focusable=false>"
            f"<use href='#i-{esc(name)}'/></svg>")


def plural(n: int, noun: str) -> str:
    """'1 item', '0 items', '247 items'."""
    return f"{n} {noun}" + ("" if n == 1 else "s")


def status_label(status: str) -> str:
    return STATUS_LABEL.get(status, status)


def source_label(source: str | None) -> str:
    return SOURCE_LABEL.get(source or "", source or "")


_MD_EMPHASIS = re.compile(r"\*+|(?<!\w)_+(?=\S)|(?<=\S)_+(?!\w)")


def plain_title(text: str | None, limit: int = 0) -> str:
    """A row title from raw post text. Display only (never stored, never fed back): drops the
    markdown emphasis marks the source text carries and flattens newlines. Underscores inside a
    word (snake_case, URLs) are kept."""
    flat = " ".join(_MD_EMPHASIS.sub("", text or "").split())
    if limit and len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return flat or "(no text)"


def lane_counts(counts: dict) -> dict[str, int]:
    return {key: sum(counts.get(s, 0) for s in statuses) for key, _, _, statuses in LANES}


def nav_html(active: str, counts: dict) -> str:
    """The sidebar links, in one place so every page has the same nav in the same order."""
    lanes = lane_counts(counts)
    entries: list[tuple[str, str, str, str, int | None]] = [
        (ACTIVE_HOME, "Overview", OVERVIEW_HREF, "overview", None)]
    entries += [(key, LANE_NAV_LABEL[key], f"/?lane={key}", LANE_ICON[key], lanes[key])
                for key, *_ in LANES]
    entries += [(ACTIVE_ALL, "All items", ITEMS_HREF, "list", sum(counts.values())),
                (ACTIVE_QUEUE, "Run order", "/queue", "order", None),
                (ACTIVE_LOGS, "Logs", "/logs", "file-text", None)]
    out = []
    for key, label, href, ico, n in entries:
        here = key == active
        badge = f"<span class=cnt>{n}</span>" if n is not None else ""
        out.append(
            f"<li><a class=nav-link{' aria-current=page' if here else ''} href='{href}'"
            f" title='{esc(label)}'>{icon(ico)}<span class=lbl>{esc(label)}</span>{badge}</a></li>")
    return f"<ul>{''.join(out)}</ul>"


def notice_html(code: str | None) -> str:
    """A toast for a whitelisted code. Anything else renders nothing."""
    kind_text = NOTICES.get(code or "")
    if not kind_text:
        return ""
    kind, text = kind_text
    role = "alert" if kind == "err" else "status"
    return (f"<div class='toast {kind}' role={role}><span>{esc(text)}</span>"
            f"<button class='quiet icon' type=button aria-label='Dismiss'>{icon('x')}</button></div>")


def notice_param(request) -> str | None:
    """The ``?notice=`` code if (and only if) it is in NOTICES; never any other text."""
    code = request.query.get("notice")
    return code if code in NOTICES else None


def redirect_with_notice(path: str, code: str) -> web.HTTPFound:
    """``path`` (+ its query) with ``notice=<code>`` set, replacing any earlier notice."""
    parts = urlsplit(path)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "notice"]
    query.append(("notice", code))
    return web.HTTPFound(f"{parts.path}?{urlencode(query)}")


def back_with_notice(request: web.Request, code: str, fallback: str = "/") -> web.HTTPFound:
    """Back to the page the form was posted from (Referer's path + query only, never its host),
    or ``fallback``, with a toast. A Referer that is not a plain local path is ignored."""
    ref = urlsplit(request.headers.get("Referer") or "")
    path = ref.path if SAFE_PATH.fullmatch(ref.path or "") and not ref.path.startswith("//") else fallback
    return redirect_with_notice(f"{path}?{ref.query}" if path == ref.path and ref.query else path, code)


def page(title: str, body: str, counts: dict, active: str = "",
         query: str = "", refresh: bool = False, *, heading: str | None = None,
         subtitle: str | None = None, actions: str = "", notice: str | None = None,
         extra_head: str = "") -> web.Response:
    """Wrap ``body`` in the app shell.

    ``title`` is the document title; ``heading`` (default: ``title``) is the page's one <h1>.
    ``active`` picks the nav item (``""`` = All items, a lane key, ``queue``, ``home``, ``logs``).
    ``subtitle`` is escaped text; ``actions`` is trusted, already-built HTML for the top bar;
    ``notice`` is a NOTICES code, never text. ``refresh`` turns on the shell's draft-safe poll.
    """
    h1 = esc(title if heading is None else heading)
    doc_title = esc(title) if title == BRAND else f"{esc(title)} - {BRAND}"
    sub = f"<span class=sub>{esc(subtitle)}</span>" if subtitle else ""
    noscript = (f"<noscript><meta http-equiv=refresh content={REFRESH_S}></noscript>"
                if refresh else "")
    live = ("<span class=live id=live data-state=live><span class=live-dot></span>"
            "<span id=live-text>Live</span></span>" if refresh else "")
    refresh_attr = f" data-refresh={REFRESH_S * 1000}" if refresh else ""
    return web.Response(content_type="text/html", text=(
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{doc_title}</title><script>{PRE_PAINT_JS}</script>{noscript}"
        f"<style>{CSS}</style>{extra_head}</head><body{refresh_attr}>"
        f"<a class=skip href='#main'>Skip to content</a>{ICONS}"
        f"<div class=app><aside class=side id=side>"
        f"<div class=side-head><a class=brand href='/' title='{BRAND}'>{icon('logo')}"
        f"<span class=lbl>{BRAND}</span></a>"
        f"<button class='quiet icon side-close' id=side-close type=button"
        f" aria-label='Close menu'>{icon('x')}</button></div>"
        f"<nav aria-label=Main>{nav_html(active, counts)}</nav>"
        f"<div class=side-foot><button class='quiet collapse-btn' id=collapse-btn type=button"
        f" aria-pressed=false aria-label='Collapse sidebar' title='Collapse sidebar'>"
        f"{icon('chevron-left')}</button>"
        f"<p class=side-note>All times UTC</p></div></aside>"
        f"<div class=scrim id=scrim hidden></div>"
        f"<div class=col><header class=top>"
        f"<button class='quiet icon menu-btn' id=menu-btn type=button aria-label='Open menu'"
        f" aria-expanded=false aria-controls=side>{icon('menu')}</button>"
        f"<div class=top-title><h1>{h1}</h1>{sub}</div>"
        f"<button class='quiet icon search-btn' id=search-btn type=button aria-label='Search'"
        f" aria-expanded=false aria-controls=search>{icon('search')}</button>"
        f"<form class=search id=search role=search method=get action='/'>{icon('search')}"
        f"<label class=sr for=q>Search items</label>"
        f"<input id=q name=q value='{esc(query)}' placeholder='Search or #id'"
        f" autocomplete=off></form>"
        f"<div class=top-actions>{live}{actions}</div></header>"
        f"<main id=main tabindex=-1>{notice_html(notice)}{body}</main></div></div>"
        f"<script>{JS}</script></body></html>"
    ))


def action(item_id: int, verb: str, label: str, cls: str = "", *, confirm: str = "") -> str:
    """A one-button POST form. ``cls`` is a button variant (``go`` primary, ``no`` danger,
    ``quiet``). ``confirm`` becomes ``data-confirm``: the shell asks before submitting (the text
    is the whole prompt, so say what will happen). Without JS it degrades to a plain submit."""
    ask = f" data-confirm='{esc(confirm)}'" if confirm else ""
    return (f"<form method=post action='/item/{item_id}/{verb}'{ask}>"
            f"<button class='btn {esc(cls)}'>{label}</button></form>")


def status_badge(status: str) -> str:
    """Status pill: dot + plain label. The raw status is in ``data-status`` and ``title``."""
    lane = LANE_OF.get(status, "")
    return (f"<span class='pill p-{lane}' data-status='{esc(status)}' title='{esc(status)}'>"
            f"<span class='dot d-{lane}' aria-hidden=true></span>{esc(status_label(status))}</span>")


def empty_state(title: str, hint: str = "", cta: str = "", icon_name: str = "inbox") -> str:
    """The one empty state. ``title``/``hint`` are escaped; ``cta`` is built HTML (a link/button)."""
    return (f"<div class=empty>{icon(icon_name)}<strong>{esc(title)}</strong>"
            + (f"<span>{esc(hint)}</span>" if hint else "")
            + (f"<div class=cta>{cta}</div>" if cta else "") + "</div>")


def card(heading: str, body: str, *, link: str = "") -> str:
    """A panel. ``heading`` is escaped; ``body`` and ``link`` (a right-aligned header link) are
    built HTML."""
    return (f"<div class=card><div class=card-head><h2>{esc(heading)}</h2>{link}</div>{body}</div>")


def segmented(items: list[tuple[str, str, bool]], label: str) -> str:
    """Segmented control of links: (text, href, is_current). ``text`` is built HTML so a count
    can sit inside; escape any user text before passing it."""
    links = "".join(
        f"<a href='{esc(href)}'{' aria-current=page' if here else ''}>{text}</a>"
        for text, href, here in items)
    return f"<nav class=seg aria-label='{esc(label)}'>{links}</nav>"


#: Statuses from which the dashboard asks for a revision (the agentic path). Anything
#: else keeps the pre-agentic regen: it forces SYNTHESIZED, which also un-sticks an item
#: that is mid-pipeline.
REVISABLE_FROM = (
    Status.AWAITING_APPROVAL.value, Status.REVISION_CONFIRM.value,
    Status.FAILED.value, Status.REJECTED.value,
)
#: Confirm verbs in /item/{id}/{verb}: the Yes/No for a destructive revision.
CONFIRM_VERBS = {"revise_yes": True, "revise_no": False}
CONFIRMED = frozenset({ConfirmResult.YES, ConfirmResult.NO_CONTINUE, ConfirmResult.NO_NOTHING_LEFT})


def revise_on(request: web.Request) -> bool:
    """REVISE_ENABLED: false is the rollback switch back to the pre-agentic regen."""
    return bool(request.app["settings"].revise_enabled)


def error_text(status: str, last_error: str | None) -> str:
    """What to show for ``items.last_error``. While a revision waits, the worker's
    deferral stores ``str(exc)`` there (it can echo a provider's response, rule 15), so
    the dashboard shows a closed sentence instead; the full text stays in the log."""
    if not last_error:
        return ""
    if status == Status.REVISION_REQUESTED.value:
        return rcopy.REVISION_WAITING_SUMMARY
    return last_error


#: Ordered (pattern, sentence) pairs: the FIRST match wins, so put the specific before the general.
#: The sentences are closed text written here, never the exception: ``last_error`` can echo a
#: provider's response or a local path (rule 15), so it is matched against and never shown.
#: Only the head of the text is classified: the stage and provider are named first, and a bounded
#: input keeps every pattern's worst case small (explain_error runs per row on list pages).
ERROR_MATCH_CHARS = 500
ERROR_REASONS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pattern, re.I), sentence) for pattern, sentence in (
        (r"researchers? produced",
         "The web research did not find enough usable sources for this story. Retrying often "
         "works; if it keeps failing the story may be too thin."),
        (r"instagram \d+[^\n]{0,200}caption[^\n]{0,200}too long",
         "Instagram refused the caption because it is too long. Shorten the caption, then retry."),
        (r"instagram \d+|container \S+ failed",
         "Instagram refused or could not process the post. Retry once; if it fails again, "
         "check the Instagram connection."),
        (r"r2 upload|endpoint url|could not connect|connection (refused|error)",
         "Uploading the slide images to storage failed because storage was unreachable."),
        (r"exceeds the available context|context (size|length|window)",
         "The text was too long for the AI model to read in one go."),
        (r"empty completion",
         "The AI model sent back an empty answer."),
        (r"unparseable json|invalid json|json ?(decode|pars)\w*|jsondecodeerror",
         "The AI model replied in a format the pipeline could not read."),
        (r"(openrouter|ollama|provider|model).{0,12}\b[45]\d\d\b|provider returned error",
         "The AI provider rejected the request or was unavailable. Retrying later usually works."),
        (r"time[ds]? ?out|timeout",
         "A service took too long to answer."),
    ))
#: Used when nothing matches. Never the raw text.
GENERIC_REASON = "Something unexpected went wrong at this step. The technical details have the full error."
#: Used when there is no error text at all.
NO_ERROR_REASON = "No reason was recorded for this failure."


def explain_error(detail: str | None) -> str:
    """One plain sentence for ``items.last_error`` / ``events.detail``, chosen from
    ``ERROR_REASONS``. Pure and closed: the result is one of our own sentences, never the input.
    Not escaped (it is trusted text), but ``esc()`` it anyway when interpolating."""
    text = (detail or "").strip()[:ERROR_MATCH_CHARS]
    if not text:
        return NO_ERROR_REASON
    for pattern, sentence in ERROR_REASONS:
        if pattern.search(text):
            return sentence
    return GENERIC_REASON


def list_error_line(status: str, last_error: str | None) -> str:
    """The one-line error under a list row: our sentence, never the exception text. ``""`` when
    there is no error. A waiting revision keeps its own closed sentence (``error_text``)."""
    if not last_error or not last_error.strip():
        return ""
    if status == Status.REVISION_REQUESTED.value:
        return error_text(status, last_error)
    return explain_error(last_error)


#: What the pipeline does FROM each status (the stage a retry re-runs), in plain words.
STAGE_ACTION = {
    Status.INGESTED.value: "reading the source",
    Status.EXTRACTED.value: "scoring the story",
    Status.TRIAGED.value: "researching",
    Status.RESEARCHED.value: "writing the brief",
    Status.SYNTHESIZED.value: "composing the slides",
    Status.COMPOSED.value: "rendering the slides",
    Status.RENDERED.value: "sending the preview",
    Status.APPROVED.value: "uploading the images",
    Status.PUBLISHING.value: "publishing to Instagram",
}
#: UX-6 seam: confirm text for Retry (empty = submit at once); same convention as DECISION_CONFIRM.
RETRY_CONFIRM = "Retry this stage? Work from this stage onward is cleared and run again."
STAGE_ID = "stage"


def failed_stage(item, events: list | None) -> str | None:
    """The status the item was in when it last failed (``events.from_status``), if that is a stage
    a requeue can restart from. ``None`` when unknown, so callers show no one-click Retry."""
    if item.status != Status.FAILED.value:
        return None
    for event in reversed(events or []):
        if event.get("to_status") == Status.FAILED.value:
            stage = event.get("from_status")
            return stage if stage in TARGETS and stage in STAGE_ACTION else None
    return None


def thumb(item_id: int, paths: list, alt: str = "") -> str:
    """48x60 slide-1 thumbnail, or the grey tile when nothing is rendered yet (or the image 404s).
    ``alt`` stays empty when the row's text already names the item."""
    if not paths:
        return "<span class=thumb></span>"
    name = Path(str(paths[0])).name
    return (f"<img class=thumb src='/media/{item_id}/{esc(name)}' alt='{esc(alt)}'"
            f" loading=lazy onerror=\"this.classList.add('broken')\">")


# ----------------------------------------------------------------- overview (UX-2)

#: Chart window, in UTC days, and how many rows each list on the Overview shows.
OVERVIEW_DAYS = 14
WAITING_LONGEST_ROWS = 5
RECENT_ACTIVITY_ROWS = 12
STOPPED_REASON_ROWS = 5
#: ``events.detail`` is exception-ish text; fetch more groups than we show so near-duplicate
#: first lines can be merged before taking the top few.
STOPPED_REASON_FETCH = 50
REASON_CHARS = 90
HOURS_DAY, HOURS_WEEK, HOURS_HOUR = 24, 168, 1
#: Instagram publish pacing: at most this many publishes per hour (config owns the gap).
HOURLY_PUBLISH_CAP = 3600 // IG_MIN_PUBLISH_GAP_S
NO_REASON = "(no reason recorded)"
COULDNT_LOAD = "Couldn't load this - reload the page."
#: A chart's colour is one of these tones (CSS classes ``c-<tone>``), never caller-supplied text.
TONES = frozenset({"good", "bad", "work", "attn"})
#: Query keys that mean "this is the list view", so ``/`` hands over to ``index()``.
LIST_PARAMS = ("lane", "q", "all", "thin")

_log = logging.getLogger("dashboard.overview")


def _count(value) -> int:
    """A chart value as a non-negative int; None, NaN, inf, negatives and junk become 0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number < 0:
        return 0
    return int(number)


def _tone(tone: str) -> str:
    return tone if tone in TONES else "work"


def _chart_svg(view: tuple[int, int], title: str, summary: str, inner: str, cls: str) -> str:
    """The one SVG wrapper: ``role=img`` with the numbers in its label and a <title>."""
    return (f"<svg class='{cls}' viewBox='0 0 {view[0]} {view[1]}' role=img"
            f" aria-label='{esc(summary)}' focusable=false preserveAspectRatio=none>"
            f"<title>{esc(title)}</title>{inner}</svg>")


def sparkline(values, *, label: str = "", tone: str = "work") -> str:
    """A 96x28 trend line. Under two points there is no trend, so it returns ''.
    With a ``label`` it is a ``role=img`` carrying the numbers; without, it is decoration
    (``aria-hidden``) and the number next to it is the content."""
    vals = [_count(v) for v in values or []]
    if len(vals) < 2:
        return ""
    width, height, pad = 96, 28, 3
    peak = max(vals) or 1
    step = (width - 2 * pad) / (len(vals) - 1)
    points = " ".join(
        f"{pad + i * step:.1f},{height - pad - (v / peak) * (height - 2 * pad):.1f}"
        for i, v in enumerate(vals))
    line = f"<polyline class='spark s-{_tone(tone)}' points='{points}'/>"
    if label:
        return _chart_svg((width, height), label,
                          f"{label}: {', '.join(map(str, vals))}", line, "spark-svg")
    return (f"<svg class=spark-svg viewBox='0 0 {width} {height}' aria-hidden=true"
            f" focusable=false preserveAspectRatio=none>{line}</svg>")


def bars(values, labels=None, *, title: str, tone: str = "good") -> str:
    """Vertical bars, one per value, zero-based. Each bar has a <title> tooltip; the whole is a
    ``role=img`` whose label lists every value plus total and peak. Under two bars: ''."""
    vals = [_count(v) for v in values or []]
    if len(vals) < 2:
        return ""
    names = [str(x) for x in (labels or [])]
    names += [str(i + 1) for i in range(len(names), len(vals))]
    width, height = 280, 80
    slot = width / len(vals)
    peak = max(vals) or 1
    rects = []
    for i, v in enumerate(vals):
        h = 0 if v == 0 else max(3, round(v / peak * (height - 6)))
        rects.append(f"<rect class='c-{_tone(tone)}' x='{i * slot + slot * .15:.1f}'"
                     f" y='{height - 1 - h}' width='{slot * .7:.1f}' height='{h}' rx=2>"
                     f"<title>{esc(names[i])}: {v}</title></rect>")
    base = f"<line class=axis x1=0 y1='{height - .5}' x2='{width}' y2='{height - .5}'/>"
    summary = (f"{title}: " + ", ".join(f"{n} {v}" for n, v in zip(names, vals))
               + f". Total {sum(vals)}, peak {max(vals)}.")
    return _chart_svg((width, height), title, summary, base + "".join(rects), "bars-svg")


def stacked_bar(parts, *, title: str) -> str:
    """One horizontal bar split into ``(label, value, tone)`` parts, widths proportional.
    Zero/negative/None parts are dropped; nothing left means no bar ('')."""
    kept = [(str(label), _count(value), _tone(tone)) for label, value, tone in parts or []]
    kept = [p for p in kept if p[1] > 0]
    total = sum(v for _, v, _ in kept)
    if not total:
        return ""
    width, x, rects = 100, 0.0, []
    for label, value, tone in kept:
        w = value / total * width
        rects.append(f"<rect class='c-{tone}' x='{x:.2f}' y=0 width='{w:.2f}' height=12>"
                     f"<title>{esc(label)}: {value}</title></rect>")
        x += w
    summary = f"{title}: " + ", ".join(f"{label} {v}" for label, v, _ in kept) + f". Total {total}."
    return _chart_svg((width, 12), title, summary, "".join(rects), "stack-svg")


def reason_line(detail: str | None, limit: int = REASON_CHARS) -> str:
    """One display line from ``events.detail`` / ``triage_reason``. Never a stack trace: a
    traceback collapses to its last line (the exception itself), anything else to its first
    non-blank line; truncated. Not escaped: ``esc()`` it."""
    lines = [ln.strip() for ln in (detail or "").splitlines() if ln.strip()]
    if not lines:
        return NO_REASON
    line = lines[-1] if lines[0].startswith("Traceback") else lines[0]
    return line[:limit].rstrip() + "…" if len(line) > limit else line


def _utc_days(now: datetime, n: int = OVERVIEW_DAYS) -> list[str]:
    """The last ``n`` UTC dates, oldest first, ending today."""
    today = now.astimezone(timezone.utc).date()
    return [(today - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


class OverviewStats:
    """Everything the Overview shows. ``failed`` names the sections whose read raised, so the
    page can render that one card as 'couldn't load' instead of a 500.

    A plain class, not a dataclass: tests load this script by path without registering it in
    ``sys.modules``, which ``@dataclass`` needs under ``from __future__ import annotations``."""

    def __init__(self, days: list[str]) -> None:
        self.days = days
        self.counts: dict = {}
        self.attn = 0
        self.attn_oldest: str | None = None
        self.failed_now = 0
        self.failed_24h = 0
        self.running = 0
        self.next_up: tuple[int, str] | None = None
        self.pub_24h = 0
        self.pub_7d = 0
        self.pub_daily: list[int] = []
        self.failures_daily: list[int] = []
        self.intake_daily: list[int] = []
        self.last_published: datetime | None = None
        self.pub_last_hour = 0
        self.held_count = 0
        self.held_until: str | None = None
        self.held_error = ""
        self.mix: list[tuple[str, int]] = []
        self.stopped_reasons: list[tuple[str, int]] = []
        self.dropped_reasons: list[tuple[str, int]] = []
        self.waiting: list[dict] = []
        self.recent: list[dict] = []
        self.failed: set[str] = set()


async def _per_day(db: Database, sql: str, since: str, days: list[str]) -> list[int]:
    """Run a ``SELECT <day>, COUNT(*) ... GROUP BY`` and zero-fill the window."""
    rows = await db.conn.execute_fetchall(sql, (since,))
    by_day = {r[0]: r[1] for r in rows}
    return [by_day.get(d, 0) for d in days]


def _merge_reasons(pairs, limit: int, *, explain: bool = False) -> list[tuple[str, int]]:
    """Group (detail, n) by display line (many raw details share a first line), top ``limit``.
    ``explain`` groups by ``explain_error``'s closed sentence instead: failure details are
    exception text (rule 15), so they are classified, never shown."""
    merged: Counter = Counter()
    for detail, n in pairs:
        merged[explain_error(detail) if explain else reason_line(detail)] += n
    return merged.most_common(limit)


async def _load_counts(db, now, s: OverviewStats) -> None:
    s.counts = await db.status_counts()
    s.mix = [(key, n) for key, n in lane_counts(s.counts).items()]
    s.attn = lane_counts(s.counts)["attn"]
    s.failed_now = s.counts.get(Status.FAILED.value, 0)
    s.running = lane_counts(s.counts)["work"]


async def _load_attn(db, now, s: OverviewStats) -> None:
    attn = LANE_BY_KEY["attn"][2]
    rows = await db.conn.execute_fetchall(
        f"SELECT MIN(status_updated_at) AS m FROM items WHERE status IN ({','.join('?' * len(attn))})",
        attn)
    s.attn_oldest = rows[0]["m"] if rows else None


async def _load_failed(db, now, s: OverviewStats) -> None:
    since = (now - timedelta(hours=HOURS_DAY)).isoformat()
    rows = await db.conn.execute_fetchall(
        "SELECT COUNT(*) AS c FROM events WHERE to_status=? AND at>=?",
        (Status.FAILED.value, since))
    s.failed_24h = rows[0]["c"] if rows else 0


async def _load_running(db, now, s: OverviewStats) -> None:
    queued = await db.queue_order(sorted(RUNNING))
    if queued:
        s.next_up = (queued[0].id, queued[0].raw_text)


async def _load_published(db, now, s: OverviewStats) -> None:
    s.pub_24h = await db.published_since(HOURS_DAY)
    s.pub_7d = await db.published_since(HOURS_WEEK)
    s.pub_daily = await _per_day(
        db, "SELECT substr(published_at,1,10) d, COUNT(*) FROM items WHERE published_at>=?"
            " GROUP BY d", s.days[0], s.days)


async def _load_charts(db, now, s: OverviewStats) -> None:
    s.failures_daily = await _per_day(
        db, f"SELECT substr(at,1,10) d, COUNT(*) FROM events WHERE to_status='{Status.FAILED.value}'"
            " AND at>=? GROUP BY d", s.days[0], s.days)
    s.intake_daily = await _per_day(
        db, "SELECT substr(created_at,1,10) d, COUNT(*) FROM items WHERE created_at>=?"
            " GROUP BY d", s.days[0], s.days)


async def _load_health(db, now, s: OverviewStats) -> None:
    s.last_published = await db.last_published_at()
    s.pub_last_hour = await db.published_since(HOURS_HOUR)
    held = (Status.APPROVED.value, Status.PUBLISHING.value)
    marks = ",".join("?" * len(held))
    rows = await db.conn.execute_fetchall(
        f"SELECT COUNT(*) AS c, MIN(next_attempt_at) AS m FROM items"
        f" WHERE status IN ({marks}) AND next_attempt_at>?", (*held, now.isoformat()))
    s.held_count, s.held_until = (rows[0]["c"], rows[0]["m"]) if rows else (0, None)
    if s.held_count:
        first = await db.conn.execute_fetchall(
            f"SELECT last_error FROM items WHERE status IN ({marks}) AND next_attempt_at>?"
            f" ORDER BY next_attempt_at LIMIT 1", (*held, now.isoformat()))
        s.held_error = explain_error(first[0]["last_error"]) if first else ""


async def _load_reasons(db, now, s: OverviewStats) -> None:
    since = (now - timedelta(hours=HOURS_WEEK)).isoformat()
    rows = await db.conn.execute_fetchall(
        "SELECT detail, COUNT(*) AS n FROM events WHERE to_status=? AND at>=?"
        " GROUP BY detail ORDER BY 2 DESC LIMIT ?",
        (Status.FAILED.value, since, STOPPED_REASON_FETCH))
    s.stopped_reasons = _merge_reasons(((r["detail"], r["n"]) for r in rows), STOPPED_REASON_ROWS,
                                       explain=True)
    dropped = await db.dropped_reasons_since(HOURS_WEEK)
    s.dropped_reasons = _merge_reasons(((d, 1) for d in dropped), STOPPED_REASON_ROWS)


async def _load_waiting(db, now, s: OverviewStats) -> None:
    attn = LANE_BY_KEY["attn"][2]
    rows = await db.conn.execute_fetchall(
        f"SELECT id, raw_text, status, status_updated_at, rendered_paths FROM items"
        f" WHERE status IN ({','.join('?' * len(attn))}) ORDER BY status_updated_at ASC LIMIT ?",
        (*attn, WAITING_LONGEST_ROWS))
    s.waiting = [dict(r) for r in rows]


async def _load_recent(db, now, s: OverviewStats) -> None:
    rows = await db.conn.execute_fetchall(
        "SELECT item_id, from_status, to_status, at, detail FROM events ORDER BY id DESC LIMIT ?",
        (RECENT_ACTIVITY_ROWS,))
    s.recent = [dict(r) for r in rows]


#: name -> loader. Order matters only for ``counts``, which the status-derived cards read.
OVERVIEW_LOADERS = (
    ("counts", _load_counts), ("attn", _load_attn), ("failed", _load_failed),
    ("running", _load_running), ("published", _load_published), ("charts", _load_charts),
    ("health", _load_health), ("reasons", _load_reasons), ("waiting", _load_waiting),
    ("recent", _load_recent),
)


async def overview_stats(db: Database, now: datetime | None = None) -> OverviewStats:
    """All Overview numbers, read-only. Each section is read on its own: one that raises is
    logged and recorded in ``failed`` (its card shows 'couldn't load'), the rest still render."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stats = OverviewStats(days=_utc_days(now))
    for name, load in OVERVIEW_LOADERS:
        try:
            await load(db, now, stats)
        except Exception:  # a card must never take the page down
            _log.exception("overview section %r failed", name)
            stats.failed.add(name)
    return stats


def _day_label(iso_day: str) -> str:
    return datetime.fromisoformat(iso_day).strftime("%b %-d")


def kpi(label: str, icon_name: str, value: str, sub: str = "", href: str = "", spark: str = "",
        tip: str = "") -> str:
    """One KPI card; a link when it has a filtered list. All text args are escaped here."""
    inner = (f"<span class=kpi-label>{icon(icon_name)}{esc(label)}</span>"
             f"<span class=kpi-value>{esc(value)}</span>"
             + (f"<span class=kpi-sub>{esc(sub)}</span>" if sub else "")
             + (f"<span class=kpi-spark>{spark}</span>" if spark else ""))
    title = f" title='{esc(tip)}'" if tip else ""
    if href:
        return f"<a class=kpi href='{esc(href)}'{title}>{inner}</a>"
    return f"<div class=kpi{title}>{inner}</div>"


def _broken(heading: str, is_kpi: bool = False) -> str:
    """The 'couldn't load' stand-in for a card whose read failed."""
    if is_kpi:
        return f"<div class='kpi kpi-err'><span class=kpi-label>{esc(heading)}</span><span class=kpi-sub>{esc(COULDNT_LOAD)}</span></div>"
    return card(heading, f"<p class=ov-note role=status>{esc(COULDNT_LOAD)}</p>")


def _failed_any(s: OverviewStats, *names: str) -> bool:
    return bool(s.failed.intersection(names))


def _kpi_cards(s: OverviewStats, now: datetime) -> str:
    cards = []
    if _failed_any(s, "counts", "attn"):
        cards.append(_broken("Waiting on you", True))
    else:
        oldest = ago(s.attn_oldest)
        cards.append(kpi("Waiting on you", "inbox", str(s.attn),
                         f"oldest: {oldest}" if oldest else "nothing waiting",
                         "/?lane=attn"))
    if _failed_any(s, "counts", "failed"):
        cards.append(_broken("Failed", True))
    else:
        cards.append(kpi("Failed", "alert", str(s.failed_now),
                         f"{s.failed_24h} new in 24h", "/?lane=bad"))
    if _failed_any(s, "counts", "running"):
        cards.append(_broken("In progress", True))
    else:
        nxt = f"next: #{s.next_up[0]}" if s.next_up else "nothing queued"
        cards.append(kpi("In progress", "play", str(s.running), nxt, "/?lane=work"))
    if _failed_any(s, "published"):
        cards.append(_broken("Published", True))
    else:
        cards.append(kpi("Published (24h)", "check", str(s.pub_24h), f"7 days: {s.pub_7d}",
                         "/?lane=good", sparkline(s.pub_daily, tone="good")))
    if _failed_any(s, "health"):
        cards.append(_broken("Publishing", True))
    else:
        cards.append(_health_kpi(s))
    return f"<section class=ov-kpis aria-label='Key numbers'>{''.join(cards)}</section>"


def _health_kpi(s: OverviewStats) -> str:
    last = ago(s.last_published.isoformat()) if s.last_published else ""
    pace = f"{s.pub_last_hour} of {HOURLY_PUBLISH_CAP} this hour"
    sub = f"last published {last} · {pace}" if last else f"never published · {pace}"
    if not s.held_count:
        return kpi("Publishing", "check", "Clear", sub, "/queue")
    retry = s.held_until or ""
    value = f"Held: {s.held_count} waiting"
    sub = f"{sub} · retries at {retry[11:16] or '?'} UTC"
    return kpi("Publishing", "alert", value, sub, "/queue", tip=s.held_error)


def _chart_block(heading: str, chart: str, values: list[int], days: list[str], total_text: str) -> str:
    """A labelled chart with its visible total, or the not-enough-data note."""
    if not chart or not any(values):
        return (f"<div class=chart-block><h3>{esc(heading)}</h3>"
                f"<p class=ov-note>Nothing in the last {len(days)} days.</p></div>")
    return (f"<div class=chart-block><h3>{esc(heading)}"
            f"<span class=chart-total>{esc(total_text)}</span></h3>{chart}"
            f"<div class=chart-axis><span>{esc(_day_label(days[0]))}</span>"
            f"<span>{esc(_day_label(days[-1]))}</span></div></div>")


def _charts_card(s: OverviewStats) -> str:
    if _failed_any(s, "published", "charts"):
        return _broken("Last 14 days")
    n, days = len(s.days), s.days
    blocks = [
        _chart_block("Published per day", bars(s.pub_daily, days, title="Published per day", tone="good"),
                     s.pub_daily, days, f"{sum(s.pub_daily)} total"),
        _chart_block("Failures per day", bars(s.failures_daily, days, title="Failures per day", tone="bad"),
                     s.failures_daily, days, f"{sum(s.failures_daily)} total"),
        _chart_block("New items per day", bars(s.intake_daily, days, title="New items per day", tone="work"),
                     s.intake_daily, days, f"{sum(s.intake_daily)} total"),
    ]
    return card(f"Last {n} days", "".join(blocks) + "<p class=ov-foot>Days are UTC.</p>")


def _mix_card(s: OverviewStats) -> str:
    if _failed_any(s, "counts"):
        return _broken("Status mix")
    tones = {"attn": "attn", "work": "work", "good": "good", "bad": "bad"}
    parts = [(LANE_NAV_LABEL[k], n, tones[k]) for k, n in s.mix]
    bar = stacked_bar(parts, title="Status mix")
    if not bar:
        return card("Status mix", empty_state("No items yet", "Queue a post to get started.",
                                              icon_name="list"))
    legend = "".join(
        f"<li><span class='sw c-{tones[k]}-bg' aria-hidden=true></span>"
        f"<a href='/?lane={k}'>{esc(LANE_NAV_LABEL[k])}</a> <b>{n}</b></li>" for k, n in s.mix)
    return card("Status mix", f"<div class=chart>{bar}</div><ul class=legend>{legend}</ul>")


def _reason_list(pairs: list[tuple[str, int]]) -> str:
    return "<ul class=ov-list>" + "".join(
        f"<li><span class=ov-text>{esc(text)}</span><b class=ov-n>{n}x</b></li>"
        for text, n in pairs) + "</ul>"


def _reasons_card(s: OverviewStats) -> str:
    if _failed_any(s, "reasons"):
        return _broken("Why items stopped")
    if not s.stopped_reasons and not s.dropped_reasons:
        return card("Why items stopped (7 days)",
                    empty_state("Nothing stopped", "No failures or drops in the last 7 days.",
                                icon_name="check"))
    body = ""
    if s.stopped_reasons:
        body += f"<h3>Failed</h3>{_reason_list(s.stopped_reasons)}"
    if s.dropped_reasons:
        body += f"<h3>Dropped</h3>{_reason_list(s.dropped_reasons)}"
    return card("Why items stopped (7 days)", body)


def _waiting_card(s: OverviewStats) -> str:
    link = f"<a class=card-link href='/?lane=attn'>See all {s.attn}</a>"
    if _failed_any(s, "waiting"):
        return _broken("Waiting longest")
    if not s.waiting:
        return card("Waiting longest", empty_state("You're all caught up.", icon_name="check"))
    rows = []
    for r in s.waiting:
        try:
            paths = json.loads(r["rendered_paths"] or "[]")
        except (TypeError, json.JSONDecodeError):
            paths = []
        rows.append(
            f"<li class=ov-row>{thumb(r['id'], paths)}"
            f"<a class=rid href='/item/{r['id']}'>#{r['id']}</a>"
            f"<span class=ov-text>{esc(plain_title(r['raw_text'], 70))}</span>"
            f"<span class=ov-age title='{esc(r['status_updated_at'])}'>{esc(ago(r['status_updated_at']))}</span>"
            f"<a class='btn' href='/item/{r['id']}'>Review</a></li>")
    return card("Waiting longest", f"<ul class=ov-list>{''.join(rows)}</ul>", link=link)


def _recent_card(s: OverviewStats) -> str:
    if _failed_any(s, "recent"):
        return _broken("Recent activity")
    if not s.recent:
        return card("Recent activity", empty_state("No activity yet", icon_name="file-text"))
    rows = []
    for e in s.recent:
        before = status_label(e["from_status"]) if e["from_status"] else "new"
        rows.append(
            f"<li class=ov-row><a class=rid href='/item/{e['item_id']}'>#{e['item_id']}</a>"
            f"<span class=ov-text>{esc(before)} → {esc(status_label(e['to_status'] or ''))}</span>"
            f"<span class=ov-age title='{esc(e['at'])}'>{esc(ago(e['at']))}</span></li>")
    return card("Recent activity", f"<ul class=ov-list>{''.join(rows)}</ul>")


def overview_body(s: OverviewStats, now: datetime) -> str:
    """The Overview page body (pure: stats in, HTML out)."""
    compose = (
        "<div class=card><div class=card-head><h2>New post</h2></div>"
        "<form method=post action='/new' class=compose>"
        "<label class=sr for=new-text>New post</label>"
        "<textarea id=new-text name=text required placeholder='A headline, a link, a "
        "paragraph, or a request like &quot;10 github repos for rust&quot;…'"
        "></textarea><button class=go>research it</button></form></div>")
    return (_kpi_cards(s, now)
            + f"<div class=ov-grid>{_charts_card(s)}<div class=ov-stack>{_mix_card(s)}"
              f"{_reasons_card(s)}</div>{_waiting_card(s)}{_recent_card(s)}</div>"
            + compose)


async def overview(request: web.Request) -> web.Response:
    """GET /overview: the read-only health snapshot."""
    now = datetime.now(timezone.utc)
    stats = await overview_stats(request.app["db"], now)
    return page("Overview", overview_body(stats, now), stats.counts, ACTIVE_HOME, refresh=True,
                notice=notice_param(request), subtitle="Last 24h · all times UTC",
                actions="<a class='btn go' href='#new-text'>New post</a>")


async def home(request: web.Request) -> web.Response:
    """GET /: the Overview, unless the query is a list view (lane/q/all/thin), which goes to
    index() untouched so old links, search and the 'too thin' redirect keep working."""
    if any(k in request.query for k in LIST_PARAMS):
        return await index(request)
    return await overview(request)


# ----------------------------------------------------------------- board


#: ``?sort=`` -> ORDER BY. A closed set, so no request text ever reaches the SQL.
SORTS = {"new": ("Newest", "id DESC"), "age": ("Oldest", "status_updated_at ASC, id ASC")}
DEFAULT_SORT = "new"
#: A rejected "new post" text is handed back in the redirect so the form keeps it. Too-thin
#: text is short by definition; the cap only stops a hostile URL from growing.
#: NOTE: the draft rides in the URL, so it would show in an access log if INFO logging
#: is ever enabled for aiohttp.access.
DRAFT_MAX = 1000
THIN_FLAG = "1"
ERROR_LINE_MAX = 160
#: Statuses whose row primary action is "Review" (the decision lives on the item page).
REVIEWABLE = (Status.AWAITING_APPROVAL.value, Status.NEEDS_INPUT.value)


def row_actions(item, *, allow_approve: bool = False) -> str:
    """The action cell of one list row. Approve/Reject are decisions, so by default they live on
    the item page behind a confirm and the row only offers "Review". ``allow_approve=True``
    restores the old one-click Approve/Reject forms in the row (the reversal switch)."""
    status, item_id = item["status"], item["id"]
    acts = []
    if status == Status.AWAITING_APPROVAL.value and allow_approve:
        acts.append(action(item_id, "approve", "approve", "go"))
        acts.append(action(item_id, "reject", "reject", "no"))
    elif status in REVIEWABLE:
        verb = "Answer" if status == Status.NEEDS_INPUT.value else "Review"
        acts.append(f"<a class='tab go' href='/item/{item_id}'>{verb}</a>")
    if status == Status.REVISION_CONFIRM.value:
        # The Yes/No (also sent to Telegram; this is the way back if that never arrived).
        acts.append(f"<a class=tab href='/item/{item_id}'>answer</a>")
    if status not in {s.value for s in TERMINAL}:
        acts.append(action(item_id, "drop", "Drop", "quiet", confirm=DECISION_CONFIRM["drop"]))
        if status in RUNNING:
            # Ordering lives in one place, the run order view, where the positions are
            # visible. Two ways to reorder invites them to disagree.
            acts.append("<a class='tab quiet' href='/queue'>order</a>")
    return f"<div class=acts>{''.join(acts)}</div>"


def _items_href(lane: str, query: str, sort: str, *, show_all: bool = False) -> str:
    params = {k: v for k, v in (("lane", lane), ("q", query)) if v}
    if sort != DEFAULT_SORT:
        params["sort"] = sort
    if show_all:
        params["all"] = "1"
    return f"{ITEMS_HREF}?{urlencode(params)}" if params else ITEMS_HREF


def _item_row(r) -> str:
    try:
        paths = json.loads(r["rendered_paths"] or "[]")
    except (TypeError, json.JSONDecodeError):
        paths = []
    i = r["id"]
    prio = f"<span class=prio>prio {r['priority']:+d}</span> " if r["priority"] else ""
    intent = f"<span class=badge>{esc(r['intent'])}</span>" if r["intent"] else ""
    first_line = list_error_line(r["status"], r["last_error"])
    err = f"<div class=err>{esc(first_line, ERROR_LINE_MAX)}</div>" if first_line else ""
    return (
        f"<tr><td class=c-thumb>{thumb(i, paths)}</td>"
        f"<td class=c-id><a class=rid href='/item/{i}'>#{i}</a></td>"
        f"<td class=c-title><a class='title clamp' href='/item/{i}'>{esc(plain_title(r['raw_text']))}</a>"
        f"<div class=meta>{prio}{esc(source_label(r['source']))} {intent}</div>{err}</td>"
        f"<td>{status_badge(r['status'])}</td>"
        f"<td class=r>{len(paths) or '-'}</td>"
        f"<td class='r age'>{esc(ago(r['status_updated_at']))}</td>"
        f"<td class=c-acts>{row_actions(r)}</td></tr>")


async def index(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    counts = await db.status_counts()
    lane = request.query.get("lane", "")
    lane = lane if lane in LANE_BY_KEY else ""
    query = (request.query.get("q") or "").strip()
    sort = request.query.get("sort", "")
    sort = sort if sort in SORTS else DEFAULT_SORT

    # "#91" and "91" jump straight to the item rather than searching for it.
    jump = query.lstrip("#")
    if jump.isascii() and jump.isdigit():  # "²".isdigit() is True but int("²") raises
        raise web.HTTPFound(f"/item/{int(jump)}")

    sql = ("SELECT id, source, status, intent, raw_text, last_error,"
           " status_updated_at, rendered_paths, priority FROM items")
    where, params = [], []
    if lane:
        statuses = LANE_BY_KEY[lane][2]
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params += list(statuses)
    if query:
        where.append("raw_text LIKE ?")
        params.append(f"%{query}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = await db.conn.execute_fetchall(sql + f" ORDER BY {SORTS[sort][1]}", params)

    # A flag, never text: the warning copy is fixed, so a crafted link cannot display a message.
    notice = TOO_THIN if request.query.get("thin") == THIN_FLAG else ""
    draft = (request.query.get("draft") or "")[:DRAFT_MAX]
    lane_label = LANE_BY_KEY[lane][0] if lane else "all items"
    active = lane or ACTIVE_ALL
    body = [
        "<div class=card><h2>New post</h2>"
        "<form method=post action='/new' class=compose>"
        "<label class=sr for=new-text>New post</label>"
        "<textarea id=new-text name=text required placeholder='A headline, a link, a "
        "paragraph, or a request like &quot;10 github repos for rust&quot;…'"
        f">{esc(draft)}</textarea><button class=go>research it</button></form>"
        + (f"<p class=warn role=alert>{esc(notice)}</p>" if notice else "")
        + "</div>"
    ]

    lanes = lane_counts(counts)
    lane_links = [(f"All <span class=cnt>{sum(counts.values())}</span>",
                   _items_href("", query, sort), not lane)]
    lane_links += [(f"{esc(LANE_NAV_LABEL[key])} <span class=cnt>{lanes[key]}</span>",
                    _items_href(key, query, sort), key == lane) for key, *_ in LANES]
    sort_links = [(esc(label), _items_href(lane, query, key), key == sort)
                  for key, (label, _) in SORTS.items()]
    crumb = f"<b>{plural(len(rows), 'item')}</b>"
    if query:
        crumb += f" matching “{esc(query)}”"
    clear = f"<a href='{ITEMS_HREF}'>clear</a>" if (query or lane) else ""
    body.append(
        f"<div class=toolbar>{segmented(lane_links, 'Filter by lane')}"
        f"<span class=grow></span><span class=sortlbl>Sort</span>"
        f"{segmented(sort_links, 'Sort order')}</div>"
        f"<p class=crumb>{crumb}<span class=grow></span>{clear}</p>")

    if not rows:
        if query:
            empty = empty_state(f"No matches for “{query}”", "Try a shorter search, or clear it.",
                                f"<a class=tab href='{ITEMS_HREF}'>All items</a>", "search")
        else:
            empty = empty_state("Nothing here", "No items in this view yet.",
                                f"<a class=tab href='{ITEMS_HREF}'>All items</a>")
        body.append(empty)
        return page(lane_label.capitalize(), "".join(body), counts, active, query,
                    notice=notice_param(request), subtitle=plural(0, "item"))

    show_all = request.query.get("all") == "1"
    shown = rows if show_all else rows[:PAGE_SIZE]
    head = "".join(f"<th scope=col{c}>{t}</th>" for t, c in (
        ('<span class=sr>Preview</span>', ""), ("#", ""), ("Title", ""), ("Status", ""),
        ("Slides", " class=r"), ("Age", " class=r"), ('<span class=sr>Actions</span>', "")))
    body.append(
        f"<div class=tbl-wrap><table class='tbl items'><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(_item_row(r) for r in shown)}</tbody></table></div>")
    if len(rows) > len(shown):
        body.append(f"<p class=more-line>Showing {len(shown)} of {len(rows)} "
                    f"<a class=more href='{esc(_items_href(lane, query, sort, show_all=True))}'>"
                    f"show all {len(rows)}</a></p>")

    live = any(r["status"] in RUNNING for r in rows)
    return page(lane_label.capitalize(), "".join(body), counts, active, query, refresh=live,
                notice=notice_param(request), subtitle=plural(len(rows), "item"))


#: A queue row shows the post's opening words; the full text is one click away on the item page.
QUEUE_TITLE_CHARS = 140


async def queue(request: web.Request) -> web.Response:
    """The claimable items, in the order the worker will take them, as a table with move buttons."""
    db: Database = request.app["db"]
    counts = await db.status_counts()
    items = await db.queue_order(sorted(RUNNING))

    if not items:
        return page("Run order", empty_state(
            "Nothing queued", "The pipeline is idle.",
            f"<a class=tab href='{OVERVIEW_HREF}'>Back to the board</a>", "order"),
            counts, ACTIVE_QUEUE, notice=notice_param(request))

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for position, item in enumerate(items):
        held = bool(item.next_attempt_at and item.next_attempt_at > now)
        retry_time = ""
        if held:
            retry_time = f"<span class=prio title='{esc(item.next_attempt_at)}'>backing off until {esc(item.next_attempt_at[:16].replace('T', ' '))}</span>"

        # Move buttons with aria-labels, min-height >= --ctl-h (36px)
        move_buttons = []
        if position > 0:
            move_buttons += [
                f"<form method=post action='/queue/{item.id}/top'>"
                f"<button class='btn icon' aria-label='Move #{item.id} to top' title='run next'>⬆</button></form>",
                f"<form method=post action='/queue/{item.id}/up'>"
                f"<button class='btn icon' aria-label='Move #{item.id} up' title='run sooner'>▲</button></form>",
            ]
        if position < len(items) - 1:
            move_buttons.append(
                f"<form method=post action='/queue/{item.id}/down'>"
                f"<button class='btn icon' aria-label='Move #{item.id} down' title='run later'>▼</button></form>")

        status_cell = status_badge(item.status)
        title = plain_title(item.raw_text, QUEUE_TITLE_CHARS)
        age_text = ago(item.status_updated_at) or "—"

        rows.append(
            f"<tr>"
            f"<td class='r nw'>{position + 1}</td>"
            f"<td class=nw><a class=rid href='/item/{item.id}'>#{item.id}</a></td>"
            f"<td class=qt><a class=qtitle href='/item/{item.id}'>{esc(title)}</a></td>"
            f"<td class=nw>{status_cell}</td>"
            f"<td class=nw>{esc(source_label(item.source))}</td>"
            f"<td class='r nw'>{age_text}</td>"
            f"<td class=nw>{retry_time}</td>"
            f"<td><div class=acts>{''.join(move_buttons)}</div></td>"
            f"</tr>"
        )

    head = "".join(f"<th scope=col{c}>{t}</th>" for t, c in (
        ("Position", " class='r nw'"), ("#", " class=nw"), ("Title", " class=qt"), ("Status", " class=nw"),
        ("Source", " class=nw"), ("Age", " class='r nw'"), ("Retry", " class=nw"), ('<span class=sr>Actions</span>', "")))
    table = (
        f"<div class=tbl-wrap><table class='tbl queue' aria-label='Run order'>"
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )

    body = (f"<p class=crumb><b>{len(items)}</b> queued, next first"
            f"<span class=grow></span>"
            f"<span>reordering rewrites the run order immediately</span></p>"
            f"{table}")
    return page("Run order", body, counts, ACTIVE_QUEUE, refresh=True,
                notice=notice_param(request), subtitle=plural(len(items), "item") + " queued, next first")


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
        raise web.HTTPBadRequest(text="unknown move")

    await db.reorder(order)
    raise redirect_with_notice("/queue", "moved")


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


#: ``logging`` line shape set in pipeline/__main__.py: "<asctime> <LEVEL>  <logger>: <message>".
#: Only this header column is the level — a message that merely mentions "Error" is not an error.
_LOG_HEADER = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (?P<level>[A-Z]+)\s")
LOG_LEVEL_RANK = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
#: The ``level`` filter values (at-or-above) -> minimum rank. ``all`` keeps unparseable lines too.
LOG_LEVELS = {"all": 0, "warning": LOG_LEVEL_RANK["WARNING"], "error": LOG_LEVEL_RANK["ERROR"]}
LOG_LEVEL_LABEL = {"all": "All levels", "warning": "Warning and above", "error": "Error and above"}
LOG_LINES_DEFAULT = 500
LOG_SHOW_ALL = 0


def log_records(text: str) -> list[tuple[int, str]]:
    """Split a log into (rank, text) entries, oldest first. A line that is not a log header — a
    traceback frame, a wrapped message — belongs to the entry above it, so filtering and
    reversing never separate a traceback from the ERROR that printed it. Rank is 0 for text that
    precedes the first header."""
    records: list[list] = []
    for line in text.splitlines():
        header = _LOG_HEADER.match(line)
        if header or not records:
            rank = LOG_LEVEL_RANK.get(header["level"], 0) if header else 0
            records.append([rank, [line]])
        else:
            records[-1][1].append(line)
    return [(rank, "\n".join(lines)) for rank, lines in records]


def filter_log(text: str, *, q: str = "", level: str = "all",
               pattern: re.Pattern | None = None) -> list[tuple[int, str]]:
    """Entries matching ``q`` (case-insensitive substring), at least ``level`` (a LOG_LEVELS key;
    unknown means all) and ``pattern`` (a compiled regex), oldest first. Shared by /logs and the
    per-item log so the two cannot disagree about what a match is."""
    floor = LOG_LEVELS.get(level, 0)
    needle = q.lower()
    return [(rank, body) for rank, body in log_records(text)
            if rank >= floor
            and (not needle or needle in body.lower())
            and (pattern is None or pattern.search(body))]


def newest_first_lines(records: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Newest entry first — the page answers "what just happened", and 25,000 lines oldest-first
    buried it. Each entry's own lines keep their order, so a traceback still reads down."""
    return list(reversed(records))


def item_log(item_id: int) -> str:
    if not _log_files():
        return ""
    entries = newest_first_lines(
        filter_log(_log_tail(), pattern=re.compile(rf"\bitem {item_id}\b")))
    lines = "\n".join(body for _, body in entries).splitlines()
    return "\n".join(lines[:ITEM_LOG_LINES] if ITEM_LOG_LINES else lines)


#: The pipeline drawn as a straight line. Includes the two statuses that are
#: NOT requeue targets — the human gate and the terminal state — because a
#: timeline that omitted them would not match the path the item actually took.
#: Only the stages in TARGETS are clickable; the other two render as markers.
PIPELINE = [
    "ingested", "extracted", "triaged", "researched", "synthesized",
    "composed", "rendered", "awaiting_approval", "approved", "publishing",
    "published",
]


def regen_form(item) -> str:
    """Ask for a rewrite, with a note saying what should change.

    The Telegram bot has offered this since the beginning and the dashboard
    never did, so an operator working from the browser could only requeue —
    which throws the deck away and re-runs the search. This is the other
    thing: same brief, same research, one compose call with an instruction.

    The bot asks for the note in a second message because a chat has no form
    fields. A browser does, so it is one step here.
    """
    if not item.brief or item.status == Status.PUBLISHED.value:
        return ""
    note = esc(item.regen_note or "")
    return (
        "<h2 class=tl-head>regenerate · same research, new deck</h2>"
        f"<form method=post action='/item/{item.id}/regen' class=regen>"
        f"<input name=note aria-label='{esc(REGEN_LABEL)}' placeholder='what should change? (optional)' "
        f"value='{note}' autocomplete=off>"
        "<button>regenerate</button></form>"
    )


def caption_form(item) -> str:
    """Rewrite just the caption, without re-rendering nine slides."""
    if item.status != Status.AWAITING_APPROVAL.value:
        return ""
    return (
        f"<form method=post action='/item/{item.id}/caption' class=capform>"
        f"<textarea name=caption rows=5 aria-label='Caption'>{esc(item.caption or '')}</textarea>"
        "<button>save caption</button></form>"
    )


def timeline_control(item, events: list | None = None) -> str:
    """The pipeline as clickable stages — click one to requeue there.

    Replaces a dropdown of stage names. The dropdown gave no sense of where the
    item was or what "requeue to composed" would actually throw away; the line
    shows both, because every stage after the one you click is work that gets
    cleared.
    """
    failed = failed_stage(item, events)
    here = PIPELINE.index(failed or item.status) if (failed or item.status) in PIPELINE else None
    nodes = []
    for index, stage in enumerate(PIPELINE):
        if here is None:
            state = "off"          # dropped/failed/needs_input — not on the line
        elif index < here:
            state = "done"
        elif index == here:
            state = "fail" if failed else "now"
        else:
            state = "todo"
        label = stage.replace("_", " ")
        note = "<span class=tl-note>failed here</span>" if state == "fail" else ""
        if stage in TARGETS:
            hint = (f"requeue to {stage} — clears {stage} onward"
                    if state not in ("now", "fail") else f"re-run from {stage}")
            nodes.append(
                f"<form method=post action='/item/{item.id}/requeue' class=tl-step"
                f" data-confirm='{esc(TIMELINE_CONFIRM.format(stage=label))}'>"
                f"<input type=hidden name=to value='{stage}'>"
                f"<button class='tl-node {state}' title='{esc(hint)}'"
                f" aria-label='{esc(hint)}{', failed here' if note else ''}'>"
                f"<span class=tl-dot></span><span class=tl-lbl>{esc(label)}</span>{note}"
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


#: Accessible names for the item page's free-text fields (a placeholder is not a label).
REGEN_LABEL = "Note for a new version of the deck (optional)"


def compose_form(item, placeholder: str, *, autofocus: bool = False) -> str:
    """The one /say form (textarea + send). ``autofocus`` is for a page whose job is this answer."""
    focus = " autofocus" if autofocus else ""
    return (f"<form method=post action='/item/{item.id}/say' class=compose>"
            f"<textarea name=text aria-label='{esc(placeholder.rstrip('…'))}' placeholder='{esc(placeholder)}'"
            f" required{focus}></textarea>"
            f"<button class=go>send</button></form>")


def chat_html(messages: list[dict], item, *, targeted: bool = False, compose: bool = True) -> str:
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
        hint = (
            "Your message becomes a revision note and is applied as a targeted edit."
            if targeted
            else "Your message becomes a revision note and the deck is recomposed."
        )
        placeholder = "What should change…"
    elif item.status == Status.REVISION_CONFIRM.value:
        hint = "A Yes/No is waiting above. A message here replaces it with a new note."
        placeholder = "What should change…"
    else:
        hint = ("Nothing is waiting on you here. A message is recorded on the "
                "thread but changes nothing until it is waiting again.")
        placeholder = "Leave a note…"

    return (
        f"<div class=chat>{''.join(bubbles)}</div>"
        f"{compose_form(item, placeholder) if compose else ''}"
        f"<p class=hint>{esc(hint)}</p>"
    )


def confirm_card(item) -> str:
    """The destructive plan waiting on a Yes/No, with the same two buttons Telegram has.
    The token in the form is the one the plan was asked with, so a page opened before a
    newer note replaced the question gets 'out of date', not a stray Yes."""
    pc = pending_confirm(item.revision_request)
    view = rcopy.confirm_view_for(item) if item.status == Status.REVISION_CONFIRM.value else None
    if pc is None or view is None:
        return ""
    token = esc(pc["token"])
    forms = "".join(
        f"<form method=post action='/item/{item.id}/{verb}'>"
        f"<input type=hidden name=token value='{token}'><button class='{cls}'>{esc(label)}</button></form>"
        for verb, label, cls in (("revise_yes", view.yes, "go"), ("revise_no", view.no, ""))
    )
    return card("waiting on your answer",
                f"<pre>{esc(view.text)}</pre><div class=bar-acts>{forms}</div>")


def revision_card(item) -> str:
    """What each revision did, newest first, as the same words the Telegram preview uses.
    Everything is escaped: notes, asks and headlines are the operator's / a model's text."""
    revision = item.revision if isinstance(item.revision, dict) else {}
    runs = revision.get("runs") if isinstance(revision.get("runs"), list) else []
    if not runs:
        return ""
    current = latest_run(revision, item.slides)
    themes = available_themes()
    blocks = []
    for run in reversed(runs):
        if not isinstance(run, dict):
            continue
        text = rcopy.run_summary(
            run, slides=item.slides if run is current else None, themes=themes)
        stamp = esc(str(run.get("at") or "")[:19].replace("T", " "))
        blocks.append(
            f"<details{' open' if run is current else ''}><summary>"
            f"{esc(run.get('origin'))} · {esc(run.get('surface'))} · {stamp}</summary>"
            f"<pre>{esc(text)}</pre></details>")
    return card(f"revisions · {len(blocks)}", "".join(blocks)) if blocks else ""


#: Statuses whose items need the operator ("Review" lane): "Next waiting" walks these.
ATTN_STATUSES = LANE_BY_KEY["attn"][2]
WAITING_HREF = f"{ITEMS_HREF}?lane=attn"
#: Longest title/headline shown before it is cut with an ellipsis.
DETAIL_TITLE_LIMIT = 140
#: DOM ids shared by the markup and the viewer script (UX-4 fence in dashboard.js).
VIEWER_ID = "slide-viewer"
CHANGES_ID = "changes"
#: The prompt each decision verb asks before it submits (``data-confirm``; the shell script shows it
#: and the server's own guards stay the authority). Each says what will happen.
DECISION_CONFIRM = {
    "approve": "Approve and publish this item? It will be posted to Instagram.",
    "reject": "Reject this item? It will not be published.",
    "drop": "Drop this item? It leaves the queue and will not be published.",
}
#: Approving an item that is already on the account posts a SECOND carousel: say so in the prompt.
DOUBLE_POST_CONFIRM = ("This item was already published. Approving again posts another carousel "
                       "to the account. Approve anyway?")
#: Clicking a stage on the timeline clears that stage onward.
TIMELINE_CONFIRM = "Re-run from {stage}? Everything from {stage} onward is cleared and run again."


def decision_confirm(item, verb: str) -> str:
    """Confirm text for a decision verb on this item (approve gets the double-post warning)."""
    if verb == "approve" and (item.publish_log or []):
        return DOUBLE_POST_CONFIRM
    return DECISION_CONFIRM[verb]


async def waiting_neighbours(db: Database, item_id: int) -> dict:
    """prev/next by id plus the next *waiting* item: the nearest attn-lane id above this one,
    wrapping to the lowest other attn id. Placeholders come from the status constants only."""
    marks = ",".join("?" * len(ATTN_STATUSES))
    rows = await db.conn.execute_fetchall(
        "SELECT (SELECT MAX(id) FROM items WHERE id < ?) AS prev,"
        "       (SELECT MIN(id) FROM items WHERE id > ?) AS next,"
        f"      (SELECT MIN(id) FROM items WHERE id > ? AND status IN ({marks})) AS wnext,"
        f"      (SELECT MIN(id) FROM items WHERE id != ? AND status IN ({marks})) AS wfirst",
        (item_id, item_id, item_id, *ATTN_STATUSES, item_id, *ATTN_STATUSES))
    row = rows[0]
    return {"prev": row["prev"], "next": row["next"], "waiting": row["wnext"] or row["wfirst"]}


def item_crumb(nav: dict) -> str:
    """Waiting-list link, prev/next by id, and the 'Next waiting' jump (hidden when none)."""
    prev, nxt, waiting = nav["prev"], nav["next"], nav["waiting"]
    crumb = [f"<a href='{esc(WAITING_HREF)}'>← Waiting list</a><span class=grow></span>",
             f"<a href='/item/{prev}'>← #{prev}</a>" if prev else "<span class=crumb-off>← prev</span>",
             f"<a href='/item/{nxt}'>#{nxt} →</a>" if nxt else "<span class=crumb-off>next →</span>"]
    if waiting:
        crumb.append(f"<a class='btn next-waiting' href='/item/{waiting}'>Next waiting →</a>")
    return f"<p class=crumb>{''.join(crumb)}</p>"


def decision_bar(item) -> str:
    """Sticky Approve / Ask for changes / Reject / Drop for an item awaiting approval. The
    forms are the existing POST routes; 'Ask for changes' jumps to the note + chat card."""
    if item.status != Status.AWAITING_APPROVAL.value:
        return ""
    approve = action(item.id, "approve", "Approve &amp; publish", "go",
                     confirm=decision_confirm(item, "approve"))
    reject = action(item.id, "reject", "Reject", "no", confirm=DECISION_CONFIRM["reject"])
    drop = action(item.id, "drop", "Drop", "quiet", confirm=DECISION_CONFIRM["drop"])
    return (f"<div class=decide role=group aria-label='Decision'>{published_warning(item)}"
            f"<div class=decide-acts>{approve}"
            f"<a class=btn href='#{CHANGES_ID}'>Ask for changes</a>{reject}"
            f"<span class=grow></span>{drop}</div></div>")


def item_actions(item) -> str:
    """Actions card for every status except awaiting approval (the bar covers that one)."""
    if item.status == Status.AWAITING_APPROVAL.value:
        return ""
    controls = []
    if item.status in RUNNING:
        controls.append("<a class=tab href='/queue'>▲ run order</a>")
    controls.append(action(item.id, "drop", "Drop", "no", confirm=DECISION_CONFIRM["drop"]))
    return card("Actions", f"{published_warning(item)}<div class=bar-acts>{''.join(controls)}</div>")


def item_header(item) -> str:
    """Title, status badges and the source text (collapsed). Errors are NOT here: see
    ``failure_card`` / ``question_card`` (the raw text lives in Technical details)."""
    meta = [status_badge(item.status),
            f"<span class=badge>{esc(source_label(item.source))}</span>",
            f"<span class=badge>{esc(item.intent or 'news')}</span>"]
    if item.theme:
        meta.append(f"<span class=badge>{esc(item.theme)}</span>")
    if item.priority:
        meta.append(f"<span class=prio>priority {item.priority:+d}</span>")
    when = ago(item.status_updated_at)
    if when:
        meta.append(f"<span class=badge>{esc(when)}</span>")
    return (f"<div class='card item-head'><div class=head>{''.join(meta)}</div>"
            f"<p class=item-title>{esc(plain_title(item.raw_text, DETAIL_TITLE_LIMIT))}</p>"
            f"<details class=source><summary>Source text</summary>"
            f"<p class=req>{esc(item.raw_text)}</p></details></div>")


def slide_alt(item, n: int, total: int) -> str:
    """'Slide 3 of 7' plus the slide's headline when the deck JSON has one (escaped by caller)."""
    slides = item.slides if isinstance(item.slides, list) else []
    slide = slides[n - 1] if n - 1 < len(slides) and isinstance(slides[n - 1], dict) else {}
    raw = str(slide.get("headline") or "")
    headline = plain_title(raw, DETAIL_TITLE_LIMIT) if raw.strip() else ""
    return f"Slide {n} of {total}" + (f": {headline}" if headline else "")


GONE_TITLE = "Images no longer available"
GONE_HINT = "The slide images were cleaned up. Re-run from the render stage to make them again."


def slide_gallery(item, media_root: Path | None = None) -> str:
    """Large slide tiles plus a native <dialog> viewer. Each tile is a plain link to the image
    (the no-JS fallback); the UX-4 script turns a click into the viewer. Media via /media only."""
    paths = item.rendered_paths or []
    if not paths:
        return ""
    if media_root is not None:
        paths = [p for p in paths if (media_root / str(item.id) / Path(p).name).is_file()]
        if not paths:
            return card("Deck", empty_state(GONE_TITLE, GONE_HINT, icon_name="alert"))
    total = len(paths)
    tiles = []
    for n, path in enumerate(paths, start=1):
        src = f"/media/{item.id}/{esc(Path(path).name)}"
        alt = esc(slide_alt(item, n, total))
        tiles.append(
            f"<a class=slide href='{src}' data-slide='{n}' target=_blank rel=noopener"
            f" aria-label='View {alt}'>"
            f"<img src='{src}' alt='{alt}' loading=lazy"
            f" onerror=\"this.classList.add('broken')\"></a>")
    viewer = (
        f"<dialog id={VIEWER_ID} class=viewer aria-label='Slide viewer'>"
        "<div class=viewer-in><div class=viewer-bar>"
        "<span class=viewer-count aria-live=polite data-count></span>"
        "<form method=dialog><button class=viewer-close aria-label='Close viewer'>Close</button></form>"
        "</div><div class=viewer-body>"
        "<button class=viewer-prev type=button aria-label='Previous slide'>←</button>"
        "<img class=viewer-img alt=''>"
        "<button class=viewer-next type=button aria-label='Next slide'>→</button>"
        "</div></div></dialog>")
    return card(f"Deck · {plural(total, 'slide')}", f"<div class=gallery>{''.join(tiles)}</div>{viewer}")


def caption_card(item) -> str:
    if not item.caption:
        return ""
    editor = caption_form(item)
    return card("Caption", editor or f"<pre>{esc(item.caption)}</pre>")


def retry_form(item, stage: str) -> str:
    """Retry = the existing /requeue route aimed at the failed stage (clears that stage onward)."""
    ask = f" data-confirm='{esc(RETRY_CONFIRM)}'" if RETRY_CONFIRM else ""
    return (f"<form method=post action='/item/{item.id}/requeue'{ask}>"
            f"<input type=hidden name=to value='{esc(stage)}'>"
            f"<button class='btn go'>Retry: {esc(STAGE_ACTION[stage])}</button></form>")


def failure_card(item, events: list) -> str:
    """Why a stopped or struggling item stopped, in our words, with the one-click fix.
    failed: 'Failed while <stage>' + reason + Retry + 'Other stages'. A running item that has
    errored and is backing off gets a quieter 'will retry' note. Otherwise nothing."""
    reason = esc(explain_error(item.last_error))
    if item.status == Status.FAILED.value:
        stage = failed_stage(item, events)
        where = f"Failed while {esc(STAGE_ACTION[stage])}" if stage else "Failed"
        retry = retry_form(item, stage) if stage else ""
        other = (f"<a class='btn quiet' href='#{STAGE_ID}'>Other stages</a>")
        return (f"<div class='card failure' role=alert><div class=card-head><h2>{where}</h2></div>"
                f"<p class=reason>{reason}</p>"
                f"<div class=bar-acts>{retry}{other}</div></div>")
    if item.status == Status.REVISION_REQUESTED.value and item.last_error:
        return card("Rewriting", f"<p class=reason>{esc(rcopy.REVISION_WAITING_SUMMARY)}</p>")
    if item.status in RUNNING and item.last_error:
        return card("Last attempt hit a problem",
                    f"<p class=reason>{reason}</p><p class=hint>It will be tried again automatically.</p>")
    return ""


#: The pipeline's question ends with Telegram-only instructions ("/post to send what I have ...").
#: The dashboard has no such command, so the tail is replaced by plain words about where it works.
QUESTION_TAIL = re.compile(r"\s*Reply with a better angle or a source,.*\Z", re.S)
QUESTION_TELEGRAM_HINT = ("Type a better angle or a source below. Sending what was found as it is "
                          "can only be done in Telegram; here you can also drop the item.")
TELEGRAM_COMMAND = re.compile(r"/(?:post|skip|drop)\b")


def dashboard_question(text: str) -> tuple[str, str]:
    """(question to show, extra hint). Telegram command words never reach the page."""
    body, tail = QUESTION_TAIL.subn("", text or "")
    hint = QUESTION_TELEGRAM_HINT if tail else ""
    return TELEGRAM_COMMAND.sub("(Telegram only)", body).strip(), hint


def question_card(item) -> str:
    """needs_input: the pipeline's question first, with the answer box focused."""
    if item.status != Status.NEEDS_INPUT.value:
        return ""
    shown, hint = dashboard_question(item.question or "")
    question = esc(shown or "The pipeline needs more information to continue.")
    return card("Needs your answer",
                f"<p class=question>{question}</p>" + (f"<p class=hint>{esc(hint)}</p>" if hint else "")
                + compose_form(item, "Answer the question…", autofocus=True))


def changes_card(item, chat: str) -> str:
    """Where feedback goes: regenerate-with-note and the conversation (anchor of 'Ask for changes')."""
    return (f"<div class=card id={CHANGES_ID}><div class=card-head><h2>Conversation</h2></div>"
            f"{regen_form(item)}{chat}</div>")


def stage_card(item, events: list | None = None) -> str:
    return (f"<div id={STAGE_ID}>"
            + card("Stage · click to requeue", timeline_control(item, events)) + "</div>")


def technical_details(item, events: list, log: str) -> str:
    """Folded diagnostics (transitions, log, research, slide JSON, clauses, brief), human titles."""
    def fold(title: str, text: str) -> str:
        return f"<details><summary>{esc(title)}</summary><pre>{esc(text)}</pre></details>"

    blocks = []
    if item.last_error:
        blocks.append(fold("Error (as recorded)", error_text(item.status, item.last_error)))
    if events:
        timeline = "\n".join(
            f"{e['at'][:19].replace('T', ' ')}  {e['from_status'] or '—':>18}"
            f" → {e['to_status']}"
            + (f"   ({e['detail']})" if e.get("detail") else "")
            for e in events)
        blocks.append(fold(f"Status history ({plural(len(events), 'transition')})", timeline))
    if log:
        blocks.append(fold(f"Log ({plural(len(log.splitlines()), 'line')})", log))
    for title, noun, value in (("Research", "source", item.research),
                               ("Slide data", "slide", item.slides),
                               ("Clauses", "clause", item.clauses)):
        if value:
            blocks.append(fold(f"{title} ({plural(len(value), noun)})", json.dumps(value, indent=1)))
    if item.brief:
        blocks.append(fold("Brief", item.brief))
    return card("Technical details", "".join(blocks)) if blocks else ""


async def detail(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    counts = await db.status_counts()
    item_id = int(request.match_info["id"])
    item = await db.get_item(item_id)
    if item is None:
        raise web.HTTPNotFound(text="no such item")

    events = await db.events_for(item.id)
    chat = chat_html(await db.messages_for(item.id), item, targeted=revise_on(request),
                     compose=item.status != Status.NEEDS_INPUT.value)
    settings = request.app.get("settings")
    media_dir = getattr(settings, "media_dir", None)
    gallery = slide_gallery(item, Path(media_dir).resolve() if media_dir else None)
    caption = caption_card(item)
    deck = (f"<div class=item-cols><div>{gallery}</div><div>{caption}</div></div>"
            if gallery and caption else gallery + caption)
    parts = [
        item_crumb(await waiting_neighbours(db, item_id)),
        item_header(item),
        failure_card(item, events),
        question_card(item),
        decision_bar(item),
        confirm_card(item),
        deck,
        changes_card(item, chat),
        revision_card(item),
        item_actions(item),
        stage_card(item, events),
        technical_details(item, events, item_log(item.id)),
    ]
    return page(f"#{item.id} - {status_label(item.status)}", "".join(parts), counts,
                LANE_OF.get(item.status, ""), refresh=item.status in RUNNING,
                heading=f"Item {item.id}", notice=notice_param(request))


# --------------------------------------------------------------- actions


async def new(request: web.Request) -> web.Response:
    """Queue a request typed here, exactly as a DM would arrive."""
    db: Database = request.app["db"]
    settings: Settings = request.app["settings"]

    form = await request.post()
    try:
        item_id = await queue_request(db, settings, str(form.get("text") or ""))
    except TooThin:
        # Back to the board with the reason rather than a bare error page —
        # the same rule the bot applies, worded the same way.
        raise web.HTTPFound(
            f"{ITEMS_HREF}?thin={THIN_FLAG}&draft={quote(str(form.get('text') or '')[:DRAFT_MAX])}")

    raise redirect_with_notice(f"/item/{item_id}", "queued")


async def _request_revision(db: Database, item, note: str) -> None:
    """Ask the revise stage for a targeted edit (an empty note is a fresh version).
    ``False`` from the guarded write means someone moved the item first: say so."""
    ok = await request_revision(
        db, item, origin=Origin.OPERATOR if note else Origin.SKIP, note=note or None,
        surface=Surface.DASHBOARD,
    )
    if not ok:
        raise web.HTTPConflict(text=rcopy.ITEM_MOVED.format(id=item.id))


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
    elif item.status == Status.AWAITING_APPROVAL.value and not revise_on(request):
        # SYNTHESIZED re-runs compose; COMPOSED would only re-render the slides
        # that are already there.
        await db.transition(item_id, Status.SYNTHESIZED, {"regen_note": text},
                            detail="dashboard revision")
    elif item.status in (Status.AWAITING_APPROVAL.value, Status.REVISION_CONFIRM.value):
        # The agentic path (REVISE_ENABLED): a targeted edit, guarded on the item's
        # status and timestamp so a Telegram approve that landed first wins.
        await _request_revision(db, item, text)
    # Any other status: recorded on the thread, nothing to act on.

    answered = item.status == Status.NEEDS_INPUT.value
    raise redirect_with_notice(f"/item/{item_id}", "answered" if answered else "sent")


async def act(request: web.Request) -> web.Response:
    """Approve, reject, regen, caption, drop or requeue.

    Mirrors the bot's guards and, for regen and caption, its semantics:
    the dashboard offering fewer actions than Telegram meant an operator
    working from the browser had no way to ask for a rewrite at all.
    """
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
            raise web.HTTPBadRequest(text="not a requeueable stage")
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
    elif verb == "regen":
        # SYNTHESIZED, not COMPOSED — the same choice the bot makes. A status
        # names the stage that finished and the registry maps it to the NEXT
        # one, so COMPOSED would re-render the same slides while SYNTHESIZED
        # re-runs compose. The brief is reused, so this is one model call, not
        # a re-research; the stage timeline is what you want for that.
        if not item.brief:
            raise web.HTTPConflict(
                text=f"item {item_id} has no brief yet — nothing to recompose. "
                     f"Use the stage timeline to re-run research.")
        if item.status == Status.PUBLISHED.value:
            raise web.HTTPConflict(
                text=f"item {item_id} is already published; requeue it instead")
        note = str((await request.post()).get("note") or "").strip()
        if revise_on(request) and item.status == Status.REVISION_REQUESTED.value:
            raise web.HTTPConflict(text=f"item {item_id} is being revised right now; wait for the new preview")
        if revise_on(request) and item.status in REVISABLE_FROM:
            await _request_revision(db, item, note)
        else:
            await db.transition(item_id, Status.SYNTHESIZED,
                                {"regen_note": note or None}, detail="dashboard regen")
    elif verb in CONFIRM_VERBS:
        token = str((await request.post()).get("token") or "")
        result = await confirm_revision(db, item_id, CONFIRM_VERBS[verb], token)
        if result not in CONFIRMED:
            raise web.HTTPConflict(text=rcopy.CONFIRM_TOASTS[result])
    elif verb == "caption":
        # Just the words. Rewriting the caption should not cost a re-render of
        # nine slides, so the item stays where it is.
        if item.status != Status.AWAITING_APPROVAL.value:
            raise web.HTTPConflict(
                text=f"item {item_id} is '{item.status}', not awaiting approval")
        caption = str((await request.post()).get("caption") or "").strip()
        if not caption:
            raise web.HTTPBadRequest(text="an empty caption would replace a real one")
        await db.update_fields(item_id, {"caption": caption})
    elif verb == "drop":
        await db.transition(item_id, Status.DROPPED, detail="dashboard")
    else:
        raise web.HTTPBadRequest(text="unknown action")

    raise back_with_notice(request, VERB_NOTICE[verb])


async def media(request: web.Request) -> web.FileResponse:
    """Serve a rendered slide, refusing anything outside the media directory."""
    settings: Settings = request.app["settings"]
    root = Path(settings.media_dir).resolve()
    target = (root / request.match_info["id"] / request.match_info["name"]).resolve()
    # A crafted name must not walk out of media_dir into the rest of the disk.
    if not target.is_relative_to(root) or not target.is_file():
        raise web.HTTPNotFound(text="no such file")
    return web.FileResponse(target)


def _log_n(raw: str) -> int:
    """``n`` query value -> line cap. Absent, non-numeric or negative means the default;
    0 (LOG_SHOW_ALL) means every line."""
    try:
        n = int(raw)
    except ValueError:
        return LOG_LINES_DEFAULT
    return n if n >= 0 else LOG_LINES_DEFAULT


def _log_line_html(rank: int, line: str) -> str:
    """One log line, tinted by its entry's level (the level word is still in the text)."""
    cls = "error" if rank >= LOG_LEVEL_RANK["ERROR"] else "warning" if rank >= LOG_LEVEL_RANK["WARNING"] else ""
    return f"<span class='lv-{cls}'>{esc(line)}</span>" if cls else esc(line)


async def logs(request: web.Request) -> web.Response:
    """The log, newest first. GET ``q`` (substring), ``level`` (LOG_LEVELS key), ``n`` (line cap,
    default LOG_LINES_DEFAULT, 0 = all). The filter form is always shown, even with no match."""
    db: Database = request.app["db"]
    counts = await db.status_counts()
    if not _log_files():
        return page("Logs", empty_state("No log file yet", "It appears once the pipeline runs.",
                                        icon_name="file-text"), counts, ACTIVE_LOGS)

    q = (request.query.get("q") or "").strip()
    level = (request.query.get("level") or "").strip().lower()
    level = level if level in LOG_LEVELS else "all"
    n = _log_n((request.query.get("n") or "").strip())

    text = _log_tail()
    entries = newest_first_lines(filter_log(text, q=q, level=level))
    flat = [(rank, line) for rank, body in entries for line in body.splitlines()]
    lines_total = len(flat)
    shown = flat if n == LOG_SHOW_ALL else flat[:n]
    used = len(shown)

    level_opts = "".join(
        f"<option value='{key}'{' selected' if key == level else ''}>{esc(label)}</option>"
        for key, label in LOG_LEVEL_LABEL.items())
    filters = (
        "<div class=card><form class=log-filters method=get action='/logs'>"
        f"<div class=field><label for=log-q>Search logs</label>"
        f"<input id=log-q name=q value='{esc(q)}' placeholder='Text or item id' autocomplete=off></div>"
        f"<div class=field><label for=log-level>Level</label>"
        f"<select id=log-level name=level>{level_opts}</select></div>"
        f"<div class=field><label for=log-n>Lines to show (0 = all)</label>"
        f"<input id=log-n name=n type=number min=0 value='{n}'></div>"
        "<button class=go>Apply</button><a class=tab href='/logs'>Clear</a></form></div>")

    if not shown:
        return page("Logs", filters + empty_state(
            "No log lines match", "Try a different search or level.",
            "<a class=tab href='/logs'>Clear filters</a>", "file-text"), counts, ACTIVE_LOGS)

    more = ""
    if n != LOG_SHOW_ALL and used < lines_total:
        params = {k: v for k, v in (("q", q), ("level", level), ("n", LOG_SHOW_ALL)) if v != "" and
                  not (k == "level" and v == "all")}
        more = f" <a class=card-link href='/logs?{urlencode(params)}'>Show all</a>"
    files = esc(", ".join(p.name for p in _log_files()))
    heading = (f"Newest first · Showing {used:,} of {lines_total:,} lines"
               + (f" (filtered from {len(text.splitlines()):,})" if q or level != "all" else ""))
    body = (filters + f"<div class=card><div class=card-head><h2>{heading}</h2>{more}</div>"
            f"<p class=hint>{files}</p>"
            f"<pre>{chr(10).join(_log_line_html(r, ln) for r, ln in shown)}</pre></div>")
    return page("Logs", body, counts, ACTIVE_LOGS)


#: (heading, sentence) per status for the styled error page. Our words, never the exception.
ERROR_COPY = {
    400: ("That request couldn't be processed", "Go back, check the form and try again."),
    404: ("Not found", "That page or item doesn't exist. It may have been removed."),
    409: ("Nothing was changed", "Someone or something changed this item first. "
                                  "Here is where it stands now."),
}
ERROR_COPY_4XX = ("That didn't work", "Go back and try again.")
ERROR_COPY_5XX = ("Something went wrong on our side",
                  "It has been logged on the server. Try again in a moment.")


async def error_body(request: web.Request, status: int, text: str) -> tuple[str, str]:
    """(heading, body HTML) for an error page. The sentence is from ERROR_COPY; the only detail
    shown is a 409's ``text`` (our own closed wording) and the item's current state."""
    heading, sentence = ERROR_COPY.get(status) or (ERROR_COPY_5XX if status >= 500 else ERROR_COPY_4XX)
    parts = [f"<p class=reason>{esc(sentence)}</p>"]
    links = [f"<a class='btn go' href='{OVERVIEW_HREF}'>Back to the board</a>"]
    raw_id = request.match_info.get("id") if request.match_info is not None else None
    if status == 409 and raw_id and str(raw_id).isdigit():
        if text:
            parts.append(f"<p class=hint>{esc(text)}</p>")
        try:
            item = await request.app["db"].get_item(int(raw_id))
        except Exception:
            item = None
        if item is not None:
            parts.append(f"<p>Item {item.id} is now: {status_badge(item.status)}</p>")
            links.insert(0, f"<a class='btn go' href='/item/{item.id}'>See item {item.id}</a>")
    return heading, card(heading, "".join(parts) + f"<div class=bar-acts>{''.join(links)}</div>")


@web.middleware
async def error_pages(request: web.Request, handler):
    """Browsers (Accept: text/html) get a styled page inside the shell with the SAME status code;
    anything else (tests, curl, scripts) gets aiohttp's plain response untouched. An unexpected
    exception is logged here and never shown (rule 15)."""
    allow = None
    try:
        return await handler(request)
    except web.HTTPRedirection:
        raise
    except web.HTTPException as exc:
        if exc.status < 400 or "text/html" not in request.headers.get("Accept", ""):
            raise
        status, text = exc.status, exc.text or ""
        allow = exc.headers.get("Allow")      # a 405 must still say which methods are allowed
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("unhandled error on %s %s", request.method, request.path)
        if "text/html" not in request.headers.get("Accept", ""):
            raise
        status, text = 500, ""
    try:
        counts = await request.app["db"].status_counts()
    except Exception:
        counts = {}
    heading, body = await error_body(request, status, text)
    response = page(heading, body, counts)
    response.set_status(status)
    if allow:
        response.headers["Allow"] = allow
    return response


async def build(settings: Settings) -> web.Application:
    app = web.Application(middlewares=[error_pages])
    app["settings"] = settings
    app["db"] = await Database(settings.db_path).connect()
    app.add_routes([
        web.get("/", home),
        web.get("/overview", overview),
        web.get(ITEMS_HREF, index),
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

    settings = Settings.load()
    # T85/D16: "regen" and "new" queue a DB transition the worker process
    # later acts on with a real model call — validate_model_config(), not
    # validate_for_run(), since "new" is documented to work "exactly as a DM
    # would arrive" with no Telegram account wired up yet.
    settings.validate_model_config()

    print(f"mission control on http://{settings.dashboard_host}:{args.port}")
    code = run_host_script("dashboard", lambda: web.run_app(
        build(settings), host=settings.dashboard_host, port=args.port, print=None))
    if code:
        raise SystemExit(code)
