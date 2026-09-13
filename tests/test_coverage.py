"""The coverage check: did the work answer what was asked?

Item 45 asked for two things — the early signs of burnout and depression in IT
professionals, and AI's effect on them. The planner welded "AI" onto every
query, nothing searched the clinical half, three researchers reported finding
nothing, and an eight-slide deck shipped answering only the second clause.
Every structural check passed it, because nothing was checking coverage.
"""

import pytest

from pipeline.coverage import COVERAGE_SYSTEM, unaddressed


class FakeLLM:
    """Returns queued verdicts; records what it was asked."""

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.calls = []

    async def cheap(self, system, user, schema=None, **kw):
        self.calls.append(user)
        if not self.verdicts:
            raise AssertionError("coverage check called more often than expected")
        result = self.verdicts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


CLAUSES = ["the early signs of burnout in IT professionals",
           "how AI is causing more or less of it"]


async def test_reports_the_clause_nothing_addresses():
    llm = FakeLLM({"uncovered": ["the early signs of burnout in IT professionals"]})
    out = await unaddressed(llm, CLAUSES, ["AI eases burnout — Workday research"])
    assert out == ["the early signs of burnout in IT professionals"]


async def test_full_coverage_returns_nothing():
    llm = FakeLLM({"uncovered": []})
    assert await unaddressed(llm, CLAUSES, ["a", "b"]) == []


async def test_a_paraphrased_clause_is_not_accepted():
    """A model that rewords an ask must not be able to halt an item over a
    clause nobody actually made."""
    llm = FakeLLM({"uncovered": ["something about burnout signs maybe"]})
    assert await unaddressed(llm, CLAUSES, ["x"]) == []


def test_the_prompt_names_the_two_traps():
    collapsed = " ".join(COVERAGE_SYSTEM.split())
    assert "Adjacent is not the same" in collapsed
    assert "report of failure, not coverage" in collapsed


async def test_no_clauses_means_no_call():
    """Most channel items ask one thing; there is nothing to check and no
    reason to spend a model call."""
    llm = FakeLLM()
    assert await unaddressed(llm, [], ["material"]) == []
    assert llm.calls == []


async def test_no_material_means_no_call():
    llm = FakeLLM()
    assert await unaddressed(llm, CLAUSES, []) == []
    assert llm.calls == []


async def test_blank_clauses_are_ignored():
    llm = FakeLLM()
    assert await unaddressed(llm, ["", "   "], ["material"]) == []
    assert llm.calls == []


async def test_a_model_failure_fails_open():
    """A safety net that fails closed would halt the pipeline on an outage —
    the same mistake as reporting an Ollama outage as irrelevant sources."""
    llm = FakeLLM(RuntimeError("model unreachable"))
    assert await unaddressed(llm, CLAUSES, ["x"]) == []


async def test_a_malformed_verdict_fails_open():
    llm = FakeLLM({"uncovered": "not a list"})
    assert await unaddressed(llm, CLAUSES, ["x"]) == []


async def test_duplicates_are_collapsed():
    llm = FakeLLM({"uncovered": [CLAUSES[0], CLAUSES[0]]})
    assert await unaddressed(llm, CLAUSES, ["x"]) == [CLAUSES[0]]


async def test_both_clauses_and_material_reach_the_prompt():
    llm = FakeLLM({"uncovered": []})
    await unaddressed(llm, CLAUSES, ["Workday says AI eases burnout"])
    sent = llm.calls[0]
    assert "the early signs of burnout in IT professionals" in sent
    assert "Workday says AI eases burnout" in sent


async def test_every_line_of_material_reaches_the_judge():
    """Uncapped on purpose. Deciding whether an ask was answered from the
    first 40 items, each cut to 220 characters, is deciding it from a
    summary — and the thing being judged is whether something is missing."""
    llm = FakeLLM({"uncovered": []})
    await unaddressed(llm, CLAUSES, [f"line {i}" for i in range(200)])
    assert llm.calls[0].count("- line ") == 200


async def test_a_long_line_is_not_shortened():
    llm = FakeLLM({"uncovered": []})
    await unaddressed(llm, CLAUSES, ["x" * 4000])
    assert "x" * 4000 in llm.calls[0]
