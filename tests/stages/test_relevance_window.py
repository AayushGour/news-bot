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

TERMS = subject_terms("AI interview preparation")


def repo(url="https://github.com/o/n", title="o/n", content="", tags=None):
    return {"url": url, "title": title, "content": content, "tags": tags or []}


def test_the_subject_words_survive_stopwording():
    assert TERMS == ["interview", "preparation"]


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
