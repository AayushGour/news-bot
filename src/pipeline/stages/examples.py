"""Worked deck examples, shown to the composer instead of described to it.

Every rule stated in prose has been partially ignored: character limits were
exceeded by 2x, headlines kept swallowing the body, theme matching runs about
half right. An example is not a rule the model can reason around — it is a
shape to imitate.

These are deliberately short. A full ten-slide deck per intent would cost more
context on every compose call than the guidance is worth, so each shows the
SHAPE and the FIELDS, with copy just long enough to demonstrate register.
"""

from __future__ import annotations

import json

#: A news deck: hook opens, substance in the middle, takeaway then sources.
#: Chosen to show a chart and a quote, because those are the types the model
#: reaches for least often.
NEWS_EXAMPLE = {
    "slides": [
        {"type": "hook",
         "headline": "OpenAI cuts off Cursor users",
         "sub": "A contract termination ends model access for the AI editor in 90 days."},
        {"type": "kpi",
         "headline": "The scale of it",
         "tiles": [
             {"value": "5%", "label": "of Cursor traffic affected"},
             {"value": "90 days", "label": "until the cutoff"},
         ]},
        {"type": "chart",
         "headline": "Annual recurring revenue",
         "unit": "USD billions",
         "series": [
             {"label": "Anthropic", "value": 65, "display": "$65B"},
             {"label": "OpenAI", "value": 40, "display": "$40B"},
         ]},
        {"type": "quote",
         "headline": "On neutrality",
         "quote": "We built on OpenAI as neutral infrastructure.",
         "attribution": "Michael Truell, Cursor"},
        {"type": "takeaway",
         "headline": "Why it matters",
         "sub": "Platform neutrality is not guaranteed when your supplier becomes your competitor."},
        {"type": "sources",
         "headline": "Sources",
         "urls": ["https://cloud.example/openai-cursor"]},
    ],
    "caption": "OpenAI is ending Cursor's access to its models within three months, "
               "citing a contract termination. Cursor says the models carry about 5% "
               "of its traffic.\n\nSource: @aipost",
    "hashtags": ["openai", "cursor", "anysphere", "aitooling", "technews"],
    "theme": "blockprint",
}

#: An enumeration deck: hook, one repo per slide, then every link in one place.
LIST_EXAMPLE = {
    "slides": [
        {"type": "hook",
         "headline": "8 repos for interview prep",
         "sub": "Ranked by stars. Every link is on the last slide."},
        {"type": "repo",
         "headline": "awesome-go-interview",
         "owner": "defer-panic", "name": "awesome-go-interview",
         "url": "https://github.com/defer-panic/awesome-go-interview",
         "stars": 169, "language": "Shell",
         "sub": "Go interview questions, patterns and reading, kept current."},
        {"type": "repo",
         "headline": "Interview-Preparation",
         "owner": "ElizaLo", "name": "Interview-Preparation",
         "url": "https://github.com/ElizaLo/Interview-Preparation",
         "stars": 32, "language": "C++",
         "sub": "Worked solutions and notes from HackerRank and LeetCode practice."},
        {"type": "links",
         "headline": "All the links",
         "sub": "Screenshot this slide.",
         "links": [
             "https://github.com/defer-panic/awesome-go-interview",
             "https://github.com/ElizaLo/Interview-Preparation",
         ]},
        {"type": "follow",
         "headline": "More like this",
         "sub": "Tools and repos worth your time, weekly."},
    ],
    "caption": "Eight repositories worth bookmarking for technical interview prep, "
               "ranked by stars. All links on the final slide.",
    "hashtags": ["github", "interviewprep", "opensource", "developers", "coding"],
    "theme": "carbon",
}

#: An explainer deck: a technical subject taught rather than reported. Shows
#: code and flow, the two types that exist precisely for this case and that the
#: model most often skips in favour of more bullets.
EXPLAINER_EXAMPLE = {
    "slides": [
        {"type": "hook",
         "headline": "OKF: plain Markdown for AI agents",
         "sub": "Google's format for exchanging organisational knowledge without a platform."},
        {"type": "code",
         "headline": "What a concept file looks like",
         "lang": "markdown",
         "code": "---\ntype: concept\ntitle: Onboarding\n---\n\n# Onboarding\n\nLinks to [payroll](./payroll.md).",
         "caption": "YAML frontmatter, then ordinary Markdown."},
        {"type": "flow",
         "headline": "How an agent reads it",
         "steps": [
             {"label": "Discover", "detail": "Finds .okf/ at the repository root"},
             {"label": "Parse", "detail": "Reads the manifest for schema and index"},
             {"label": "Resolve", "detail": "Follows Markdown links between concepts"},
         ]},
        {"type": "facts",
         "headline": "Reserved filenames",
         "rows": [["index.md", "Directory overview"], ["log.md", "Change history"]]},
        {"type": "takeaway",
         "headline": "Why it matters",
         "sub": "Knowledge outlives the tool that produced it when the format is just files."},
        {"type": "sources",
         "headline": "Sources",
         "urls": ["https://okf.example/spec"]},
    ],
    "caption": "Google's Open Knowledge Format is plain Markdown with YAML frontmatter, "
               "letting AI agents share organisational knowledge with no proprietary "
               "tooling.\n\nSource: @aipost",
    "hashtags": ["okf", "google", "aiagents", "markdown", "opensource"],
    "theme": "signal",
}

EXAMPLES = {
    "news": NEWS_EXAMPLE,
    "list": LIST_EXAMPLE,
    "explainer": EXPLAINER_EXAMPLE,
}


def pick(intent: str | None, request: str) -> tuple[str, dict]:
    """Choose the example closest to this request.

    An enumeration is already labelled by the classifier. Everything else is
    news unless the request reads like a teaching ask, where the explainer deck
    is a far better shape to imitate than a breaking-news one.
    """
    if intent == "list":
        return "list", LIST_EXAMPLE

    lowered = (request or "").lower()
    teaching = ("explain", "how does", "how do", "what is", "what are",
                "understand", "guide", "walk me through", "how it works")
    if any(phrase in lowered for phrase in teaching):
        return "explainer", EXPLAINER_EXAMPLE
    return "news", NEWS_EXAMPLE


def block(intent: str | None, request: str) -> str:
    """The example, rendered for the prompt."""
    name, example = pick(intent, request)
    return (
        f"EXAMPLE OF A GOOD {name.upper()} DECK — imitate this shape and register,\n"
        f"not its subject. Your slides must come from the brief below it.\n\n"
        f"{json.dumps(example, indent=1)}"
    )
