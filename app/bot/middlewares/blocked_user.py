"""Middleware that silently drops all updates from blocked Telegram users."""
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.services.blocked_service import is_blocked

logger = logging.getLogger(__name__)


class BlockedUserMiddleware(BaseMiddleware):
    """
    Silently ignore any update originating from a blocked username.

    Runs after DBSessionMiddleware so that data["session"] is already available.
    Wrapped in try/except: if the DB check fails for any reason (e.g. table not
    yet created during a rolling deploy), the user is let through — fail-open.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            user = data.get("event_from_user")
            if user and user.username:
                session = data.get("session")
                if session and await is_blocked(session, user.username):
                    logger.debug(
                        "Blocked user @%s — update silently dropped", user.username
                    )
                    return None
        except Exception:
            logger.exception("BlockedUserMiddleware check failed — letting user through")

        return await handler(event, data)
