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
    "bot_username": "",          # used for the "share bot" button (without @)
    "telegram_channel_id": "",
    "admin_telegram_user_id": "",
    "privacy_policy_url": "https://example.com/privacy",
    # Feature flags
    "channel_check_enabled": "1",        # "1" = enabled, "0" = disabled
    "privacy_policy_link_enabled": "0",  # show "Read policy" URL button on disclaimer screen
    "video_enabled": "0",                # show bonus Eldor video after approved result
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
    # --- Extra photo collection messages (multi-angle flow) ---
    "msg_extra_photo_prompt_ru": (
        "📸 Для более точного результата вы можете прислать 1–2 дополнительных фото с другого ракурса "
        "(например, вполоборота или при другом освещении). Это поможет AI лучше сохранить ваши черты лица.\n\n"
        "Или нажмите «Пропустить», чтобы продолжить с одним фото."
    ),
    "msg_extra_photo_prompt_uz": (
        "📸 Yaxshiroq natija uchun boshqa burchakdan 1–2 ta qo'shimcha rasm yuborishingiz mumkin "
        "(masalan, yonga burilgan holda yoki boshqa yoritishda). Bu AI'ga yuz xususiyatlaringizni "
        "aniqroq saqlashga yordam beradi.\n\n"
        "Yoki bitta rasm bilan davom etish uchun «O'tkazib yuborish» tugmasini bosing."
    ),
    "msg_extra_photo_added_ru": (
        "✅ Фото {n} принято! Вы можете прислать ещё одно или нажать «Готово»."
    ),
    "msg_extra_photo_added_uz": (
        "✅ {n}-rasm qabul qilindi! Yana bitta rasm yuborishingiz yoki «Tayyor» tugmasini bosishingiz mumkin."
    ),
    "msg_regen_ask_your_photo_ru": (
        "📸 Хотите загрузить новое фото?\n"
        "Пришлите ВАШЕ фото (не фото амбассадора) или нажмите «Пропустить»."
    ),
    "msg_regen_ask_your_photo_uz": (
        "📸 Yangi rasm yuklashni xohlaysizmi?\n"
        "O'ZINGIZNING rasmingizni yuboring (elchi rasmi emas) yoki «O'tkazib yuborish» tugmasini bosing."
    ),
    "msg_regen_ask_extra_photos_ru": (
        "📸 Хотите добавить фото с другого ракурса для лучшего результата?\n"
        "Пришлите дополнительное фото или нажмите «Пропустить»."
    ),
    "msg_regen_ask_extra_photos_uz": (
        "📸 Yaxshiroq natija uchun boshqa burchakdan rasm qo'shmoqchimisiz?\n"
        "Qo'shimcha rasm yuboring yoki «O'tkazib yuborish» tugmasini bosing."
    ),
    "btn_extra_photo_done_ru": "✅ Готово, генерировать",
    "btn_extra_photo_done_uz": "✅ Tayyor, yaratish",
    "btn_extra_photo_skip_ru": "⏭ Пропустить",
    "btn_extra_photo_skip_uz": "⏭ O'tkazib yuborish",
    # Image generation system instruction (passed as system_instruction to the model)
    "system_instruction": (
        "You are a photorealistic image compositor. "
        "Your absolute constraint: reproduce every person's face exactly as shown "
        "in the reference photos — identical facial features, skin tone, ethnicity, "
        "hair, and proportions. Never alter, idealize, westernize, or average any "
        "person's appearance. The output must be indistinguishable from a real photograph."
    ),
    # Image generation prompt template
    "generation_prompt": (
        "Create a natural, realistic photo of two people standing together and posing for a photo, "
        "as if a third person took the picture. "
        "The background is a soccer (association football) field — green grass pitch, goal posts visible. "
        "Person 1 is from the first reference photo provided. "
        "Person 2 is from the second reference photo provided. "
        "CRITICAL: This is NOT a selfie — no hands, arms, or phone should be visible in the frame. "
        "The photo should look like it was taken by someone else standing in front of them. "
        "CRITICAL: Preserve EXACTLY the facial features, skin tone, hair color, body type, and clothing "
        "of BOTH people exactly as shown in the reference photos. Do NOT alter, idealize, or change "
        "any physical characteristics of either person. "
        "The lighting should be natural outdoor lighting. "
        "The photo quality should be natural and candid, similar to an iPhone 16 snapshot. "
        "{extra}"
    ),
    # --- Russian messages ---
    "msg_welcome_ru": "Добро пожаловать! 👋",
    "msg_select_language_ru": "Выберите язык / Tilni tanlang:",
    "msg_privacy_ru": (
        "Добро пожаловать!\n\n"
        "Бот создаёт развлекательное AI-изображение с Эльдором Шомуродовым на основе вашего фото.\n\n"
        "Загружайте только своё фото — не чужое и не фото несовершеннолетних. Вы несёте "
        "полную ответственность за загружаемое фото и за дальнейшее использование результата.\n\n"
        "Изображение создаётся ИИ, носит развлекательный характер и не отражает реального события.\n"
        "Запрещается использовать сервис для создания оскорбительного, дискредитирующего, "
        "незаконного или вводящего в заблуждение контента, а также для целей, наносящих ущерб третьим лицам.\n\n"
        "Нажимая «Начать», вы подтверждаете своё согласие на обработку персональных данных "
        "или наличие необходимых согласий со стороны уполномоченных лиц."
    ),
    "btn_disclaimer_start_ru": "Начать",
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
    "msg_regenerate_prompt_ru": "Хотите сгенерировать новое фото? Нажмите кнопку ниже.",
    "msg_regenerate_2left_ru": "Хотите попробовать ещё раз? У вас осталось две попытки.",
    "msg_regenerate_1left_ru": "Хотите попробовать ещё раз? У вас осталась одна попытка.",
    "msg_regen_ask_photo_ru": (
        "📸 Хотите загрузить новое селфи?\n"
        "Отправьте фото или нажмите «Пропустить»."
    ),
    "msg_regen_ask_text_ru": (
        "✏️ Хотите добавить пожелания к генерации?\n"
        "Напишите, что хотите изменить, или нажмите «Пропустить»."
    ),
    "msg_regen_nothing_changed_ru": (
        "Вы ничего не изменили.\n"
        "Загрузите новое фото или добавьте описание, чтобы сгенерировать новый результат."
    ),
    "msg_already_participated_ru": "Вы уже приняли участие. Спасибо! 🙌",
    "msg_no_attempts_left_ru": "Вы использовали все попытки генерации. Спасибо за участие!",
    # --- Uzbek messages ---
    "msg_welcome_uz": "Xush kelibsiz! 👋",
    "msg_select_language_uz": "Выберите язык / Tilni tanlang:",
    "msg_privacy_uz": (
        "Xush kelibsiz!\n\n"
        "Bot suratingiz asosida Eldor Shomurodov ishtirokidagi AI tasvirni yaratadi.\n\n"
        "Faqat o'zingizning suratingizni yuklang. Boshqa shaxslarning yoki voyaga yetmaganlarning "
        "suratlarini yuklamang. Yuklanayotgan surat va yaratilgan tasvirdan foydalanish uchun "
        "to'liq javobgarlikni o'zingizga olasiz.\n\n"
        "Tasvir sun'iy intellekt (AI) tomonidan yaratiladi, ko'ngilochar xarakterga ega va "
        "haqiqiy voqeani aks ettirmaydi.\n"
        "Xizmatdan haqoratli, obro'sizlantiruvchi, noqonuniy yoki chalg'ituvchi kontent yaratish, "
        "shuningdek, uchinchi shaxslarga zarar yetkazish maqsadida foydalanish taqiqlanadi.\n\n"
        "\"Boshlash\" tugmasini bosish orqali siz shaxsiy ma'lumotlaringizni qayta ishlashga "
        "rozilik bildirasiz yoki buning uchun zarur roziliklar olinganini tasdiqlaysiz."
    ),
    "btn_disclaimer_start_uz": "Boshlash",
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
    "msg_regenerate_prompt_uz": "Yangi rasm yaratishni xohlaysizmi? Quyidagi tugmani bosing.",
    "msg_regenerate_2left_uz": "Yana bitta rasm olasizmi? Sizda yana 2 ta imkoniyat bor.",
    "msg_regenerate_1left_uz": "Yana bitta rasm olasizmi? Sizda yana 1 ta imkoniyat bor.",
    "msg_regen_ask_photo_uz": (
        "📸 Yangi selfi yuklashni xohlaysizmi?\n"
        "Rasm yuboring yoki «O'tkazib yuborish» tugmasini bosing."
    ),
    "msg_regen_ask_text_uz": (
        "✏️ Generatsiyaga izoh qo'shishni xohlaysizmi?\n"
        "Nima o'zgartirishni xohlayotganingizni yozing yoki «O'tkazib yuborish» tugmasini bosing."
    ),
    "msg_regen_nothing_changed_uz": (
        "Siz hech narsa o'zgartirmadingiz.\n"
        "Yangi natija olish uchun rasm yuboring yoki izoh qo'shing."
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
