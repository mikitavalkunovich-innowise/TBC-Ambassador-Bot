"""
Video and card promo delivery handlers.
Called after an image is approved to optionally show bonus content to the user.
"""
import logging

from aiogram import Bot, Router
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import get_absolute_path
from app.models.card_promo_delivery import CardPromoSource
from app.models.user import User
from app.services import settings_service
from app.services.card_promo_service import send_card_promo_to_user

router = Router(name="media")
logger = logging.getLogger(__name__)


async def send_card_promo_after_result(
    bot: Bot,
    chat_id: int,
    user: User,
    session: AsyncSession,
) -> None:
    """Send the TBC Salom Visa card promo after an approved photo (normal bot flow)."""
    try:
        await send_card_promo_to_user(
            bot,
            user,
            session,
            source=CardPromoSource.FLOW,
        )
    except Exception:
        logger.exception("Failed to send card promo to user %d", chat_id)


async def send_video_after_result(
    bot: Bot,
    chat_id: int,
    user: User,
    session: AsyncSession,
) -> None:
    """Send the bonus Eldor video after an image has been approved."""
    lang = user.language.value if user.language else "ru"

    video_url = await settings_service.get(session, f"video_url_{lang}")
    video_path_rel = await settings_service.get(session, f"video_path_{lang}")
    video_text = await settings_service.get_text(session, "msg_video", lang)

    if video_url:
        await bot.send_video(chat_id=chat_id, video=video_url, caption=video_text)
    elif video_path_rel:
        video_path = get_absolute_path(video_path_rel)
        if video_path.exists():
            from aiogram.types import FSInputFile
            await bot.send_video(
                chat_id=chat_id,
                video=FSInputFile(str(video_path)),
                caption=video_text,
            )
        else:
            logger.warning("Video file not found: %s", video_path)
            await bot.send_message(chat_id=chat_id, text=video_text)
    else:
        await bot.send_message(chat_id=chat_id, text=video_text)
