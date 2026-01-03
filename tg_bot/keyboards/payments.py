from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

def payment_method_kb(submission_id: int, priority: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ TG Stars", callback_data=f"pay:stars:{submission_id}:{priority}")
    kb.button(text="💸 DonationAlerts", callback_data=f"pay:da:{submission_id}:{priority}")
    kb.button(text="⬅️ Назад", callback_data=f"nav:prio:{submission_id}")
    kb.adjust(1)
    return kb.as_markup()
