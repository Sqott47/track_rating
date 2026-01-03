from __future__ import annotations

import time
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Settings
from ..states import RaisePriority
from ..keyboards.priority import priority_choice_kb
from ..keyboards.main import main_menu_kb, check_sub_kb
from ..keyboards.payments import payment_method_kb
from ..services.subscription_check import check_subscription
from ..services.trackrater_api import TrackRaterAPI

router = Router()

def _started_at() -> float:
    return time.time()

def _expired(data: dict, settings: Settings) -> bool:
    started_at = float(data.get("started_at") or 0)
    return bool(started_at and (time.time() - started_at) > settings.fsm_ttl_seconds)

async def _require_sub(call: CallbackQuery, settings: Settings) -> bool:
    user = call.from_user
    assert user is not None
    res = await check_subscription(call.bot, user.id, settings.required_chat_ids, ttl_seconds=10*60)
    if res.ok:
        return True
    await call.message.answer("Для действия нужна подписка на каналы спонсоров:", reply_markup=check_sub_kb(settings.sponsor_links))
    return False

def _tracks_kb(items: list[dict]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for it in items[:20]:
        sid = int(it.get("submission_id") or it.get("id") or 0)
        title = (it.get("title") or "").strip()
        artist = (it.get("artist") or "").strip()
        status = (it.get("status") or "").strip()
        prio = int(it.get("priority") or 0)
        label = f"{artist} — {title}" if (artist or title) else f"Трек #{sid}"
        if status:
            label += f" [{status}]"
        if prio:
            label += f" (prio {prio})"
        kb.button(text=label[:60], callback_data=f"raise:pick:{sid}")
    kb.button(text="⬅️ Назад", callback_data="nav:back")
    kb.adjust(1)
    return kb

@router.callback_query(F.data == "menu:raise")
async def start_raise(call: CallbackQuery, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    await call.answer()
    if not await _require_sub(call, settings):
        return

    user = call.from_user
    assert user is not None

    try:
        items = await api.my_queue(user.id)
    except Exception as e:
        await call.message.answer(f"Не удалось получить список треков: {e}", reply_markup=main_menu_kb())
        return

    if not items:
        await call.message.answer("У тебя нет треков в очереди.", reply_markup=main_menu_kb())
        return

    await state.clear()
    await state.update_data(started_at=_started_at())
    await state.set_state(RaisePriority.choosing_track)

    kb = _tracks_kb(items).as_markup()
    await call.message.answer("Выбери трек, которому поднять приоритет:", reply_markup=kb)

@router.callback_query(RaisePriority.choosing_track, F.data.startswith("raise:pick:"))
async def picked_track(call: CallbackQuery, state: FSMContext, settings: Settings):
    await call.answer()
    data = await state.get_data()
    if _expired(data, settings):
        await state.clear()
        await call.message.answer("Сессия истекла. Начни заново через меню.", reply_markup=main_menu_kb())
        return

    sid = int((call.data or "").split(":")[-1])
    await state.update_data(submission_id=sid)
    await state.set_state(RaisePriority.choosing_priority)
    await call.message.answer("Выберите новый приоритет:", reply_markup=priority_choice_kb(include_free=False))

@router.callback_query(RaisePriority.choosing_priority, F.data.startswith("prio:"))
async def picked_prio(call: CallbackQuery, state: FSMContext, settings: Settings):
    await call.answer()
    data = await state.get_data()
    if _expired(data, settings):
        await state.clear()
        await call.message.answer("Сессия истекла. Начни заново через меню.", reply_markup=main_menu_kb())
        return

    prio = int(call.data.split(":")[1])
    sid = int(data["submission_id"])

    await state.update_data(priority=prio)
    await state.set_state(RaisePriority.waiting_payment)
    await call.message.answer(
        f"Чтобы поднять приоритет, оплатите {prio}.\nВыберите способ оплаты:",
        reply_markup=payment_method_kb(sid, prio),
    )
