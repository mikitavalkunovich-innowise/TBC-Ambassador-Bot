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
from app.core.storage import relative_to_url
from app.models.event import EventType
from app.models.generation import GeneratedImage, ImageStatus
from app.models.user import User
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

    items = [
        {
            "id": img.id,
            "status": img.status.value,
            "attempt_number": img.attempt_number,
            "created_at": img.created_at,
            "reviewed_at": img.reviewed_at,
            "image_url": relative_to_url(img.image_path) if img.image_path else None,
            "user_photo_url": relative_to_url(img.user_photo_path) if img.user_photo_path else None,
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

        if image.image_path:
            from app.core.storage import get_absolute_path
            from aiogram.types import FSInputFile
            img_path = get_absolute_path(image.image_path)
            if img_path.exists():
                await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=FSInputFile(str(img_path)),
                    caption=approved_text,
                )

        # Offer regeneration if attempts remain
        max_attempts = int(await settings_service.get(session, "max_regeneration_attempts") or "3")
        attempts_remaining = max_attempts - user.regenerations_used
        if attempts_remaining > 0:
            from app.bot.instance import set_user_fsm_state
            from app.bot.keyboards.builders import regenerate_keyboard
            from app.bot.states import UserFlow
            from app.models.user import FlowStatus

            text = await settings_service.get_text(session, "msg_regenerate_prompt", lang)
            user.flow_status = FlowStatus.DONE
            await session.flush()
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=regenerate_keyboard(lang),
            )
            await set_user_fsm_state(user.telegram_id, UserFlow.awaiting_regeneration_input)

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
        if attempts_remaining > 0:
            from app.bot.instance import set_user_fsm_state
            from app.bot.keyboards.builders import regenerate_keyboard
            from app.bot.states import UserFlow
            from app.models.user import FlowStatus

            text = await settings_service.get_text(session, "msg_regenerate_prompt", lang)
            user.flow_status = FlowStatus.DONE
            await session.flush()
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=regenerate_keyboard(lang),
            )
            await set_user_fsm_state(user.telegram_id, UserFlow.awaiting_regeneration_input)

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
