"""Card promo delivery, click tracking, broadcast, and statistics."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.bot.keyboards.builders import card_promo_keyboard
from app.core.config import get_settings
from app.core.storage import get_absolute_path
from app.models.blocked_user import BlockedUser
from app.models.card_promo_delivery import CardPromoDelivery, CardPromoSource
from app.models.user import Language, User
from app.services import settings_service

logger = logging.getLogger(__name__)

_RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
_BUNDLED_CARD_PROMO = _RESOURCES_DIR / "card_promo.webp"

_BROADCAST_DELAY_SEC = 0.04  # ~25 messages/sec


class UserBlockedBotError(Exception):
    """Raised when Telegram reports the user has blocked the bot."""


@dataclass
class SourceStats:
    sent: int
    clicked: int

    @property
    def ctr(self) -> float:
        if self.sent == 0:
            return 0.0
        return round(100.0 * self.clicked / self.sent, 1)


@dataclass
class PromoStats:
    flow: SourceStats
    broadcast: SourceStats

    @property
    def total(self) -> SourceStats:
        return SourceStats(
            sent=self.flow.sent + self.broadcast.sent,
            clicked=self.flow.clicked + self.broadcast.clicked,
        )


def format_tariff_link(tariff_url: str, lang: str) -> str:
    """Build an HTML anchor for the tariff PDF link."""
    if not tariff_url:
        return ""
    label = "Подробные условия" if lang == "ru" else "Batafsil shartlar"
    return f'<a href="{tariff_url}">{label}</a>'


async def get_tariff_url_for_lang(session: AsyncSession, lang: str) -> str:
    """Return the tariff PDF URL for the given promo language."""
    if lang == "uz":
        uz_url = await settings_service.get(session, "card_promo_tariff_url_uz") or ""
        if uz_url:
            return uz_url
    return await settings_service.get(session, "card_promo_tariff_url") or ""


def resolve_card_promo_image_path(image_path_rel: str | None) -> Path | None:
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


def build_tracking_url(delivery_id: str) -> str:
    """Build the redirect URL used for order-button click tracking."""
    base = get_settings().webhook_base_url.rstrip("/")
    return f"{base}/r/card-order/{delivery_id}"


async def create_delivery(
    session: AsyncSession,
    user_id: int,
    source: str,
    language: str,
) -> CardPromoDelivery:
    """Create a delivery record before sending the promo message."""
    delivery = CardPromoDelivery(
        user_id=user_id,
        source=source,
        language=language,
    )
    session.add(delivery)
    await session.flush()
    return delivery


async def send_card_promo_to_user(
    bot: Bot,
    user: User,
    session: AsyncSession,
    *,
    source: str,
    language_override: str | None = None,
) -> CardPromoDelivery | None:
    """
    Send the card promo photo message to one user and record delivery.

    Returns the delivery record on success, None if skipped or failed.
    """
    if await settings_service.get(session, "card_promo_enabled") != "1":
        return None

    lang = language_override or (user.language.value if user.language else "ru")
    caption_raw = await settings_service.get_text(session, "msg_card_promo", lang)
    if not caption_raw:
        return None

    tariff_url = await get_tariff_url_for_lang(session, lang)
    caption = caption_raw.replace("{tariff_link}", format_tariff_link(tariff_url, lang))

    order_url = await settings_service.get(session, "card_promo_order_url") or ""
    if not order_url:
        logger.warning("card_promo_order_url is not configured — skipping card promo")
        return None

    button_label = await settings_service.get_text(session, "btn_card_promo", lang)
    if not button_label:
        button_label = "Заказать карту" if lang == "ru" else "Kartaga buyurtma berish"

    image_path_rel = await settings_service.get(session, "card_promo_image_path")
    image_path = resolve_card_promo_image_path(image_path_rel)
    if image_path is None:
        logger.warning("Card promo image not found — skipping card promo")
        return None

    delivery = await create_delivery(session, user.id, source, lang)
    if await settings_service.get(session, "card_promo_click_tracking_enabled", "1") == "1":
        button_url = build_tracking_url(delivery.id)
    else:
        button_url = order_url

    try:
        await bot.send_photo(
            chat_id=user.telegram_id,
            photo=FSInputFile(str(image_path)),
            caption=caption,
            reply_markup=card_promo_keyboard(button_url, button_label),
        )
    except TelegramForbiddenError:
        await session.delete(delivery)
        await mark_user_bot_blocked(session, user.id)
        await session.flush()
        raise UserBlockedBotError from None
    except Exception:
        await session.delete(delivery)
        await session.flush()
        raise
    return delivery


async def mark_user_bot_blocked(session: AsyncSession, user_id: int) -> None:
    """Record that the user blocked the bot (idempotent)."""
    db_user = await session.get(User, user_id)
    if db_user is not None and db_user.bot_blocked_at is None:
        db_user.bot_blocked_at = datetime.now(timezone.utc)
        await session.flush()


async def clear_user_bot_blocked(session: AsyncSession, user: User) -> None:
    """Clear bot block flag when the user returns via /start."""
    if user.bot_blocked_at is not None:
        user.bot_blocked_at = None
        await session.flush()


async def count_bot_blocked_users(session: AsyncSession) -> int:
    """Count users who blocked the bot in Telegram."""
    result = await session.execute(
        select(func.count(User.id)).where(User.bot_blocked_at.isnot(None))
    )
    return result.scalar_one()


async def record_click(session: AsyncSession, delivery_id: str) -> None:
    """Record the first click on an order button (idempotent)."""
    delivery = await session.get(CardPromoDelivery, delivery_id)
    if delivery is not None and delivery.clicked_at is None:
        delivery.clicked_at = datetime.now(timezone.utc)
        await session.commit()


async def get_order_redirect_url(session: AsyncSession) -> str:
    """Return the configured bank app order URL (fallback if empty)."""
    url = await settings_service.get(session, "card_promo_order_url") or ""
    return url or "https://app.tbcbank.uz/SfqR/hzztbuhk"


async def _count_for_source(
    session: AsyncSession,
    source: str,
    *,
    clicked_only: bool = False,
) -> int:
    query = select(func.count(CardPromoDelivery.id)).where(
        CardPromoDelivery.source == source
    )
    if clicked_only:
        query = query.where(CardPromoDelivery.clicked_at.isnot(None))
    return (await session.execute(query)).scalar_one()


async def get_stats(session: AsyncSession) -> PromoStats:
    """Aggregate delivery and click counts by source."""
    flow_sent = await _count_for_source(session, CardPromoSource.FLOW)
    flow_clicked = await _count_for_source(session, CardPromoSource.FLOW, clicked_only=True)
    bc_sent = await _count_for_source(session, CardPromoSource.BROADCAST)
    bc_clicked = await _count_for_source(session, CardPromoSource.BROADCAST, clicked_only=True)
    return PromoStats(
        flow=SourceStats(sent=flow_sent, clicked=flow_clicked),
        broadcast=SourceStats(sent=bc_sent, clicked=bc_clicked),
    )


async def get_recent_deliveries(
    session: AsyncSession,
    limit: int = 50,
) -> list[CardPromoDelivery]:
    """Return recent deliveries with user loaded."""
    result = await session.execute(
        select(CardPromoDelivery)
        .options(selectinload(CardPromoDelivery.user))
        .order_by(CardPromoDelivery.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _blocked_usernames(session: AsyncSession) -> set[str]:
    result = await session.execute(select(BlockedUser.username))
    return {row[0] for row in result.all()}


async def get_user_by_telegram_id(
    session: AsyncSession,
    telegram_id: int,
) -> User | None:
    """Return a user by Telegram ID, or None if not registered."""
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def send_card_promo_test(
    bot: Bot,
    session: AsyncSession,
    telegram_id: int,
    language: str,
) -> CardPromoDelivery | None:
    """
    Send a single test promo to one user with an explicit language variant.

    Raises ValueError if the user is not registered in the bot.
    """
    user = await get_user_by_telegram_id(session, telegram_id)
    if user is None:
        raise ValueError("user_not_found")

    if await settings_service.get(session, "card_promo_enabled") != "1":
        raise ValueError("promo_disabled")

    try:
        delivery = await send_card_promo_to_user(
            bot,
            user,
            session,
            source=CardPromoSource.TEST,
            language_override=language,
        )
    except UserBlockedBotError:
        await session.commit()
        raise ValueError("user_blocked_bot") from None
    if delivery is None:
        raise ValueError("promo_not_configured")
    return delivery


def _is_user_blocked(user: User, blocked: set[str]) -> bool:
    if not user.telegram_username:
        return False
    normalized = user.telegram_username.lower().lstrip("@")
    return normalized in blocked


async def _delivered_user_ids_for_language(
    session: AsyncSession,
    language: str,
) -> set[int]:
    """User IDs who already received a card promo in the given language."""
    result = await session.execute(
        select(CardPromoDelivery.user_id).where(
            CardPromoDelivery.language == language,
        )
    )
    return {row[0] for row in result.all()}


async def get_broadcast_recipients(
    session: AsyncSession,
    language: str,
    *,
    missed_only: bool = False,
) -> list[User]:
    """Return users eligible for a language-specific broadcast."""
    lang_enum = Language.RU if language == "ru" else Language.UZ
    blocked = await _blocked_usernames(session)
    delivered = await _delivered_user_ids_for_language(session, language) if missed_only else set()

    result = await session.execute(select(User).where(User.language == lang_enum))
    users: list[User] = []
    for user in result.scalars().all():
        if user.bot_blocked_at is not None:
            continue
        if _is_user_blocked(user, blocked):
            continue
        if missed_only and user.id in delivered:
            continue
        users.append(user)
    return users


async def count_broadcast_recipients(
    session: AsyncSession,
    language: str,
    *,
    missed_only: bool = False,
) -> int:
    """Count users eligible for a language-specific broadcast."""
    return len(await get_broadcast_recipients(session, language, missed_only=missed_only))


async def broadcast_card_promo(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    language: str,
    *,
    missed_only: bool = False,
) -> None:
    """
    Send card promo to users with the given language in the background.

    When missed_only is True, skips users who already have any card promo delivery
    recorded for that language (flow, broadcast, or test).

    Uses its own DB sessions per batch; does not touch user FSM state.
    """
    sent = 0
    failed = 0
    blocked = 0
    skipped = 0

    async with session_factory() as session:
        users = await get_broadcast_recipients(session, language, missed_only=missed_only)

    for user in users:
        try:
            async with session_factory() as session:
                try:
                    delivery = await send_card_promo_to_user(
                        bot,
                        user,
                        session,
                        source=CardPromoSource.BROADCAST,
                    )
                except UserBlockedBotError:
                    await session.commit()
                    blocked += 1
                    logger.warning(
                        "User blocked bot during card promo broadcast, telegram_id=%d",
                        user.telegram_id,
                    )
                    continue
                if delivery is None:
                    skipped += 1
                else:
                    await session.commit()
                    sent += 1
        except Exception:
            logger.exception(
                "Card promo broadcast failed for user telegram_id=%d",
                user.telegram_id,
            )
            failed += 1

        await asyncio.sleep(_BROADCAST_DELAY_SEC)

    mode = "missed-only" if missed_only else "all"
    logger.info(
        "Card promo broadcast (%s, %s) finished: sent=%d blocked=%d failed=%d skipped=%d",
        language,
        mode,
        sent,
        blocked,
        failed,
        skipped,
    )
