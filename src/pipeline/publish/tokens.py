"""Instagram long-lived token refresh.

Tokens expire after 60 days. If the refresh lapses, publishing stops and the
only symptom is that nothing happens — no error, no post, no clue. So the
refresh runs on a schedule and, crucially, **tells the operator when it fails**.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GRAPH = "https://graph.instagram.com"
REFRESH_AFTER_DAYS = 50  # Tokens last 60; refresh with 10 days of headroom.
WARN_AFTER_DAYS = 57

ALERT_FAILED = (
    "⚠️ Instagram token refresh FAILED.\n\n"
    "The token expires 60 days after it was issued. Once it lapses, publishing "
    "stops silently — previews will keep arriving but nothing will post.\n\n"
    "Generate a new long-lived token and update IG_ACCESS_TOKEN."
)


class TokenStore:
    """Refreshed token on disk, so a restart does not lose it.

    The environment holds the token you bootstrap with; this file holds the
    current one after any refresh.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, token: str, expires_in: int | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "token": token,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "expires_in": expires_in,
        }), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover - filesystem dependent
            pass

    def current_token(self, fallback: str) -> str:
        return self.load().get("token") or fallback

    def age_days(self) -> float | None:
        stamp = self.load().get("refreshed_at")
        if not stamp:
            return None
        try:
            refreshed = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - refreshed).total_seconds() / 86400


async def refresh_token_if_due(
    http: Any, settings: Any, store: TokenStore, notify: Any = None,
    force: bool = False, age_days: float | None = None,
) -> bool:
    """Refresh when due. Returns True if a new token was stored."""
    if settings.dry_run and not force:
        return False

    age = age_days if age_days is not None else store.age_days()
    if age is None:
        # Never refreshed: start the clock so the next run has a baseline.
        store.save(store.current_token(settings.ig_access_token))
        return False
    if age < REFRESH_AFTER_DAYS and not force:
        return False

    token = store.current_token(settings.ig_access_token)
    try:
        response = await http.get(
            f"{GRAPH}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
            timeout=60,
        )
        ok = response.status_code == 200
        body = response.json() if ok else {}
    except Exception as exc:
        log.error("instagram token refresh errored: %s", exc)
        ok, body = False, {}

    new_token = body.get("access_token") if ok else None
    if not new_token:
        log.error("instagram token refresh failed at age %.1f days", age)
        await _notify(notify, ALERT_FAILED + f"\n\nToken age: {age:.0f} days.")
        return False

    store.save(new_token, body.get("expires_in"))
    log.info("instagram token refreshed at age %.1f days", age)
    return True


async def _notify(notify: Any, text: str) -> None:
    if notify is None:
        return
    try:
        await notify(text)
    except Exception:  # pragma: no cover - alerting must not cascade
        log.exception("token alert delivery failed")
