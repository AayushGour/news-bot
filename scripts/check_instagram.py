"""Check the Instagram credentials without printing them.

Answers the three questions that actually block publishing, in the order they
bite: is the token valid, does the account it names match the one you meant,
and does it hold the publishing permission. A token missing the publish scope
looks perfectly healthy until the first post fails.

Prints nothing secret — the token is never echoed, only what it resolves to.

    ./.venv/bin/python scripts/check_instagram.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline.config import Settings  # noqa: E402

GRAPH = "https://graph.instagram.com/v23.0"

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def line(mark: str, text: str, detail: str = "") -> None:
    print(f"[{mark}] {text}")
    if detail:
        print(f"         {detail}")


async def main() -> int:
    settings = Settings.load()
    failures = 0

    if not settings.ig_access_token:
        line(BAD, "IG_ACCESS_TOKEN is not set")
        return 1
    if not settings.ig_user_id:
        line(BAD, "IG_USER_ID is not set")
        return 1
    line(OK, f"credentials present (token {len(settings.ig_access_token)} chars)")

    async with httpx.AsyncClient(timeout=30) as http:
        # 1. Does the token resolve to an account at all?
        try:
            response = await http.get(
                f"{GRAPH}/me",
                params={"fields": "id,username,account_type",
                        "access_token": settings.ig_access_token},
            )
        except Exception as exc:
            line(BAD, "graph.instagram.com unreachable", str(exc))
            return 1

        if response.status_code != 200:
            # Meta puts the useful part in the body; the status alone says little.
            line(BAD, f"token rejected (HTTP {response.status_code})",
                 response.text[:300])
            return 1

        me = response.json()
        line(OK, f"token valid — @{me.get('username')} "
                 f"({me.get('account_type', 'unknown type')})")

        # 2. Does IG_USER_ID name the same ACCOUNT? Not the same string —
        #    an account has more than one valid id (the app-scoped one /me
        #    returns, and the 17841… business id), and both are accepted.
        #    Comparing ids rejected a working config; comparing the account
        #    they resolve to is the question actually being asked.
        if str(me.get("id")) == str(settings.ig_user_id):
            line(OK, "IG_USER_ID is the id this token resolves to")
        else:
            named = await http.get(
                f"{GRAPH}/{settings.ig_user_id}",
                params={"fields": "id,username",
                        "access_token": settings.ig_access_token})
            if (named.status_code == 200
                    and named.json().get("username") == me.get("username")):
                line(OK, f"IG_USER_ID is a valid alias for @{me.get('username')}",
                     f"/me reports {me.get('id')}; both are accepted")
            else:
                line(BAD, "IG_USER_ID is a different account than the token",
                     f"token is @{me.get('username')} ({me.get('id')}); "
                     f"config id returned HTTP {named.status_code}")
                failures += 1

        if me.get("account_type") not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR"):
            line(WARN, "account is not Business or Creator",
                 "content publishing needs a professional account")

        # 3. Can it actually reach the publishing endpoint? Reading the media
        #    edge is the cheapest call that exercises the same permission.
        probe = await http.get(
            f"{GRAPH}/{settings.ig_user_id}/media",
            params={"fields": "id", "limit": 1,
                    "access_token": settings.ig_access_token},
        )
        if probe.status_code == 200:
            line(OK, "media edge readable — publishing scope looks present")
        else:
            line(BAD, f"media edge refused (HTTP {probe.status_code})",
                 "check the instagram_business_content_publish scope\n         "
                 + probe.text[:240])
            failures += 1

    print()
    if failures:
        print(f"{failures} problem(s) — publishing would fail.")
        return 1
    print("Instagram side is ready. Media hosting is the remaining piece.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
