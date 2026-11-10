"""T362 / U17: slide Markdown renders sanitised; legacy code slides convert; no code/JSON shown."""

from __future__ import annotations

import re
import time

import pytest

from pipeline import rich_text as rt
from pipeline.stages.compose import normalise_slides
from pipeline.stages.render import build_html
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
TAG = re.compile(r"</?([a-z0-9]+)")


def tags(html: str) -> set[str]:
    return set(TAG.findall(str(html)))


# ---- sanitisation --------------------------------------------------------
HOSTILE = [
    "<script>alert(1)</script>",
    '<img src="file:///etc/passwd" onerror="x()">',
    "<div onclick=1>raw</div>",
    "[x](javascript:alert(1))",
    "![i](https://evil.example/p.png)",
    "![i](file:///etc/passwd)",
    "<http://evil.example/track>",
    "<iframe src=//evil></iframe>",
    "<a href='file:///etc/passwd'>l</a>",
    "<style>body{background:url(file:///x)}</style>",
]


@pytest.mark.parametrize("src", HOSTILE)
def test_hostile_markup_never_becomes_an_element(src):
    out = str(rt.render_markdown(f"hello\n\n{src}\n\nbye"))
    assert tags(out) <= rt.ALLOWED_TAGS
    for banned in ("<script", "<img", "<a ", "<iframe", "<style", "<div"):
        assert banned not in out


def test_links_and_images_keep_only_their_text():
    out = str(rt.render_markdown("see [the docs](https://a.example/x) and ![alt text](https://a.example/i.png)"))
    assert "the docs" in out and "alt text" in out
    assert "a.example" not in out and "<a" not in out and "<img" not in out


def test_markdown_elements_render():
    out = str(rt.render_markdown(
        "- a\n- b\n\n1. one\n2. two\n\n| H1 | H2 |\n|:--|--:|\n| x | y |\n\n**bold** *it*\n\n> quote\n\n# Head"
    ))
    for tag in ("ul", "ol", "li", "table", "thead", "th", "td", "strong", "em", "blockquote"):
        assert f"<{tag}" in out, tag
    assert 'class="mdh"' in out
    assert "style=" not in out and "align" not in out  # table alignment attrs stripped


def test_fence_never_renders_as_code():
    out = str(rt.render_markdown('before\n\n```json\n{"a": 1, "b": 2}\n```\n\nafter'))
    assert "<pre" not in out and "<code" not in out and "```" not in out and "{" not in out
    assert "<table" in out  # the JSON became a table


def test_inline_code_and_indented_code_are_plain():
    out = str(rt.render_markdown("use `foo()` now\n\n    indented()\n"))
    assert "<code" not in out and "<pre" not in out and "foo()" in out


def test_a_token_stream_that_leaks_a_tag_falls_back_to_text(monkeypatch):
    # if the token rewrite ever let a tag through, the audit falls back to escaped text
    monkeypatch.setattr(rt, "_clean_tokens", lambda tokens: tokens)
    monkeypatch.setattr(rt._MD.options, "html", True, raising=False)
    out = str(rt.render_markdown("[x](https://a.example) <b>z</b>"))
    assert "<a" not in out
    assert "&lt;" in out or "<b" not in out


def test_empty_and_garbage_input_do_not_raise():
    for src in ("", None, "\x00\x00", "|" * 5000, "- " * 3000, "[" * 4000):
        assert isinstance(str(rt.render_markdown(src)), str)


# ---- legacy conversion ---------------------------------------------------
def test_flat_json_object_becomes_a_two_column_table():
    md = rt.code_to_markdown('{"total_capacity_gw": 5, "ok": true, "x": null}', "json")
    assert "Field" not in md and "Value" not in md  # no generic header row
    assert "| Total capacity (GW) | 5 |" in md and "| Ok | yes |" in md and "| X | none |" in md
    assert "{" not in md and '"' not in md
    html = str(rt.render_markdown(md))
    assert "<table" in html and "<thead" not in html and "<th" not in html


def test_list_of_objects_becomes_a_table_with_their_keys():
    md = rt.code_to_markdown('[{"a": 1, "b": 2}, {"a": 3, "b": 4}]', "json")
    assert md.splitlines()[0] == "| A | B |" and "| 3 | 4 |" in md


def test_nested_json_becomes_a_nested_list():
    md = rt.code_to_markdown('{"a": {"b": [1, 2], "c": {"d": 1}}}', "json")
    assert "- **A**" in md and "  - **B**" in md and "    - 1" in md


def test_relaxed_and_truncated_json_still_convert():
    assert "Adaptive thinking" in rt.code_to_markdown("k: { adaptive_thinking: true, n: 'x' }", "json")
    md = rt.code_to_markdown('{"a": 1, "b": "two", "c": [', "json")
    assert "| A | 1 |" in md and "| B | two |" in md and "[" not in md.replace("\\[", "")


def test_json_detected_without_a_lang():
    assert rt.code_to_markdown('{"a": 1}', "text").endswith("| A | 1 |")


def test_yaml_is_parsed_safely():
    md = rt.code_to_markdown("name: x\nitems:\n  - a\n  - b", "yaml")
    assert "- **Name**: x" in md and "  - a" in md


def test_python_tags_in_yaml_are_never_executed(tmp_path):
    """!!python/object/apply would call builtins.open(path, "w") under an unsafe loader."""
    marker = tmp_path / "executed"
    payload = f"x: !!python/object/apply:builtins.open [{str(marker)!r}, w]"
    assert rt._safe_yaml(payload) is None
    for lang in ("yaml", "json", "text"):
        out = rt.code_to_markdown(payload, lang)
        assert "```" not in out
    rt.render_markdown(f"```yaml\n{payload}\n```")
    rt.coerce_body(f"```yaml\n{payload}\n```")
    assert not marker.exists()


def test_markdown_lang_stays_markdown_and_frontmatter_becomes_a_table():
    md = rt.code_to_markdown("---\ntype: concept\ntitle: T\n---\n\n# H\n\ntext", "markdown")
    assert "| Type | concept |" in md and "# H" in md


def test_text_lang_keeps_structure_without_layout():
    md = rt.code_to_markdown("Total: 1\n\nItems:\n  • a\n  • b\n\n1. x\n2. y", "text")
    assert "- a" in md and "- b" in md and "1. x" in md and "•" not in md


def test_ascii_box_key_values_become_a_table():
    md = rt.code_to_markdown("┌────┐\n│ MODEL: A │\n│ TIER: B │\n└────┘", "text")
    assert "| MODEL | A |" in md and "┌" not in md


@pytest.mark.parametrize("lang", ["python", "bash", "shell", "typescript", "c"])
def test_other_languages_become_a_short_bullet_list(lang):
    md = rt.code_to_markdown("# set up\nimport os\nprint('x')\n", lang)
    assert md.startswith("- ") and "```" not in md
    assert len(md.splitlines()) <= rt.LIST_MAX_ITEMS


def test_migrate_slide_is_deterministic_pure_and_keeps_extras():
    old = {"type": "code", "headline": "H", "lang": "json", "code": '{"a": 1}', "caption": "c", "image": "/x.png"}
    snap = dict(old)
    new = rt.migrate_slide(old)
    assert old == snap
    assert new == {"type": "notes", "headline": "H", "caption": "c", "image": "/x.png", "body": rt.code_to_markdown('{"a": 1}', "json")}
    assert rt.migrate_slide(old) == new
    point = {"type": "point", "headline": "p", "bullets": ["a"]}
    assert rt.migrate_slide(point) is point


def test_prepare_for_render_hands_the_template_safe_html_for_a_legacy_slide():
    out = rt.prepare_for_render([{"type": "code", "headline": "H", "lang": "json", "code": '{"a": "<script>x</script>"}'}])
    assert out[0]["type"] == "notes" and "<table" in str(out[0]["body_html"]) and "<script" not in str(out[0]["body_html"])


def test_every_legacy_example_in_the_wild_converts_without_code_markup():
    samples = [("{\n  \"a\": [1, 2]\n}", "json"), ("a: 1", "yaml"), ("x = 1", "python"), ("plain text", "text"), ("", "json")]
    for code, lang in samples:
        html = str(rt.render_markdown(rt.code_to_markdown(code, lang)))
        assert "<pre" not in html and "<code" not in html and tags(html) <= rt.ALLOWED_TAGS


# ---- composer-side validation -------------------------------------------
def _notes(body):
    return normalise_slides([{"type": "notes", "headline": "H", "body": body}])


def test_composer_converts_fenced_json_in_a_body():
    out = _notes('Intro\n\n```json\n{"a": 1}\n```')
    assert out and "```" not in out[0]["body"] and "| A | 1 |" in out[0]["body"]


def test_composer_converts_a_whole_json_body():
    assert "| A | 1 |" in _notes('{"a": 1, "b": 2}')[0]["body"]


@pytest.mark.parametrize("body", [
    "def f(x):\n    return x\n\nimport os",
    'int main() {\n  return 0;\n}',
    '{"a": [1, 2',
])
def test_composer_rejects_a_body_that_is_still_code(body):
    assert _notes(body) == []


def test_composer_keeps_prose_lists_and_tables():
    body = "- one\n- two\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
    assert _notes(body)[0]["body"] == body


def test_composer_bounds_body_length_at_a_line_boundary():
    body = "\n".join(f"- item number {i} with a few words" for i in range(60))
    out = _notes(body)[0]["body"]
    assert len(out) <= rt.BODY_HARD_CHARS and out.splitlines()[-1].startswith("- item number")


def test_a_notes_slide_with_no_body_is_dropped():
    assert _notes("") == [] and _notes("   ") == []


def test_notes_template_inserts_only_the_sanitised_html():
    theme = {"bg": "#000", "fg": "#fff", "muted": "#999", "accent": "#08f", "accent2": "#80f",
             "card": "#111", "rule": "#222", "handle": "@h", "font": "Arial", "style": "signal",
             "kickers": {"notes": "THE DETAIL"}}
    slides = [{"type": "notes", "headline": "<b>H</b>", "body": "<script>alert(1)</script> **ok**"}]
    html = build_html(slides, TEMPLATES, theme)
    assert "<script>alert" not in html and "&lt;script&gt;" in html and "<strong>ok</strong>" in html
    assert "&lt;b&gt;H" in html
    assert "THE DETAIL" in html


# ---- polish: labels, values, object lists, header-less tables --------------
@pytest.mark.parametrize("key,label", [
    ("total_capacity_gw", "Total capacity (GW)"),
    ("maxTokens", "Max tokens"),
    ("max-tokens", "Max tokens"),
    ("rate_pct", "Rate (%)"),
    ("latency_ms", "Latency (ms)"),
    ("price_usd", "Price (USD)"),
    ("gw", "Gw"),  # a lone unit word is not a unit suffix
    ("API_key", "API key"),
    ("revenue_per_gw", "Revenue per GW"),
    ("sonnet-5.5-changes", "Sonnet 5.5 changes"),
])
def test_keys_are_humanised(key, label):
    assert rt._label(key) == label


def test_identifier_values_become_words_but_other_values_are_kept():
    md = rt.code_to_markdown('{"mode": "panel_simulation", "id": "claude-3-5", "n": 1.25, "s": "a_b c"}', "json")
    assert "| Mode | panel simulation |" in md
    assert "claude-3-5" in md and "1.25" in md and "a\\_b c" in md  # not an identifier: kept (escaped)


def test_ordinal_plus_text_objects_become_a_numbered_list_in_order():
    md = rt.code_to_markdown('[{"step": 2, "action": "Ask"}, {"step": 1, "action": "Analyze"}]', "json")
    assert md == "1. Analyze\n2. Ask"
    assert rt.code_to_markdown('[{"rank": 1, "name": "A"}, {"rank": 2, "name": "B"}]', "json") == "1. A\n2. B"
    nested = rt.code_to_markdown('{"process": [{"step": 1, "action": "Analyze"}]}', "json")
    assert "  1. Analyze" in nested and "step" not in nested.lower()


def test_ordinal_rule_needs_exactly_an_ordinal_and_a_text_key():
    md = rt.code_to_markdown('[{"step": 1, "action": "A", "who": "x"}, {"step": 2, "action": "B", "who": "y"}]', "json")
    assert md.startswith("| Step | Action | Who |")


def test_two_text_keys_become_bold_dash_bullets():
    md = rt.code_to_markdown('[{"name": "cuDSS", "use": "device simulation"}, {"name": "cuEST", "use": "em"}]', "json")
    assert md == "- **cuDSS** \u2014 device simulation\n- **cuEST** \u2014 em"


def test_three_or_more_keys_become_a_table_within_limits():
    rows = ",".join('{"a": %d, "b": 2, "c": 3, "d": 4}' % i for i in range(12))
    md = rt.code_to_markdown(f"[{rows}]", "json")
    lines = md.splitlines()
    assert lines[0] == "| A | B | C |"  # at most TABLE_MAX_COLS columns
    assert len(lines) - 2 <= rt.TABLE_HARD_ROWS


def test_a_header_row_with_text_is_kept():
    html = str(rt.render_markdown("| H1 | H2 |\n| --- | --- |\n| x | y |"))
    assert "<thead" in html and "H1" in html
    assert "<thead" not in str(rt.render_markdown("|  |  |\n| --- | --- |\n| x | y |"))


def test_plain_key_value_text_is_also_headerless():
    md = rt.code_to_markdown("MODEL: A\nTIER: B", "text")
    assert "Field" not in md and "| MODEL | A |" in md


# ---- resource bounds ----------------------------------------------------------
def _alias_bomb(levels: int = 9) -> str:
    lines = ["defs:", "  - &a0 [" + ", ".join(["lol"] * 9) + "]"]
    for n in range(1, levels):
        lines.append(f"  - &a{n} [" + ", ".join([f"*a{n - 1}"] * 9) + "]")
    lines.append(f"boom: *a{levels - 1}")
    return "\n".join(lines)


def _in_child(fn, args, seconds):
    """Run ``fn(*args)`` in a forked child and give up after ``seconds``. A runaway
    regex holds the GIL, so a thread's join(timeout) would never fire: only a
    separate process fails fast. Returns (elapsed, result)."""
    import multiprocessing

    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe(duplex=False)

    def work():
        start = time.perf_counter()
        out = fn(*args)
        child.send((time.perf_counter() - start, out))

    proc = ctx.Process(target=work, daemon=True)
    proc.start()
    try:
        if not parent.poll(seconds):
            pytest.fail(f"{getattr(fn, '__name__', fn)} still running after {seconds}s (runaway)")
        return parent.recv()
    finally:
        proc.kill()
        proc.join()


def _bounded(fn, *args, seconds=0.5, size=20_000):
    elapsed, out = _in_child(fn, args, seconds + 0.5)  # +0.5: process start-up
    assert elapsed < seconds, (fn.__name__, elapsed)
    assert len(str(out)) < size
    return out


def test_yaml_aliases_and_anchors_are_refused():
    assert rt._safe_yaml("a: &x 1\nb: *x") is None
    assert rt._safe_yaml("a: &x 1") is None
    assert rt._safe_yaml("a: 1\nb: [2, 3]") == {"a": 1, "b": [2, 3]}


def test_yaml_input_is_cut_inside_safe_yaml_too():
    assert len(rt._safe_yaml("- 1\n" * 5000)) == rt.INPUT_MAX_CHARS // 4


def test_alias_bomb_is_cheap_through_every_entry_point():
    bomb = _alias_bomb()
    fenced = f"intro\n\n```yaml\n{bomb}\n```"
    _bounded(rt.render_markdown, fenced)
    _bounded(rt.coerce_body, fenced)
    _bounded(rt.code_to_markdown, bomb, "yaml")
    _bounded(rt.migrate_slide, {"type": "code", "headline": "h", "lang": "yaml", "code": bomb})
    _bounded(normalise_slides, [{"type": "notes", "headline": "h", "body": fenced}])


def test_compact_is_bounded_whatever_the_value_expands_to():
    node: list = ["lol"] * 9
    for _ in range(9):
        node = [node] * 9  # shared references, 9**10 leaves if walked
    out = _bounded(rt._compact, node)
    assert len(out) <= rt.ITEM_MAX_CHARS + 1
    deep: dict = {"k": 1}
    for _ in range(500):
        deep = {"k": deep}
    assert len(_bounded(rt._compact, deep)) <= rt.ITEM_MAX_CHARS + 1


@pytest.mark.parametrize("depth", [50, 400, 10_000])
def test_deeply_nested_json_is_cheap_and_never_raises(depth):
    for text in ("[" * depth + "]" * depth, '{"a":' * depth + "1" + "}" * depth):
        _bounded(rt.code_to_markdown, text, "json")
        _bounded(rt.coerce_body, text)
        _bounded(rt.render_markdown, f"```json\n{text}\n```")


def test_input_is_cut_before_parsing():
    rendered = _bounded(rt.render_markdown, "word " * 100_000)
    assert len(str(rendered)) < rt.INPUT_MAX_CHARS * 1.2  # cut by convert_fences before parsing
    assert str(_bounded(rt.render_markdown, "- x\n" * 200_000)).count("<li>") <= rt.INPUT_MAX_CHARS // 4
    assert len(_bounded(rt.code_to_markdown, "hello world " * 50_000, "text")) < rt.INPUT_MAX_CHARS * 2
    assert len(rt.coerce_body("- x\n" * 200_000)) <= rt.BODY_HARD_CHARS
    json_big = "[" + ",".join(["1"] * 100_000) + "]"
    assert len(_bounded(rt.code_to_markdown, json_big, "json")) <= rt.BODY_HARD_CHARS


def test_converted_structures_are_bounded_in_size():
    wide = {f"key{i}": {"inner": [i] * 50} for i in range(500)}
    assert len(rt.structure_to_markdown(wide)) <= rt.BODY_HARD_CHARS


# ---- the audit itself --------------------------------------------------------
@pytest.mark.parametrize("html", [
    "<p>ok</p><script>x</script>", "<img src=x>", "<a href='x'>t</a>", "<pre>c</pre>",
    '<p onclick="x">t</p>', '<p class="evil">t</p>', '<td style="x">t</td>', "<!-- c --><p>t</p>",
    "<hr>", "<input>", "<pre>unclosed", "<?php x ?>", "<!DOCTYPE html>", "<p>t</p></script>",
])
def test_audit_rejects_anything_outside_the_allowlist(html):
    assert rt._audited(html) is False


def test_audit_accepts_the_renderers_own_output():
    assert rt._audited('<p class="mdh">h</p><ul><li><strong>a</strong></li></ul><table><tbody><tr><td>x</td></tr></tbody></table><br>')


# ---- regexes: no pattern may be slow on adversarial input -------------------
PATTERNS = {
    name: value for name, value in vars(rt).items()
    if isinstance(value, re.Pattern)
}
FUZZ_LEN = 4000
FUZZ_BUDGET_S = 0.05


def _adversarial(pattern: re.Pattern) -> dict[str, str]:
    n = FUZZ_LEN
    base = {
        "spaces": " " * n, "word": "a" * n, "quotes": '"' * n, "colons": ":" * n, "commas": "," * n,
        "dots": "." * n, "words": "a " * (n // 2), "kv_then_spaces": '"key: v' + " " * (n - 9) + 'x',
        "kv_unquoted": "key: v" + " " * (n - 8) + "x", "quoted_words": '"a" ' * (n // 4),
        "colon_words": "a: " * (n // 3), "eq_words": "a = " * (n // 4), "dashes": "-" * n,
        "newlines": "\n" * n, "nl_spaces": "\n " * (n // 2), "backslash": "\\ " * (n // 2),
        "calls": "a(" * (n // 2), "methods": "a.b(" * (n // 4), "paren_tail": "x = f(" + ") " * (n // 2),
        "semis": "x = 1; " * (n // 7), "digits": "1" * n, "snake": "a_" * (n // 2),
        "tab_mix": " \t" * (n // 2), "braces": "{ " * (n // 2),
    }
    for ch in sorted({c for c in pattern.pattern if not c.isalnum() and c not in "\\()[]{}?*+|^$"}):
        base[f"lit{ord(ch)}"] = ch * n
        base[f"lit{ord(ch)}_sp"] = (ch + " ") * (n // 2)
        base[f"lit{ord(ch)}_a"] = ("a" + ch) * (n // 2)
    return base


def _fuzz_one(name: str) -> tuple[str, str, float]:
    """The slowest (label, op, seconds) of ``PATTERNS[name]`` over every adversarial string,
    each also with a non-matching tail (a run that ALMOST matches is what backtracks)."""
    pattern = PATTERNS[name]
    worst = ("", "", 0.0)
    for label, text in _adversarial(pattern).items():
        for tail in ("", "!", "\u00e9z"):
            for op in ("search", "match", "sub", "findall", "split"):
                start = time.perf_counter()
                if op == "sub":
                    pattern.sub("", text + tail)
                else:
                    getattr(pattern, op)(text + tail)
                took = time.perf_counter() - start
                if took > worst[2]:
                    worst = (f"{label}+{tail!r}", op, took)
    return worst


def test_every_module_level_pattern_is_fuzzed():
    assert len(PATTERNS) >= 20
    for required in ("_KV_LINE", "_JSON_PAIR", "_CODE_CALL", "_CODE_LINE_END", "_FRONTMATTER", "_FENCE_OPEN",
                     "_TRAILING_BACKSLASH", "_NOISE_LINE", "_SNAKE_VALUE"):
        assert required in PATTERNS


def test_no_regex_is_built_or_used_inline():
    """A pattern written inline in a function is invisible to the fuzz above."""
    import inspect

    source = inspect.getsource(rt)
    call = re.compile(r"\bre\.(?:sub|match|search|split|findall|fullmatch|compile)\(")
    inline = [ln.strip() for ln in source.splitlines() if ln.startswith(" ") and call.search(ln)]
    assert len(inline) == 1 and inline[0].startswith("close = re.compile(rf")  # the per-fence closer, fuzzed below


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_no_pattern_is_slow_on_4000_char_adversarial_input(name):
    _, (label, op, took) = _in_child(_fuzz_one, (name,), 8)
    assert took < FUZZ_BUDGET_S, (name, label, op, took)


def test_the_old_kv_line_attack_is_cheap_everywhere():
    for n in (2000, 4000, 20_000):
        line = "key: v" + " " * n + "x"
        _bounded(rt.render_markdown, f"```text\n{line}\n```")
        _bounded(rt.coerce_body, f"```text\n{line}\n```")
        _bounded(rt.code_to_markdown, line + "\nk2: v2", "text")
        _bounded(rt.code_to_markdown, line, "json")
        _bounded(rt.render_markdown, line)


def test_lines_are_cut_before_any_regex_sees_them():
    assert all(len(ln) == rt.LINE_MAX_CHARS for ln in rt._lines("x" * 5000 + "\n" + "y" * 5000))
    assert rt._kv("k: " + "v" * 5000)[1] == "v" * (rt.LINE_MAX_CHARS - 3)


def test_fence_markers_on_adversarial_lines_are_cheap():
    for fence in ("`" * 3, "`" * 500, "~" * 500):
        text = f"{fence}text\n" + ("a: " + " " * 900 + "\n") * 4 + fence
        _bounded(rt.convert_fences, text)
        _bounded(rt.render_markdown, text + "\n" + fence + " " * 3000)


def test_key_value_parsing_still_reads_real_lines():
    assert rt._kv('"key one": value, ') == ("key one", "value")
    assert rt._kv("name = x") == ("name", "x")
    assert rt._kv("a.b/c: 1") == ("a.b/c", "1")
    assert rt._kv("just words") is None and rt._kv("key:") is None
