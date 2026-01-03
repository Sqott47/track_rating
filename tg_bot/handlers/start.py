from __future__ import annotations

import time
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext

from ..config import Settings
from ..keyboards.main import main_menu_kb, check_sub_kb
from ..services.subscription_check import check_subscription
from ..services.trackrater_api import TrackRaterAPI

router = Router()

async def _ensure_subscribed(msg: Message | CallbackQuery, settings: Settings) -> bool:
    bot = msg.bot
    user = msg.from_user
    assert user is not None

    res = await check_subscription(bot, user.id, settings.required_chat_ids, ttl_seconds=10*60)
    if res.ok:
        return True

    if isinstance(msg, CallbackQuery):
        await msg.answer()
        target = msg.message
    else:
        target = msg

    reason = res.reason or "not_member"
    if reason == "cant_verify":
        text = (
            "Я не могу проверить подписку на каналы (скорее всего у бота нет прав или указан неверный chat_id).\n"
            "Сообщи админу, чтобы добавил бота в каналы (минимум: просмотр участников), или поправил TG_REQUIRED_CHAT_IDS."
        )
    elif reason == "rate_limited":
        text = "Telegram временно ограничивает запросы. Попробуй ещё раз через 20–30 секунд."
    else:
        text = "Для отправки трека нужна подписка на каналы спонсоров:"
    await target.answer(text, reply_markup=check_sub_kb(settings.sponsor_links))
    return False

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, settings: Settings):
    await state.clear()
    await message.answer("Привет! Выбирай действие:", reply_markup=main_menu_kb())

@router.callback_query(F.data == "sub:check")
async def cb_check(call: CallbackQuery, state: FSMContext, settings: Settings):
    await call.answer()
    if await _ensure_subscribed(call, settings):
        await call.message.answer("✅ Подписка подтверждена. Главное меню:", reply_markup=main_menu_kb())

@router.callback_query(F.data == "nav:back")
async def nav_back(call: CallbackQuery, state: FSMContext, settings: Settings):
    await call.answer()
    await call.message.answer("Главное меню:", reply_markup=main_menu_kb())
