"""A secret must not survive a trip through a handler.

These assert on the *output of a real handler*, not on redact() alone. The bug
being guarded against was never in the regex — it was that nothing stood
between httpx's URL logging and the file, so a test that only exercises the
helper would have passed on the day the token leaked.
"""

import logging

import pytest

from pipeline.logredact import RedactingFilter, install, redact

#: Synthetic. Must never be a real token, even a prefix — the first version
#: of this constant was copied out of the leaked log line and carried 64
#: live characters into a file bound for a public repository.
IG_TOKEN = "IGAAsyntheticFIXTUREtokenNOTrealDOnotUSE0123456789abcdefGHIJKL"


def emit(record_args, level=logging.INFO, exc=None) -> str:
    """Push one record through a handler that has the filter installed."""
    stream = logging.StreamHandler(stream=__import__("io").StringIO())
    stream.setFormatter(logging.Formatter("%(message)s"))
    install([stream])
    logger = logging.getLogger(f"test.{id(record_args)}")
    logger.handlers = [stream]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.log(level, *record_args, exc_info=exc)
    return stream.stream.getvalue()


def test_token_in_a_lazy_format_argument_is_redacted():
    """httpx logs `"HTTP Request: %s %s"` style — the secret is in args, not
    msg. Redacting only record.msg would let it straight through."""
    out = emit(("HTTP Request: GET %s", f"https://g.com/x?access_token={IG_TOKEN}"))
    assert IG_TOKEN not in out
    assert "access_token=<redacted>" in out


def test_the_rest_of_the_line_survives_redaction():
    """A redacted log is only useful if it still says what happened."""
    url = (f"https://graph.instagram.com/v23.0/17902716117592870"
           f"?fields=status_code&access_token={IG_TOKEN}")
    out = emit(("GET %s", url))
    assert "graph.instagram.com/v23.0/17902716117592870" in out
    assert "fields=status_code" in out


def test_bare_token_outside_a_query_parameter_is_redacted():
    out = emit(("token is %s", IG_TOKEN))
    assert IG_TOKEN not in out


def test_traceback_text_is_redacted():
    """An exception carrying a signed URL formats through a separate path."""
    try:
        raise RuntimeError(f"container unreadable: https://g.com/?access_token={IG_TOKEN}")
    except RuntimeError:
        out = emit(("publish failed",), level=logging.ERROR, exc=True)
    assert IG_TOKEN not in out


def test_a_clean_record_is_passed_through_untouched():
    out = emit(("published item %d as %s", 84, "18092835962538597"))
    assert "published item 84 as 18092835962538597" in out


def test_filter_always_returns_true_so_no_record_is_dropped():
    """Redaction must never be a reason a log line disappears."""
    record = logging.LogRecord("n", logging.INFO, "p", 1,
                               f"access_token={IG_TOKEN}", None, None)
    assert RedactingFilter().filter(record) is True


@pytest.mark.parametrize("secret,label", [
    ("sk-or-v1-" + "a" * 40, "api-key"),
    ("8457717415:AAH" + "b" * 32, "bot-token"),
    ("EAA" + "c" * 30, "fb-token"),
])
def test_other_credential_shapes(secret, label):
    assert secret not in redact(f"using {secret} now")


def test_a_record_whose_message_cannot_be_formatted_is_still_emitted():
    """getMessage() raises when msg and args disagree. Redaction failing to
    inspect a record is not a reason to silently drop it — that would turn a
    logging bug into missing evidence during an incident."""
    record = logging.LogRecord("n", logging.INFO, "p", 1, "%d items", ("x",), None)
    assert RedactingFilter().filter(record) is True
