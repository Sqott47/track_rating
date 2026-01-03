from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from ..states import RaisePriority
from ..keyboards.priority import priority_choice_kb
from ..keyboards.payments import payment_method_kb
from ..keyboards.main import main_menu_kb, check_sub_kb
from ..services.trackrater_api import TrackRaterAPI
from ..services.subscription_check import check_subscription
from ..config import Settings

router = Router()

@router.callback_query(F.data == "menu:raise")
async def start_raise(call: CallbackQuery, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    await call.answer()
    ok = await check_subscription(call.bot, call.from_user.id, settings.required_chat_ids)
    if not ok:
        await call.message.answer("Перед использованием подпишитесь на каналы:", reply_markup=check_sub_kb(settings.sponsor_links))
        return

    items = await api.my_queue(call.from_user.id)
    if not items:
        await state.clear()
        await call.message.answer("У вас нет треков в очереди.", reply_markup=main_menu_kb())
        return

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    for it in items[:20]:
        sid = it["id"]
        name = it.get("display") or f"{it.get('artist','')} — {it.get('title','')}".strip(" —")
        kb.button(text=name[:60], callback_data=f"raise:pick:{sid}")
    kb.button(text="⬅️ Назад", callback_data="nav:back")
    kb.adjust(1)
    await state.clear()
    await state.set_state(RaisePriority.choosing_track)
    await call.message.answer("Выберите трек, который хотите поднять:", reply_markup=kb.as_markup())

@router.callback_query(RaisePriority.choosing_track, F.data.startswith("raise:pick:"))
async def picked_track(call: CallbackQuery, state: FSMContext):
    await call.answer()
    sid = int(call.data.split(":")[-1])
    await state.update_data(submission_id=sid)
    await call.message.answer("Выберите новый приоритет:", reply_markup=priority_choice_kb(include_free=False))
    await state.set_state(RaisePriority.choosing_priority)

@router.callback_query(RaisePriority.choosing_priority, F.data.startswith("prio:"))
async def picked_prio(call: CallbackQuery, state: FSMContext, api: TrackRaterAPI):
    await call.answer()
    prio = int(call.data.split(":")[1])
    data = await state.get_data()
    sid = int(data["submission_id"])
    await api.set_waiting_payment(sid, priority=prio)
    await state.update_data(priority=prio)
    await call.message.answer(
        f"Чтобы поднять приоритет, оплатите {prio}.\nВыберите способ оплаты:",
        reply_markup=payment_method_kb(sid, prio),
    )
    await state.set_state(RaisePriority.choosing_payment_method)
