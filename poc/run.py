"""
THROWAWAY SPIKE — not production code.

Answers one question: can local Ollama models carry the risky middle of the
Telegram -> Instagram pipeline (schema-valid slide JSON + synthesis worth
posting), and how slow is it on this machine?

Covers: research fan-out -> synthesis -> slide JSON -> HTML -> PNG.
Deliberately omits: Telegram, Instagram, approval flow, database.

Usage:
    ./.venv/bin/python run.py              # full pipeline once
    ./.venv/bin/python run.py --schema 5   # compose stage x5, schema validity only
"""

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import trafilatura
from jinja2 import Environment, FileSystemLoader

HERE = Path(__file__).parent
OUT = HERE / "out"
OLLAMA = "http://localhost:11434/api/chat"
SEARX = "http://localhost:8080/search"

CHEAP = "qwen3:4b-instruct"
GOOD = "qwen3.5:9b"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

INPUT_TEXT = """Cursor's leadership has responded to OpenAI's announcement that it will block Cursor users from accessing OpenAI models within the next three months.

According to Cursor, OpenAI models account for approximately five percent of Cursor's user traffic. Cursor representatives stated that they are currently in discussions with OpenAI to address the situation.

The company further noted that Cursor was among the early adopters of OpenAI's technologies and had relied on OpenAI's platform as neutral infrastructure for its business. Cursor claims that this decision by OpenAI challenges that expectation of neutrality.

📰 @aipost"""

# ---------------------------------------------------------------- schemas

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 4,
            "maxItems": 5,
        }
    },
    "required": ["queries"],
}

NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "detail": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["claim", "detail", "confidence"],
}

SLIDES_SCHEMA = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "minItems": 3,
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["hook", "point", "compare", "takeaway", "sources"],
                    },
                    "headline": {"type": "string"},
                    "sub": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "stat": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string"},
                            "label": {"type": "string"},
                        },
                        "required": ["value", "label"],
                    },
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["type", "headline"],
            },
        },
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["slides", "caption", "hashtags"],
}

# ---------------------------------------------------------------- timing

TIMINGS = []


class stage:
    def __init__(self, name):
        self.name = name

    async def __aenter__(self):
        self.t0 = time.perf_counter()
        print(f"  ▶ {self.name} ...", flush=True)
        return self

    async def __aexit__(self, *exc):
        dt = time.perf_counter() - self.t0
        TIMINGS.append((self.name, dt))
        print(f"  ✔ {self.name}: {dt:.1f}s", flush=True)
        return False


# ---------------------------------------------------------------- ollama

async def llm(client, model, system, user, schema=None, num_ctx=8192, temp=0.3):
    """Call Ollama chat. Retries without `think` if the model rejects it."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": temp, "num_ctx": num_ctx},
    }
    if schema:
        payload["format"] = schema

    for attempt in (1, 2):
        r = await client.post(OLLAMA, json=payload, timeout=600)
        if r.status_code == 400 and "think" in r.text.lower() and attempt == 1:
            payload.pop("think", None)
            continue
        r.raise_for_status()
        return r.json()["message"]["content"]
    raise RuntimeError("unreachable")


async def llm_json(client, model, system, user, schema, **kw):
    raw = await llm(client, model, system, user, schema=schema, **kw)
    # strip any stray reasoning fence a model might still emit
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(raw)


# ---------------------------------------------------------------- research

async def searx(client, q, n=6):
    try:
        r = await client.get(
            SEARX, params={"q": q, "format": "json"}, timeout=30
        )
        r.raise_for_status()
        return r.json().get("results", [])[:n]
    except Exception as e:
        print(f"    ! search failed {q[:40]!r}: {e}", flush=True)
        return []


async def fetch_text(client, url):
    try:
        r = await client.get(
            url, timeout=15, follow_redirects=True, headers={"User-Agent": UA}
        )
        if r.status_code != 200 or len(r.content) > 2_000_000:
            return None
        txt = await asyncio.to_thread(trafilatura.extract, r.text)
        return txt[:3500] if txt else None
    except Exception:
        return None


RESEARCH_SYS = (
    "You are a research assistant for a tech-news publisher. "
    "Read the provided web excerpts and answer the research question with a "
    "single factual finding. Use ONLY what the excerpts support. "
    "If the excerpts do not answer the question, say so and set confidence low. "
    "Never invent numbers, dates, or quotes."
)


async def research_one(client, idx, question, original):
    results = await searx(client, question)
    # keep at most one result per domain
    seen, picked = set(), []
    for r in results:
        d = urlparse(r.get("url", "")).netloc
        if d and d not in seen:
            seen.add(d)
            picked.append(r)
        if len(picked) == 3:
            break

    texts = await asyncio.gather(
        *[fetch_text(client, r["url"]) for r in picked]
    )

    corpus, sources = [], []
    for r, t in zip(picked, texts):
        body = t or r.get("content") or ""
        if not body.strip():
            continue
        corpus.append(f"[SOURCE: {r['url']}]\n{body}")
        sources.append(r["url"])

    if not corpus:
        print(f"    ! researcher {idx}: no usable sources", flush=True)
        return None

    user = (
        f"ORIGINAL NEWS ITEM:\n{original}\n\n"
        f"RESEARCH QUESTION: {question}\n\n"
        f"WEB EXCERPTS:\n\n" + "\n\n---\n\n".join(corpus)
    )
    try:
        note = await llm_json(
            client, CHEAP, RESEARCH_SYS, user, NOTE_SCHEMA, num_ctx=16384
        )
    except Exception as e:
        print(f"    ! researcher {idx} llm failed: {e}", flush=True)
        return None

    note["question"] = question
    note["sources"] = sources
    print(f"    · researcher {idx}: {note['claim'][:70]}", flush=True)
    return note


# ---------------------------------------------------------------- stages

PLAN_SYS = (
    "You plan web research for a tech-news Instagram account. "
    "Given a news item, produce 4-5 distinct search queries that would each "
    "uncover a DIFFERENT facet: what exactly happened, the business/technical "
    "background, how the parties are related, prior comparable events, and "
    "criticism or consequences. Queries must be plain search strings, "
    "not questions to a chatbot."
)

SYNTH_SYS = (
    "You are a fact-focused editor. Combine the research notes into a tight "
    "factual brief for an Instagram carousel about a tech-news story.\n"
    "Rules:\n"
    "- Use only claims the notes support. Drop anything unsupported.\n"
    "- If two notes contradict, say so explicitly rather than picking one.\n"
    "- Keep every fact attributed to its source URL inline like (source: url).\n"
    "- 250 words maximum. No preamble, no headings, just the brief."
)

COMPOSE_SYS = """You write Instagram carousel slides for a tech-news account.

Turn the brief into slides. Choose how many slides (3-10) based on how much
the research actually supports — do not pad.

Slide types and their HARD character limits (the template physically cannot
fit more, text will be cut off):
- "hook":     headline <= 60 chars, sub <= 90 chars.       Use exactly one, first.
- "point":    headline <= 45 chars, up to 4 bullets, each bullet <= 95 chars.
              Optional "stat": {value <= 12 chars, label <= 45 chars}.
- "compare":  headline <= 45 chars, up to 4 "rows", each row is exactly
              [left <= 30 chars, right <= 34 chars].
- "takeaway": headline <= 55 chars, sub <= 110 chars.      Use exactly one, near last.
- "sources":  headline <= 45 chars, up to 4 "urls".        Use exactly one, last.

Style: declarative, specific, no hype, no emoji inside slides. Numbers beat
adjectives. Never invent a fact that is not in the brief.

Also write "caption" (<= 500 chars, may use emoji, must credit the source
channel) and 8-12 lowercase "hashtags" without the # symbol."""


async def plan(client, text):
    return await llm_json(
        client, CHEAP, PLAN_SYS, f"NEWS ITEM:\n{text}", PLAN_SCHEMA
    )


async def synthesize(client, text, notes):
    blob = "\n\n".join(
        f"Q: {n['question']}\nFINDING: {n['claim']}\nDETAIL: {n['detail']}\n"
        f"CONFIDENCE: {n['confidence']}\nSOURCES: {', '.join(n['sources'])}"
        for n in notes
    )
    user = f"ORIGINAL NEWS ITEM:\n{text}\n\nRESEARCH NOTES:\n\n{blob}"
    return await llm(client, GOOD, SYNTH_SYS, user, num_ctx=16384)


async def compose(client, brief, sources):
    user = (
        f"BRIEF:\n{brief}\n\n"
        f"AVAILABLE SOURCE URLS:\n" + "\n".join(sources[:8]) + "\n\n"
        "Source channel to credit in the caption: @aipost"
    )
    return await llm_json(
        client, GOOD, COMPOSE_SYS, user, SLIDES_SCHEMA, num_ctx=16384, temp=0.6
    )


# ---------------------------------------------------------------- render

async def render(slides, tag="run"):
    from playwright.async_api import async_playwright

    env = Environment(loader=FileSystemLoader(HERE / "templates"))
    html = env.get_template("slides.html.j2").render(slides=slides)
    html_path = OUT / f"{tag}.html"
    html_path.write_text(html, encoding="utf-8")

    paths, overflows = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1080, "height": 1350})
        await page.goto(html_path.as_uri())
        await page.wait_for_timeout(300)

        for i in range(len(slides)):
            sel = f"#slide-{i}"
            over = await page.eval_on_selector(
                sel + " .body",
                "el => el.scrollHeight > el.clientHeight + 2",
            )
            if over:
                overflows.append(i)
            out = OUT / f"{tag}_slide_{i+1:02d}.png"
            await page.locator(sel).screenshot(path=str(out))
            paths.append(out)
        await browser.close()
    return paths, overflows


# ---------------------------------------------------------------- runs

async def full_run():
    OUT.mkdir(exist_ok=True)
    t_all = time.perf_counter()
    async with httpx.AsyncClient() as client:
        print("\n=== 1. RESEARCH PLAN ===")
        async with stage("plan queries"):
            queries = (await plan(client, INPUT_TEXT))["queries"]
        for q in queries:
            print(f"    - {q}")

        print("\n=== 2. RESEARCH FAN-OUT ===")
        async with stage(f"{len(queries)} researchers"):
            notes = await asyncio.gather(
                *[
                    research_one(client, i + 1, q, INPUT_TEXT)
                    for i, q in enumerate(queries)
                ]
            )
        notes = [n for n in notes if n]
        print(f"    {len(notes)}/{len(queries)} researchers returned notes")
        if len(notes) < 2:
            print("!! fewer than 2 notes — would mark item failed")
            return

        print("\n=== 3. SYNTHESIS ===")
        async with stage("synthesize brief"):
            brief = await synthesize(client, INPUT_TEXT, notes)
        print("\n" + brief + "\n")

        print("=== 4. COMPOSE SLIDES ===")
        all_sources = [u for n in notes for u in n["sources"]]
        async with stage("compose slide json"):
            doc = await compose(client, brief, all_sources)
        print(f"    {len(doc['slides'])} slides: "
              f"{[s['type'] for s in doc['slides']]}")
        print(f"    caption ({len(doc['caption'])} chars): "
              f"{doc['caption'][:160]}...")
        print(f"    hashtags: {' '.join('#'+h for h in doc['hashtags'])}")

        print("\n=== 5. RENDER ===")
        async with stage("render pngs"):
            paths, overflows = await render(doc["slides"])
        print(f"    {len(paths)} PNGs -> {OUT}")
        print(f"    overflow on slides: "
              f"{[i+1 for i in overflows] if overflows else 'none'}")

        (OUT / "run.json").write_text(
            json.dumps(
                {"brief": brief, "notes": notes, **doc}, indent=2
            ),
            encoding="utf-8",
        )

    print("\n=== TIMINGS ===")
    for name, dt in TIMINGS:
        print(f"  {name:24s} {dt:7.1f}s")
    print(f"  {'TOTAL':24s} {time.perf_counter() - t_all:7.1f}s")


SAMPLE_BRIEF = """Cursor, the AI coding editor by Anysphere, publicly responded after OpenAI
said it would cut off Cursor users' access to OpenAI models within three months.
Cursor says OpenAI models represent roughly 5% of its user traffic, and that talks
with OpenAI are ongoing. Cursor positions itself as an early adopter of OpenAI's
API that treated the platform as neutral infrastructure, and argues the cutoff
undermines that neutrality. The dispute sits against a backdrop of model providers
moving into the coding-agent product layer themselves, putting them in direct
competition with customers built on their APIs."""


async def schema_run(n):
    """Compose stage only, n times — measures schema adherence and speed."""
    OUT.mkdir(exist_ok=True)
    ok = 0
    srcs = ["https://example.com/a", "https://example.com/b"]
    async with httpx.AsyncClient() as client:
        for i in range(n):
            t0 = time.perf_counter()
            try:
                doc = await compose(client, SAMPLE_BRIEF, srcs)
                types = [s["type"] for s in doc["slides"]]
                over = [
                    s["headline"]
                    for s in doc["slides"]
                    if len(s.get("headline", "")) > 62
                ]
                ok += 1
                print(
                    f"  run {i+1}: OK  {time.perf_counter()-t0:5.1f}s  "
                    f"{len(doc['slides'])} slides {types}"
                    + (f"  LONG HEADLINES: {len(over)}" if over else "")
                )
            except Exception as e:
                print(f"  run {i+1}: FAIL {time.perf_counter()-t0:5.1f}s  "
                      f"{type(e).__name__}: {str(e)[:120]}")
    print(f"\n  schema-valid: {ok}/{n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", type=int, default=0)
    a = ap.parse_args()
    try:
        asyncio.run(schema_run(a.schema) if a.schema else full_run())
    except KeyboardInterrupt:
        sys.exit(130)
