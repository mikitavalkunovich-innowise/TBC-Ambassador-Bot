"""
Singleton holder for the Bot and Dispatcher instances.
Initialized once during app startup from settings stored in the DB.
"""
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

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


async def shutdown() -> None:
    """Clean up bot resources."""
    global _bot, _dp
    if _bot is not None:
        await _bot.session.close()
        _bot = None
    _dp = None
