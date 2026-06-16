"""
Service for managing bot settings stored in the database.
Provides a simple cache with TTL to avoid repeated DB queries.
"""
import time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import BotSetting

_CACHE: dict[str, tuple[str | None, float]] = {}
_CACHE_TTL = 60.0  # seconds


DEFAULT_SETTINGS: dict[str, str] = {
    # Bot infrastructure
    "bot_token": "",
    "telegram_channel_id": "",
    "admin_telegram_user_id": "",
    "privacy_policy_url": "https://example.com/privacy",
    # Feature flags
    "channel_check_enabled": "1",  # "1" = enabled, "0" = disabled
    # Limits
    "max_regeneration_attempts": "3",
    "budget_limit_usd": "100.00",
    "budget_spent_usd": "0.000000",
    # Media paths (populated via admin panel file upload)
    "ambassador_photo_path": "",
    "logo_path": "",
    "video_url_ru": "",
    "video_url_uz": "",
    "video_path_ru": "",
    "video_path_uz": "",
    # Instagram frame templates (portrait format).
    # Default values point to the bundled static frames shipped with the app.
    # Admins can override by uploading custom frames via the Media settings tab.
    "frame_path_ru": "frame_ru.png",
    "frame_path_uz": "frame_uz.png",
    # Image generation prompt template
    "generation_prompt": (
        "Create a natural, realistic photo that looks like it was taken with an iPhone 16. "
        "The photo shows two people standing together on a football field background, taking a photo together: "
        "Person 1 is from the first reference photo provided, "
        "Person 2 is from the second reference photo provided. "
        "CRITICAL: Preserve EXACTLY the facial features, skin tone, hair color, body type, and clothing "
        "of BOTH people exactly as shown in the reference photos. Do NOT alter, idealize, or change "
        "any physical characteristics. The lighting should be natural outdoor lighting. "
        "The photo quality should be natural and candid (not studio-like). "
        "{extra}"
    ),
    # --- Russian messages ---
    "msg_welcome_ru": "Добро пожаловать! 👋",
    "msg_select_language_ru": "Выберите язык / Tilni tanlang:",
    "msg_privacy_ru": (
        "Пожалуйста, ознакомьтесь с нашей политикой конфиденциальности:\n"
        "{privacy_url}\n\n"
        "Нажмите «Согласен», чтобы продолжить."
    ),
    "msg_video_ru": "Посмотрите это видео перед тем, как сгенерировать ваше фото! 🎬",
    "msg_subscribe_ru": (
        "Подпишитесь на наш Telegram-канал, чтобы продолжить:\n"
        "{channel_link}\n\n"
        "После подписки нажмите кнопку «Я подписался»."
    ),
    "msg_not_subscribed_ru": (
        "❌ Вы ещё не подписались на канал. "
        "Подпишитесь и нажмите кнопку ещё раз."
    ),
    "msg_send_photo_ru": "📸 Отправьте ваше селфи в хорошем качестве для генерации совместного фото.",
    "msg_invalid_photo_ru": "Пожалуйста, отправьте фотографию.",
    "msg_generating_ru": "⏳ Генерируем ваше фото… Пожалуйста, подождите.",
    "msg_pending_review_ru": "✅ Ваше фото отправлено на проверку. Мы уведомим вас о результате.",
    "msg_approved_ru": "🎉 Ваше фото одобрено! Вот ваш результат:",
    "msg_regenerate_prompt_ru": (
        "Хотите сгенерировать новое фото? Опишите, что хотите изменить, "
        "или отправьте новое селфи."
    ),
    "msg_already_participated_ru": "Вы уже приняли участие. Спасибо! 🙌",
    "msg_no_attempts_left_ru": "Вы использовали все попытки генерации. Спасибо за участие!",
    # --- Uzbek messages ---
    "msg_welcome_uz": "Xush kelibsiz! 👋",
    "msg_select_language_uz": "Выберите язык / Tilni tanlang:",
    "msg_privacy_uz": (
        "Iltimos, maxfiylik siyosatimizni ko'rib chiqing:\n"
        "{privacy_url}\n\n"
        "Davom etish uchun «Roziman» tugmasini bosing."
    ),
    "msg_video_uz": "Rasmingizni yaratishdan oldin ushbu videoni tomosha qiling! 🎬",
    "msg_subscribe_uz": (
        "Davom etish uchun Telegram kanalimizga obuna bo'ling:\n"
        "{channel_link}\n\n"
        "Obuna bo'lgandan so'ng «Obuna bo'ldim» tugmasini bosing."
    ),
    "msg_not_subscribed_uz": (
        "❌ Siz hali kanalga obuna bo'lmadingiz. "
        "Obuna bo'ling va tugmani qayta bosing."
    ),
    "msg_send_photo_uz": "📸 Birgalikdagi surat yaratish uchun yaxshi sifatli selfi yuboring.",
    "msg_invalid_photo_uz": "Iltimos, rasm yuboring.",
    "msg_generating_uz": "⏳ Rasmingiz yaratilmoqda… Iltimos, kuting.",
    "msg_pending_review_uz": "✅ Rasmingiz tekshirish uchun yuborildi. Natija haqida xabardor qilamiz.",
    "msg_approved_uz": "🎉 Rasmingiz tasdiqlandi! Mana natija:",
    "msg_regenerate_prompt_uz": (
        "Yangi rasm yaratishni xohlaysizmi? "
        "Nima o'zgartirishni xohlayotganingizni yozing yoki yangi selfi yuboring."
    ),
    "msg_already_participated_uz": "Siz allaqachon ishtirok etgansiz. Rahmat! 🙌",
    "msg_no_attempts_left_uz": "Barcha urinishlar ishlatildi. Ishtirok etganingiz uchun rahmat!",
    # --- Moderation / system ---
    "rejection_message_ru": (
        "К сожалению, ваше фото не прошло проверку. "
        "Попробуйте сгенерировать новое."
    ),
    "rejection_message_uz": (
        "Afsuski, rasmingiz tekshirishdan o'tmadi. "
        "Yangi rasm yaratishga harakat qiling."
    ),
    "budget_exceeded_message_ru": (
        "К сожалению, генерация временно недоступна. "
        "Пожалуйста, попробуйте позже."
    ),
    "budget_exceeded_message_uz": (
        "Afsuski, yaratish vaqtincha mavjud emas. "
        "Keyinroq urinib ko'ring."
    ),
}


async def seed_defaults(session: AsyncSession) -> None:
    """Insert default settings that are missing from the DB."""
    from app.core.config import get_settings

    config = get_settings()

    for key, value in DEFAULT_SETTINGS.items():
        existing = await session.get(BotSetting, key)
        if existing is None:
            session.add(BotSetting(key=key, value=value))

    # Seed bot_token from env var if set and not already in DB
    if config.bot_token:
        existing = await session.get(BotSetting, "bot_token")
        if existing is None or not existing.value:
            session.add(BotSetting(key="bot_token", value=config.bot_token))

    await session.commit()
    _CACHE.clear()


async def get(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    """Get a setting value, using the in-memory cache."""
    now = time.monotonic()
    if key in _CACHE:
        value, ts = _CACHE[key]
        if now - ts < _CACHE_TTL:
            return value

    row = await session.get(BotSetting, key)
    value = row.value if row is not None else default
    _CACHE[key] = (value, now)
    return value


async def get_many(session: AsyncSession, keys: list[str]) -> dict[str, str | None]:
    """Bulk-get settings with cache support."""
    result: dict[str, str | None] = {}
    missing_keys: list[str] = []
    now = time.monotonic()

    for key in keys:
        if key in _CACHE:
            value, ts = _CACHE[key]
            if now - ts < _CACHE_TTL:
                result[key] = value
                continue
        missing_keys.append(key)

    if missing_keys:
        rows = await session.execute(
            select(BotSetting).where(BotSetting.key.in_(missing_keys))
        )
        db_rows = {row.key: row.value for row in rows.scalars()}
        for key in missing_keys:
            value = db_rows.get(key)
            _CACHE[key] = (value, now)
            result[key] = value

    return result


async def set(session: AsyncSession, key: str, value: str | None) -> None:
    """Upsert a setting value and invalidate cache."""
    existing = await session.get(BotSetting, key)
    if existing is not None:
        existing.value = value
    else:
        session.add(BotSetting(key=key, value=value))
    await session.commit()
    _CACHE.pop(key, None)


async def set_many(session: AsyncSession, data: dict[str, str | None]) -> None:
    """Bulk upsert settings."""
    for key, value in data.items():
        existing = await session.get(BotSetting, key)
        if existing is not None:
            existing.value = value
        else:
            session.add(BotSetting(key=key, value=value))
    await session.commit()
    for key in data:
        _CACHE.pop(key, None)


async def get_all(session: AsyncSession) -> dict[str, str | None]:
    """Return all settings as a dict."""
    rows = await session.execute(select(BotSetting))
    return {row.key: row.value for row in rows.scalars()}


def invalidate_cache() -> None:
    _CACHE.clear()


async def add_budget_spent(session: AsyncSession, amount: Decimal) -> Decimal:
    """Atomically increment budget_spent_usd and return the new total."""
    row = await session.get(BotSetting, "budget_spent_usd")
    current = Decimal(row.value or "0") if row else Decimal("0")
    new_total = current + amount
    await set(session, "budget_spent_usd", str(new_total))
    return new_total


async def get_text(session: AsyncSession, key_prefix: str, language: str) -> str:
    """
    Convenience helper: get a bot message for the given language.
    key_prefix is e.g. 'msg_welcome', language is 'ru' or 'uz'.
    """
    key = f"{key_prefix}_{language}"
    value = await get(session, key)
    if not value:
        # Fallback to Russian
        value = await get(session, f"{key_prefix}_ru")
    return value or ""
