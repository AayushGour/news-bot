"""Publish-time guard: no two consecutive Instagram posts share a theme (T358/U15).

The theme is chosen when a deck is rendered, long before it is published, and
previews are approved as shown. So the pick at render time only *prefers* to
avoid the last published theme; the authoritative check is here, run inside the
serialised publish flow (``publish_carousel`` holds the publish lock) where the
"last published" lookup cannot race another item's publish.

An approved preview is never changed ahead of publishing. At publish time, and
only then, a deck whose rendered theme equals the last post's is re-rendered
with that theme avoided, re-uploaded, and the operator is told in one line.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..errors import Recompose
from ..models import Item
from ..stages.render import is_auto_theme, item_media_dir, render
from .media_host import upload

log = logging.getLogger(__name__)

#: Operator copy for the swap (plain text: theme names are file stems, ours).
RETHEMED_NOTE = (
    "#{id} re-themed from {old} to {new} so it doesn't repeat the last post."
)
#: The deck would not fit the alternative theme, so the repeat is accepted.
KEPT_NOTE = (
    "#{id} keeps the {old} theme, same as the last post: the text did not fit "
    "another theme."
)

#: Scratch dir (inside the item's media dir) for a publish-time re-render.
SCRATCH_PREFIX = ".retheme-"

Notify = Callable[[str], Awaitable[Any]]


async def _say(notify: Notify | None, text: str) -> None:
    if notify is None:
        return
    try:
        await notify(text)
    except Exception:
        log.exception("theme-guard note failed to send")


async def ensure_distinct_theme(
    item: Item, *, db, settings, notify: Notify | None = None,
    render_fn=render, upload_fn=upload,
) -> list[str] | None:
    """Swap a repeated theme before publishing. Returns the new media urls, or None.

    None means "publish what is already uploaded": nothing to change, a pin, a
    named THEME, or the deck cannot take another theme (``Recompose`` -> original
    published, operator told). Any other render/upload failure propagates
    (``Retryable``): a repeat is never published silently. Render, upload and the
    three column writes are ordered so a failure leaves the row unchanged.
    """
    if not is_auto_theme(item, settings):
        log.info("item %s: theme is pinned/named; not swapping to avoid a repeat", item.id)
        return None
    last = await db.last_published_theme(exclude_id=item.id)
    if not last or item.rendered_theme != last:
        return None

    final_dir = item_media_dir(item.id, settings)
    scratch = final_dir / f"{SCRATCH_PREFIX}{uuid.uuid4().hex}"
    try:
        try:
            fields = await render_fn(item, settings, db=db, out_dir=scratch)
        except Recompose:
            log.warning("item %s: deck does not fit another theme; publishing %r", item.id, last)
            await _say(notify, KEPT_NOTE.format(id=item.id, old=last))
            return None
        new = fields.get("rendered_theme")
        if not new or new == last:
            log.warning("item %s: no other theme available; publishing %r", item.id, last)
            return None

        # Upload from scratch (keys come from file names, identical to the final
        # ones), then move into place: the deck on disk changes only once the
        # new one is known good.
        urls = await upload_fn(fields["rendered_paths"], item.id, settings)
        paths = []
        for src in fields["rendered_paths"]:
            dest = final_dir / Path(src).name
            os.replace(src, dest)
            paths.append(str(dest))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    await db.update_fields(item.id, {
        "rendered_paths": paths,
        "rendered_theme": new,
        "media_urls": urls,
    })
    log.info("item %s re-themed %r -> %r at publish time", item.id, last, new)
    await _say(notify, RETHEMED_NOTE.format(id=item.id, old=last, new=new))
    return urls
