"""
Telegram notification service for admin moderation alerts.
Sends generated images to the admin with inline Approve/Reject buttons.
"""
import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.core.config import get_settings

logger = logging.getLogger(__name__)

APPROVE_CB = "mod_approve"
REJECT_CB = "mod_reject"


def _build_approval_keyboard(image_id: str, admin_base_url: str) -> InlineKeyboardMarkup:
    admin_link = f"{admin_base_url.rstrip('/')}/admin/moderation"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"{APPROVE_CB}:{image_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"{REJECT_CB}:{image_id}"),
            ],
            [
                InlineKeyboardButton(text="🔗 Open Admin Panel", url=admin_link),
            ],
        ]
    )


async def notify_new_image(
    bot: Bot,
    admin_chat_id: int,
    image_id: str,
    image_bytes: bytes,
    telegram_id: int,
    telegram_username: str | None,
    language: str,
    attempt_number: int,
    max_attempts: int,
    admin_base_url: str,
) -> tuple[int, int, str | None]:
    """
    Send the generated image to the admin for moderation.

    Returns:
        (message_id, chat_id, photo_file_id) — photo_file_id can be saved to
        avoid keeping the image on local disk after confirmation.
    """
    username_display = f"@{telegram_username}" if telegram_username else f"ID: {telegram_id}"
    caption = (
        f"🖼 <b>New image awaiting approval</b>\n\n"
        f"User: {username_display} (<code>{telegram_id}</code>)\n"
        f"Language: {language.upper()}\n"
        f"Attempt: {attempt_number}/{max_attempts}"
    )

    keyboard = _build_approval_keyboard(image_id, admin_base_url)
    filename = f"generated_{image_id[:8]}.jpg"

    msg = await bot.send_photo(
        chat_id=admin_chat_id,
        photo=BufferedInputFile(image_bytes, filename=filename),
        caption=caption,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    photo_file_id = msg.photo[-1].file_id if msg.photo else None
    return msg.message_id, msg.chat.id, photo_file_id


async def edit_approval_message_approved(
    bot: Bot,
    chat_id: int,
    message_id: int,
    telegram_username: str | None,
    telegram_id: int,
    admin_base_url: str,
) -> None:
    """Edit the admin approval message to show it was approved."""
    username_display = f"@{telegram_username}" if telegram_username else f"ID: {telegram_id}"
    admin_link = f"{admin_base_url.rstrip('/')}/admin/moderation"
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=f"✅ <b>Approved</b> — {username_display}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔗 Admin Panel", url=admin_link)]
                ]
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Could not edit approval message %d in chat %d", message_id, chat_id)


async def edit_approval_message_rejected(
    bot: Bot,
    chat_id: int,
    message_id: int,
    telegram_username: str | None,
    telegram_id: int,
    admin_base_url: str,
) -> None:
    """Edit the admin approval message to show it was rejected."""
    username_display = f"@{telegram_username}" if telegram_username else f"ID: {telegram_id}"
    admin_link = f"{admin_base_url.rstrip('/')}/admin/moderation"
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=f"❌ <b>Rejected</b> — {username_display}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔗 Admin Panel", url=admin_link)]
                ]
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Could not edit rejection message %d in chat %d", message_id, chat_id)


async def notify_budget_exceeded(bot: Bot, admin_chat_id: int, budget_limit: float, spent: float) -> None:
    """Alert the admin that the budget limit has been reached."""
    try:
        await bot.send_message(
            chat_id=admin_chat_id,
            text=(
                f"⚠️ <b>Budget limit reached!</b>\n\n"
                f"Limit: <b>${budget_limit:.2f}</b>\n"
                f"Spent: <b>${spent:.4f}</b>\n\n"
                f"New image generations are suspended. "
                f"Increase the limit in the admin panel to resume."
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Could not send budget exceeded notification to admin %d", admin_chat_id)
