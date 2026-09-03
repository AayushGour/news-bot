"""Upload rendered slides somewhere Instagram can fetch them.

Meta's servers fetch media by URL, so the images must be publicly reachable —
``localhost`` cannot work even when everything else runs locally. Cloudflare R2
is S3-compatible, has a free tier with no egress fees, and stays correct if the
orchestrator later moves to a VM.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path

from ..errors import Retryable, Terminal

log = logging.getLogger(__name__)

DRY_RUN_BASE = "https://dry-run.invalid"


def object_key(item_id: int, index: int, path: Path | str) -> str:
    return f"items/{item_id}/{Path(path).name or f'slide_{index:02d}.png'}"


def endpoint_url(settings) -> str:
    """Where to talk S3.

    Explicit S3_ENDPOINT wins, so any S3-compatible backend works — MinIO,
    Backblaze B2, Wasabi, real S3. Falls back to Cloudflare R2 derived from the
    account id, which is the documented default.
    """
    explicit = getattr(settings, "s3_endpoint", "")
    if explicit:
        return explicit.rstrip("/")
    return f"https://{settings.r2_account_id}.r2.cloudflarestorage.com"


def _client(settings):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url(settings),
        aws_access_key_id=settings.r2_access_key,
        aws_secret_access_key=settings.r2_secret_key,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3},
            # MinIO needs path-style addressing; R2 accepts it too.
            s3={"addressing_style": "path"},
        ),
        region_name=getattr(settings, "s3_region", "") or "auto",
    )


async def upload(paths: list[str], item_id: int, settings) -> list[str]:
    """Upload every slide and return public URLs in slide order."""
    if not paths:
        raise Retryable("nothing to upload: no rendered slides")

    if settings.dry_run:
        urls = [f"{DRY_RUN_BASE}/{object_key(item_id, i, p)}"
                for i, p in enumerate(paths)]
        log.info("DRY_RUN: skipping R2 upload of %d files for item %s",
                 len(paths), item_id)
        return urls

    if not (settings.r2_bucket and settings.r2_public_base):
        raise Terminal("object storage is not configured; cannot publish")

    if "localhost" in settings.r2_public_base or "127.0.0.1" in settings.r2_public_base:
        # Instagram fetches these URLs from its own servers. A loopback address
        # fails there with an opaque media error, so catch it here instead.
        raise Terminal(
            f"R2_PUBLIC_BASE is {settings.r2_public_base!r}, which Instagram "
            "cannot reach. Media URLs must be publicly resolvable — put a tunnel "
            "in front of local storage, or use a hosted bucket."
        )

    client = _client(settings)
    urls: list[str] = []
    for index, path in enumerate(paths):
        key = object_key(item_id, index, path)
        content_type = mimetypes.guess_type(path)[0] or "image/png"
        try:
            await asyncio.to_thread(
                client.upload_file, str(path), settings.r2_bucket, key,
                ExtraArgs={"ContentType": content_type},
            )
        except FileNotFoundError as exc:
            raise Terminal(f"rendered slide missing: {path}") from exc
        except Exception as exc:
            raise Retryable(f"R2 upload failed for {key}: {exc}") from exc
        urls.append(f"{settings.r2_public_base.rstrip('/')}/{key}")

    log.info("uploaded %d slides for item %s", len(urls), item_id)
    return urls
