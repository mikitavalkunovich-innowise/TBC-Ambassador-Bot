"""Moderation queue routes — approve/reject generated images."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.auth import get_current_admin
from app.core.database import get_db_session
from app.core.storage import get_absolute_path, relative_to_url
from app.models.event import EventType
from app.models.generation import GeneratedImage, ImageStatus
from app.models.user import FlowStatus, User
from app.services import settings_service
from app.services.analytics_service import track_event
from app.services.notify_service import (
    edit_approval_message_approved,
    edit_approval_message_rejected,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/admin/templates")
logger = logging.getLogger(__name__)


@router.get("/", response_class=HTMLResponse, response_model=None)
async def moderation_list(
    request: Request,
    status_filter: str = "pending",
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> HTMLResponse:
    query = (
        select(GeneratedImage)
        .options(selectinload(GeneratedImage.user))
        .order_by(GeneratedImage.created_at.desc())
    )
    if status_filter in ("pending", "approved", "rejected"):
        query = query.where(GeneratedImage.status == ImageStatus(status_filter))

    rows = (await session.execute(query)).scalars().all()

    def _image_url(img: GeneratedImage, kind: str) -> str | None:
        file_id = (
            img.telegram_image_file_id if kind == "generated" else img.telegram_user_photo_file_id
        )
        local_path = img.image_path if kind == "generated" else img.user_photo_path
        if file_id:
            return f"/admin/media/telegram/{img.id}?kind={kind}"
        if local_path:
            abs_path = get_absolute_path(local_path)
            if abs_path.exists():
                return relative_to_url(local_path)
        return None  # template shows "in Telegram" placeholder

    items = [
        {
            "id": img.id,
            "status": img.status.value,
            "attempt_number": img.attempt_number,
            "created_at": img.created_at,
            "reviewed_at": img.reviewed_at,
            "image_url": _image_url(img, "generated"),
            "user_photo_url": _image_url(img, "user_photo"),
            "has_telegram_backup": bool(img.telegram_image_file_id),
            "cost_usd": float(img.cost_usd) if img.cost_usd else None,
            "user_prompt_extra": img.user_prompt_extra,
            "user": {
                "telegram_id": img.user.telegram_id,
                "telegram_username": img.user.telegram_username,
                "language": img.user.language.value if img.user.language else "?",
            } if img.user else {},
        }
        for img in rows
    ]

    pending_count = sum(1 for i in items if i["status"] == "pending") if status_filter != "pending" else None

    return templates.TemplateResponse(
        "moderation.html",
        {
            "request": request,
            "items": items,
            "status_filter": status_filter,
            "active_page": "moderation",
            "pending_count": pending_count,
        },
    )


@router.post("/{image_id}/approve", response_class=RedirectResponse, response_model=None)
async def approve_image(
    image_id: str,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    image = await session.get(GeneratedImage, image_id)
    if image and image.status == ImageStatus.PENDING:
        image.status = ImageStatus.APPROVED
        image.reviewed_at = datetime.now(timezone.utc)
        await session.flush()

        user = await session.get(User, image.user_id)
        if user:
            await track_event(session, user.id, EventType.IMAGE_APPROVED)
            await _notify_user_approved(image, user, session)

    return RedirectResponse("/admin/moderation?status_filter=pending", status_code=303)


@router.post("/{image_id}/reject", response_class=RedirectResponse, response_model=None)
async def reject_image(
    image_id: str,
    session: AsyncSession = Depends(get_db_session),
    _admin: str = Depends(get_current_admin),
) -> RedirectResponse:
    image = await session.get(GeneratedImage, image_id)
    if image and image.status == ImageStatus.PENDING:
        image.status = ImageStatus.REJECTED
        image.reviewed_at = datetime.now(timezone.utc)
        await session.flush()

        user = await session.get(User, image.user_id)
        if user:
            await track_event(session, user.id, EventType.IMAGE_REJECTED)
            await _notify_user_rejected(image, user, session)

    return RedirectResponse("/admin/moderation?status_filter=pending", status_code=303)


async def _notify_user_approved(image: GeneratedImage, user: User, session: AsyncSession) -> None:
    """Send approved image to user and optionally edit the admin TG message."""
    try:
        from app.bot.instance import get_bot, is_initialized
        if not is_initialized():
            return

        bot = get_bot()
        lang = user.language.value if user.language else "ru"
        approved_text = await settings_service.get_text(session, "msg_approved", lang)

        sent_photo = False
        # Priority 1: resend via Telegram file_id
        if image.telegram_image_file_id:
            try:
                await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=image.telegram_image_file_id,
                    caption=approved_text,
                )
                sent_photo = True
            except Exception:
                logger.warning("file_id send failed for image %s, trying local file", image.id)
        # Priority 2: local file
        if not sent_photo and image.image_path:
            from app.core.storage import get_absolute_path
            from aiogram.types import FSInputFile
            img_path = get_absolute_path(image.image_path)
            if img_path.exists():
                await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=FSInputFile(str(img_path)),
                    caption=approved_text,
                )
                sent_photo = True
        if not sent_photo:
            await bot.send_message(chat_id=user.telegram_id, text=approved_text)

        # Optionally send the bonus Eldor video after the approved image
        video_enabled = await settings_service.get(session, "video_enabled") == "1"
        if video_enabled:
            try:
                from app.bot.handlers.media import send_video_after_result
                await send_video_after_result(bot, user.telegram_id, user, session)
            except Exception:
                logger.exception("Failed to send bonus video to user %d", user.telegram_id)

        # Offer regeneration if attempts remain
        max_attempts = int(await settings_service.get(session, "max_regeneration_attempts") or "3")
        attempts_remaining = max_attempts - user.regenerations_used
        user.flow_status = FlowStatus.DONE
        await session.flush()
        if attempts_remaining > 0:
            from app.bot.instance import set_user_fsm_state
            from app.bot.keyboards.builders import regenerate_keyboard
            from app.bot.states import UserFlow

            regen_key = "msg_regenerate_1left" if attempts_remaining == 1 else "msg_regenerate_2left"
            text = await settings_service.get_text(session, regen_key, lang)
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=regenerate_keyboard(lang),
            )
            await set_user_fsm_state(user.telegram_id, UserFlow.awaiting_regeneration_input)
        else:
            from app.bot.keyboards.builders import share_bot_keyboard
            no_attempts_text = await settings_service.get_text(session, "msg_no_attempts_left", lang)
            bot_username = await settings_service.get(session, "bot_username") or ""
            markup = share_bot_keyboard(lang, bot_username) if bot_username else None
            await bot.send_message(chat_id=user.telegram_id, text=no_attempts_text, reply_markup=markup)

        from app.core.config import get_settings
        config = get_settings()
        if image.admin_tg_chat_id and image.admin_tg_message_id:
            await edit_approval_message_approved(
                bot=bot,
                chat_id=image.admin_tg_chat_id,
                message_id=image.admin_tg_message_id,
                telegram_username=user.telegram_username,
                telegram_id=user.telegram_id,
                admin_base_url=config.webhook_base_url,
            )
    except Exception:
        logger.exception("Failed to notify user %d of approval", user.telegram_id)


async def _notify_user_rejected(image: GeneratedImage, user: User, session: AsyncSession) -> None:
    """Send rejection message to user and optionally edit the admin TG message."""
    try:
        from app.bot.instance import get_bot, is_initialized
        if not is_initialized():
            return

        bot = get_bot()
        lang = user.language.value if user.language else "ru"
        rejection_text = await settings_service.get_text(session, "rejection_message", lang)
        await bot.send_message(chat_id=user.telegram_id, text=rejection_text)

        max_attempts = int(await settings_service.get(session, "max_regeneration_attempts") or "3")
        attempts_remaining = max_attempts - user.regenerations_used
        user.flow_status = FlowStatus.DONE
        await session.flush()
        if attempts_remaining > 0:
            from app.bot.instance import set_user_fsm_state
            from app.bot.keyboards.builders import regenerate_keyboard
            from app.bot.states import UserFlow

            regen_key = "msg_regenerate_1left" if attempts_remaining == 1 else "msg_regenerate_2left"
            text = await settings_service.get_text(session, regen_key, lang)
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=regenerate_keyboard(lang),
            )
            await set_user_fsm_state(user.telegram_id, UserFlow.awaiting_regeneration_input)
        else:
            from app.bot.keyboards.builders import share_bot_keyboard
            no_attempts_text = await settings_service.get_text(session, "msg_no_attempts_left", lang)
            bot_username = await settings_service.get(session, "bot_username") or ""
            markup = share_bot_keyboard(lang, bot_username) if bot_username else None
            await bot.send_message(chat_id=user.telegram_id, text=no_attempts_text, reply_markup=markup)

        from app.core.config import get_settings
        config = get_settings()
        if image.admin_tg_chat_id and image.admin_tg_message_id:
            await edit_approval_message_rejected(
                bot=bot,
                chat_id=image.admin_tg_chat_id,
                message_id=image.admin_tg_message_id,
                telegram_username=user.telegram_username,
                telegram_id=user.telegram_id,
                admin_base_url=config.webhook_base_url,
            )
    except Exception:
        logger.exception("Failed to notify user %d of rejection", user.telegram_id)
