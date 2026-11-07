"""Build tests/fixtures/t298_real_decks.json from a read-only .backup of data/app.db.

For each slide type in compose.REQUIRED_BODY_FIELDS, keep up to PER_TYPE real decks
containing that type (whole deck, so the edit is judged in context). Local file
paths in ``image`` / ``logo`` become placeholders (the tests only compare them for
identity). Re-run only to refresh the fixture; the tests never read the live DB.

    ./.venv/bin/python scripts/build_t298_fixture.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pipeline.stages.compose import REQUIRED_BODY_FIELDS  # noqa: E402

SRC_DB = ROOT / "data" / "app.db"
OUT = ROOT / "tests" / "fixtures" / "t298_real_decks.json"
PER_TYPE = 3
PATH_FIELDS = {"image": "/media/placeholder-{n}.jpg", "logo": "/media/logo-{n}.png"}


def scrub(slide: dict, n: int) -> dict:
    return {k: PATH_FIELDS[k].format(n=n) if k in PATH_FIELDS and v else v for k, v in slide.items()}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dst = sqlite3.connect(Path(tmp) / "copy.db")
        src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
        src.backup(dst)
        src.close()
        rows = dst.execute("select id, slides from items where slides != '[]' order by id desc").fetchall()
        dst.close()
    taken: dict[str, int] = {}
    cases = []
    for iid, raw in rows:
        slides = json.loads(raw)
        wanted = [
            i for i, s in enumerate(slides)
            if s.get("type") in REQUIRED_BODY_FIELDS and taken.get(s["type"], 0) < PER_TYPE
        ]
        if not wanted:
            continue
        for i in wanted:
            taken[slides[i]["type"]] = taken.get(slides[i]["type"], 0) + 1
        cases.append({"id": iid, "slides": [scrub(s, n) for n, s in enumerate(slides)], "idx": wanted})
    OUT.write_text(json.dumps(cases, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(cases)} decks, per-type counts: {dict(sorted(taken.items()))}")


if __name__ == "__main__":
    main()
