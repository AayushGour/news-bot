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


def _client(settings):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key,
        aws_secret_access_key=settings.r2_secret_key,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        region_name="auto",
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
        raise Terminal("R2 is not configured; cannot publish")

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
