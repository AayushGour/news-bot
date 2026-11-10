"""Slide Markdown: sanitised rendering, and the conversion of legacy "code" slides.

T362 / U17. A "notes" slide carries a Markdown ``body`` and nothing else the
reader would see as source: no JSON, YAML, code blocks or Markdown syntax. This
module is the one place that turns that string into HTML and the one place that
knows how to turn a structure (a parsed JSON/YAML document) into Markdown.

Security model (the renderer is Chromium on file://, T308): the model's text is
UNTRUSTED. ``render_markdown`` parses with raw HTML OFF, rewrites the token
stream down to a fixed set of elements (links, images, inline code, headings and
rules never survive as such), and then re-parses its own output and refuses
anything outside ``ALLOWED_TAGS`` / ``ALLOWED_ATTRS``, falling back to escaped
plain text. Only the result is marked safe.

A leaf module: it imports nothing from the pipeline.
"""

from __future__ import annotations

import json
import re
from html import escape as html_escape
from html.parser import HTMLParser
from typing import Any

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token
from markupsafe import Markup

NOTES_TYPE = "notes"
#: The retired slide type. Stored decks still carry it; it is converted, never composed.
LEGACY_CODE_TYPE = "code"

# --- limits ---------------------------------------------------------------
# Editorial (what the composer is asked for) vs hard (what normalise_slides
# tolerates before cutting). The layout facts they are set against were measured
# with a real Chromium render on 2026-11-09 across all 12 themes: see the T362
# handoff; re-measure when the .mdbody CSS in templates/base.html.j2 changes.
#: doodle (the large handwritten theme) is the binding theme: 5 list items of
#: ~60 chars fit, 6 do not.
BODY_MAX_CHARS = 340
BODY_MAX_LINES = 8
TABLE_MAX_ROWS = 5  # body rows, header excluded
TABLE_MAX_COLS = 3
#: a converted legacy table may be taller than the composer is asked for.
TABLE_HARD_ROWS = 8
#: normalise_slides cuts a body at this length (a request is not a guarantee).
BODY_HARD_CHARS = 480
#: Cells and list items longer than this are cut when converting a structure.
ITEM_MAX_CHARS = 70
LIST_MAX_ITEMS = 8
NEST_MAX_DEPTH = 3
#: Every converter reads at most this much of a code/JSON/YAML/body string before any
#: parsing: a parser's cost grows with its input, and the input is model-written.
INPUT_MAX_CHARS = 4000
#: Nodes ``_compact`` may visit for one value, and the output it may build.
COMPACT_MAX_NODES = 60
#: Every regex in this module sees a line cut to this length first. Editorial: no
#: legacy slide line is anywhere near it, and it bounds any pattern's worst case.
LINE_MAX_CHARS = 1000

ALLOWED_TAGS = frozenset({
    "p", "ul", "ol", "li", "strong", "em", "s", "br", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td",
})
HEADING_CLASS = "mdh"
#: tag -> the only attributes (and their only values) it may carry.
ALLOWED_ATTRS: dict[str, dict[str, str]] = {"p": {"class": HEADING_CLASS}}

JSON_LANGS = frozenset({"json", "jsonc", "json5", "jsonl", "geojson"})
YAML_LANGS = frozenset({"yaml", "yml", "toml", "ini"})
MARKDOWN_LANGS = frozenset({"", "md", "markdown", "text", "txt", "plain", "plaintext"})

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([^\n`]*)$")
_MD_SPECIALS = re.compile(r"([\\`*_\[\]<>|#!&~])")
_LEADING_MARKER = re.compile(r"^([-+]|\d+[.)])(\s|$)")
#: One "key: value" line, matched on a stripped, length-capped line. The key is words
#: joined by SINGLE spaces, so no quantifier ranges over whitespace twice (the old
#: lazy ``[\w .\-/]+?`` + optional-trailer form backtracked cubically).
_KV_LINE = re.compile(r'^"?([\w.\-/]+(?: [\w.\-/]+)*)"?\s*[:=]\s*(.+)$')
_JSON_PAIR = re.compile(r'"([^"\n]+)"\s*:\s*("(?:[^"\\\n]|\\.)*"|[^,\n}\]]+)')
_COMMENT_MARK = re.compile(r"^(#+|//+|--+|/\*+|\*+)\s*")
_BOX_CHARS = re.compile("[\u2500-\u259f\u25b2\u25bc]")
_RULE_LINE = re.compile(r"^[-=_*~]{3,}$")
_WRAP_COLUMN = 60  # a line this long was hard-wrapped; shorter ones are deliberate breaks
_BULLET = re.compile(r"^\s*[\u2022\u25aa\u25e6\u2023\u2219*+-]\s+")
_NUMBERED = re.compile(r"^\s*\d{1,2}[.)]\s+")
_NOISE_LINE = re.compile(r"^[\s{}\[\]()<>;,/\\*#-]*$")
_CODE_CALL = re.compile(
    r"^\s*[\w.\[\]]+\s*=\s*.+[;)\]]\s*$"  # x[0] = f(a);  y = g(b)
    r"|^\s*[\w.]+\(.*\)\s*;?\s*$"  # f(a, b)
    r"|[\w\]]\.\w+\(.*\)\s*;?\s*$"  # obj.method(arg)
    r"|\)\s*:\s*$"  # def f(x):
)
_CODE_LINE_END = re.compile(r"[{};\\]\s*$|^\s*(def |import |from \S+ import |function |const |let |var |class |\$ )")
_TRAILING_BACKSLASH = re.compile(r"\s*\\$")
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")
_LEADING_DIGITS = re.compile(r"\d+")
_WORD_SEPARATORS = re.compile(r"[\s_\-]+")
_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)(.*)\Z", re.DOTALL)


# --- escaping -------------------------------------------------------------


def _lines(text: str) -> list[str]:
    """``text`` split into lines, each cut to ``LINE_MAX_CHARS``: the only way this
    module feeds a line to a regex."""
    return [ln[:LINE_MAX_CHARS] for ln in text.splitlines()]


def _kv(line: str) -> tuple[str, str] | None:
    """``("key", "value")`` for a "key: value" / "key = value" line, else None."""
    m = _KV_LINE.match(line.strip()[:LINE_MAX_CHARS])
    return (m.group(1), m.group(2).strip().rstrip(" ,").strip('"')) if m else None


def escape_md(text: Any) -> str:
    """``text`` as inert Markdown inline content: one line, specials backslashed,
    a leading list marker defused."""
    flat = " ".join(str(text).split())
    out = _MD_SPECIALS.sub(r"\\\1", flat)
    marker = _LEADING_MARKER.match(out)
    if not marker:
        return out
    # defuse by backslashing the marker punctuation ("1\\." / "\\-"); a backslash
    # before a digit is not an escape and would show.
    at = 0 if out[0] in "-+" else len(_LEADING_DIGITS.match(out).group())
    return out[:at] + "\\" + out[at:]


def _clip(text: str, limit: int = ITEM_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _cell(value: Any) -> str:
    return escape_md(_clip(_humanise_value(" ".join(_scalar(value).split()))))


def _scalar(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value is None:
        return "none"
    return str(value)


#: Trailing unit words on a key -> how the unit reads in a label ("capacity_gw" -> "Capacity (GW)").
UNIT_SUFFIXES = {
    "gw": "GW", "mw": "MW", "kw": "kW", "mb": "MB", "gb": "GB", "kb": "KB", "tb": "TB",
    "ms": "ms", "hz": "Hz", "pct": "%", "usd": "USD", "eur": "EUR", "sqft": "sq ft",
}
#: "revenue per gw": the unit is part of the phrase, not a bracketed suffix.
UNIT_JOINERS = frozenset({"per", "in", "of", "to", "by"})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SNAKE_VALUE = re.compile(r"^(?=.*[A-Za-z])[a-z0-9]+(?:_[a-z0-9]+)+$")


def _label(key: Any) -> str:
    """A JSON/YAML key as a reader-facing label: words, sentence case, unit in brackets."""
    words = [w for w in _WORD_SEPARATORS.split(_CAMEL.sub(" ", str(key))) if w]
    if not words:
        return ""
    unit = ""
    if len(words) > 1 and words[-1].lower() in UNIT_SUFFIXES and words[-2].lower() not in UNIT_JOINERS:
        unit = f" ({UNIT_SUFFIXES[words[-1].lower()]})"
        words = words[:-1]
    text = " ".join(
        UNIT_SUFFIXES[w.lower()] if unit == "" and i and w.lower() in UNIT_SUFFIXES and words[i - 1].lower() in UNIT_JOINERS
        else (w if w.isupper() and len(w) > 1 else w.lower())
        for i, w in enumerate(words)
    )
    return text[:1].upper() + text[1:] + unit


def _humanise_value(text: str) -> str:
    """An identifier-shaped value ("panel_simulation") as words; anything else as is."""
    return text.replace("_", " ") if _SNAKE_VALUE.match(text) else text


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list, tuple))


# --- structure -> Markdown ------------------------------------------------


def _table(header: list[str], rows: list[list[str]]) -> str:
    cols = max(len(header), 1)
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * cols]
    for row in rows[:TABLE_HARD_ROWS]:
        padded = row + [""] * (cols - len(row))
        lines.append("| " + " | ".join(padded[:cols]) + " |")
    return "\n".join(lines)


def _kv_table(rows: list[list[str]]) -> str:
    """A two-column key/value table with no header row: ``_clean_tokens`` drops the
    empty header, so the reader sees keys and values and no "FIELD | VALUE"."""
    return _table(["", ""], rows)


ORDINAL_KEYS = ("step", "order", "index", "n", "no", "number", "rank", "stage", "position")
TEXT_KEYS = ("action", "description", "name", "title", "text", "label", "summary", "task")


def _ordinal_list(items: list[dict]) -> list[str] | None:
    """Objects that are exactly an ordinal plus one text key -> numbered lines."""
    texts: list[tuple[float, str]] = []
    for item in items:
        keys = {str(k).lower(): k for k in item}
        ordinal = next((keys[k] for k in ORDINAL_KEYS if k in keys), None)
        text = next((keys[k] for k in TEXT_KEYS if k in keys), None)
        if ordinal is None or text is None or len(item) != 2 or not isinstance(item[text], str):
            return None
        try:
            position = float(item[ordinal])
        except (TypeError, ValueError):
            return None
        texts.append((position, item[text]))
    ordered = [t for _, t in sorted(texts, key=lambda pair: pair[0])]
    return [f"{i}. {_cell(t)}" for i, t in enumerate(ordered[:LIST_MAX_ITEMS], start=1)]


def _pair_list(items: list[dict]) -> list[str] | None:
    """Objects with exactly two text values -> "**first** - second" bullets."""
    lines = []
    for item in items:
        values = list(item.values())
        if len(values) != 2 or not all(isinstance(v, str) for v in values):
            return None
        lines.append(f"- **{_cell(values[0])}** \u2014 {_cell(values[1])}")
    return lines[:LIST_MAX_ITEMS]


def _object_list_lines(items: list[dict]) -> list[str] | None:
    """The reader-friendly forms for a list of flat objects, or None (-> a table)."""
    return _ordinal_list(items) or _pair_list(items)


def _nested(value: Any, depth: int) -> list[str]:
    """Bullet lines for a structure, two spaces of indent per level."""
    pad = "  " * depth
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in list(value.items())[:LIST_MAX_ITEMS]:
            name = f"**{escape_md(_label(key))}**"
            if _is_scalar(item):
                lines.append(f"{pad}- {name}: {_cell(item)}")
            elif depth + 1 >= NEST_MAX_DEPTH:
                lines.append(f"{pad}- {name}: {_cell(_compact(item))}")
            else:
                lines.append(f"{pad}- {name}")
                lines.extend(_nested(item, depth + 1))
    elif isinstance(value, (list, tuple)):
        dicts = [i for i in value if isinstance(i, dict) and all(_is_scalar(v) for v in i.values())]
        friendly = _object_list_lines(dicts) if len(dicts) == len(value) and dicts else None
        if friendly:
            return [f"{pad}{line}" for line in friendly]
        for item in list(value)[:LIST_MAX_ITEMS]:
            if _is_scalar(item):
                lines.append(f"{pad}- {_cell(item)}")
            elif isinstance(item, dict) and all(_is_scalar(v) for v in item.values()):
                parts = [f"**{escape_md(_label(k))}**: {_cell(v)}" for k, v in list(item.items())[:TABLE_MAX_COLS + 1]]
                lines.append(f"{pad}- " + "; ".join(parts))
            elif depth + 1 >= NEST_MAX_DEPTH:
                lines.append(f"{pad}- {_cell(_compact(item))}")
            else:
                inner = _nested(item, depth + 1)
                if inner:
                    # first child line joins the bullet; the rest stay nested
                    lines.append(f"{pad}- " + inner[0].lstrip()[2:])
                    lines.extend(inner[1:])
    return lines


def _compact(value: Any, _budget: list[int] | None = None) -> str:
    """A nested value flattened to one plain sentence-like string. Bounded: it stops
    after ``COMPACT_MAX_NODES`` nodes or ``ITEM_MAX_CHARS`` characters, whatever the
    value's expanded size."""
    budget = [COMPACT_MAX_NODES] if _budget is None else _budget
    budget[0] -= 1
    if budget[0] < 0:
        return "\u2026"
    if isinstance(value, dict):
        parts = [(_label(k), v) for k, v in value.items()]
        render = lambda pair: f"{pair[0]} {_compact(pair[1], budget)}"  # noqa: E731
    elif isinstance(value, (list, tuple)):
        parts = list(value)
        render = lambda item: _compact(item, budget)  # noqa: E731
    else:
        return _scalar(value)[:ITEM_MAX_CHARS]
    out: list[str] = []
    for part in parts:
        piece = render(part)
        out.append(piece)
    return ", ".join(out)[: ITEM_MAX_CHARS + 1]


def structure_to_markdown(value: Any) -> str:
    """``_structure_to_markdown`` with its output bounded at a line boundary."""
    return _cap(_structure_to_markdown(value), BODY_HARD_CHARS)


def _structure_to_markdown(value: Any) -> str:
    """A parsed JSON/YAML value as a table (flat object, list of flat objects),
    a bullet list (list of scalars) or nested bullets. Deterministic."""
    if isinstance(value, dict) and value:
        if all(_is_scalar(v) for v in value.values()):
            rows = [[escape_md(_label(k)), _cell(v)] for k, v in value.items()]
            return _kv_table(rows)
        return "\n".join(_nested(value, 0))
    if isinstance(value, (list, tuple)) and value:
        dicts = [v for v in value if isinstance(v, dict)]
        if len(dicts) == len(value) and all(all(_is_scalar(x) for x in d.values()) for d in dicts):
            friendly = _object_list_lines(dicts)
            if friendly:
                return "\n".join(friendly)
            keys: list[str] = []
            for d in dicts:
                keys.extend(k for k in d if k not in keys)
            keys = keys[:TABLE_MAX_COLS]
            header = [escape_md(_label(k)) for k in keys]
            rows = [[_cell(d.get(k, "")) if k in d else "" for k in keys] for d in dicts]
            return _table(header, rows)
        return "\n".join(_nested(value, 0))
    if value in (None, "", {}, []):
        return ""
    return escape_md(_clip(_scalar(value), BODY_MAX_CHARS))


def _lines_to_markdown(lines: list[str]) -> str:
    """Key/value lines -> a table; anything else -> bullets. Used where a parse failed."""
    kept = [ln.strip() for ln in (ln[:LINE_MAX_CHARS] for ln in lines) if ln.strip() and not _NOISE_LINE.match(ln)]
    pairs = [_kv(ln) for ln in kept]
    if kept and all(pairs):
        return _kv_table([[escape_md(_label(k)), _cell(v)] for k, v in filter(None, pairs)])
    return "\n".join(f"- {_cell(ln)}" for ln in kept[:LIST_MAX_ITEMS])


# --- legacy code -> Markdown ---------------------------------------------


def looks_like_json(text: str) -> bool:
    t = text.strip()
    return len(t) > 1 and ((t[0] == "{" and t[-1] == "}") or (t[0] == "[" and t[-1] == "]"))


def _json_to_markdown(code: str) -> str:
    try:
        return structure_to_markdown(json.loads(code))
    except (ValueError, RecursionError):
        pass
    # Relaxed JSON (bare keys, single quotes, a leading "name: {...}") is valid YAML flow syntax.
    loaded = _safe_yaml(code)
    if isinstance(loaded, (dict, list)) and loaded:
        return structure_to_markdown(loaded)
    pairs = [  # truncated / commented JSON: keep what is readable
        (k, v) for line in _lines(code) for k, v in _JSON_PAIR.findall(line) if v.strip() not in {"[", "{"}
    ]
    if pairs:
        rows = [[escape_md(_label(k)), _cell(v.strip().strip('"'))] for k, v in pairs[:TABLE_MAX_ROWS + 3]]
        return _kv_table(rows)
    return _lines_to_markdown(code.splitlines())


class _NoAliasLoader(yaml.SafeLoader):
    """``safe_load`` minus anchors and aliases. An alias is a shared reference, so a
    few hundred bytes of nested aliases expand to a tree of astronomical size
    ("billion laughs"); nothing a slide needs uses one."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not accepted")
        if getattr(self.peek_event(), "anchor", None) is not None:
            raise yaml.YAMLError("YAML anchors are not accepted")
        return super().compose_node(parent, index)


def _safe_yaml(code: str) -> Any:
    """``code`` parsed as YAML with no tags, anchors or aliases; None if it will not."""
    try:
        return yaml.load(code[:INPUT_MAX_CHARS], Loader=_NoAliasLoader)  # noqa: S506 - a SafeLoader
    except (yaml.YAMLError, ValueError, RecursionError, MemoryError):
        return None


def _yaml_to_markdown(code: str) -> str:
    loaded = _safe_yaml(code)
    if isinstance(loaded, (dict, list)) and loaded:
        return structure_to_markdown(loaded)
    return _lines_to_markdown(code.splitlines())


def code_to_markdown(code: str, lang: str = "", _depth: int = 0) -> str:
    """The Markdown that stands in for ``code`` (written in ``lang``) on a notes
    slide. Never a code block: JSON/YAML become tables or lists, Markdown stays
    Markdown, any other language becomes a short bullet list of its lines."""
    lang = (lang or "").strip().lower()
    text = (code or "")[:INPUT_MAX_CHARS].replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        return ""
    if lang in JSON_LANGS or (lang in MARKDOWN_LANGS and looks_like_json(text)):
        return _json_to_markdown(text)
    if lang in YAML_LANGS:
        return _yaml_to_markdown(text)
    if lang in MARKDOWN_LANGS:
        front = _FRONTMATTER.match(text)
        if front:
            head = _yaml_to_markdown(front.group(1))
            rest = convert_fences(front.group(2).strip(), _depth + 1)
            return "\n\n".join(p for p in (head, rest) if p)
        if lang in ("md", "markdown"):
            return convert_fences(text, _depth + 1)
        if not body_is_code(text):
            return _plain_text_to_markdown(text)
    return _code_to_prose(text)


def _code_to_prose(text: str) -> str:
    """Source in a programming language as a short bullet list: its comments when it
    has any (they are the prose), else its meaningful lines. Never a code block."""
    lines = [_TRAILING_BACKSLASH.sub("", ln.strip()) for ln in _lines(text) if ln.strip()]
    comments = [_COMMENT_MARK.sub("", ln) for ln in lines if _COMMENT_MARK.match(ln)]
    comments = [c for c in comments if c and not _NOISE_LINE.match(c)]
    kept = comments or [ln for ln in lines if not _NOISE_LINE.match(ln)]
    return "\n".join(f"- {escape_md(_clip(ln))}" for ln in kept[:LIST_MAX_ITEMS])


def _plain_text_to_markdown(text: str) -> str:
    """Preformatted plain text (ASCII boxes, bullets, numbered steps, "Key: value"
    lines) as Markdown that keeps its structure but none of its layout."""
    items: list[tuple[str, list[str]]] = []  # (kind, lines); kind is p | ul | ol | row | "" (blank)
    for raw in _lines(text):
        cells = [c.strip() for c in raw.split("\u2502") if c.strip(" \t\u2500-\u259f")]
        if "\u2502" in raw and len(cells) >= 2:
            items.append(("row", [_BOX_CHARS.sub(" ", c).strip() for c in cells]))
            continue
        line = _BOX_CHARS.sub(" ", raw).rstrip()
        stripped = line.strip()
        if not stripped or _RULE_LINE.match(stripped):
            items.append(("", []))
            continue
        bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
        if bullet:
            items.append(("ul", [line[bullet.end():].strip()]))
        elif numbered:
            items.append(("ol", [line[numbered.end():].strip()]))
        elif items and items[-1][0] in ("ul", "ol") and line[:1].isspace():
            items[-1][1][0] += f" {stripped}"  # an indented continuation of the bullet / step
        elif items and items[-1][0] == "p":
            items[-1][1].append(stripped)
        else:
            items.append(("p", [stripped]))
    content = [(k, ls) for k, ls in items if k]
    lines_p = [ln for k, ls in content if k == "p" for ln in ls]
    pairs = [_kv(t) for t in lines_p]
    if len(lines_p) >= 2 and all(k == "p" for k, _ in content) and all(pairs):
        return _kv_table([[escape_md(_label(k)), _cell(v)] for k, v in filter(None, pairs)])
    out: list[str] = []
    steps = 0
    previous = ""
    for kind, lines in items:
        if not kind:
            previous = ""
            continue
        if out and kind != previous:
            out.append("")
        steps = steps + 1 if kind == "ol" else 0
        if kind == "p":
            out.append(_wrapped(lines))
            out.append("")
        elif kind == "row":
            out.append(f"- **{escape_md(lines[0])}**: " + " \u2014 ".join(escape_md(c) for c in lines[1:]))
        elif kind == "ul":
            out.append("- " + escape_md(lines[0]))
        else:
            out.append(f"{max(steps, 1)}. " + escape_md(lines[0]))
        previous = kind
    return _EXTRA_BLANK_LINES.sub("\n\n", "\n".join(out)).strip()


def _wrapped(lines: list[str]) -> str:
    """One paragraph from consecutive text lines: a hard-wrapped line (long) flows
    into the next; a short one keeps its break."""
    out = escape_md(lines[0])
    for prev, line in zip(lines, lines[1:], strict=False):
        out += (" " if len(prev) >= _WRAP_COLUMN else "  \n") + escape_md(line)
    return out


def convert_fences(source: str, _depth: int = 0) -> str:
    """Replace every fenced block with ``code_to_markdown`` of its content. An
    unclosed fence runs to the end. After this, no fence marker is left."""
    if _depth > 2:
        return source
    source = source[:INPUT_MAX_CHARS]
    out: list[str] = []
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        opened = _FENCE_OPEN.match(lines[i][:LINE_MAX_CHARS])
        if not opened:
            out.append(lines[i])
            i += 1
            continue
        marker, info = opened.group(1), opened.group(2).strip()
        close = re.compile(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$")
        j = i + 1
        while j < len(lines) and not close.match(lines[j][:LINE_MAX_CHARS]):
            j += 1
        body = "\n".join(lines[i + 1 : j])
        converted = code_to_markdown(body, info.split()[0] if info else "", _depth + 1)
        out.extend(["", converted, ""] if converted else [""])
        i = j + 1
    return "\n".join(out)


# --- composer-side guards -------------------------------------------------


def _is_code_line(line: str) -> bool:
    return bool(_CODE_LINE_END.search(line) or _CODE_CALL.search(line))


def body_is_code(text: str) -> bool:
    """True if ``text`` is still source code / data rather than prose, after fences
    were converted: broken JSON, or most non-comment lines shaped like code."""
    stripped = text.strip()
    if not stripped:
        return False
    if looks_like_json(stripped) or (stripped[0] in "{[" and ('":' in stripped or "}," in stripped)):
        return True
    lines = [
        ln for ln in _lines(stripped)
        if ln.strip() and not _COMMENT_MARK.match(ln.strip()) and not ln.lstrip().startswith("|")
    ]
    if len(lines) < 2:
        return False
    return sum(1 for ln in lines if _is_code_line(ln)) * 2 >= len(lines)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n" in cut:
        cut = cut[: cut.rfind("\n")]
    else:
        cut = cut[: max(cut.rfind(" "), 0) or limit]
    return cut.rstrip()


def coerce_body(body: Any) -> str:
    """A model-written ``body`` as it is stored: fences converted, a whole-body JSON
    document turned into a table/list, length bounded. Empty when nothing is left."""
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = convert_fences(text).strip()
    if looks_like_json(text):
        text = _json_to_markdown(text)
    text = _EXTRA_BLANK_LINES.sub("\n\n", text)
    return _cap(text, BODY_HARD_CHARS).strip()


# --- Markdown -> sanitised HTML ------------------------------------------

_MD = (
    MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": False})
    .enable("table")
    .disable(["fence", "code"])
)


def _text(content: str) -> Token:
    tok = Token("text", "", 0)
    tok.content = content
    return tok


def _clean_inline(children: list[Token]) -> list[Token]:
    out: list[Token] = []
    for child in children:
        if child.type in ("link_open", "link_close"):
            continue
        if child.type in ("image", "code_inline"):
            out.append(_text(child.content))
            continue
        child.attrs = {}
        out.append(child)
    return out


def _drop_empty_headers(tokens: list[Token]) -> list[Token]:
    """Remove a <thead> whose cells are all empty (the header-less key/value table)."""
    out: list[Token] = []
    i = 0
    while i < len(tokens):
        if tokens[i].type == "thead_open":
            end = next(j for j in range(i, len(tokens)) if tokens[j].type == "thead_close")
            if not any(t.content.strip() for t in tokens[i : end + 1] if t.type == "inline"):
                i = end + 1
                continue
        out.append(tokens[i])
        i += 1
    return out


def _clean_tokens(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    for tok in _drop_empty_headers(tokens):
        if tok.type in ("hr", "fence", "code_block", "html_block"):
            continue
        if tok.type in ("heading_open", "heading_close"):
            tok.type = tok.type.replace("heading", "paragraph")
            tok.tag = "p"
            tok.attrs = {"class": HEADING_CLASS} if tok.nesting == 1 else {}
            tok.markup = ""
        else:
            tok.attrs = {}
        if tok.type == "inline" and tok.children:
            tok.children = _clean_inline(tok.children)
        out.append(tok)
    return out


class _Audit(HTMLParser):
    """Rejects any tag or attribute outside the allowlist in our own output."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ok = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        allowed = ALLOWED_ATTRS.get(tag, {})
        if tag not in ALLOWED_TAGS or any(allowed.get(k) != v for k, v in attrs):
            self.ok = False

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            self.ok = False

    def handle_comment(self, data: str) -> None:
        self.ok = False

    def handle_decl(self, decl: str) -> None:
        self.ok = False

    def handle_pi(self, data: str) -> None:
        self.ok = False


def _audited(html: str) -> bool:
    audit = _Audit()
    audit.feed(html)
    audit.close()
    return audit.ok


def _plain(source: str) -> Markup:
    paras: list[list[str]] = [[]]
    for line in source.splitlines():
        if line.strip():
            paras[-1].append(line.strip())
        elif paras[-1]:
            paras.append([])
    return Markup("".join(f"<p>{html_escape(' '.join(p))}</p>" for p in paras if p))


def render_markdown(source: Any) -> Markup:
    """``source`` (untrusted Markdown) as HTML restricted to ``ALLOWED_TAGS``.

    Raw HTML is escaped to text; links, images, inline code, headings, rules and
    code blocks are flattened to text/paragraphs; fenced code is converted first
    (``convert_fences``). The only ``Markup`` handed to a template."""
    text = convert_fences(str(source or ""))  # which cuts the input at INPUT_MAX_CHARS
    try:
        tokens = _clean_tokens(_MD.parse(text))
        html = _MD.renderer.render(tokens, _MD.options, {})
    except Exception:  # noqa: BLE001 - hostile input must degrade to text, never raise
        return _plain(text)
    return Markup(html) if _audited(html) else _plain(text)


# --- legacy slide migration ----------------------------------------------


def migrate_slide(slide: dict) -> dict:
    """A legacy ``code`` slide as the ``notes`` slide it renders as; any other slide
    is returned unchanged (same object). The input is never mutated."""
    if slide.get("type") != LEGACY_CODE_TYPE:
        return slide
    kept = {k: v for k, v in slide.items() if k not in ("type", "code", "lang")}
    body = code_to_markdown(str(slide.get("code") or ""), str(slide.get("lang") or ""))
    return {"type": NOTES_TYPE, **kept, "body": body}


def migrate_deck(slides: list[dict]) -> list[dict]:
    return [migrate_slide(s) if isinstance(s, dict) else s for s in slides]


def prepare_for_render(slides: list[dict]) -> list[dict]:
    """Slides as the template wants them: legacy code migrated, every notes ``body``
    rendered to safe HTML under ``body_html``. Inputs are not mutated."""
    out = []
    for slide in migrate_deck(slides):
        if slide.get("type") == NOTES_TYPE:
            slide = {**slide, "body_html": render_markdown(slide.get("body"))}
        out.append(slide)
    return out
