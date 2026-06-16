"""
Utility commands for bot admins and setup helpers.
These commands are available to anyone but are mainly useful during configuration.
"""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="utils")
logger = logging.getLogger(__name__)


@router.message(Command("myid"))
async def handle_myid(message: Message) -> None:
    """
    Reply with the sender's numeric Telegram user ID.
    Useful for admins who need to enter their ID in the admin panel.
    """
    user = message.from_user
    if user is None:
        return

    username_line = f"Username: @{user.username}\n" if user.username else ""
    full_name = user.full_name or ""

    await message.answer(
        f"<b>Your Telegram ID</b>\n\n"
        f"{username_line}"
        f"Name: {full_name}\n"
        f"<b>ID: <code>{user.id}</code></b>\n\n"
        f"Copy the number above and paste it into the <i>Admin Telegram User ID</i> "
        f"field in the admin panel.",
        parse_mode="HTML",
    )
