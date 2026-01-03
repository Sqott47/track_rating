from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext

from ..keyboards.main import main_menu_kb, check_sub_kb
from ..services.subscription_check import check_subscription
from ..services.trackrater_api import TrackRaterAPI
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


@router.callback_query(F.data == "nav:cancel")
async def nav_cancel(call: CallbackQuery, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    """Cancel any in-progress flow.

    - Clears FSM state
    - Asks backend to cancel submission / clear pending payment (best-effort)
    - Returns user to main menu
    """
    await call.answer()
    data = await state.get_data()
    submission_id = data.get("submission_id")
    await state.clear()
    try:
        if submission_id is not None:
            await api.cancel_submission(int(submission_id))
    except Exception:
        # best-effort cleanup
        pass
    if not await _ensure_subscribed(call, settings):
        return
    await call.message.answer("❌ Отменено. Главное меню:", reply_markup=main_menu_kb())


@router.callback_query(F.data.startswith("nav:prio:"))
async def nav_back_to_priority(call: CallbackQuery, state: FSMContext, settings: Settings):
    """Return to priority selection during payment step."""
    await call.answer()
    if not await _ensure_subscribed(call, settings):
        return
    # keep current state data (submission_id) but reset to priority selection
    from ..states import SubmitTrack, RaisePriority
    from ..keyboards.priority import priority_choice_kb

    data = await state.get_data()
    # If we are in raise flow, free option should be hidden
    if state.get_state() == RaisePriority.choosing_payment_method.state:
        await state.set_state(RaisePriority.choosing_priority)
        await call.message.answer("Выберите новый приоритет:", reply_markup=priority_choice_kb(include_free=False))
        return

    # Default: submit flow
    await state.set_state(SubmitTrack.choose_priority)
    await call.message.answer("Выберите приоритет:", reply_markup=priority_choice_kb(include_free=True))
