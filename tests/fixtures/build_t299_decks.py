"""Freeze real decks for tests/test_t299_blackbox.py from a READ-ONLY sqlite backup.

Usage (the live db is only ever touched by sqlite's online backup):
    sqlite3 data/app.db ".backup /some/scratch/b.db"
    python tests/fixtures/build_t299_decks.py /some/scratch/b.db

Local media paths are replaced by the MEDIA token so the fixture holds no
machine path; the test substitutes a temp media_dir.
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

OUT = Path(__file__).with_name("t299_decks.json")
MEDIA = "{MEDIA}"
PATH_RE = re.compile(r"/[^\"']*?/data/media/")
WANT = [  # (label, predicate on the list of slide types)
    ("photo", lambda t: "photo" in t),
    ("photo", lambda t: "photo" in t),
    ("hook_image", lambda t: t[0] == "hook"),
    ("repo", lambda t: t.count("repo") >= 2),
    ("repo", lambda t: t.count("repo") >= 4),
    ("point", lambda t: t.count("point") >= 3),
    ("point_facts", lambda t: "point" in t and "facts" in t and "kpi" in t),
    ("compare_quote", lambda t: "compare" in t or "quote" in t),
]


def main(db_path: str) -> None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "select id, raw_text, slides, research, caption from items where slides != '[]' order by id desc"
    ).fetchall()
    chosen, used = [], set()
    for label, pred in WANT:
        for id_, raw, slides, research, caption in rows:
            deck = json.loads(slides)
            if id_ in used or not any(s.get("image") for s in deck) or not pred([s["type"] for s in deck]):
                continue
            used.add(id_)
            blob = json.dumps(
                {"label": label, "id": id_, "raw_text": (raw or "")[:600], "slides": deck,
                 "research": json.loads(research or "[]"), "caption": caption or ""},
                ensure_ascii=False,
            )
            blob = PATH_RE.sub(MEDIA + "/", blob)
            assert "/Users/" not in blob and "/home/" not in blob
            chosen.append(json.loads(blob))
            break
    OUT.write_text(json.dumps(chosen, ensure_ascii=False, indent=1))
    print(len(chosen), "decks", [(d["label"], d["id"]) for d in chosen])


if __name__ == "__main__":
    main(sys.argv[1])
