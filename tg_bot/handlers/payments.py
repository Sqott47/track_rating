from __future__ import annotations

import secrets
import time

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from ..config import Settings
from ..services.trackrater_api import TrackRaterAPI
from ..keyboards.main import main_menu_kb

router = Router()

def _new_code() -> str:
    # human-friendly short token
    return secrets.token_hex(4)

def _state_started(now: float | None = None) -> dict:
    return {"started_at": (now or time.time())}

def _is_expired(data: dict, settings: Settings) -> bool:
    started_at = float(data.get("started_at") or 0)
    return bool(started_at and (time.time() - started_at) > settings.fsm_ttl_seconds)

async def _cleanup_backend_if_needed(state: FSMContext, api: TrackRaterAPI) -> None:
    try:
        data = await state.get_data()
        sid = data.get("submission_id")
        if sid:
            await api.cancel_submission(int(sid))
    except Exception:
        pass

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, api: TrackRaterAPI):
    await _cleanup_backend_if_needed(state, api)
    await state.clear()
    await message.answer("Ок, отменил.", reply_markup=main_menu_kb())

@router.callback_query(F.data == "nav:cancel")
async def nav_cancel(call: CallbackQuery, state: FSMContext, api: TrackRaterAPI):
    await call.answer()
    await _cleanup_backend_if_needed(state, api)
    await state.clear()
    await call.message.answer("Ок, отменил.", reply_markup=main_menu_kb())

@router.callback_query(F.data.startswith("pay:da:"))
async def pay_donationalerts(call: CallbackQuery, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    """DonationAlerts payment instruction.

    Flow:
    - generate unique code
    - store it into submission.payment_ref via /waiting_payment
    - show instructions and clear FSM
    """
    await call.answer()

    parts = (call.data or "").split(":")
    # pay:da:<submission_id>:<priority>
    if len(parts) < 4:
        await call.message.answer("Ошибка оплаты: некорректные данные.", reply_markup=main_menu_kb())
        return

    submission_id = int(parts[2])
    prio_i = int(parts[3])

    code = _new_code()
    try:
        await api.set_waiting_payment(submission_id, priority=prio_i, provider="donationalerts", provider_ref=code)
    except Exception as e:
        await call.message.answer(f"Не удалось подготовить оплату: {e}", reply_markup=main_menu_kb())
        return

    link = (settings.donationalerts_base_url or "").strip()
    text = (
        f"💸 DonationAlerts\n\n"
        f"Сумма: {prio_i}\n"
        f"Комментарий: {code}\n\n"
        f"⚠️ Важно: не меняйте сумму и комментарий, иначе бот не распознает оплату."
    )
    if link:
        text += f"\n\nСсылка: {link}"
    else:
        text += "\n\n(Ссылка не настроена: установите DONATIONALERTS_URL в .env)"
    await state.clear()
    await call.message.answer(text)
    await call.message.answer("Главное меню:", reply_markup=main_menu_kb())
