"""Measure the ReDoS in ``eval_research.py``'s ``_visible_text`` and the
input-size cap that closes it (T130 bounce 1, ``_MAX_CHALLENGE_SCREEN_HTML_CHARS``).

``_visible_text``'s ``_SCRIPT_RE`` is a lazy-DOTALL pattern
(``<script\\b[^>]*>.*?</script>``) applied with ``.sub()`` -- QUADRATIC in
the number of UNCLOSED ``<script`` tags a page contains, because each one
triggers a full remaining-string search hunting for a ``</script>`` that
never arrives. ``is_challenge_page`` reaches this pass on any 200 response
whose ``<title>`` matches a challenge phrase (the title gate runs first and
is O(n)); fetched web text is untrusted by this repo's own standard
(D29/D60), and ``search.py``'s own ``MAX_BODY_BYTES`` (2,000,000 bytes)
already permits a page large enough to turn this into a multi-minute stall
from nothing more than a hostile ``<title>``.

house rule 4 requires a number that governs behaviour to be either cited
from a platform limit or produced by a re-runnable measurement whose output
is pasted into the task log -- this script, and its pasted output, is that
measurement for ``_MAX_CHALLENGE_SCREEN_HTML_CHARS``.

**Method.** Imports the REAL ``_visible_text``/``_MAX_CHALLENGE_SCREEN_HTML_CHARS``
straight off ``eval_research.py`` (same ``sys.path`` trick
``measure_relevance_excerpt.py`` uses -- ``scripts/`` is not an importable
package) rather than re-deriving the regexes here, so this measurement can
never drift from what the real code actually runs (DRY; coding-standards.md
non-negotiable). Builds an adversarial page out of realistic ~70-char
unclosed ``<script src="...">`` tags (a minimal ``<script>x`` tag
under-counts a real attacker payload's byte cost per tag and understates the
blowup), times ``_visible_text`` on the RAW html and again on the capped
``html[:CAP]`` the fix actually uses, and confirms a real long article's
visible-text count still clears ``_MAX_CHALLENGE_VISIBLE_TEXT_CHARS``
comfortably within that capped window.

No live model/network needed -- pure CPU regex timing, unlike
``measure_relevance_excerpt.py``.

Usage:
    ./.venv/bin/python scripts/measure_challenge_screen_redos.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_research  # noqa: E402  (reuse the real module, do not reimplement its regexes)

#: A realistic unclosed <script src="..."> tag (~70 chars) -- a minimal
#: "<script>x" (9 chars) undercounts what a real attacker page's per-tag
#: byte cost looks like and understates how fast MAX_BODY_BYTES is reached.
_UNCLOSED_TAG = '<script type="text/javascript" src="/assets/bundle.min.js?v=12345">'


def _adversarial_page(n_tags: int) -> str:
    return ("<html><head><title>Just a moment...</title></head><body>"
            + (_UNCLOSED_TAG * n_tags) + "</body></html>")


def _real_long_article() -> str:
    return ("<html><head><title>Local Startup Raises $5M</title></head>"
            "<body><article>"
            + ("<p>A local startup announced today it raised five million "
               "dollars in seed funding to build a new widget factory on "
               "the edge of town, with hiring expected to ramp through the "
               "next two quarters. </p>" * 40)
            + "</article></body></html>")


def run() -> None:
    cap = eval_research._MAX_CHALLENGE_SCREEN_HTML_CHARS
    threshold = eval_research._MAX_CHALLENGE_VISIBLE_TEXT_CHARS
    print(f"config: unclosed tag={_UNCLOSED_TAG!r} ({len(_UNCLOSED_TAG)} chars), "
          f"cap={cap}, visible_text_threshold={threshold}\n")

    print("uncapped (pre-fix behaviour -- _visible_text(html)):")
    for n in (1000, 5000, 20000):
        html = _adversarial_page(n)
        t0 = time.perf_counter()
        eval_research._visible_text(html)
        elapsed = time.perf_counter() - t0
        print(f"  {n:6} unclosed <script> tags  ({len(html):>9} html chars)  ->  {elapsed:7.2f}s")

    print("\ncapped (the fix, as is_challenge_page actually calls it -- "
          "_visible_text(html[:cap])):")
    for n in (1000, 5000, 20000):
        html = _adversarial_page(n)
        t0 = time.perf_counter()
        eval_research._visible_text(html[:cap])
        elapsed = time.perf_counter() - t0
        print(f"  {n:6} unclosed <script> tags  ({len(html):>9} html chars, "
              f"capped to {min(len(html), cap)})  ->  {elapsed:7.2f}s")

    article = _real_long_article()
    visible = eval_research._visible_text(article[:cap])
    print(f"\nreal long article stays unflagged within the cap: "
          f"html={len(article)} chars, visible={len(visible)} chars "
          f"(threshold={threshold}) -- "
          f"{'UNFLAGGED (correct)' if len(visible) >= threshold else 'FLAGGED (regression!)'}")


if __name__ == "__main__":
    run()
