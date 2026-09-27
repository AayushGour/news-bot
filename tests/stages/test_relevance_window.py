"""A repository is judged on its description, not on a dumped README.

SearXNG returns a repo's description in `content` — except when it returns the
whole README. On one live search every genuine result carried 62-270
characters while four spam repositories carried ~64,000. In those, the subject
word appeared once at roughly offset 9,000, which was enough to pass relevance:
a Chinese propaganda repo and an anime message board ranked beside a
machine-learning interview repo, all three scoring 88.
"""

from pipeline.stages.enumerate_items import (
    DESCRIPTION_CHARS,
    is_relevant,
    subject_terms,
)

# T170: "AI interview preparation" would now also carry "ai" as a required
# short term (subject_terms no longer drops it), which is orthogonal to what
# this file tests -- the DESCRIPTION_CHARS window, not the short-term gate.
# Kept to "interview preparation" so TERMS is unchanged from before T170 and
# every test below still measures only the windowing behaviour it was
# written for. The short-term interaction has its own coverage in
# test_the_short_term_gate_is_independent_of_the_window below.
TERMS = subject_terms("interview preparation")


def repo(url="https://github.com/o/n", title="o/n", content="", tags=None):
    return {"url": url, "title": title, "content": content, "tags": tags or []}


def test_the_subject_words_survive_stopwording():
    assert TERMS == ["interview", "preparation"]


def test_the_short_term_gate_is_independent_of_the_window():
    """T170: adding "ai" back in (a short, required, whole-word term) must
    still respect DESCRIPTION_CHARS -- a short required term buried past the
    window is exactly the spam-repo shape this file exists to guard, and
    "required" must not become "unbounded"."""
    terms = subject_terms("ai interview preparation")
    assert terms == ["ai", "interview", "preparation"]
    assert is_relevant(
        repo(content="An AI-assisted interview preparation toolkit."), terms) is True
    outside_window = "a" * (DESCRIPTION_CHARS + 50) + " ai interview preparation"
    assert is_relevant(repo(content=outside_window), terms) is False


def test_a_real_short_description_still_matches():
    assert is_relevant(repo(content="Technical Interview Questions for ML"), TERMS)


def test_a_readme_dump_with_one_incidental_hit_is_rejected():
    """The exact shape of the four spam repos."""
    dump = "x" * 9000 + " interview " + "y" * 50000
    assert not is_relevant(repo(content=dump), TERMS)


def test_the_boundary_is_the_description_window():
    inside = "a" * (DESCRIPTION_CHARS - 20) + " interview"
    outside = "a" * (DESCRIPTION_CHARS + 50) + " interview"
    assert is_relevant(repo(content=inside), TERMS)
    assert not is_relevant(repo(content=outside), TERMS)


def test_url_and_title_are_never_windowed():
    """The naming fields are short by construction and always carry meaning."""
    assert is_relevant(
        repo(url="https://github.com/x/ml-interview-questions", content="z" * 70000),
        TERMS)
    assert is_relevant(repo(title="x/interview-prep", content="z" * 70000), TERMS)


def test_tags_still_count_regardless_of_content_size():
    assert is_relevant(
        repo(content="z" * 70000, tags=["interview", "ml"]), TERMS)


def test_no_terms_means_nothing_is_excluded():
    """Dropping every candidate is worse than not filtering."""
    assert is_relevant(repo(content="anything"), [])
