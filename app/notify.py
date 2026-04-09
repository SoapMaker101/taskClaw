import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def notify_chairman(text: str) -> None:
    """Outgoing sendMessage only — does not call getUpdates (OpenClaw keeps polling)."""
    url = f"https://api.telegram.org/bot{settings.chairman_bot_token}/sendMessage"
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
