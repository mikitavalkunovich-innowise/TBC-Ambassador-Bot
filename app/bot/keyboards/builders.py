"""All keyboard builders for the bot."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def language_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇺🇿 O'zbek", callback_data="lang:uz"),
    )
    return builder.as_markup()


def disclaimer_keyboard(
    lang: str,
    privacy_url: str | None = None,
    link_label: str | None = None,
    start_label: str | None = None,
) -> InlineKeyboardMarkup:
    """
    Disclaimer screen buttons:
    - Optional URL button to open the privacy policy (when enabled in admin).
    - Primary action button to continue (Start / Boshlash by default).
    """
    builder = InlineKeyboardBuilder()
    if privacy_url:
        label = link_label or ("📄 Ознакомиться" if lang == "ru" else "📄 Tanishib chiqish")
        builder.row(InlineKeyboardButton(text=label, url=privacy_url))
    start = start_label or ("Начать" if lang == "ru" else "Boshlash")
    builder.row(InlineKeyboardButton(text=start, callback_data="privacy:agree"))
    return builder.as_markup()


def agree_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Deprecated alias — use disclaimer_keyboard instead."""
    return disclaimer_keyboard(lang)


def generate_keyboard(lang: str) -> InlineKeyboardMarkup:
    label = "🎨 Сгенерировать изображение" if lang == "ru" else "🎨 Rasm yaratish"
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data="action:generate")
    return builder.as_markup()


def subscribed_keyboard(lang: str, channel_link: str) -> InlineKeyboardMarkup:
    sub_label = "📢 Подписаться" if lang == "ru" else "📢 Obuna bo'lish"
    confirm_label = (
        "Я уже подписан" if lang == "ru" else "Obuna bo'ldim"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=sub_label, url=channel_link))
    builder.row(InlineKeyboardButton(text=confirm_label, callback_data="sub:check"))
    return builder.as_markup()


def regenerate_keyboard(lang: str) -> InlineKeyboardMarkup:
    label = "Загрузить ещё фото" if lang == "ru" else "Yana rasm yuklash"
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data="action:regenerate")
    return builder.as_markup()


def skip_keyboard(lang: str) -> InlineKeyboardMarkup:
    label = "⏭ Пропустить" if lang == "ru" else "⏭ O'tkazib yuborish"
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data="regen:skip")
    return builder.as_markup()


def card_promo_keyboard(order_url: str, button_label: str) -> InlineKeyboardMarkup:
    """URL button for the TBC Salom Visa card order link."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=button_label, url=order_url))
    return builder.as_markup()


def share_bot_keyboard(lang: str, bot_username: str) -> InlineKeyboardMarkup:
    """Button to share the bot with friends (shown after attempts exhausted)."""
    label = "Поделиться ботом" if lang == "ru" else "Botni ulashish"
    url = f"https://t.me/{bot_username}"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=label, url=url))
    return builder.as_markup()


def channel_keyboard(lang: str, channel_link: str) -> InlineKeyboardMarkup:
    """Button to go to the Telegram channel (shown when bot is paused)."""
    label = "Перейти на канал" if lang == "ru" else "Kanalga o'tish"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=label, url=channel_link))
    return builder.as_markup()


def extra_photos_keyboard(lang: str, has_photo: bool) -> InlineKeyboardMarkup:
    """
    Keyboard for the extra-photos collection step.

    has_photo=False  →  only Skip button (no extra photo added yet)
    has_photo=True   →  Done button + Skip button (at least one extra photo added)
    """
    builder = InlineKeyboardBuilder()
    if has_photo:
        done_label = "✅ Готово, генерировать" if lang == "ru" else "✅ Tayyor, yaratish"
        builder.button(text=done_label, callback_data="extra:done")
    skip_label = "⏭ Пропустить" if lang == "ru" else "⏭ O'tkazib yuborish"
    builder.button(text=skip_label, callback_data="extra:skip")
    builder.adjust(1)
    return builder.as_markup()
