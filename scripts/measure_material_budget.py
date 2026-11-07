"""Calibrate the news content-slide budget (T248 / D217, D219, D222).

Read-only against a COPY of the DB (opened ``mode=ro``); ZERO model calls.
Measures, over news items with slides:

1. candidate material-unit counts per deck (A notes-only, B notes + raw_text,
   each with/without confidence:"low") vs current total / content slides;
2. a SCRIPT-LOCAL diagnostic - how many content slides trace to the material
   (NOT quality.py, rule 12: grounding there reads the brief);
3. the D219 evidence gate's false-drop rate on existing code/quote/chart/kpi
   slides, using a number normaliser ("$65B" == "65 billion", "40%" == "40
   percent", commas) - with each drop printed for hand spot-check;
4. prevalence of decks missing hook / takeaway / sources / follow.

    ./.venv/bin/python scripts/measure_material_budget.py /path/to/app-copy.db

Numbers go stale when notes/compose output shape changes - re-run and
re-paste in the task that touches it (house rule 4).
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from difflib import SequenceMatcher
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Import reusable helpers from pipeline.integrity (moved in T249)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pipeline.integrity import NON_CONTENT_TYPES, NUM_RE as _NUM_RE, numbers_in

# --- configuration (house rule 4: every tunable in one header) -------------
MIN_ITEM_ID = 250            # items with id > this
INTENT = "news"
RAW_SUBSTANCE_CHARS = 600    # candidate B threshold; see raw_text distribution
FIXED_SLIDES = 4             # hook + takeaway + sources + follow
MAX_BUDGET = 6               # MAX_SLIDES(10) - FIXED_SLIDES
MIN_CHART_SERIES = 2         # D219 _MIN_CHART_SERIES
MIN_CODE_LINE_CHARS = 8      # shorter code lines are not evidence either way
CODE_LINE_FRACTION = 0.5     # verbatim-line share that counts as "code present"
TRACE_RATIO = 0.6            # share of a slide's key tokens found in material
SPOT_CHECK_DROPS = 40        # how many drops to print for hand review
# ---------------------------------------------------------------------------

QUOTE_FUZZY_COVERAGE = 0.9   # reported-only variant: share of quote tokens in >=3-token runs of material
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")
_STOP = {"The", "This", "That", "These", "Those", "And", "But", "For", "With", "What", "Why",
         "How", "When", "Who", "Its", "Their", "Not", "Are", "Was", "Has", "Have", "Can", "Will"}


def fuzzy_quote_coverage(q: str, mat_norm: str) -> float:
    """Share of the quote's tokens lying in a >=3-token run that also occurs in material."""
    qt, mt = norm_text(q).split(), mat_norm.split()
    sm = SequenceMatcher(None, qt, mt, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks() if b.size >= 3) / max(1, len(qt))


def norm_text(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("‘", "'")
    s = re.sub(r"[^a-z0-9']+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def js(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def notes_of(row) -> list[dict]:
    return [n for n in js(row["research"], []) if isinstance(n, dict)]


def material_text(row) -> str:
    parts = []
    for n in notes_of(row):
        parts += [str(n.get("claim") or ""), str(n.get("detail") or "")]
    parts.append(row["raw_text"] or "")
    return "\n".join(parts)


def units(row, *, raw_plus: bool, keep_low: bool) -> int:
    seen = set()
    for n in notes_of(row):
        if n.get("fail_open"):
            continue
        if not keep_low and n.get("confidence") == "low":
            continue
        c = norm_text(str(n.get("claim") or ""))
        if c:
            seen.add(c)
    return len(seen) + (1 if raw_plus and len(row["raw_text"] or "") >= RAW_SUBSTANCE_CHARS else 0)


def slide_text(s: dict) -> str:
    bits = []
    for k, v in s.items():
        if k in ("type", "image", "image_mode", "urls", "lang"):
            continue
        bits.append(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)
    return " ".join(bits)


def traces(s: dict, mat_norm: str, mat_nums: set[float]) -> tuple[bool, str]:
    txt = slide_text(s)
    nums = [n for m in _NUM_RE.finditer(txt) for n in [float(m.group(1).replace(",", ""))]]
    props = [w for w in _WORD_RE.findall(txt) if w[0].isupper() and w not in _STOP and len(w) > 2]
    keys = [("n", n) for n in nums] + [("p", p) for p in props]
    if keys:
        hit = sum(1 for k, v in keys if (v in mat_nums if k == "n" else norm_text(v) in mat_norm))
        return hit / len(keys) >= TRACE_RATIO, "key"
    words = [w for w in norm_text(txt).split() if len(w) >= 4]
    if not words:
        return True, "empty"
    return sum(1 for w in words if w in mat_norm) / len(words) >= TRACE_RATIO, "word"


def gate(s: dict, mat: str, mat_norm: str, mat_nums: set[float]) -> tuple[bool, str]:
    t = s.get("type")
    if t == "quote":
        q = s.get("quote") or ""
        segs = [norm_text(x) for x in re.split(r"\.{2,}|\u2026|(?<=[.!?])\s+", q) if norm_text(x)]
        miss = [x for x in segs if x not in mat_norm]
        return not miss, f"missing segs: {miss} fuzzy_cov={fuzzy_quote_coverage(q, mat_norm):.2f}"
    if t == "code":
        c = s.get("code") or ""
        if "```" in mat:
            return True, "fenced code in material"
        lines = [re.sub(r"\s+", " ", ln.strip()) for ln in c.splitlines() if len(ln.strip()) >= MIN_CODE_LINE_CHARS]
        flat = re.sub(r"\s+", " ", mat)
        hit = sum(1 for ln in lines if ln in flat)
        return bool(lines) and hit / len(lines) >= CODE_LINE_FRACTION, f"verbatim lines {hit}/{len(lines)}"
    if t == "chart":
        ser = s.get("series") or []
        vals = [numbers_in(str(p.get("display") or "")) | {round(float(p["value"]), 6)} for p in ser if isinstance(p, dict) and "value" in p]
        hit = sum(1 for v in vals if v & mat_nums)
        return hit >= MIN_CHART_SERIES, f"values in material {hit}/{len(vals)}"
    if t == "kpi":
        tiles = [x for x in (s.get("tiles") or []) if isinstance(x, dict)]
        miss = []
        for x in tiles:
            v = str(x.get("value") or "")
            nv = numbers_in(v)
            ok = bool(nv & mat_nums) if nv else norm_text(v) in mat_norm
            if not ok:
                miss.append(v)
        return not miss, f"tiles not in material: {miss}"
    return True, ""


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else 0


def dist(xs):
    return (f"n={len(xs)} min={min(xs)} p25={pct(xs,25)} med={statistics.median(xs)} "
            f"p75={pct(xs,75)} max={max(xs)} mean={statistics.mean(xs):.2f}")


def main(path: str) -> None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [r for r in con.execute(
        "SELECT * FROM items WHERE id>? AND intent=? AND slides!='[]' ORDER BY id", (MIN_ITEM_ID, INTENT))]
    decks = [(r, js(r["slides"], [])) for r in rows]
    print(f"decks: {len(decks)} (id>{MIN_ITEM_ID}, intent={INTENT}); by source: "
          f"{dict(Counter(r['source'] for r, _ in decks))}")

    raws = [len(r["raw_text"] or "") for r, _ in decks]
    print("\n== raw_text length (chars):", dist(raws))
    for th in (300, 400, 500, 600, 800, 1000):
        print(f"   share >= {th}: {sum(1 for x in raws if x >= th)/len(raws):.0%}")

    total = [len(s) for _, s in decks]
    content = [sum(1 for x in s if x.get("type") not in NON_CONTENT_TYPES) for _, s in decks]
    cands = {
        "A  notes, low in": lambda r: units(r, raw_plus=False, keep_low=True),
        "A  notes, low out": lambda r: units(r, raw_plus=False, keep_low=False),
        "B  notes+raw, low in": lambda r: units(r, raw_plus=True, keep_low=True),
        "B  notes+raw, low out": lambda r: units(r, raw_plus=True, keep_low=False),
        "clauses-gaps": lambda r: max(0, len(js(r["clauses"], [])) - len(js(r["gaps"], []))),
    }
    print("\n== 1. unit distributions (budget = clamp(max(units, clauses-gaps),1,%d))" % MAX_BUDGET)
    print(f"   current total slides  : {dist(total)}")
    print(f"   current CONTENT slides: {dist(content)}  (excl {sorted(NON_CONTENT_TYPES)})")
    U = {}
    for k, f in cands.items():
        U[k] = [f(r) for r, _ in decks]
        print(f"   {k:<22}: {dist(U[k])}")
    cg = U["clauses-gaps"]
    budgets = {}
    for k in list(cands)[:4]:
        budgets[k] = [min(MAX_BUDGET, max(1, max(u, c))) for u, c in zip(U[k], cg)]
    print("\n   budget (clamped) and resulting new total = %d + budget; vs content slides now:" % FIXED_SLIDES)
    for k, b in budgets.items():
        over = sum(1 for bb, c in zip(b, content) if c > bb)
        print(f"   {k:<22}: {dist(b)} | decks whose current content > budget: {over}/{len(b)} "
              f"| mean slides cut {statistics.mean(max(0, c-bb) for bb, c in zip(b, content)):.2f}")
    print("\n   units-bucket table (A low-in / B low-in): n, median total now, median content now")
    for name in ("A  notes, low in", "B  notes+raw, low in"):
        by = defaultdict(list)
        for u, t, c in zip(U[name], total, content):
            by[min(u, 7)].append((t, c))
        print(f"   {name}")
        for u in sorted(by):
            v = by[u]
            print(f"     units={u}{'+' if u==7 else ' '} n={len(v):>3} total_med={statistics.median(a for a,_ in v)} content_med={statistics.median(b for _,b in v)}")
    print("\n   P2 population (news decks with <=2 notes):")
    two = [i for i, (r, _) in enumerate(decks) if len(notes_of(r)) <= 2]
    print(f"     n={len(two)}  current total median={statistics.median(total[i] for i in two) if two else None}")
    for k, b in budgets.items():
        if two:
            print(f"     {k:<22}: budget med={statistics.median(b[i] for i in two)}  -> total med = "
                  f"{FIXED_SLIDES + statistics.median(b[i] for i in two)}  (dist {dist([b[i] for i in two])})")

    print("\n== 2. content-slide traceability (script-local, NOT quality.py)")
    by_type = defaultdict(lambda: [0, 0])
    tot = tr = 0
    for r, s in decks:
        m = material_text(r)
        mn, nums = norm_text(m), numbers_in(m)
        for x in s:
            if x.get("type") in NON_CONTENT_TYPES:
                continue
            ok, _ = traces(x, mn, nums)
            by_type[x["type"]][0] += 1
            by_type[x["type"]][1] += ok
            tot += 1
            tr += ok
    print(f"   traced (>= {TRACE_RATIO:.0%} of numbers+proper nouns, else words, in notes+raw): {tr}/{tot} = {tr/tot:.0%}")
    for t, (n, k) in sorted(by_type.items()):
        print(f"     {t:<8} {k}/{n} = {k/n:.0%}")
    per_deck = [sum(1 for x in s if x.get("type") not in NON_CONTENT_TYPES and not traces(x, norm_text(material_text(r)), numbers_in(material_text(r)))[0]) for r, s in decks]
    print(f"   untraced content slides per deck: {dist(per_deck)}; decks with >=1: {sum(1 for x in per_deck if x)}/{len(per_deck)}")

    print("\n== 3. evidence gate (D219) vs existing rich slides")
    drops = []
    stat = defaultdict(lambda: [0, 0])
    for r, s in decks:
        m = material_text(r)
        mn, nums = norm_text(m), numbers_in(m)
        for x in s:
            if x.get("type") in ("code", "quote", "chart", "kpi"):
                ok, why = gate(x, m, mn, nums)
                stat[x["type"]][0] += 1
                stat[x["type"]][1] += (not ok)
                if not ok:
                    drops.append((r["id"], x, why, m))
    for t, (n, d) in sorted(stat.items()):
        print(f"   {t:<6} slides={n:>3} dropped={d:>3} ({d/n:.0%})")
    qd = [d for d in drops if d[1]["type"] == "quote"]
    fz = sum(1 for d in qd if float(d[2].rsplit("=", 1)[1]) >= QUOTE_FUZZY_COVERAGE)
    print(f"   (quote drops with fuzzy coverage >= {QUOTE_FUZZY_COVERAGE}: {fz}/{len(qd)} - candidates for a tolerant rule)")
    print(f"   TOTAL  slides={sum(v[0] for v in stat.values())} dropped={len(drops)}")
    print(f"\n   -- drops for hand spot-check (first {SPOT_CHECK_DROPS}, spread by type) --")
    bytype = defaultdict(list)
    for d in drops:
        bytype[d[1]["type"]].append(d)
    picked = []
    while len(picked) < min(SPOT_CHECK_DROPS, len(drops)):
        for t in list(bytype):
            if bytype[t]:
                picked.append(bytype[t].pop(0))
        if not any(bytype.values()):
            break
    for i, (iid, x, why, m) in enumerate(picked, 1):
        body = {k: v for k, v in x.items() if k not in ("type", "image", "image_mode")}
        print(f"   [{i}] item {iid} {x['type']}: {why}\n       slide: {json.dumps(body, ensure_ascii=False)[:420]}\n       material_chars={len(m)}")

    print("\n== 4. decks missing hook / takeaway / sources / follow")
    for src in sorted({r["source"] for r, _ in decks}):
        sub = [s for r, s in decks if r["source"] == src]
        line = []
        for t in ("hook", "takeaway", "sources", "follow"):
            miss = sum(1 for s in sub if not any(x.get("type") == t for x in s))
            line.append(f"no-{t}={miss}/{len(sub)} ({miss/len(sub):.0%})")
        print(f"   {src:<8} n={len(sub)}: " + "  ".join(line))
    print("   decks at 10 slides:", sum(1 for t in total if t >= 10), "/", len(total))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
