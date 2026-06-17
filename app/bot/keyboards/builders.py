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


def disclaimer_keyboard(lang: str, privacy_url: str | None = None) -> InlineKeyboardMarkup:
    """
    Disclaimer screen buttons:
    - Optional URL button to open the privacy policy (when enabled in admin).
    - Primary action button to continue (Начать / Boshlash).
    """
    builder = InlineKeyboardBuilder()
    if privacy_url:
        link_label = "📄 Ознакомиться" if lang == "ru" else "📄 Tanishib chiqish"
        builder.row(InlineKeyboardButton(text=link_label, url=privacy_url))
    start_label = "Начать" if lang == "ru" else "Boshlash"
    builder.row(InlineKeyboardButton(text=start_label, callback_data="privacy:agree"))
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
    confirm_label = "✅ Я подписался" if lang == "ru" else "✅ Obuna bo'ldim"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=sub_label, url=channel_link),
        InlineKeyboardButton(text=confirm_label, callback_data="sub:check"),
    )
    return builder.as_markup()


def regenerate_keyboard(lang: str) -> InlineKeyboardMarkup:
    label = "🔄 Сгенерировать новое" if lang == "ru" else "🔄 Yangi rasm yaratish"
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data="action:regenerate")
    return builder.as_markup()
