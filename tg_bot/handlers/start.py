from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext

from ..keyboards.main import main_menu_kb, check_sub_kb
from ..services.subscription_check import check_subscription
from ..config import Settings

router = Router()

async def _ensure_subscribed(msg: Message | CallbackQuery, settings: Settings) -> bool:
    bot = msg.bot
    user = msg.from_user
    assert user
    ok = await check_subscription(bot, user.id, settings.required_chat_ids)
    if ok:
        return True
    text = "Перед загрузкой подпишитесь на каналы антигазовцев:"
    kb = check_sub_kb(settings.sponsor_links)
    if isinstance(msg, CallbackQuery):
        await msg.message.answer(text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)
    return False

@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext, settings: Settings):
    await state.clear()
    if not await _ensure_subscribed(message, settings):
        return
    await message.answer("Главное меню:", reply_markup=main_menu_kb())

@router.callback_query(F.data == "sub:check")
async def on_sub_check(call: CallbackQuery, state: FSMContext, settings: Settings):
    await call.answer()
    ok = await check_subscription(call.bot, call.from_user.id, settings.required_chat_ids)
    if ok:
        await state.clear()
        await call.message.answer("✅ Подписка подтверждена. Главное меню:", reply_markup=main_menu_kb())
    else:
        await call.message.answer("❌ Подписка не найдена. Подпишитесь и нажмите «Проверить» ещё раз.", reply_markup=check_sub_kb(settings.sponsor_links))

@router.callback_query(F.data == "nav:back")
async def nav_back(call: CallbackQuery, state: FSMContext, settings: Settings):
    await call.answer()
    await state.clear()
    if not await _ensure_subscribed(call, settings):
        return
    await call.message.answer("Главное меню:", reply_markup=main_menu_kb())
