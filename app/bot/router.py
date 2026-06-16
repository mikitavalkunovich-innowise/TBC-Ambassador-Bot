"""Assembles all bot routers into the Dispatcher."""
from aiogram import Dispatcher

from app.bot.handlers import (
    admin_notify,
    language,
    media,
    photo,
    privacy,
    start,
    subscription,
    utils,
)


def setup_dispatcher(dp: Dispatcher) -> None:
    """Register all routers in the correct priority order."""
    # Admin callbacks must be checked first (they have no FSM state guard)
    dp.include_router(admin_notify.router)

    # Utility commands (e.g. /myid) — before user-flow routers so they always respond
    dp.include_router(utils.router)

    # User flow routers
    dp.include_router(start.router)
    dp.include_router(language.router)
    dp.include_router(privacy.router)
    dp.include_router(media.router)
    dp.include_router(subscription.router)
    dp.include_router(photo.router)
