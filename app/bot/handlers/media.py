"""
Video and card promo delivery handlers.
Called after an image is approved to optionally show bonus content to the user.
"""
import logging
from pathlib import Path

from aiogram import Bot, Router
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.builders import card_promo_keyboard
from app.core.storage import get_absolute_path
from app.models.user import User
from app.services import settings_service

router = Router(name="media")
logger = logging.getLogger(__name__)

_RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
_BUNDLED_CARD_PROMO = _RESOURCES_DIR / "card_promo.webp"


def _format_tariff_link(tariff_url: str, lang: str) -> str:
    """Build an HTML anchor for the tariff PDF link."""
    if not tariff_url:
        return ""
    label = "Подробные условия" if lang == "ru" else "Batafsil shartlar"
    return f'<a href="{tariff_url}">{label}</a>'


def _resolve_card_promo_image_path(image_path_rel: str | None) -> Path | None:
    """Return the first existing path for the card promo image."""
    if image_path_rel:
        path = get_absolute_path(image_path_rel)
        if path.exists():
            return path
    bundled_dst = get_absolute_path("card_promo/card_promo.webp")
    if bundled_dst.exists():
        return bundled_dst
    if _BUNDLED_CARD_PROMO.exists():
        return _BUNDLED_CARD_PROMO
    return None


async def send_card_promo_after_result(
    bot: Bot,
    chat_id: int,
    user: User,
    session: AsyncSession,
) -> None:
    """Send the TBC Salom Visa card promo (photo + caption + order button)."""
    try:
        if await settings_service.get(session, "card_promo_enabled") != "1":
            return

        lang = user.language.value if user.language else "ru"
        caption_raw = await settings_service.get_text(session, "msg_card_promo", lang)
        if not caption_raw:
            return

        tariff_url = await settings_service.get(session, "card_promo_tariff_url") or ""
        tariff_link = _format_tariff_link(tariff_url, lang)
        caption = caption_raw.replace("{tariff_link}", tariff_link)

        order_url = await settings_service.get(session, "card_promo_order_url") or ""
        if not order_url:
            logger.warning("card_promo_order_url is not configured — skipping card promo")
            return

        button_label = await settings_service.get_text(session, "btn_card_promo", lang)
        if not button_label:
            button_label = "Заказать карту" if lang == "ru" else "Kartaga buyurtma berish"

        image_path_rel = await settings_service.get(session, "card_promo_image_path")
        image_path = _resolve_card_promo_image_path(image_path_rel)
        if image_path is None:
            logger.warning("Card promo image not found — skipping card promo")
            return

        await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile(str(image_path)),
            caption=caption,
            reply_markup=card_promo_keyboard(order_url, button_label),
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
