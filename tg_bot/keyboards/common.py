from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def cancel_kb() -> InlineKeyboardMarkup:
    """Universal cancel button.

    Clears FSM state and (if possible) cancels current submission on backend.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="nav:cancel")
    kb.adjust(1)
    return kb.as_markup()
