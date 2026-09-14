"""The public media base has to be read when it is used, not when we booted.

A Cloudflare quick tunnel regenerates its hostname on every start. Pinned in
R2_PUBLIC_BASE it was stale from the next restart onward, and Instagram
reported the resulting dead hostname as "Only photo or video can be accepted
as media type" — a message about file formats, for a DNS problem.
"""

from dataclasses import replace

from pipeline.publish.tunnel import live_origin, public_base

LIVE = "https://soft-recruiting-york-parliament.trycloudflare.com"


def cfg(settings, **over):
    return replace(settings, **{"r2_bucket": "slides",
                                "r2_public_base": "https://configured.example/slides",
                                **over})


def test_no_origin_file_falls_back_to_the_configured_base(settings, tmp_path):
    """A hosted bucket or a named tunnel needs no special case."""
    assert public_base(cfg(settings), tmp_path / "absent.txt") == \
        "https://configured.example/slides"


def test_a_live_origin_wins_over_the_configured_base(settings, tmp_path):
    f = tmp_path / "origin.txt"
    f.write_text(LIVE + "\n")
    assert public_base(cfg(settings), f) == f"{LIVE}/slides"


def test_trailing_slashes_do_not_double_up(settings, tmp_path):
    f = tmp_path / "origin.txt"
    f.write_text(LIVE + "/\n")
    assert public_base(cfg(settings), f) == f"{LIVE}/slides"


def test_a_half_written_file_is_ignored(settings, tmp_path):
    """Write-then-rename makes this unlikely, but a truncated hostname would
    become the base of every media URL in a post."""
    f = tmp_path / "origin.txt"
    f.write_text("https:/soft-recr")
    assert public_base(cfg(settings), f) == "https://configured.example/slides"


def test_an_empty_file_is_ignored(settings, tmp_path):
    f = tmp_path / "origin.txt"
    f.write_text("\n")
    assert live_origin(f) == ""


def test_a_plaintext_origin_is_refused(settings, tmp_path):
    """Instagram requires https; an http origin would fail at Meta instead."""
    f = tmp_path / "origin.txt"
    f.write_text("http://insecure.example\n")
    assert public_base(cfg(settings), f) == "https://configured.example/slides"


def test_no_bucket_means_the_origin_is_the_base(settings, tmp_path):
    f = tmp_path / "origin.txt"
    f.write_text(LIVE + "\n")
    assert public_base(cfg(settings, r2_bucket=""), f) == LIVE


# ------------------------------------------------ the base actually used


class _StubS3:
    """Accepts uploads without touching the network."""

    def __init__(self):
        self.keys = []

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.keys.append(key)


async def test_upload_builds_urls_from_the_LIVE_origin(settings, tmp_path, monkeypatch):
    """The whole point. Reading r2_public_base here instead would rebuild the
    exact bug: URLs pointing at a hostname that stopped resolving."""
    from pipeline.publish import media_host, tunnel

    origin_file = tmp_path / "origin.txt"
    origin_file.write_text(LIVE + "\n")
    monkeypatch.setattr(tunnel, "ORIGIN_FILE", origin_file)
    monkeypatch.setattr(media_host, "_client", lambda s: _StubS3())

    slide = tmp_path / "item9_slide_01.png"
    slide.write_bytes(b"\x89PNG")
    urls = await media_host.upload(
        [str(slide)], 9,
        cfg(settings, dry_run=False, r2_access_key="k", r2_secret_key="s",
            r2_account_id="a"),
    )
    assert urls[0].startswith(f"{LIVE}/slides/"), urls
    assert "configured.example" not in urls[0]


async def test_upload_falls_back_when_no_tunnel_is_running(settings, tmp_path, monkeypatch):
    from pipeline.publish import media_host, tunnel

    monkeypatch.setattr(tunnel, "ORIGIN_FILE", tmp_path / "absent.txt")
    monkeypatch.setattr(media_host, "_client", lambda s: _StubS3())

    slide = tmp_path / "item9_slide_01.png"
    slide.write_bytes(b"\x89PNG")
    urls = await media_host.upload(
        [str(slide)], 9,
        cfg(settings, dry_run=False, r2_access_key="k", r2_secret_key="s",
            r2_account_id="a"),
    )
    assert urls[0].startswith("https://configured.example/slides/"), urls


# ------------------------------------------------------ origin detection


def test_cloudflares_api_endpoint_is_not_mistaken_for_the_tunnel():
    """cloudflared logs https://api.trycloudflare.com during normal startup.
    Publishing that as the origin sends Instagram to Cloudflare's API for
    every slide — which is exactly what the first version of this did."""
    from pipeline.publish.tunnel import _ORIGIN_RE
    assert _ORIGIN_RE.search(b"connecting to https://api.trycloudflare.com") is None
    assert _ORIGIN_RE.search(b"see https://update.trycloudflare.com") is None


def test_an_assigned_hostname_is_found_inside_the_banner():
    from pipeline.publish.tunnel import _ORIGIN_RE
    banner = b"|  https://talks-protective-columnists-copper.trycloudflare.com  |"
    assert _ORIGIN_RE.search(banner).group(0) == \
        b"https://talks-protective-columnists-copper.trycloudflare.com"


def test_a_two_word_hostname_still_counts():
    """The slug length is Cloudflare's business; one hyphen is enough to tell
    an assigned name from a service endpoint."""
    from pipeline.publish.tunnel import _ORIGIN_RE
    assert _ORIGIN_RE.search(b"https://red-panda.trycloudflare.com") is not None
