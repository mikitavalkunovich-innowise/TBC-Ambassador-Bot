"""
Server-side proxy for Telegram-hosted images in the admin panel.

Fetches image bytes from Telegram using getFile (which requires the bot token),
then streams them back to the browser. The bot token never appears in HTML.

GET /admin/media/telegram/{image_id}?kind=generated|user_photo
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import get_current_admin
from app.core.database import get_db_session
from app.models.generation import GeneratedImage

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/media/telegram/{image_id}", response_model=None)
async def proxy_telegram_image(
    image_id: str,
    kind: str = "generated",
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> Response:
    """
    Proxy an image from Telegram servers through our backend.

    kind: "generated" → telegram_image_file_id
          "user_photo" → telegram_user_photo_file_id
    """
    result = await session.execute(
        select(GeneratedImage).where(GeneratedImage.id == image_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Image record not found")

    file_id = (
        record.telegram_image_file_id
        if kind == "generated"
        else record.telegram_user_photo_file_id
    )
    if not file_id:
        raise HTTPException(status_code=404, detail="No Telegram file_id for this image")

    try:
        from app.bot.instance import get_bot, is_initialized
        if not is_initialized():
            raise HTTPException(status_code=503, detail="Bot not initialized")
        bot = get_bot()
        tg_file = await bot.get_file(file_id)
        if not tg_file.file_path:
            raise HTTPException(status_code=502, detail="Telegram did not return file_path")
        url = f"https://api.telegram.org/file/bot{bot.token}/{tg_file.file_path}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/webp")
            return Response(content=resp.content, media_type=content_type)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to proxy Telegram image for record %s", image_id)
        raise HTTPException(status_code=502, detail="Could not retrieve image from Telegram")
