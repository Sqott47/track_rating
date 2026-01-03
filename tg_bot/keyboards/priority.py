from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PRIORITIES = [400, 300, 200, 100, 1]

def priority_choice_kb(include_free: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in PRIORITIES:
        kb.button(text=("1 (тест)" if p==1 else str(p)), callback_data=f"prio:{p}")
    if include_free:
        kb.button(text="БЕСПЛАТНО", callback_data="prio:0")
    kb.adjust(2,2,1,1)
    return kb.as_markup()
