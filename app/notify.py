import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TG_API = f"https://api.telegram.org/bot{settings.chairman_bot_token}"


async def notify_chairman(text: str) -> None:
    """Outgoing sendMessage only — does not call getUpdates (OpenClaw keeps polling)."""
    url = f"{_TG_API}/sendMessage"
    payload = {
        "chat_id": settings.chairman_chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error("chairman notify failed: %s %s", r.status_code, r.text)
            raise


async def forward_attachments_to_chairman(attachments: list[dict[str, Any]]) -> None:
    """Send stored files to the chairman chat as Telegram documents."""
    from app.db import attachment_fs_path, get_attachment, get_conn

    url = f"{_TG_API}/sendDocument"
    async with httpx.AsyncClient(timeout=60.0) as client:
        for att in attachments:
            with get_conn() as con:
                full = get_attachment(con, att["id"])
            if not full:
                continue
            fp = attachment_fs_path(full["stored_path"])
            if not fp.exists():
                logger.warning("attachment file missing: %s", fp)
                continue
            file_bytes = fp.read_bytes()
            r = await client.post(
                url,
                data={"chat_id": str(settings.chairman_chat_id)},
                files={"document": (full["file_name"], file_bytes, full.get("mime_type") or "application/octet-stream")},
            )
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError:
                logger.error("chairman file send failed: %s %s", r.status_code, r.text)
