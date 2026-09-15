"""The public media base has to be read when it is used, not when we booted.

A Cloudflare quick tunnel regenerates its hostname on every start. Pinned in
R2_PUBLIC_BASE it was stale from the next restart onward, and Instagram
reported the resulting dead hostname as "Only photo or video can be accepted
as media type" — a message about file formats, for a DNS problem.
"""

from dataclasses import replace

import pytest

from pipeline.models import Status
from pipeline.publish.tunnel import live_origin, public_base

LIVE = "https://soft-recruiting-york-parliament.trycloudflare.com"


LIVE_IG = {
    "dry_run": False, "ig_user_id": "17841400000000000",
    "ig_access_token": "IGT", "r2_bucket": "slides",
    "r2_public_base": "https://configured.example/slides",
    "r2_account_id": "acct", "r2_access_key": "k", "r2_secret_key": "s",
}


def live(settings, **over):
    return replace(settings, **{**LIVE_IG, **over})


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


# ------------------------------------------------------------ liveness


async def test_health_watch_returns_when_the_origin_stops_answering(tmp_path, monkeypatch):
    """cloudflared does not exit when its hostname is withdrawn. One ran for 29
    hours after going NXDOMAIN, still advertising the dead host, and two
    finished decks failed publishing against it."""
    import asyncio

    from pipeline.publish import tunnel

    origin_file = tmp_path / "origin.txt"
    origin_file.write_text(LIVE + "\n")
    monkeypatch.setattr(tunnel, "ORIGIN_FILE", origin_file)
    monkeypatch.setattr(tunnel, "HEALTHCHECK_INTERVAL_S", 0.01)
    monkeypatch.setattr(tunnel, "HEALTHCHECK_FAILURES", 2)

    async def dead(origin):
        return False
    monkeypatch.setattr(tunnel, "_probe", dead)

    await asyncio.wait_for(tunnel._watch_health(asyncio.Event()), timeout=3)
    assert not origin_file.exists(), "a dead origin must be dropped, not left to be used"


async def test_health_watch_stays_put_while_the_origin_answers(tmp_path, monkeypatch):
    import asyncio

    from pipeline.publish import tunnel

    origin_file = tmp_path / "origin.txt"
    origin_file.write_text(LIVE + "\n")
    monkeypatch.setattr(tunnel, "ORIGIN_FILE", origin_file)
    monkeypatch.setattr(tunnel, "HEALTHCHECK_INTERVAL_S", 0.01)

    async def alive(origin):
        return True
    monkeypatch.setattr(tunnel, "_probe", alive)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(tunnel._watch_health(asyncio.Event()), timeout=0.2)
    assert origin_file.exists()


async def test_a_single_blip_does_not_kill_a_working_tunnel(tmp_path, monkeypatch):
    """One failure is a flaky connection; the threshold exists so a residential
    hiccup does not churn the hostname every time."""
    import asyncio

    from pipeline.publish import tunnel

    origin_file = tmp_path / "origin.txt"
    origin_file.write_text(LIVE + "\n")
    monkeypatch.setattr(tunnel, "ORIGIN_FILE", origin_file)
    monkeypatch.setattr(tunnel, "HEALTHCHECK_INTERVAL_S", 0.01)
    monkeypatch.setattr(tunnel, "HEALTHCHECK_FAILURES", 3)

    # Alternating forever, so the counter is exercised indefinitely. With the
    # reset, failures never exceed 1; without it they accumulate to the
    # threshold and the tunnel is torn down over nothing. A finite sequence
    # that settles on success cannot tell those two apart.
    import itertools
    results = itertools.cycle([False, True])

    async def flaky(origin):
        return next(results)
    monkeypatch.setattr(tunnel, "_probe", flaky)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(tunnel._watch_health(asyncio.Event()), timeout=0.2)
    assert origin_file.exists()


async def test_health_watch_exits_when_asked_to_stop(tmp_path, monkeypatch):
    import asyncio

    from pipeline.publish import tunnel

    monkeypatch.setattr(tunnel, "ORIGIN_FILE", tmp_path / "origin.txt")
    monkeypatch.setattr(tunnel, "HEALTHCHECK_INTERVAL_S", 5)
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(tunnel._watch_health(stop), timeout=1)


# ------------------------------------------- surviving a hostname rotation


def test_urls_are_re_addressed_to_the_live_host(settings, tmp_path, monkeypatch):
    """The failure this exists to end: an item approved before a tunnel
    rotation and published after it held URLs naming a withdrawn hostname.
    Retrying them was futile — they could never become valid again."""
    from pipeline.publish import media_host, tunnel

    origin_file = tmp_path / "origin.txt"
    origin_file.write_text(LIVE + "\n")
    monkeypatch.setattr(tunnel, "ORIGIN_FILE", origin_file)

    stale = [f"https://withdrawn.trycloudflare.com/slides/items/106/item106_slide_0{n}.png"
             for n in (1, 2)]
    fresh = media_host.current_urls(106, stale, cfg(settings))
    assert fresh == [f"{LIVE}/slides/items/106/item106_slide_0{n}.png" for n in (1, 2)]


def test_re_addressing_preserves_slide_order_and_filenames(settings, tmp_path, monkeypatch):
    """Slide order is the carousel's order; a reshuffle here would publish the
    deck out of sequence."""
    from pipeline.publish import media_host, tunnel

    origin_file = tmp_path / "origin.txt"
    origin_file.write_text(LIVE + "\n")
    monkeypatch.setattr(tunnel, "ORIGIN_FILE", origin_file)

    stale = [f"https://old.example/slides/items/7/item7_slide_{n:02d}.png"
             for n in range(1, 6)]
    fresh = media_host.current_urls(7, stale, cfg(settings))
    assert [u.rsplit("/", 1)[-1] for u in fresh] == \
        [f"item7_slide_{n:02d}.png" for n in range(1, 6)]


def test_already_current_urls_are_left_alone(settings, tmp_path, monkeypatch):
    from pipeline.publish import media_host, tunnel

    origin_file = tmp_path / "origin.txt"
    origin_file.write_text(LIVE + "\n")
    monkeypatch.setattr(tunnel, "ORIGIN_FILE", origin_file)

    good = [f"{LIVE}/slides/items/3/item3_slide_01.png"]
    assert media_host.current_urls(3, good, cfg(settings)) == good


def test_no_base_at_all_leaves_urls_untouched(settings, tmp_path, monkeypatch):
    """A misconfiguration must surface as its own error, not as empty URLs."""
    from pipeline.publish import media_host, tunnel

    monkeypatch.setattr(tunnel, "ORIGIN_FILE", tmp_path / "absent.txt")
    stale = ["https://old.example/slides/items/3/item3_slide_01.png"]
    assert media_host.current_urls(3, stale, cfg(settings, r2_public_base="")) == stale


async def test_publish_survives_a_rotation_between_approval_and_publish(
        db, settings, fake_http, tmp_path, monkeypatch):
    """End to end: the item was uploaded against a host that is now gone."""
    from pipeline.publish import tunnel
    from pipeline.publish.instagram import publish_carousel

    origin_file = tmp_path / "origin.txt"
    origin_file.write_text("https://cdn.example\n")
    monkeypatch.setattr(tunnel, "ORIGIN_FILE", origin_file)

    i = await db.insert_item(source="channel", source_chat_id=-100,
                             source_msg_id=77, raw_text="news")
    stale_url = (f"https://withdrawn.trycloudflare.com/slides/items/{i}/"
                 f"item{i}_slide_01.png")
    await db.transition(i, Status.APPROVED, {
        "caption": "c",
        "rendered_paths": ["/tmp/a.png"],
        "media_urls": [stale_url],
    })
    fake_http.respond_for("cdn.example", "", status=200)
    # One body serves both calls Meta gets here: the container creations
    # (which need an id) and the readiness poll (which needs a state).
    fake_http.respond_for("graph.instagram.com",
                          {"id": "1", "status_code": "FINISHED"})

    await publish_carousel(await db.get_item(i), fake_http, db,
                           live(settings, r2_bucket="slides"))

    item = await db.get_item(i)
    assert item.media_urls[0].startswith("https://cdn.example/slides/"), item.media_urls
