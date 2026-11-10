"""Checks a deck has to pass before anything renders it.

The compose prompt asks for grounded slides, and mostly gets them. This is the
part that does not depend on asking.

The failure that motivated it: item 116 requested "the psychology of
narcissistic people and how they affect kids in their childhood which turns
into illness as adults". Research returned eight general "what is NPD"
articles and nothing about developmental outcomes, which the coverage gate
correctly caught. /post then built a deck anyway, compose found a hole it had
not been told about, and filled it with a thesis — "What the research doesn't
show", "The gap is the finding", and a caption asserting "that research simply
hasn't been done yet".

That claim is false, and more importantly it is not a claim the pipeline is in
any position to make: it searched a handful of pages and found nothing, which
says something about the search and nothing about the literature. Presenting
your own failure to find something as evidence that it does not exist is the
one error here that actively misinforms a reader, so it is the one checked in
code rather than requested in a prompt.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

# --- T249: material-evidence gates (deck-length budget). Each threshold names its decision. ---
CODE_LINE_MIN_CHARS = 8           # D219, T248: code lines shorter than this are not evidence
CODE_MIN_MATCHED_LINES = 2        # D230, T277: matched qualifying lines needed to back code...
CODE_SINGLE_LINE_MIN_CHARS = 20   # D230, T277: ...unless one matched line is at least this long
SMALL_INT_MAX = 10                # D230, T277: a bare integer <= this needs a digit match, not a number word
CODE_EVIDENCE_FRACTION = 0.5      # D219, T248: verbatim-line share that counts as "code present"
CHART_MIN_SERIES = 2              # D219, T248: minimum series with values in material
QUOTE_FUZZY_THRESHOLD = 0.9       # D226, T248: share of quote tokens in >=3-token runs
QUOTE_MIN_RUN_TOKENS = 3          # D226, T248: minimum token run length for quote substring match

#: Slide types the pipeline writes itself (or code adds); they carry no reportable
#: material, so they neither count toward the content budget nor need backing.
NON_CONTENT_TYPES = frozenset({"hook", "takeaway", "sources", "follow", "links", "photo"})

#: Magnitude suffix -> multiplier (numbers_in).
_MAGNITUDES = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "million": 1e6, "mn": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "t": 1e12, "trillion": 1e12,
}
#: Number words recognised by numbers_in (zero-twenty, round tens up to sixty, hundred).
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "hundred": 100,
}
NUM_RE = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*"
    r"(trillion|billion|million|thousand|bn|mn|k|m|b|t|%|percent|per\s+cent|x|×)?(?![A-Za-z0-9])",
    re.I,
)
_WORDNUM_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.I)
# ---------------------------------------------------------------------------

#: Ways a deck can claim something is not there. Deliberately about absence of
#: EVIDENCE — "experts disagree" or "the study found no effect" are findings a
#: source can support, and are not matched here.
ABSENCE_CLAIMS = (
    re.compile(r"\b(research|stud(?:y|ies)|data|evidence|literature)\b[^.]{0,40}"
               r"\b(doesn'?t|does not|hasn'?t|has not|never|no longer)\b"
               r"[^.]{0,20}\b(exist|been done|show|been conducted|been published)\b",
               re.IGNORECASE),
    re.compile(r"\bno\s+(longitudinal\s+)?(research|studies|data|evidence)\b"
               r"[^.]{0,30}\b(exist|available|found|published)\b", re.IGNORECASE),
    re.compile(r"\b(this|that|the)\s+research\s+(simply\s+)?"
               r"(hasn'?t|has not|doesn'?t|does not)\b", re.IGNORECASE),
    re.compile(r"\bthe\s+(gap|absence|silence)\s+is\s+the\s+(finding|story|point)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+the\s+(research|data|evidence)\s+doesn'?t\s+show\b", re.IGNORECASE),
)


def absence_claims(text: str) -> list[str]:
    """The absence-of-evidence assertions in ``text``, if any."""
    found: list[str] = []
    for pattern in ABSENCE_CLAIMS:
        found.extend(match.group(0).strip() for match in pattern.finditer(text or ""))
    return found


def unsupported_absence(deck_text: str, notes: list[dict]) -> list[str]:
    """Absence claims the research does not actually support.

    A source may genuinely report that something has not been studied, and a
    deck is entitled to repeat that. What it may not do is reach the
    conclusion itself because its own search came back thin — so the claim is
    allowed only when the same kind of statement appears in a note.
    """
    claimed = absence_claims(deck_text)
    if not claimed:
        return []
    supported = absence_claims(" ".join(
        f"{n.get('claim', '')} {n.get('detail', '')}"
        for n in notes if isinstance(n, dict)
    ))
    return [] if supported else claimed


#: A slide whose content is mostly about what could not be found. One such
#: line is honest; a deck built out of them is not worth a reader's swipe.
GAP_PHRASES = re.compile(
    r"\b(the\s+)?(provided\s+)?sources?\s+(do|does)\s+not\b"
    r"|\bno\s+(provided\s+)?sources?\b"
    r"|\bnot\s+(covered|addressed|available)\s+(here|in\s+(these|the)\s+sources?)\b"
    r"|\bthese\s+sources?\s+(do|does)\s+not\b",
    re.IGNORECASE,
)

#: At most this many slides may be about absent material.
MAX_GAP_SLIDES = 1


def gap_slides(slides: list[dict]) -> list[int]:
    """1-based positions of slides that are mostly about what is missing."""
    found = []
    for index, slide in enumerate(slides, start=1):
        body = " ".join([
            str(slide.get("headline", "")), str(slide.get("sub", "")),
            " ".join(map(str, slide.get("bullets", []) or [])), str(slide.get("body", "")),
        ])
        if GAP_PHRASES.search(body):
            found.append(index)
    return found


#: A small integer that stands for "how many things follow" — "7 Best
#: Practices", "Top 12 Rust Tools" — as opposed to a version ("3.13"), a year
#: ("2026"), a multiplier ("100x"), a ratio's own numerator ("3" in "3 of 5" —
#: the "N of" prefix is what's excluded; the denominator "5" is not
#: distinguishable from a genuine count and IS matched, e.g. "3 of 5 startups
#: failed" reads as [5], not []), a percentage ("45%"), money ("$65B"), or
#: either half of a hyphenated range, spaced or not ("5-7 ways to ship",
#: "5 - 7 ways to ship", "5 – 7 ways to ship", "5 — 7 ways to ship" — neither
#: the 5 nor the 7 is itself a count, and rewriting only the second half
#: produces a malformed, reversed-looking range; a SPACED range is the
#: likelier defect of the two, since a model writing "5 – 7 ways to ship"
#: tends to emit exactly 7 bullets — it is writing to its own stated upper
#: bound, so the coincidence rate with a real receipt is far above the
#: unspaced form's). \b alone already excludes a digit glued to a letter
#: (GPT-4o's "4o", $65B's "65B", 100x's "100" is 3 digits so it never even
#: reaches \d{1,2}) — the lookarounds below handle what \b does not: a
#: decimal/version number (the "." creates a \b on both sides of each half),
#: the two prose shapes "N of M" and "N%" that \b cannot distinguish from a
#: genuine count, and either side of a hyphenated range, including one with
#: spaces around the dash ("-" is a non-word char, so \b sits right next to
#: it too; the spaced form additionally needs the explicit ` ` in the
#: lookaround, since \b does not span whitespace).
#:
#: This is NOT a semantic count detector, and a bare digit match cannot be
#: one: a proper noun with a trailing version number ("Windows 7 finally
#: dies") is structurally identical to a genuine count ("Top 7 picks") — same
#: shape, same position, same digit — and nothing in the text distinguishes
#: them without knowing "Windows" names a product. On its own that is a
#: precise false-positive on nearly every such headline; it only becomes a
#: real residual for a caller that ALSO gates on the number equalling
#: something external to the text — e.g. `compose.reconcile_headline_counts`
#: only rewrites a digit that matches the slide's OWN received item count, so
#: the surviving collision is narrower still: a proper noun with a trailing
#: version number that ALSO happens to equal this slide's own received item
#: count. That collision is a known, undecided residual (T102 bounce 1); it
#: is not reachable by widening this regex without also breaking the "Top 7
#: picks" case it must keep matching, since both share the identical local
#: shape.
HEADLINE_COUNT_RE = re.compile(
    r"(?<![-–—])(?<![-–—] )(?<!\.)\b(\d{1,2})\b(?!\.\d)(?!\s*(?:of|%))(?!\s*[-–—]\s*\d)"
)


def headline_counts(headline: str) -> list[int]:
    """Every number in ``headline`` that reads as an item count, in the order
    it appears — never a version, year, multiplier, a ratio's numerator,
    money figure, or either half of a hyphenated range (spaced or not). A
    ratio's denominator ("5" in "3 of 5") IS returned — it is not
    distinguishable from a genuine count at this level.

    A number that coincidentally matches a real count (a proper noun's
    trailing version number, e.g. "Windows 7") is NOT excluded by this
    function alone — it has no way to know "Windows" names a product. A
    caller that additionally gates on the number matching some external
    receipt (`compose.reconcile_headline_counts` gates on the slide's own
    received item count) narrows the residual to a proper noun whose version
    number ALSO happens to equal that receipt — see HEADLINE_COUNT_RE's own
    docstring for why even that narrower collision cannot be closed here."""
    return [int(m) for m in HEADLINE_COUNT_RE.findall(headline or "")]


def check_deck(slides: list[dict], notes: list[dict], caption: str = "") -> list[str]:
    """Every integrity problem in this deck, as human-readable strings.

    Empty means it may render.
    """
    problems: list[str] = []

    text = " ".join(
        " ".join([
            str(s.get("headline", "")), str(s.get("sub", "")),
            str(s.get("quote", "")), " ".join(map(str, s.get("bullets", []) or [])),
            str(s.get("body", "")),
        ]) for s in slides
    ) + " " + (caption or "")

    for claim in unsupported_absence(text, notes):
        problems.append(
            f"claims absence of evidence that no source supports: {claim!r}"
        )

    if not slides:
        problems.append("deck has no slides")

    # Naming a gap once is honest. Item 116's rebuild spent two of seven
    # slides on it — one headed "Childhood exposure effects" whose bullets
    # were a single sentence about the sources not covering childhood
    # exposure — which is a deck about its own shortfall.
    gaps_at = gap_slides(slides)
    if len(gaps_at) > MAX_GAP_SLIDES:
        problems.append(
            f"{len(gaps_at)} slides are about missing material (positions "
            f"{gaps_at}); at most {MAX_GAP_SLIDES} may be"
        )

    return problems


#: T104: the user's report was "the heading says 7 best but the rest of the
#: image contains only max of 4 points" -- a headline promising a count the
#: deck does not deliver. A bare leading digit is not enough to raise that
#: claim: "Python 3.13 is out", "2026 in review" and "3 of 5 startups failed"
#: all carry one, with no enumeration behind it. A match therefore requires
#: an explicit "top"/"best" qualifier beside the number, or a listicle noun
#: within a bounded gap of it -- narrow on purpose, matching the scope rule
#: below.
#: `\d{1,2}` (never 3+ digits) already excludes a 4-digit year; the `\b`
#: required directly after the digits excludes a version ("3.13"), a
#: multiplier ("100x") and a currency scale ("$65B", "GPT-4o") -- none of
#: those leave a word boundary right after the digits for a bare `\d{1,2}\b`
#: to land on, since digit and the following letter are both `\w`.
#:
#: Bounce 1 (reviewer, real data): the original "listicle noun immediately
#: after the digits" version returned `[]` on all three decks in this
#: repo's own `data/eval/golden_current.json` that are the user's exact
#: complaint shape -- "8 GitHub repos for technical interview prep" (id
#: 26), "8 GitHub repos worth studying" (id 29), "8 GitHub repos for
#: interview prep" (id 30), each carrying 7 `repo` slides -- because a
#: qualifier ("GitHub", "curated", ...) sat between the digit and the
#: listicle noun. A detector that never fires on the exact defect it exists
#: to catch is the D78/D92 shape: zero hits reads as "clean" while
#: guarding nothing. Fixed by allowing a bounded gap of 0-3 words between
#: the digit and the count word -- `(?:\s+[\w'’-]+){0,3}?` (lazy, so it
#: takes the smallest gap that still reaches a real count word). The gap
#: still requires literal `\s+` before each gap word and before the count
#: word, so a hyphen-glued token right after the digit ("06-CSS3",
#: "30-Days-Of-Python", "...8-hour plan into 16-hour ordeal") still cannot
#: match at all -- there is no whitespace immediately after the digit for
#: that mandatory `\s+` to land on, gap or no gap. ids 26/29/30 in
#: `golden_listonly.json` (the same hooks, delivering counts that match)
#: still resolve consistent, because the gap only changes what counts as a
#: *stated* number, never whether stated equals delivered; id 34 "4
#: panpsychism research projects" (a 2-word gap, 4 stated == 4 delivered)
#: is the same free negative control.
_COUNT_WORDS = (
    r"best|worst|essential|key|biggest|greatest|must-\w+|"
    r"ways|things|reasons|tips|tricks|hacks|secrets|mistakes|facts|ideas|"
    r"examples|features|steps|picks|"
    r"tools|repos?|repositories|projects|libraries|frameworks|languages|"
    r"apps|links|sources"
)
_HEADLINE_COUNT = re.compile(
    rf"\b(?:top|best)\s+(\d{{1,2}})(?!\.\d)\b"
    rf"|\b(\d{{1,2}})(?!\.\d)\b(?:\s+[\w'’-]+){{0,3}}?\s+(?:{_COUNT_WORDS})\b",
    re.IGNORECASE,
)


def _stated_count(headline: str) -> int | None:
    """The count a headline states, or None if it states none.

    Searched once per slide's own headline text only -- body prose is a
    different claim (D5/rule 8's territory), not this one.
    """
    match = _HEADLINE_COUNT.search(headline or "")
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def headline_count_slides(slides: list[dict], intent: str) -> list[int]:
    """1-based positions of slides whose headline states a count the deck
    does not actually deliver.

    Scope rule (architect, T104), deliberately narrow -- no other case is a
    checkable claim:
      1. A slide carrying its own list-shaped ("countable") field is checked
         against that field's own `len()`. No per-type field-name table is
         hand-maintained here to decide which field that is (see the module
         docstring: this module may import only `logging` + `re`, and
         `compose.py` -- the owner of that contract -- imports `check_deck`
         from here, so importing compose.py's registry back would be
         circular) -- a field is "countable" purely by its own runtime
         shape, so a slide is consistent if ANY of its list-shaped fields'
         length equals the stated count.
      2. A `hook` slide on a `list`-intent deck is checked against the
         number of `repo` slides that survived into the final deck --
         `hook`'s only body field is `sub`, a string, so it has no
         list-shaped field of its own to fall under rule 1.
      3. Everything else (a `hook` on a non-list deck, a `quote`, a `repo`'s
         own headline, ...) has no structural claim behind its number and is
         left alone -- flagging it would mean guessing what the number
         means, which is exactly what this function exists not to do.

    House rule 11: name who wrote what this reads. `headline` is read on
    every slide, including the ones the pipeline itself writes --
    `ensure_closing_slide`'s "Sources" and "Follow for more",
    `ensure_links_slide`'s "All the links" (compose.py). None of the three
    is digit-bearing, so today they cannot distort this metric either way.
    A `repo` slide's headline, unlike its `name`/`url`/`stars`/`language`/
    `logo`/`sub` fields, is never touched by `_restore_repo_facts` -- it
    stays whatever the model wrote. This metric therefore carries no
    composite weight in `quality.py`, correctly: it is a raw shape count in
    `scripts/eval_compose.py`'s per-deck row, not folded into
    `substance_composite`.
    """
    found: list[int] = []
    for position, slide in enumerate(slides, start=1):
        stated = _stated_count(str(slide.get("headline", "")))
        if stated is None:
            continue
        if slide.get("type") == "hook" and intent == "list":
            delivered = [sum(1 for s in slides if s.get("type") == "repo")]
        else:
            delivered = [len(v) for v in slide.values() if isinstance(v, list)]
        if not delivered:
            continue  # no countable claim on this slide -- leave it alone
        if stated not in delivered:
            found.append(position)
    return found


# --- T249: Material-grounding helpers for deck-length calibration (D217, D219, D226) ---

def _norm_text(s: str) -> str:
    """Normalize text: lowercase, fold curly quotes, replace punctuation with spaces, collapse whitespace."""
    s = (s or "").lower()
    # Fold curly quotes to ASCII straight quotes
    s = (s.replace("\u2018", "'").replace("\u2019", "'")
          .replace("\u201c", '"').replace("\u201d", '"'))
    # Replace punctuation with spaces
    s = re.sub(r"[^a-z0-9']+", " ", s)
    # Collapse whitespace
    return re.sub(r"\s+", " ", s).strip()


def numbers_in(text: str) -> set[float]:
    """Canonical numeric values from text: raw numbers and scaled values for magnitude suffixes.

    Recognizes:
    - Digits with optional commas (drop commas for parsing)
    - Magnitude suffixes: k/thousand/m/million/mn/b/bn/billion/t/trillion (yields both raw and scaled)
    - Percent/percentage/x suffixes (raw value only, no scaling)
    - Number words zero-twenty, tens (30/40/50/60), hundred (all converted to digits)

    D226 (T248): "3 months" normalizes the same as "3" — number words are included.
    """
    out: set[float] = set()
    for m in NUM_RE.finditer(text or ""):
        try:
            raw = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        out.add(round(raw, 6))
        suffix = " ".join((m.group(2) or "").lower().split())
        if suffix in _MAGNITUDES:
            out.add(round(raw * _MAGNITUDES[suffix], 6))
    for m in _WORDNUM_RE.finditer(text or ""):
        out.add(float(_NUMBER_WORDS[m.group(1).lower()]))
    return out


_UNIT_SUFFIX_CLASS = {"%": "pct", "percent": "pct", "per cent": "pct", "x": "x", "×": "x"}
_PLAIN_CLASSES = ("num", "word")


def typed_numbers_in(text: str) -> set[tuple[float, str]]:
    """Like ``numbers_in`` but each value carries its unit class (T277, D230).

    Classes: ``num`` bare digits, ``word`` number word, ``pct`` (%/percent),
    ``x`` (multiplier), ``mag`` (value scaled by a magnitude suffix) and
    ``raw_mag`` (the unscaled digits of a magnitude figure; plain-matching only).
    """
    out: set[tuple[float, str]] = set()
    for m in NUM_RE.finditer(text or ""):
        try:
            raw = round(float(m.group(1).replace(",", "")), 6)
        except ValueError:
            continue
        suffix = " ".join((m.group(2) or "").lower().split())
        if suffix in _MAGNITUDES:
            out.add((round(raw * _MAGNITUDES[suffix], 6), "mag"))
            out.add((raw, "raw_mag"))
        else:
            out.add((raw, _UNIT_SUFFIX_CLASS.get(suffix, "num")))
    for m in _WORDNUM_RE.finditer(text or ""):
        out.add((float(_NUMBER_WORDS[m.group(1).lower()]), "word"))
    return out


def _token_backed(value: float, cls: str, material: set[tuple[float, str]]) -> bool:
    """One typed figure against typed material numbers (T277, D230)."""
    if cls == "raw_mag":
        return False  # only a plain-matching aid on the material side
    if cls in ("pct", "x"):
        return (value, cls) in material
    if cls == "mag":
        return (value, "mag") in material or (value, "num") in material
    small = float(value).is_integer() and value <= SMALL_INT_MAX
    return any(
        (value, c) in material
        for c in ("num", "pct", "x", "mag", "raw_mag") + (("word",) if (not small or cls == "word") else ())
    )


def figure_backed(text: str, material: str | set[tuple[float, str]]) -> bool:
    """True if any figure in ``text`` is backed by a same-unit-class number in ``material``
    (raw material text, or its already-computed ``typed_numbers_in`` set)."""
    mat = typed_numbers_in(material) if isinstance(material, str) else material
    return any(_token_backed(v, c, mat) for v, c in typed_numbers_in(text))


def unbacked_figures(text: str, material: set[tuple[float, str]], known: str = "") -> list[str]:
    """Figures in ``text`` that are in neither ``material`` (a ``typed_numbers_in``
    set) nor ``known`` (text whose figures are already accounted for, e.g. the
    slide before an edit). Rendered ``{value:g}``, sorted. T298."""
    already = typed_numbers_in(known)
    # ``raw_mag`` ("2" of "2 billion") is a material-side matching aid; as a text
    # token it is never backed, so it would flag every magnitude figure. The
    # paired ``mag`` token ("2e9") carries the claim and is checked.
    return sorted(
        f"{v:g}" for v, c in typed_numbers_in(text) - already
        if c != "raw_mag" and not _token_backed(v, c, material)
    )


def material_text(item) -> str:
    """Concatenate material for evidence gates: research notes (claim + detail, skip fail_open) + raw_text.

    Each note contributes its claim and detail (skipping those marked fail_open).
    D226 (T248): raw_text is always appended; the budget normaliser uses this to assess
    how many distinct claims and raw text support material.
    """
    parts = []
    for n in item.research if item.research else []:
        if not isinstance(n, dict) or n.get("fail_open"):
            continue
        claim = str(n.get("claim") or "").strip()
        detail = str(n.get("detail") or "").strip()
        if claim:
            parts.append(claim)
        if detail:
            parts.append(detail)
    parts.append(item.raw_text or "")
    return "\n".join(parts)


def material_units(item) -> int:
    """Count of distinct non-fail-open notes with non-empty normalised claim.

    D226 (T248): low-confidence notes ARE counted (included). Fail_open notes are
    skipped. A note contributes 1 unit if its claim, once normalised, is non-empty
    and not a duplicate of another note's normalised claim.
    """
    seen = set()
    for n in item.research if item.research else []:
        if not isinstance(n, dict) or n.get("fail_open"):
            continue
        claim = _norm_text(str(n.get("claim") or ""))
        if claim:
            seen.add(claim)
    return len(seen)


def has_code_evidence(code: str, material: str) -> bool:
    """True if >=50% of the code's lines (>=8 chars) appear verbatim in the material.

    D230 (corrects D219 reading): content match, never fence presence. A fenced
    block in the material only matters through what is inside it; the lines of a
    fenced block are part of ``material``, so one flattened-material check covers
    "inside one fenced block" and "elsewhere in the material" alike. Whitespace is
    collapsed on both sides; lines under CODE_LINE_MIN_CHARS are noise.
    """
    if not code or not material:
        return False
    code_lines = [
        re.sub(r"\s+", " ", ln.strip())
        for ln in code.splitlines()
        if len(ln.strip()) >= CODE_LINE_MIN_CHARS
    ]
    if not code_lines:
        return False
    flat_material = re.sub(r"\s+", " ", material)
    matched_lines = [ln for ln in code_lines if ln in flat_material]
    if len(matched_lines) / len(code_lines) < CODE_EVIDENCE_FRACTION:
        return False
    # D230 (T277): a lone `import os` / `return None` is not evidence for a 2-line slide.
    return (len(matched_lines) >= CODE_MIN_MATCHED_LINES
            or any(len(ln) >= CODE_SINGLE_LINE_MIN_CHARS for ln in matched_lines))


def quote_in_material(quote: str, material: str) -> bool:
    """True if all segments of the quote (split on …, …, or sentence ends) appear in material.

    D226 (T248): strict rule — every segment of the quote must be a normalised substring
    of the material. If strict fails, fallback to fuzzy: ≥90% of quote tokens appear in
    runs of ≥3 consecutive tokens from the material. The tolerant fallback handles
    near-verbatim quotes (e.g., dropped lead-ins, spliced sentences).
    """
    if not quote or not material:
        return False

    mat_norm = _norm_text(material)

    # Strict: split on …, …, sentence ends; each segment must be a normalised substring
    quote_segments = [
        _norm_text(x)
        for x in re.split(r"\.{2,}|…|(?<=[.!?])\s+", quote)
        if _norm_text(x)
    ]

    if not quote_segments:
        return False

    # Token-bounded: "ai" must not match inside "said", "rain" not inside "train"
    padded = f" {mat_norm} "
    if all(f" {seg} " in padded for seg in quote_segments):
        return True

    # Tolerant fallback: ≥90% of quote tokens in >=3-token runs
    quote_tokens = _norm_text(quote).split()
    if not quote_tokens:
        return False

    material_tokens = mat_norm.split()
    sm = SequenceMatcher(None, quote_tokens, material_tokens, autojunk=False)

    # Sum sizes of matching blocks with ≥3-token runs
    matching_tokens = sum(
        block.size
        for block in sm.get_matching_blocks()
        if block.size >= QUOTE_MIN_RUN_TOKENS
    )

    coverage = matching_tokens / len(quote_tokens)
    return coverage >= QUOTE_FUZZY_THRESHOLD


def chart_backed(values: list[dict], material: str) -> bool:
    """True if ≥2 series values (value or display field) appear in material numbers.

    D219 (T248): a chart has backing if at least 2 of its series' numeric values
    (either the `value` field or the `display` field) can be found in the material.
    """
    if not values or not material:
        return False

    mat_typed = typed_numbers_in(material)
    if not mat_typed:
        return False

    # Count how many values from the series can be found in material numbers
    hit_count = 0
    for item in values:
        if not isinstance(item, dict):
            continue

        # T277: a unit-bearing display ("65%") is the claim; only a unit-free one falls back to value.
        display = str(item.get("display") or "")
        fields = [display] if any(c not in _PLAIN_CLASSES for _, c in typed_numbers_in(display)) else [
            display, str(item.get("value") if item.get("value") is not None else "")]
        if any(f and figure_backed(f, mat_typed) for f in fields):
            hit_count += 1

    return hit_count >= CHART_MIN_SERIES


def kpi_tile_backed(tile: dict, material: str) -> bool:
    """True if tile value appears in material (as number or normalised substring).

    D226 (T248): for numeric values, check if any parsed number is in material numbers.
    For non-numeric (string) values, check if the normalised value is a substring.
    """
    if not tile or not material:
        return False

    value = str(tile.get("value") or "").strip()
    if not value:
        return False

    mat_norm = _norm_text(material)

    # Try to parse as number(s)
    if numbers_in(value):
        # T277: same unit class; a bare integer <= SMALL_INT_MAX needs a digit match
        return figure_backed(value, material)

    # Non-numeric: check if normalised value is a substring
    return f" {_norm_text(value)} " in f" {mat_norm} "


def slide_backed(slide: dict, material: str) -> bool:
    """The one evidence gate for a slide (T249; T251 reuses it, rule 8).

    Quote / code / chart / kpi need their payload traced to ``material`` (D219,
    D230); every other type is backed by construction.
    """
    kind = slide.get("type")
    if kind == "quote":
        return quote_in_material(slide.get("quote") or "", material)
    if kind == "code":
        return has_code_evidence(slide.get("code") or "", material)
    if kind == "chart":
        return chart_backed(slide.get("series") or [], material)
    if kind == "kpi":
        tiles = [t for t in (slide.get("tiles") or []) if isinstance(t, dict)]
        return bool(tiles) and all(kpi_tile_backed(t, material) for t in tiles)  # T277: no tiles = nothing backed
    return True
