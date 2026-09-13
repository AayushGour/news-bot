"""Strip credentials out of log records, whatever put them there.

httpx logs every request URL at INFO, and Meta's read endpoints take the
access token as a *query parameter* — so a routine container-status GET wrote
a live, account-equivalent Instagram token into pipeline.log in cleartext. The
POSTs never leaked, which is exactly what makes this the kind of bug that sits
unnoticed: the interesting calls looked clean.

Quieting httpx would have fixed the one path that was observed. A filter on
every handler fixes the paths nobody has hit yet — an exception whose text
carries a signed URL, a debug line added next month, a library that logs its
own retries. Redaction belongs at the sink, where every record must pass,
rather than at each of the places a secret might enter one.
"""

from __future__ import annotations

import logging
import re

#: Ordered (pattern, replacement) pairs. Each keeps the surrounding text — the
#: log stays readable and greppable — and replaces only the secret itself, so a
#: redacted line still says which endpoint was called and how it answered.
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Any `access_token=...` in a URL or form body. Stops at the next
    # separator so the rest of the query string survives.
    (re.compile(r"(access_token=)[^&\s\"']+"), r"\1<redacted>"),
    # Instagram/Facebook long-lived tokens, even outside a token= parameter.
    (re.compile(r"\bIGAA[A-Za-z0-9_-]{20,}"), "<redacted-ig-token>"),
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), "<redacted-fb-token>"),
    # OpenRouter / OpenAI-style keys.
    (re.compile(r"\bsk-(?:or-)?[A-Za-z0-9-]{20,}"), "<redacted-api-key>"),
    # Telegram bot token: numeric id, colon, 35-char secret.
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{35}\b"), "<redacted-bot-token>"),
    # Authorization headers, if one is ever echoed into a message.
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1<redacted>"),
)


def redact(text: str) -> str:
    """Return ``text`` with every known credential shape replaced."""
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


#: Used only to render an exception into text so it can be redacted.
_FORMATTER = logging.Formatter()


class RedactingFilter(logging.Filter):
    """Rewrite each record's message in place, then always allow it through.

    The record is formatted here rather than left lazy: a secret can arrive in
    ``args`` as easily as in ``msg`` (``log.info("GET %s", url)``), and only the
    formatted string is guaranteed to contain both. Formatting once and
    clearing ``args`` keeps every downstream handler from re-expanding the
    original, unredacted values.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - see below
            # Deliberately broad. A logging filter that raises takes down
            # the emitting call, so any failure to inspect a record must
            # end in the record passing through unredacted rather than in
            # an exception escaping into unrelated code.
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        # A traceback is the other way a secret reaches the file, and it takes
        # a different path: at filter time ``exc_text`` is still None, because
        # the *formatter* fills it in afterwards. Formatting it here and
        # storing the redacted result is what makes the filter effective —
        # Formatter.format reuses ``exc_text`` when it is already set, so the
        # raw traceback is never rendered. Redacting the existing ``exc_text``
        # alone silently did nothing, which a test caught only because it
        # asserted on a handler's output rather than on redact().
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(_FORMATTER.formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install(handlers: list[logging.Handler]) -> None:
    """Attach the filter to every handler, so no sink can be forgotten."""
    for handler in handlers:
        handler.addFilter(RedactingFilter())
