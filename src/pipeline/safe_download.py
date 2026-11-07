"""Download one image from an UNTRUSTED url without letting the url pick the target.

The caller (revision ``replace_image``, T299) hands us a url that came out of a
web search. Everything about it is attacker-influenced, so this module is the
one place that decides whether and how it is fetched:

  * scheme is http/https only (no ``file://``, ``ftp://``, ``gopher://``...);
  * no credentials in the url, and only the default web ports;
  * the host must RESOLVE and EVERY address it resolves to must be a public one
    (loopback, private, link-local, CGNAT, multicast, reserved and unspecified
    are all refused, IPv4-mapped IPv6 included);
  * redirects are followed by hand, at most ``MAX_REDIRECTS`` hops, and every hop
    is re-validated (a public host that 302s to ``127.0.0.1`` is refused);
  * the body is streamed and cut off at ``MAX_IMAGE_BYTES`` -- never buffered
    whole first -- under one wall-clock deadline (a slow-drip host cannot hold
    the worker);
  * it must declare ``image/*``, be at least ``MIN_IMAGE_BYTES``, DECODE as
    jpeg/png/webp, stay under ``MAX_IMAGE_PIXELS`` (decompression bomb) and
    reach ``MIN_IMAGE_EDGE`` on its long side.

It returns the verified bytes; it never writes a file and never derives a
filename (the caller names the file, from nothing remote). The result is a
``Downloaded`` or a ``Refusal`` -- a closed code, never an exception message.

DNS rebinding is closed at the socket, not by a pre-check: ``PinnedTransport``
resolves the name INSIDE the connect, vets every address it got, and dials the
vetted literal IP. The request still carries the original Host header and the
TLS handshake still sends the hostname as SNI and verifies the certificate
against it (only the TCP dial target is swapped), so there is no second lookup
for an attacker to answer differently. ``check_url`` stays as the cheap early
refusal that yields a precise ``Refusal`` code. The pin only exists when the
client's transport is a ``PinnedTransport`` or a ``RoutingTransport`` (see
``__main__``); a plain ``httpx.AsyncClient`` (eval scripts) keeps the pre-check
only.

The same guarded fetch serves the research path's pages and avatars
(``search.fetch_text`` / ``download_image`` / ``download_avatar``) through
``fetch_public`` + a ``Policy``; each policy fixes the byte cap, accepted
content types, timeout and whether a single gzip/deflate layer is inflated
(bounded by the same cap) or refused.

Not covered: re-encoding the bytes (T218's design asks for it; the research
stage stores the raw bytes too).
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import logging
import re
import socket
import zlib
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
#: Minimum pixels on the long edge. A slide is 1080x1350, and the background is
#: scaled up 8% to hide the blur edge, so anything smaller visibly degrades.
MIN_IMAGE_EDGE = 900
#: Editorial: largest image body accepted (raw bytes on the wire).
MAX_IMAGE_BYTES = 8_000_000
#: Editorial: largest avatar/logo body accepted.
MAX_AVATAR_BYTES = 3_000_000
#: Editorial: largest page body accepted, raw AND after inflating one gzip layer.
MAX_BODY_BYTES = 2_000_000
#: Editorial: wall-clock for one page across every hop.
PAGE_DEADLINE_S = 30.0
#: Editorial: per-socket-operation timeout for a page request.
PAGE_TIMEOUT_S = 15.0
#: Request extension that asks a ``RoutingTransport`` for the pinned transport.
PUBLIC_ONLY_EXTENSION = "pipeline.public_only"
#: Content-Encoding values we can inflate under a cap (one layer only).
INFLATABLE_ENCODINGS = frozenset({"gzip", "x-gzip", "deflate"})
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)

#: Editorial: hops followed. Real image CDNs redirect once or twice.
MAX_REDIRECTS = 3
#: Editorial: wall-clock for one image across every hop, resolve and body.
DOWNLOAD_DEADLINE_S = 25.0
#: Editorial: per-socket-operation timeout handed to the HTTP client.
REQUEST_TIMEOUT_S = 10.0
#: Editorial: the research stage's inline floor in ``search.download_image``
#: (a smaller file is a tracking pixel or an icon, not a photograph).
MIN_IMAGE_BYTES = 15_000
#: Editorial decompression-bomb guard: 40 megapixels (8000x5000) is far above a
#: 1080x1350 slide and far below what exhausts memory.
MAX_IMAGE_PIXELS = 40_000_000
#: Editorial: shortest side. A slide is 1080x1350 and an image fills the slide
#: (background) or sits in a panel (inset/hero); 500 px is under half a slide
#: width, so anything thinner is a banner or a tracking strip, not a picture.
MIN_IMAGE_SHORT_EDGE = 500
#: Editorial: longest:shortest. 3:1 is a wide panorama that still crops to a
#: usable 4:5 slide (a 1500x500 crop keeps a third of its width); beyond that a
#: background crop discards most of the picture.
MAX_ASPECT_RATIO = 3.0
#: Formats we accept after decoding (PIL's ``format``, lower-cased); the same
#: set ``search.download_image`` accepts.
ALLOWED_FORMATS = ("jpeg", "png", "webp")
#: Platform: the only ports an image url may name (RFC 9110 defaults).
ALLOWED_PORTS = {None, 80, 443}
#: RFC 6052 well-known NAT64 prefix: ``64:ff9b::7f00:1`` reaches 127.0.0.1 through a gateway.
NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_FORBIDDEN_URL_CHARS = frozenset("\\ \t\r\n\0")
ALLOWED_SCHEMES = ("http", "https")
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: File suffix per decoded format. The suffix comes from the DECODED format only.
SUFFIX = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}


class Refusal(StrEnum):
    """Why a url was not used. Closed; logged by value, never with exception text."""

    BAD_SCHEME = "bad_scheme"
    BAD_PORT = "bad_port"
    BAD_URL = "bad_url"
    CREDENTIALS = "url_credentials"
    PRIVATE_HOST = "private_host"
    UNRESOLVED = "unresolved_host"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    DOWNGRADE = "https_downgrade"
    ENCODED = "content_encoded"
    HTTP_STATUS = "http_status"
    UNREACHABLE = "unreachable"
    NOT_IMAGE_TYPE = "not_image_type"
    NOT_TEXT_TYPE = "not_text_type"
    TOO_BIG = "too_big"
    TOO_SMALL = "too_small"
    BAD_FORMAT = "bad_format"
    UNDECODABLE = "undecodable"
    LOW_RESOLUTION = "low_resolution"
    BAD_ASPECT = "bad_aspect"
    TOO_MANY_PIXELS = "too_many_pixels"


@dataclass(frozen=True)
class Downloaded:
    blob: bytes
    fmt: str  # one of ALLOWED_FORMATS
    width: int
    height: int

    @property
    def suffix(self) -> str:
        return SUFFIX[self.fmt]


def is_public_address(raw: str) -> bool:
    """True only for a globally routable unicast address."""
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in NAT64_PREFIX:  # a v4 address in v6 clothes: judge the v4 address
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not (
        ip.is_multicast or ip.is_reserved or ip.is_loopback
        or ip.is_link_local or ip.is_unspecified or getattr(ip, "is_site_local", False)
    )


async def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (a literal resolves to itself)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def check_url(url: str) -> Refusal | None:
    """None if ``url`` may be requested, else the reason it may not."""
    if not url.isprintable() or any(c in url for c in _FORBIDDEN_URL_CHARS):
        return Refusal.BAD_URL  # parsers disagree about these (backslash, spaces): refuse, don't interpret
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return Refusal.BAD_URL
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return Refusal.BAD_SCHEME
    if not host or not host.isascii():
        # IDNA 2003 (the resolver) and IDNA 2008 (httpx) turn the same non-ASCII
        # name into different hosts; only a punycode (ASCII) host is unambiguous.
        return Refusal.BAD_URL
    if parts.username is not None or parts.password is not None:
        return Refusal.CREDENTIALS
    if port not in ALLOWED_PORTS:
        return Refusal.BAD_PORT
    try:
        addresses = await _resolve(host, port or (443 if parts.scheme.lower() == "https" else 80))
    except (OSError, UnicodeError):
        return Refusal.UNRESOLVED
    if not addresses:
        return Refusal.UNRESOLVED
    if not all(is_public_address(a) for a in addresses):
        return Refusal.PRIVATE_HOST
    return None


def _decode(blob: bytes) -> Downloaded | Refusal:
    """Verify ``blob`` is a real, fully decodable, in-bounds jpeg/png/webp."""
    if len(blob) < MIN_IMAGE_BYTES:
        return Refusal.TOO_SMALL
    try:
        from PIL import Image

        with Image.open(BytesIO(blob), formats=[f.upper() for f in ALLOWED_FORMATS]) as img:
            fmt = (img.format or "").lower()
            width, height = img.size
            if fmt not in ALLOWED_FORMATS:
                return Refusal.BAD_FORMAT
            if width * height > MAX_IMAGE_PIXELS:
                return Refusal.TOO_MANY_PIXELS
            if max(width, height) < MIN_IMAGE_EDGE or min(width, height) < MIN_IMAGE_SHORT_EDGE:
                return Refusal.LOW_RESOLUTION
            if max(width, height) > MAX_ASPECT_RATIO * min(width, height):
                return Refusal.BAD_ASPECT
            img.load()  # a truncated or corrupt file raises here
    except Exception:
        return Refusal.UNDECODABLE
    return Downloaded(blob, fmt, width, height)


@dataclass(frozen=True)
class Policy:
    """What one kind of fetch accepts. The guards (address, redirects, streaming) are shared."""

    max_bytes: int
    accept: tuple[str, ...]  # content-type prefixes
    wrong_type: Refusal
    timeout_s: float | None = None  # None: REQUEST_TIMEOUT_S, read at call time
    deadline_s: float | None = None  # None: DOWNLOAD_DEADLINE_S, read at call time
    allow_missing_type: bool = False
    inflate: bool = False  # one gzip/deflate layer, bounded by max_bytes


IMAGE_POLICY = Policy(MAX_IMAGE_BYTES, ("image/",), Refusal.NOT_IMAGE_TYPE)
#: Research backgrounds and avatars were never type-checked (they are decoded
#: instead), so octet-stream stays acceptable there; the decode is the check.
LEGACY_IMAGE_TYPES = ("image/", "application/octet-stream", "binary/octet-stream")
BACKGROUND_POLICY = Policy(MAX_IMAGE_BYTES, LEGACY_IMAGE_TYPES, Refusal.NOT_IMAGE_TYPE)
AVATAR_POLICY = Policy(MAX_AVATAR_BYTES, LEGACY_IMAGE_TYPES, Refusal.NOT_IMAGE_TYPE, timeout_s=12.0)
PAGE_POLICY = Policy(
    MAX_BODY_BYTES, ("text/", "application/xhtml+xml", "application/xml"), Refusal.NOT_TEXT_TYPE,
    timeout_s=PAGE_TIMEOUT_S, deadline_s=PAGE_DEADLINE_S, allow_missing_type=True, inflate=True,
)


@dataclass(frozen=True)
class Fetched:
    body: bytes
    content_type: str  # lower-cased, parameters stripped; "" if absent
    charset: str | None

    def text(self) -> str:
        """Decode like ``httpx.Response.text``: declared charset, else utf-8, never raising."""
        try:
            encoding = codecs.lookup(self.charset).name if self.charset else "utf-8"
        except LookupError:
            encoding = "utf-8"
        return self.body.decode(encoding, errors="replace")


class BlockedAddress(httpcore.ConnectError):
    """Raised at connect time when a name resolves to a non-public address."""


class VettingBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, vet every address, dial the vetted literal.

    httpcore calls ``connect_tcp`` with the URL's host and then, for https, runs
    the TLS handshake with that same hostname as SNI / certificate name -- so
    swapping only the dial target pins the connection without weakening TLS.
    """

    def __init__(self, delegate: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._delegate = delegate or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await _resolve(host, port)
        except (OSError, UnicodeError) as exc:
            raise httpcore.ConnectError("unresolved") from exc
        if not addresses or not all(is_public_address(a) for a in addresses):
            raise BlockedAddress("non-public address")
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._delegate.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last or httpcore.ConnectError("no address")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedAddress("unix sockets are never public")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """``AsyncHTTPTransport`` whose connections go through ``VettingBackend``.

    No proxies (a proxy resolves the name itself, which defeats the pin) and no
    HTTP/2 (one code path to reason about). Replacing the pool touches one httpx
    private (``_pool``); ``test_pinned_transport_uses_the_vetting_backend`` fails
    loudly if an httpx upgrade renames it.
    """

    def __init__(self, backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        super().__init__(trust_env=False, http2=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True),
            max_connections=20, max_keepalive_connections=10, keepalive_expiry=5.0,
            http1=True, http2=False,
            network_backend=VettingBackend(backend),
        )


class RoutingTransport(httpx.AsyncBaseTransport):
    """Pinned transport for requests that ask for it, the normal one for the rest.

    The shared client also talks to loopback services (SearXNG, Ollama), so it
    cannot be pinned wholesale; the guarded fetchers mark their requests with
    ``PUBLIC_ONLY_EXTENSION`` instead.
    """

    def __init__(self, default: httpx.AsyncBaseTransport | None = None,
                 pinned: httpx.AsyncBaseTransport | None = None) -> None:
        self._default = default or httpx.AsyncHTTPTransport()
        self._pinned = pinned or PinnedTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.extensions.get(PUBLIC_ONLY_EXTENSION):
            return await self._pinned.handle_async_request(request)
        return await self._default.handle_async_request(request)

    async def aclose(self) -> None:
        await self._default.aclose()
        await self._pinned.aclose()


def _within_cap(inflater: Any, chunk: bytes, body: bytearray, limit: int) -> bool:
    """Inflate ``chunk`` into ``body``; False once the output would exceed ``limit``."""
    data = chunk
    while data:
        body += inflater.decompress(data, limit + 1 - len(body))
        if len(body) > limit:
            return False
        data = inflater.unconsumed_tail
    return True


async def _read_capped(response: Any, policy: Policy, encoding: str) -> bytes | Refusal:
    body = bytearray()
    if encoding in ("", "identity"):
        async for chunk in response.aiter_raw():
            body += chunk
            if len(body) > policy.max_bytes:
                return Refusal.TOO_BIG
        return bytes(body)
    inflater = zlib.decompressobj(zlib.MAX_WBITS | 32)  # gzip or zlib framing
    raw = 0
    async for chunk in response.aiter_raw():
        raw += len(chunk)
        if raw > policy.max_bytes or not _within_cap(inflater, chunk, body, policy.max_bytes):
            return Refusal.TOO_BIG
    if not inflater.eof:  # truncated stream: do not hand half a document on
        return Refusal.UNREACHABLE
    return bytes(body)


async def _fetch_body(http: Any, url: str, policy: Policy) -> Fetched | Refusal:
    """Follow redirects by hand, validating every hop; return the capped body."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        refusal = await check_url(current)
        if refusal is not None:
            return refusal
        try:
            async with http.stream(
                "GET", current, headers={"User-Agent": UA, "Accept-Encoding": "gzip" if policy.inflate else "identity"},
                timeout=REQUEST_TIMEOUT_S if policy.timeout_s is None else policy.timeout_s,
                follow_redirects=False,
                extensions={PUBLIC_ONLY_EXTENSION: True},
            ) as response:
                status = response.status_code
                if status in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        return Refusal.HTTP_STATUS
                    target = urljoin(current, location)
                    if urlsplit(current).scheme.lower() == "https" and urlsplit(target).scheme.lower() != "https":
                        return Refusal.DOWNGRADE
                    current = target
                    continue
                if status != 200:
                    return Refusal.HTTP_STATUS
                # Content-Encoding would be decoded by the client BEFORE the byte cap
                # (a stacked-gzip bomb is a few hundred bytes on the wire), so we read
                # the RAW stream and either count it (identity) or inflate one gzip
                # layer ourselves under the same cap; anything else is refused.
                encoding = str(response.headers.get("content-encoding", "")).strip().lower()
                if encoding not in ("", "identity") and not (policy.inflate and encoding in INFLATABLE_ENCODINGS):
                    return Refusal.ENCODED
                content_type = str(response.headers.get("content-type", ""))
                kind = content_type.split(";", 1)[0].strip().lower()
                if not (kind.startswith(policy.accept) if kind else policy.allow_missing_type):
                    return policy.wrong_type
                declared = str(response.headers.get("content-length", ""))
                if declared.isdigit() and int(declared) > policy.max_bytes:
                    return Refusal.TOO_BIG
                body = await _read_capped(response, policy, encoding)
                if isinstance(body, Refusal):
                    return body
                match = _CHARSET_RE.search(content_type)
                return Fetched(body, kind, match.group(1) if match else None)
        except Exception as exc:
            # The type only: an exception's text can carry the url.
            log.info("guarded fetch failed: %s", type(exc).__name__)
            if isinstance(exc.__cause__, BlockedAddress):
                return Refusal.PRIVATE_HOST
            return Refusal.UNREACHABLE
    return Refusal.TOO_MANY_REDIRECTS


async def fetch_public(http: Any, url: str, policy: Policy) -> Fetched | Refusal:
    """GET an untrusted ``url`` under every guard in the module docstring."""
    try:
        async with asyncio.timeout(DOWNLOAD_DEADLINE_S if policy.deadline_s is None else policy.deadline_s):
            return await _fetch_body(http, url, policy)
    except TimeoutError:
        return Refusal.UNREACHABLE


async def download_public_image(http: Any, url: str) -> Downloaded | Refusal:
    """Fetch ``url`` under every guard in the module docstring and verify it decodes."""
    got = await fetch_public(http, url, IMAGE_POLICY)
    if isinstance(got, Refusal):
        return got
    return await asyncio.to_thread(_decode, got.body)
