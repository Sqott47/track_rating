from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎵 Отправить трек в очередь", callback_data="menu:submit")
    kb.button(text="⬆️ Поднять приоритет трека", callback_data="menu:raise")
    kb.adjust(1)
    return kb.as_markup()

def check_sub_kb(sponsor_links: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, url in enumerate(sponsor_links[:6]):  # keep sane
        kb.row(InlineKeyboardButton(text=f"📣 Канал {idx+1}", url=url))
    kb.button(text="✅ Проверить подписку", callback_data="sub:check")
    kb.button(text="⬅️ Назад", callback_data="nav:back")
    kb.adjust(1)
    return kb.as_markup()
