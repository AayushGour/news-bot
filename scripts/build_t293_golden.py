"""Build tests/revision/fixtures/t293_golden.json from the T293 planner spike.

Provenance (house rule 4): the fixture is NOT hand-written. It is extracted from the
spike's scratch directory - the decks it ran on (``spike.db``, a read-only ``.backup`` of
data/app.db), its 15 hand-labelled notes (``common.notes_for``) and the model's recorded
plans (``localC.jsonl`` = design C x 75 note-deck pairs, ``localD.jsonl`` = design D x 30).
Decks are reduced to what grounding reads (type, headline, has-image). Run:

    ./.venv/bin/python scripts/build_t293_golden.py <spike_dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tests/revision/fixtures/t293_golden.json"
DESIGNS = ("localC.jsonl", "localD.jsonl")
SOURCE_DATE = "2026-11-07"


def main(spike: Path) -> None:
    sys.path.insert(0, str(spike))
    import common  # the spike's own harness

    decks, notes = {}, {}
    for d in common.DECKS:
        slides, caption, theme = common.load_deck(d)
        decks[str(d)] = [
            {"type": s["type"], "headline": s.get("headline", ""), "image": bool(s.get("image"))}
            for s in slides
        ]
        notes[str(d)] = {
            n["id"]: {"kind": n["kind"], "note": n["note"],
                      "exp": None if n["exp"] is None else sorted(map(list, n["exp"]), key=str),
                      "glob_ok": n["glob_ok"]}
            for n in common.notes_for(slides)
        }
    plans = []
    for name in DESIGNS:
        for line in (spike / name).read_text().splitlines():
            r = json.loads(line)
            plans.append({"design": r["design"], "deck": str(r["deck"]), "note_id": r["note_id"],
                          "plan": r["plan"], "err": r["err"]})
    OUT.write_text(json.dumps({
        "_provenance": f"T293 spike {SOURCE_DATE}: decks {common.DECKS}, designs {DESIGNS}",
        "themes": list(common.THEMES), "decks": decks, "notes": notes, "plans": plans,
    }, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(plans)} plans)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
