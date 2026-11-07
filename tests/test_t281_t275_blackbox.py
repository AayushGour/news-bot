"""T281/T275 tester blackbox: odd-spacing numbers through the gate; bad triage
verdicts through the real stage and a real Worker tick."""
import time

import pytest

from pipeline.errors import BadCompletion
from pipeline.integrity import NUM_RE, kpi_tile_backed, slide_backed
from pipeline.models import Status
from pipeline.stages.triage import triage
from pipeline.worker import Worker

LONG = "Acme released the Foo-9 model with 40 percent lower latency in Berlin today."


def kpi(v):
    return {"type": "kpi", "tiles": [{"value": v, "label": "x"}]}


@pytest.mark.parametrize("value,material,expected", [
    ("40%", "Latency fell 40  % overnight", True),
    ("40%", "Latency fell 40 % overnight", True),
    ("40%", "fell 40 per\xa0cent overnight", True),
    ("40%", "fell 40 per  cent overnight", True),
    ("3.2x", "it is 3.2  × faster", True),
    ("40%", "40 per centre of town", False),
    ("4x", "the 4xl model", False),
    ("65%", "revenue hit $65B", False),
    ("$65B", "revenue hit 65%", False),
])
def test_odd_spacing(value, material, expected):
    assert kpi_tile_backed({"value": value, "label": "x"}, material) is expected
    assert slide_backed(kpi(value), material) is expected


def test_num_re_linear_on_whitespace_heavy_input():
    for s in (" " * 100_000, "1" + " " * 100_000 + "z", ("5 \n\t " * 25_000), "1 " * 50_000):
        t = time.perf_counter()
        list(NUM_RE.finditer(s))
        assert time.perf_counter() - t < 1.0


class StubLLM:
    def __init__(self, out):
        self.out = out

    async def cheap(self, *a, **k):
        return self.out


async def _item(db, text=LONG, msg_id=1):
    i = await db.insert_item(source="channel", source_chat_id=1, source_msg_id=msg_id, raw_text=text)
    return i, await db.get_item(i)


BAD = [{"score": "high"}, {"score": None}, [8], "score: 8", {"score": float("inf")}]


@pytest.mark.parametrize("bad", BAD, ids=repr)
async def test_triage_bad_verdict(db, settings, bad):
    _, item = await _item(db)
    with pytest.raises(BadCompletion):
        await triage(item, StubLLM(bad), db, settings)


@pytest.mark.parametrize("bad", BAD, ids=repr)
async def test_worker_tick_spends_one_attempt(db, settings, bad):
    i, _ = await _item(db)
    llm = StubLLM(bad)

    async def handler(item):
        return await triage(item, llm, db, settings)

    w = Worker(db, {Status.INGESTED: (handler, Status.TRIAGED)})
    assert await w.tick() == 1
    after = await db.get_item(i)
    assert after.status == Status.INGESTED
    assert after.attempts == 1
    assert after.next_attempt_at is not None
    assert "BadCompletion" in (after.last_error or "")


@pytest.mark.parametrize("score,expected", [(8, Status.TRIAGED), (6, Status.TRIAGED), (5, Status.DROPPED), (0, Status.DROPPED)])
async def test_worker_valid_verdict(db, settings, score, expected):
    i, _ = await _item(db)
    llm = StubLLM({"score": score, "reason": "r", "topic": "t"})

    async def handler(item):
        return await triage(item, llm, db, settings, threshold=6)

    await Worker(db, {Status.INGESTED: (handler, Status.TRIAGED)}).tick()
    after = await db.get_item(i)
    assert after.status == expected and after.triage_score == score and after.attempts == 0
