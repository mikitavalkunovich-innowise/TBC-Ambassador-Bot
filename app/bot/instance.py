"""
Singleton holder for the Bot and Dispatcher instances.
Initialized once during app startup from settings stored in the DB.
"""
import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_bot: Bot | None = None
_dp: Dispatcher | None = None


def get_bot() -> Bot:
    if _bot is None:
        raise RuntimeError("Bot has not been initialized. Set bot_token in admin panel and restart.")
    return _bot


def get_dp() -> Dispatcher:
    if _dp is None:
        raise RuntimeError("Dispatcher has not been initialized.")
    return _dp


def is_initialized() -> bool:
    return _bot is not None


async def initialize(token: str) -> tuple[Bot, Dispatcher]:
    """
    Create and configure the Bot and Dispatcher.
    Called once from app lifespan with the token from DB settings.
    The Dispatcher and its routers are module-level singletons — call this
    only once.  For subsequent token/webhook changes use reinit_bot_session().
    """
    global _bot, _dp

    from app.bot.router import setup_dispatcher
    from app.bot.middlewares.db_session import DBSessionMiddleware

    _bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    _dp = Dispatcher(storage=MemoryStorage())

    # Register the DB session middleware on all update types
    _dp.update.middleware(DBSessionMiddleware())

    setup_dispatcher(_dp)

    logger.info("Bot initialized successfully")
    return _bot, _dp


async def reinit_bot_session(token: str) -> Bot:
    """
    Replace only the Bot client with a new token, keeping the existing
    Dispatcher and all registered handlers intact.  Safe to call at runtime
    after the initial startup — avoids the 'Router already attached' error
    that would occur if we tried to rebuild the full Dispatcher again.
    """
    global _bot

    if _bot is not None:
        try:
            await _bot.session.close()
        except Exception:
            pass

    _bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    logger.info("Bot session reinitialized with new token")
    return _bot


async def set_user_fsm_state(user_telegram_id: int, state: State | None) -> None:
    """
    Set the FSM state for a user by their Telegram ID.
    Used when the bot sends a proactive message and needs to advance the user's state
    (e.g., admin approves an image and we offer the user a 'Generate new' button).
    """
    if _bot is None or _dp is None:
        return
    try:
        me = await _bot.get_me()
        key = StorageKey(
            bot_id=me.id,
            chat_id=user_telegram_id,
            user_id=user_telegram_id,
        )
        await _dp.fsm.storage.set_state(key=key, state=state)
    except Exception:
        logger.warning("Could not set FSM state for user %d", user_telegram_id)


async def shutdown() -> None:
    """Clean up bot resources."""
    global _bot, _dp
    if _bot is not None:
        await _bot.session.close()
        _bot = None
    _dp = None
