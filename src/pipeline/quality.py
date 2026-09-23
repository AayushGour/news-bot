"""Mechanical substance metrics for a composed deck.

The eval harness (`scripts/eval_compose.py`) has measured *shape* for a while
— 3 slides minimum, a hook first, a closing slide, no overlong headline — and
shape compliance is solved. The user's complaint ("posts produced by the free
models are very bad") is not about shape. It is about substance: generic
claims, no numbers, the same opener every run, text that could have been
written without reading the source article at all.

Every metric in this module is deterministic and reproducible from saved
output — string/regex/set arithmetic only. **No model ever judges another
model's output here.** A judge would share the same blind spots as the model
it is grading and return a number that feels like evidence without being one
(see `scripts/eval_compose.py`'s own docstring, and coding-standards.md rule
6). If a future change adds an LLM call anywhere in this file, that change is
wrong regardless of how well-intentioned it is.

Two building blocks feed all seven metrics:

  - `deck_body_text(doc)`  — every prose fragment the model composed for one
    deck, across every slide type, with links/urls/code/image fields excluded
    (a URL is not prose the model wrote, and `code_preserved` covers "code"
    on its own terms).
  - `source_text_from_item(item)` — the brief plus every research note's
    claim + detail, i.e. everything the model was actually given to work
    from. `grounding_ratio` diffs the two.

`substance_composite()` folds all seven into one 0-100 number. The weighting
is explained where the weights are defined below — it is a first guess
pending real baseline data (T68's own `--rescore` run below IS that first
data point), not a fitted model. Revisit it once T75/T77 produce enough runs
to see whether any single term dominates the ranking.
"""

from __future__ import annotations

import re
import unicodedata

#: Word tokenizer shared by every metric that needs one, so "what counts as a
#: word" is defined exactly once. Includes internal apostrophes/hyphens
#: ("don't", "GPT-5") so a hyphenated model name or contraction is one token,
#: not two plus noise.
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")

#: Naive sentence splitter: break after . ! or ? followed by whitespace. Good
#: enough for "was this token the first word of its sentence" — the one thing
#: proper_noun_density needs it for — without pulling in an NLP dependency for
#: a mechanical eval script.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

#: A version-like or product-like token: "3.13", "v2", "GPT-4o", "iOS-17".
#: Counted as a named entity regardless of sentence position, since a version
#: number is exactly the kind of specific detail this metric exists to find.
_VERSION_RE = re.compile(r"^[A-Za-z]*\d+(?:\.\d+)*[A-Za-z]*$")

#: Slide fields that carry the prose/labels a reader actually sees, across
#: every slide type in `pipeline.stages.compose.SLIDE_TYPES`. Deliberately
#: excludes "urls", "links", "image", "image_mode", "lang" and "type" — a URL
#: or a field the model didn't write prose into is not part of the substance
#: question these metrics ask. "code" is excluded from general text too: its
#: own fidelity is measured by `code_preserved`, and mixing a JSON blob into
#: prose-density counts would just add noise to numeric/proper-noun/filler
#: density. This is a distinct list from `eval_compose.py`'s BODY_FIELDS,
#: which answers a different question (is this slide non-empty at all).
_SCALAR_TEXT_FIELDS = ("sub", "quote", "attribution", "caption", "name",
                       "left_title", "right_title")


def _slide_text(slide: dict) -> list[str]:
    """Every prose fragment on one slide, as a flat list of strings."""
    parts: list[str] = []
    headline = slide.get("headline")
    if headline:
        parts.append(str(headline))
    for key in _SCALAR_TEXT_FIELDS:
        value = slide.get(key)
        if value:
            parts.append(str(value))
    bullets = slide.get("bullets")
    if isinstance(bullets, list):
        parts.extend(str(b) for b in bullets if b)
    stat = slide.get("stat")
    if isinstance(stat, dict):
        for key in ("value", "label"):
            if stat.get(key):
                parts.append(str(stat[key]))
    for row in slide.get("rows") or []:
        if isinstance(row, (list, tuple)):
            parts.extend(str(cell) for cell in row if cell)
    for step in slide.get("steps") or []:
        if isinstance(step, dict):
            for key in ("label", "detail"):
                if step.get(key):
                    parts.append(str(step[key]))
    for tile in slide.get("tiles") or []:
        if isinstance(tile, dict):
            for key in ("value", "label", "delta"):
                if tile.get(key):
                    parts.append(str(tile[key]))
    for entry in slide.get("series") or []:
        if isinstance(entry, dict):
            for key in ("label", "display"):
                if entry.get(key):
                    parts.append(str(entry[key]))
    return parts


def _slides(doc: dict) -> list[dict]:
    """`doc`'s slide list, dict entries only.

    `--rescore` reads a saved run's `raw` field for a row where compose
    failed — the model's last completion before any validation ever touched
    it. A free model producing a malformed shape (a slide that is a string,
    or a "slides" key that is not a list at all) is exactly the kind of thing
    `BadCompletion`/`_is_provider_fault` exist to handle elsewhere in the
    pipeline; a metric reading that same raw completion off disk must not
    crash on it either. Anything that is not a dict is silently dropped
    rather than scored as if it were a slide.

    `doc` itself is guarded too, not just `doc["slides"]` — `_deck_from_row`
    shields every call `--rescore` makes, but `score_substance` has no such
    shield when called directly on a fresh compose result (T75), so `None`,
    a list, or a bare string must degrade to "no slides", not raise.
    """
    if not isinstance(doc, dict):
        return []
    raw = doc.get("slides")
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict)]


def deck_body_text(doc: dict) -> str:
    """Every prose fragment in the deck, in slide order, one blob.

    Each fragment (a headline, a sub, one bullet, one table cell, one KPI
    label...) is sentence-terminated before joining. Slide fields are
    routinely written without trailing punctuation -- a headline, a table
    cell, a KPI label never end in a period -- so without this,
    `proper_noun_density`'s "not the first word of its sentence" rule would
    spuriously treat the first word of the NEXT field as mid-sentence just
    because the previous field happened to lack one: an artifact of how the
    fragments were concatenated, not of anything the model wrote.
    """
    parts: list[str] = []
    for slide in _slides(doc):
        parts.extend(_slide_text(slide))
    terminated = [
        p if p.rstrip().endswith((".", "!", "?")) else p.rstrip() + "."
        for p in parts if p and p.strip()
    ]
    return " ".join(terminated)


def source_text_from_item(item) -> str:
    """The brief plus every research note's claim + detail.

    This is what the model was actually handed. `grounding_ratio` measures
    how much of the deck's language traces back to it.
    """
    parts = [item.brief or ""]
    for note in item.research or []:
        parts.append(str(note.get("claim") or ""))
        parts.append(str(note.get("detail") or ""))
    return " ".join(p for p in parts if p)


def hook_headline(doc: dict) -> str:
    """The deck's hook headline, or "" if the deck has none (a shape defect
    `score_deck` already flags — this just must not crash on it)."""
    for slide in _slides(doc):
        if slide.get("type") == "hook":
            return str(slide.get("headline") or "")
    return ""


# --------------------------------------------------------------- metric 1


def numeric_density(doc: dict) -> float:
    """Figures per 100 words of deck body text.

    "Numbers beat adjectives" is already in the compose SYSTEM prompt
    (`pipeline/stages/compose.py`) — this measures obedience to it. A
    "figure" is any word token containing at least one digit: "92%", "$65B",
    "2026", "4.2s" all count; "ninety-two" does not, because the model was
    told to write the number, not the word.
    """
    words = _WORD_RE.findall(deck_body_text(doc))
    if not words:
        return 0.0
    figures = sum(1 for w in words if any(c.isdigit() for c in w))
    return round(figures / len(words) * 100, 2)


# --------------------------------------------------------------- metric 2


def proper_noun_density(doc: dict) -> float:
    """Named-entity-like tokens per 100 words of deck body text.

    Mechanical, not NLP: a token counts when it is either (a) version/product
    shaped ("GPT-4o", "v3.13" — regardless of position), (b) an all-caps
    acronym of 2+ letters ("API", "MMLU" — regardless of position, since an
    acronym reads as a proper noun wherever it lands), or (c) capitalised and
    NOT the first word of its sentence. (c) is the deliberate design choice:
    every sentence starts with a capital letter whether or not the subject is
    a named entity, so counting sentence-initial capitals would just measure
    sentence count. Counting only the *interior* capitals is what turns this
    into a specificity signal instead of a grammar signal — at the cost of
    undercounting entities that happen to open a sentence, which is an
    accepted trade for not overcounting every sentence there is.
    """
    text = deck_body_text(doc)
    words = _WORD_RE.findall(text)
    if not words:
        return 0.0
    hits = 0
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        tokens = _WORD_RE.findall(sentence)
        for i, tok in enumerate(tokens):
            if _VERSION_RE.match(tok) and any(c.isdigit() for c in tok):
                hits += 1
            elif tok.isupper() and len(tok) > 1:
                hits += 1
            elif i > 0 and tok[0].isupper() and not tok.isupper():
                hits += 1
    return round(hits / len(words) * 100, 2)


# --------------------------------------------------------------- metric 3


def _fold_apostrophes(text: str) -> str:
    """NFKC-normalise `text` and fold the typographic apostrophe (U+2019) to
    the ASCII one `_WORD_RE` actually matches.

    LLM output routinely writes "Apple’s" with a curly apostrophe; briefs and
    research notes routinely write "Apple's" with the ASCII one. `_WORD_RE`'s
    character class includes `'` (U+0027) but not `’` (U+2019), so without
    this fold the two spellings of the identical word tokenise differently
    ("apple's" as one token vs "apple" + "s" as two) — shifting every 3-gram
    downstream of the split and deflating `grounding_ratio` on a sentence
    that is, word for word, the same as the source.
    """
    return unicodedata.normalize("NFKC", text).replace("’", "'")


def _ngrams(text: str, n: int = 3) -> set[tuple[str, ...]]:
    words = [w.lower() for w in _WORD_RE.findall(_fold_apostrophes(text))]
    if len(words) < n:
        return set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def grounding_ratio(doc: dict, source_text: str) -> float:
    """Share of the deck's word 3-grams that also occur in the source text.

    This is the direct measure of "generic claims": a phrase the model could
    have written without reading anything shares no 3-gram with the brief or
    research notes, while a specific, sourced claim tends to reuse the
    source's own wording for at least a few words at a time. Unique 3-grams
    on both sides (a set, not a multiset), so a deck that repeats one grounded
    phrase five times cannot inflate the ratio past what a single use of it
    would score.

    The deck side is built **per fragment** — the union of `_ngrams` over
    each individual fragment `_slide_text` returns, not `_ngrams` of the
    joined `deck_body_text` blob. Joining fragments with ". " before
    tokenising (as `deck_body_text` does, for `proper_noun_density`'s
    sentence splitter) forges 3-grams that straddle the boundary between two
    adjacent fragments — the last word of one and the first two of the next —
    that exist in neither the deck's actual prose nor, generally, the source.
    Those artifacts track fragment count, not grounding, and can manufacture
    a nonzero score for a deck that shares nothing real with the source at
    all (see `test_grounding_ratio_does_not_invent_grams_at_fragment_joins`).
    Scoring per fragment and unioning the results cannot produce a 3-gram
    that never appeared within a single fragment as the model wrote it.

    A deck with fewer than 3 words of body text has no 3-grams to check and
    scores 0.0 — that deck has a much larger problem than grounding, and 0.0
    correctly reports "no evidence of grounding" rather than raising.
    """
    deck_grams: set[tuple[str, ...]] = set()
    for slide in _slides(doc):
        for fragment in _slide_text(slide):
            deck_grams |= _ngrams(str(fragment))
    if not deck_grams:
        return 0.0
    source_grams = _ngrams(source_text or "")
    if not source_grams:
        return 0.0
    return round(len(deck_grams & source_grams) / len(deck_grams), 4)


# --------------------------------------------------------------- metric 4


def hook_similarity(hooks: list[str]) -> float:
    """Mean, over every hook, of its highest Jaccard similarity to any other
    hook in the list — a run-level metric, not a per-deck one.

    The same opener every time is a real, user-visible failure mode: a user
    scrolling several posts in a row sees "Big news in AI today" open every
    single one. Jaccard is computed over each hook's lowercased word set.
    A run of fewer than 2 hooks has nothing to compare and scores 0.0.
    1.0 means every hook has an exact word-set duplicate somewhere in the run.
    """
    tokenised = [set(_WORD_RE.findall(h.lower())) for h in hooks if h]
    if len(tokenised) < 2:
        return 0.0
    best_per_hook = []
    for i, a in enumerate(tokenised):
        best = 0.0
        for j, b in enumerate(tokenised):
            if i == j:
                continue
            union = a | b
            if not union:
                continue
            best = max(best, len(a & b) / len(union))
        best_per_hook.append(best)
    return round(sum(best_per_hook) / len(best_per_hook), 4)


# --------------------------------------------------------------- metric 5


#: Blocklist of generic filler phrases that read as padding rather than
#: substance ("in today's fast-paced world", "game-changer", "stay tuned" —
#: the kind of line that could sit on any post about any subject).
#:
#: T74: harvested from real saved decks in `data/eval/*.json` (`golden_current`,
#: `golden_listonly`, `golden_baseline`, `golden_fewshot`, `baseline`, `fewshot`,
#: `e26_baseline`, `e26_fewshot`, 2026-09-22), not invented — each entry below
#: was found as a literal (case-insensitive) substring of a `doc`-validated
#: deck's `_slide_text()` fields, the same field set `deck_body_text()` walks.
#: The full phrase -> file -> item id -> field citation table is in the T74
#: task handoff (`.claude/logs/senior-dev.md` T93/T74 entry); every entry here
#: is reproducible by re-running that extraction. Honest finding from the
#: harvest, not assumed going in: this golden set is unusually factual/curated
#: (numbers, quotes, named sources), so classic hype filler ("game-changer",
#: "in today's fast-paced world") essentially never occurs in it — the real,
#: measured filler is (a) generic call-to-action/engagement boilerplate on the
#: mandatory `follow`/`links` closing slides, some of it `ensure_closing_slide`'s
#: own fallback text and some of it the model's own near-identical phrasing for
#: the same closing role, and (b) generic section-header templates ("Why it
#: matters", "Key details") reused verbatim across unrelated topics. Two wrong
#: conclusions have already shipped in this programme from guessing instead of
#: measuring (D9, D27) — this list is deliberately what was FOUND, not what
#: "generic AI slop" was assumed to look like.
FILLER_PHRASES: tuple[str, ...] = (
    # Generic call-to-action / engagement boilerplate — reads the same
    # regardless of the deck's subject (golden_current id=26 `follow`/`links`
    # unless noted).
    "follow for more",
    "daily tech, explained",
    "stay updated on",
    "screenshot this slide",
    "all the links",
    "get the latest on",  # golden_current id=12 follow
    "get updates on",  # golden_listonly id=14 follow
    "follow for updates on",  # golden_current id=37 follow
    # Generic section-header templates — the identical header recurs across
    # decks about unrelated topics (chess, OKF, MCP, neo-Nazi networks...).
    "why it matters",  # golden_fewshot id=10 takeaway
    "key details",  # golden_listonly id=3 facts
    "key takeaway",  # golden_fewshot id=22 takeaway
    "key characteristics",  # golden_current id=14 point
    "key impact metrics",  # golden_current id=20 kpi
    "what went wrong",  # golden_listonly id=10 point
    "what changed",  # golden_baseline id=8 point ("What changed?")
    "at a glance",  # golden_baseline id=5 facts
    "core tenets",  # golden_listonly id=37 point
    # Generic hedge/preamble phrasing found in body prose — conveys no
    # subject-specific information, interchangeable across any listicle/story.
    "ideal for",  # golden_baseline id=30 repo
    "worth exploring",  # golden_listonly id=29 hook
    "worth studying",  # golden_current id=29 hook
    "the future of",  # golden_baseline id=12 follow
    "raises questions",  # golden_listonly id=15 takeaway
    "curated toolkit",  # golden_listonly id=26 hook
    "curated list of",  # golden_baseline id=29 point
    "curated resources",  # golden_current id=30 hook
    "ranked by relevance",  # golden_current id=26 hook
    "curriculum for developers at any stage",  # golden_listonly id=30 hook
    "ready to clone",  # golden_baseline id=30 hook
)


def filler_count(doc: dict, phrases: tuple[str, ...] | None = None) -> int:
    """Case-insensitive substring hits against a filler-phrase blocklist.

    `phrases` defaults to `None`, resolved to the CURRENT `FILLER_PHRASES`
    inside the function body rather than bound as a parameter default —
    `def filler_count(doc, phrases=FILLER_PHRASES)` would capture whatever
    `FILLER_PHRASES` was AT IMPORT TIME, so a later runtime reassignment of
    `quality.FILLER_PHRASES` (exactly what T74 just did, and exactly what a
    test or a future re-harvest does again) would be invisible to every call
    that relies on the default — `score_substance` would keep reporting
    `filler_count=0` from the stale binding forever (T93/D26). Re-reading the
    module global on every call, the way `substance_composite` already does
    for its own `if FILLER_PHRASES` check below, is what keeps the two in
    sync. The explicit-override path stays: it is what lets `filler_count`'s
    own counting logic be tested independently of whatever the module
    constant currently holds.
    """
    if phrases is None:
        phrases = FILLER_PHRASES
    if not phrases:
        return 0
    text = deck_body_text(doc).lower()
    return sum(text.count(p.lower()) for p in phrases if p)


# --------------------------------------------------------------- metric 6


#: Slide types the compose prompt calls richer than headline-and-bullets —
#: each demands something structured and specific (a real code snippet, a
#: genuine A-vs-B, an ordered sequence, comparable figures, an actual quoted
#: statement) rather than paraphrased prose.
RICH_SLIDE_TYPES: frozenset[str] = frozenset(
    {"code", "flow", "compare", "kpi", "chart", "quote"}
)

#: Slide types the pipeline mandates regardless of the story's substance:
#: exactly one hook opens every deck (`compose.py` requires it first), one
#: takeaway and one of links/sources/follow close it, and "repo" is the
#: enumeration-intent card populated straight from research (owner/name/url/
#: stars/language) — a structural fact, not a prose choice the model made
#: between "lazy bullets" and something richer. Excluding these from both
#: sides of the ratio isolates the slides where the model actually chose
#: between padding the deck with bullets and reaching for something better,
#: which is the one the compose prompt actually calls out as "the failure
#: mode" in so many words.
STRUCTURAL_SLIDE_TYPES: frozenset[str] = frozenset(
    {"hook", "takeaway", "sources", "links", "follow", "repo"}
)


def bare_bullet_ratio(doc: dict) -> float:
    """Share of content slides (excluding mandatory bookends) that are NOT
    one of the richer types the compose prompt names.

    A deck with no content slides at all (only bookends) has nothing to
    judge on this axis and scores 0.0 — that failure is caught by
    `shape_score`/`empty_slides` in `scripts/eval_compose.py`, not here.
    """
    types = [s.get("type") for s in _slides(doc)]
    content = [t for t in types if t not in STRUCTURAL_SLIDE_TYPES]
    if not content:
        return 0.0
    bare = sum(1 for t in content if t not in RICH_SLIDE_TYPES)
    return round(bare / len(content), 4)


# --------------------------------------------------------------- metric 7


_FENCE_RE = re.compile(r"```[A-Za-z0-9_\-]*\n?(.*?)```", re.DOTALL)

#: How much of a fenced block's distinct tokens must reappear in a "code"
#: slide for the block to count as preserved. Below 1.0 on purpose — the
#: model is allowed to trim whitespace/comments or add a caption without
#: that counting as loss, since `compose.py`'s own instruction only demands
#: it stay verbatim source material, not a byte-identical copy.
_CODE_OVERLAP_THRESHOLD = 0.5


def code_preserved(doc: dict, brief: str | None) -> bool | None:
    """Did a fenced block in the brief survive into a "code" slide?

    Returns None (not applicable) when the brief carried no fenced block at
    all — most stories have no code to preserve, and scoring those as a
    failure would just add noise `substance_composite` then has to filter
    back out. `substance_composite` treats None as "exclude and renormalise
    the remaining weights", not as zero.
    """
    blocks = _FENCE_RE.findall(brief or "")
    if not blocks:
        return None
    code_slides = [
        str(s.get("code") or "")
        for s in _slides(doc)
        if s.get("type") == "code"
    ]
    if not code_slides:
        return False
    for block in blocks:
        block_tokens = set(_WORD_RE.findall(block.lower()))
        if not block_tokens:
            continue
        for code in code_slides:
            code_tokens = set(_WORD_RE.findall(code.lower()))
            if not code_tokens:
                continue
            overlap = len(block_tokens & code_tokens) / len(block_tokens)
            if overlap >= _CODE_OVERLAP_THRESHOLD:
                return True
    return False


# ------------------------------------------------------------- composite


def score_substance(item, doc: dict) -> dict:
    """The six per-deck raw metrics (all but `hook_similarity`, which is a
    run-level property — see `hook_similarity()`), plus `word_count` so
    `substance_composite` can turn `filler_count` into a density without
    retokenising the deck, plus `metric_set_version` (see `METRIC_SET_VERSION`)
    so a saved run carries, on every row, which composite era it belongs to —
    a reader does not have to cross-reference `FILLER_PHRASES`' current length
    against the date a run was saved to know whether it is comparable to
    another one (D15/D23).

    `grounding_ratio` and every other raw per-metric value returned here are
    UNCHANGED by which `METRIC_SET_VERSION` is active — only `filler_count`'s
    role in `substance_composite`'s weighted sum moves at that boundary (see
    there). A `grounding_ratio` of 0.30 means the same thing in v1 and v2.
    """
    text = deck_body_text(doc)
    word_count = len(_WORD_RE.findall(text)) or 1
    return {
        "numeric_density": numeric_density(doc),
        "proper_noun_density": proper_noun_density(doc),
        "grounding_ratio": grounding_ratio(doc, source_text_from_item(item)),
        "filler_count": filler_count(doc),
        "bare_bullet_ratio": bare_bullet_ratio(doc),
        "code_preserved": code_preserved(doc, item.brief),
        "word_count": word_count,
        "metric_set_version": METRIC_SET_VERSION,
    }


#: Density (per 100 words) at which numeric_density / proper_noun_density
#: saturate to a full 1.0 component. First guesses, not fitted: roughly "one
#: figure every 12 words" and "one named entity every 7 words" read as
#: comfortably specific prose without demanding a spec sheet. Revisit once
#: `--rescore` has been run against more than the two golden sets.
NUMERIC_DENSITY_TARGET = 8.0
PROPER_NOUN_DENSITY_TARGET = 15.0

#: Filler hits (per 100 words) at which the filler_count term saturates to a
#: full 1.0 penalty. First guess, not fitted, same status as the two targets
#: above — unlike them it was inert until T74 populated FILLER_PHRASES.
FILLER_DENSITY_TARGET = 3.0

#: Bumped whenever the composite's ACTIVE weight set changes shape (a term's
#: applicability flips, one is added/removed/reweighted) — not on every code
#: change. Exists so a saved run (or a person reading two numbers side by
#: side) can tell which composite era a figure belongs to without
#: re-deriving it from `FILLER_PHRASES`' current length (D15).
#:   v1 — FILLER_PHRASES was the empty T74 stub: filler_count always excluded
#:        and renormalised (5 applicable terms / 0.80 total weight, given
#:        code_preserved is also None on every deck in both golden sets — D22).
#:   v2 — T74 (2026-09-22): FILLER_PHRASES is populated, so filler_count is
#:        included whenever it was actually measured (6 applicable terms /
#:        0.90 total weight, same code_preserved caveat). See D23: composites
#:        stamped v1 and v2 are NOT comparable even though nothing about
#:        grounding_ratio or the other raw metrics changed.
METRIC_SET_VERSION: int = 2

#: Composite weights, fractions of 100. A first guess pending baseline data
#: — T68's own `--rescore` runs against the golden sets ARE that first data
#: point, nothing here is fit to a dataset yet.
#:
#: grounding_ratio carries the most weight because it is the direct,
#: unambiguous measure of the reported complaint ("generic claims") taken
#: straight from the project brief in project-context.md — it is the one
#: metric that cannot be gamed by a deck that merely sounds specific.
#: numeric_density and bare_bullet_ratio get the next tier: both are named,
#: literal instructions in the compose SYSTEM prompt ("numbers beat
#: adjectives", "a deck of nothing but headline-and-bullets is the failure
#: mode"), so obeying them is worth real weight, but obeying an instruction
#: is weaker evidence of substance than grounding is. proper_noun_density is
#: a softer version of the same idea as numeric_density (specific nouns
#: rather than specific numbers) and gets a smaller share for it.
#: hook_similarity and filler_count are real but narrower — visible symptoms
#: of low-effort generation rather than the substance question itself — so
#: they get the smallest shares; filler_count's weight is currently inert
#: (see FILLER_PHRASES) but reserved so populating the blocklist does not
#: require touching this table. code_preserved is scoped to the minority of
#: decks whose source even contains a fenced block, so when it does not
#: apply its weight is redistributed across whichever terms do (see
#: `substance_composite`), rather than penalising every codeless deck for
#: having nothing to preserve.
#:
#: These are the *documented* weights, not necessarily the *effective* ones:
#: `code_preserved` is `None` on every deck in both golden sets on disk today
#: (neither brief carries a fenced block), so every `--rescore` run so far
#: renormalised across the other six terms. As of METRIC_SET_VERSION 2
#: (T74's populated FILLER_PHRASES), `filler_count` is no longer universally
#: N/A, so the *effective* weights now depend on whether a given deck's brief
#: had a fence AND whether that deck actually contained any harvested filler
#: phrase — both per-deck facts, not a single constant like the v1 note this
#: replaced could state. Call `active_component_weights()` for the run-wide
#: part of that picture (whether `filler_count` is in play at all, given the
#: current `FILLER_PHRASES`); `code_preserved`'s per-deck applicability is
#: still only knowable from that deck's own metrics.
COMPOSITE_WEIGHTS: dict[str, float] = {
    "grounding_ratio": 0.30,
    "numeric_density": 0.15,
    "bare_bullet_ratio": 0.15,
    "proper_noun_density": 0.10,
    "hook_similarity": 0.10,
    "filler_count": 0.10,
    "code_preserved": 0.10,
}


def active_component_weights() -> dict[str, float]:
    """`COMPOSITE_WEIGHTS`, minus `filler_count` when `FILLER_PHRASES` is empty.

    The D15/D23 discoverability mechanism this module chose: rather than
    requiring a reader to separately check `len(FILLER_PHRASES)` or a run's
    date against T74's landing, this returns exactly the weight table
    `substance_composite` is using RIGHT NOW for the one component whose
    applicability is a global, run-wide fact rather than a per-deck one.

    `code_preserved` is deliberately NOT filtered here even though it is
    `None` (excluded) on every deck in both golden sets today — its
    applicability is per-deck (does THIS brief carry a fenced block?), so no
    module-level answer is correct for every deck a caller might score next.
    It stays in this table at its documented weight; `substance_composite`'s
    own per-deck renormalisation is still what actually applies during
    scoring. See `METRIC_SET_VERSION` for the version this table's shape
    corresponds to.
    """
    weights = dict(COMPOSITE_WEIGHTS)
    if not FILLER_PHRASES:
        del weights["filler_count"]
    return weights


def _saturate(value: float, target: float) -> float:
    """Map a raw per-100-words density to 0-1, saturating at `target`."""
    if target <= 0:
        return 0.0
    return max(0.0, min(1.0, value / target))


def substance_composite(metrics: dict, hook_similarity: float = 0.0) -> float:
    """Combine the seven substance metrics into one 0-100 score.

    `metrics` is `score_substance()`'s return value (or anything with the
    same keys). `hook_similarity` is a separate argument, not a key of
    `metrics`, because it is a run-level property (see `hook_similarity()`
    above) — the caller computes it once across every hook in the run and
    passes the same value in for every deck in that run. It defaults to 0.0
    (no penalty) so a single deck can still be scored with no run context.

    `code_preserved` is excluded from the weighted sum, and the remaining
    weights renormalised, whenever it is None (the source had no fenced
    block to preserve) — see `code_preserved()`. `filler_count` got the same
    treatment while `FILLER_PHRASES` was still the empty T74 stub (checked
    here via the live module global, not a value baked into `metrics` —
    see `filler_count()`'s own docstring on why that matters, T93/D26): a
    term nothing was ever checked against was excluded and renormalised, not
    credited a flat "fully good" 1.0. Now that `FILLER_PHRASES` is populated,
    `filler_count` is an active term like any other, and this function no
    longer treats it specially except through this same None-when-N/A path
    (reachable again if `FILLER_PHRASES` is ever reset to empty, e.g. in a
    test — see `active_component_weights()`).

    **The v1/v2 boundary this crossed (D15/D23, METRIC_SET_VERSION):** the
    moment `FILLER_PHRASES` went from empty to populated, the composite's
    active term count went from 5 (weight 0.80) to 6 (weight 0.90) given
    `code_preserved`'s N/A-on-every-golden-deck status (D22) — every *clean*
    deck's composite moves on that boundary alone, with ZERO change in deck
    quality. `grounding_ratio` and every other raw per-metric value are
    UNCHANGED by this boundary; only the composite does. Never compare a bare
    composite number across it — compare `metric_set_version` first, or
    compare the raw metrics `substance_composite` was built from instead.
    """
    word_count = max(1, metrics.get("word_count", 1))
    filler_density = metrics.get("filler_count", 0) / word_count * 100
    code = metrics.get("code_preserved")

    components: dict[str, float | None] = {
        "grounding_ratio": max(0.0, min(1.0, metrics.get("grounding_ratio", 0.0))),
        "numeric_density": _saturate(metrics.get("numeric_density", 0.0), NUMERIC_DENSITY_TARGET),
        "bare_bullet_ratio": 1.0 - max(0.0, min(1.0, metrics.get("bare_bullet_ratio", 1.0))),
        "proper_noun_density": _saturate(metrics.get("proper_noun_density", 0.0), PROPER_NOUN_DENSITY_TARGET),
        "hook_similarity": 1.0 - max(0.0, min(1.0, hook_similarity)),
        "filler_count": (1.0 - _saturate(filler_density, FILLER_DENSITY_TARGET)
                          if FILLER_PHRASES else None),
        "code_preserved": None if code is None else (1.0 if code else 0.0),
    }

    score, total_weight = 0.0, 0.0
    for key, value in components.items():
        if value is None:
            continue
        weight = COMPOSITE_WEIGHTS[key]
        score += weight * value
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return round(score / total_weight * 100, 2)
