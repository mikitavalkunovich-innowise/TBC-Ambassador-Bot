"""All keyboard builders for the bot."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def language_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇺🇿 O'zbek", callback_data="lang:uz"),
    )
    return builder.as_markup()


def agree_keyboard(lang: str) -> InlineKeyboardMarkup:
    label = "✅ Согласен" if lang == "ru" else "✅ Roziman"
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data="privacy:agree")
    return builder.as_markup()


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
