from __future__ import annotations

import io
import os
import time
from typing import Optional

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from ..states import SubmitTrack
from ..config import Settings
from ..keyboards.priority import priority_choice_kb
from ..keyboards.main import main_menu_kb, check_sub_kb
from ..keyboards.common import cancel_kb
from ..keyboards.payments import payment_method_kb
from ..services.subscription_check import check_subscription
from ..services.trackrater_api import TrackRaterAPI

router = Router()

def _ext_from_filename(name: str) -> str:
    base = (name or "").strip().lower()
    if "." in base:
        return base.rsplit(".", 1)[-1]
    return ""

def _is_allowed_ext(ext: str, settings: Settings) -> bool:
    ext = (ext or "").lower().lstrip(".")
    return ext in [e.lower().lstrip(".") for e in (settings.allowed_exts or [])]

async def _require_sub(msg: Message | CallbackQuery, settings: Settings) -> bool:
    bot = msg.bot
    user = msg.from_user
    assert user is not None
    res = await check_subscription(bot, user.id, settings.required_chat_ids, ttl_seconds=10*60)
    if res.ok:
        return True
    # show sponsor links
    if isinstance(msg, CallbackQuery):
        await msg.answer()
        target = msg.message
    else:
        target = msg
    await target.answer("Для отправки трека нужна подписка на каналы спонсоров:", reply_markup=check_sub_kb(settings.sponsor_links))
    return False

def _started_at() -> float:
    return time.time()

def _expired(data: dict, settings: Settings) -> bool:
    started_at = float(data.get("started_at") or 0)
    return bool(started_at and (time.time() - started_at) > settings.fsm_ttl_seconds)

@router.callback_query(F.data == "menu:submit")
async def menu_submit(call: CallbackQuery, state: FSMContext, settings: Settings):
    await call.answer()
    if not await _require_sub(call, settings):
        return
    await state.clear()
    await state.update_data(started_at=_started_at())
    await state.set_state(SubmitTrack.waiting_file)
    await call.message.answer(
        "Отправь аудиофайл (mp3/wav/...) одним сообщением.",
        reply_markup=cancel_kb(),
    )

@router.message(SubmitTrack.waiting_file)
async def got_file(message: Message, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    data = await state.get_data()
    if _expired(data, settings):
        await state.clear()
        await message.answer("Сессия истекла. Начни заново через меню.", reply_markup=main_menu_kb())
        return

    if not await _require_sub(message, settings):
        return

    file_id = None
    filename = None

    if message.audio:
        file_id = message.audio.file_id
        filename = message.audio.file_name or "track.mp3"
    elif message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or "track.bin"
    else:
        await message.answer("Это не похоже на аудиофайл. Пришли mp3/wav как файл.", reply_markup=cancel_kb())
        return

    ext = _ext_from_filename(filename)
    if ext and not _is_allowed_ext(ext, settings):
        await message.answer(f"Расширение .{ext} не разрешено. Разрешены: {', '.join(settings.allowed_exts)}", reply_markup=cancel_kb())
        return

    # Download bytes
    buf = io.BytesIO()
    try:
        tg_file = await message.bot.get_file(file_id)
        await message.bot.download_file(tg_file.file_path, destination=buf)
    except Exception as e:
        await message.answer(f"Не смог скачать файл из Telegram: {e}", reply_markup=cancel_kb())
        return

    file_bytes = buf.getvalue()
    if not file_bytes:
        await message.answer("Файл пустой или не скачался. Попробуй ещё раз.", reply_markup=cancel_kb())
        return

    user = message.from_user
    assert user is not None
    try:
        resp = await api.create_submission(
            tg_user_id=user.id,
            tg_username=user.username,
            filename=filename,
            ext=ext or "bin",
            file_bytes=file_bytes,
        )
    except Exception as e:
        await message.answer(f"Не удалось создать заявку: {e}", reply_markup=main_menu_kb())
        await state.clear()
        return

    submission_id = int(resp.get("submission_id") or resp.get("id") or 0)
    if not submission_id:
        await message.answer("Не удалось получить submission_id от сервера.", reply_markup=main_menu_kb())
        await state.clear()
        return

    await state.update_data(submission_id=submission_id, filename=filename, ext=ext or "bin")
    await state.set_state(SubmitTrack.waiting_artist)
    await message.answer("Введите исполнителя:", reply_markup=cancel_kb())

@router.message(SubmitTrack.waiting_artist)
async def got_artist(message: Message, state: FSMContext, settings: Settings):
    data = await state.get_data()
    if _expired(data, settings):
        await state.clear()
        await message.answer("Сессия истекла. Начни заново через меню.", reply_markup=main_menu_kb())
        return
    artist = (message.text or "").strip()
    if not artist:
        await message.answer("Введите исполнителя текстом.", reply_markup=cancel_kb())
        return
    await state.update_data(artist=artist)
    await state.set_state(SubmitTrack.waiting_title)
    await message.answer("Введите название трека:", reply_markup=cancel_kb())

@router.message(SubmitTrack.waiting_title)
async def got_title(message: Message, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    data = await state.get_data()
    if _expired(data, settings):
        await state.clear()
        await message.answer("Сессия истекла. Начни заново через меню.", reply_markup=main_menu_kb())
        return

    title = (message.text or "").strip()
    if not title:
        await message.answer("Введите название трека текстом.", reply_markup=cancel_kb())
        return

    sid = int(data["submission_id"])
    artist = (data.get("artist") or "").strip()
    try:
        await api.set_metadata(sid, artist=artist, title=title)
    except Exception as e:
        await message.answer(f"Не удалось сохранить метаданные: {e}", reply_markup=main_menu_kb())
        await state.clear()
        return

    await state.update_data(title=title)
    await state.set_state(SubmitTrack.choose_priority)
    await message.answer(
        f"Ок! {artist} — {title}\n\nВыберите приоритет:",
        reply_markup=priority_choice_kb(include_free=True),
    )

@router.callback_query(SubmitTrack.choose_priority, F.data.startswith("prio:"))
async def picked_priority(call: CallbackQuery, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    await call.answer()
    data = await state.get_data()
    if _expired(data, settings):
        await state.clear()
        await call.message.answer("Сессия истекла. Начни заново через меню.", reply_markup=main_menu_kb())
        return

    prio = int((call.data or "prio:0").split(":")[1])
    sid = int(data["submission_id"])

    if prio == 0:
        try:
            await api.enqueue_free(sid)
        except Exception as e:
            await call.message.answer(f"Не удалось поставить в очередь: {e}", reply_markup=main_menu_kb())
            await state.clear()
            return
        await state.clear()
        await call.message.answer("✅ Трек добавлен в очередь (бесплатно).", reply_markup=main_menu_kb())
        return

    # Paid: show DonationAlerts instructions via existing keyboard (single click)
    await state.update_data(priority=prio)
    await state.set_state(SubmitTrack.waiting_payment)
    await call.message.answer(
        f"Чтобы отправить трек в платную очередь, оплатите {prio}.\nВыберите способ оплаты:",
        reply_markup=payment_method_kb(sid, prio),
    )

@router.callback_query(SubmitTrack.waiting_payment, F.data.startswith("nav:prio:"))
async def back_to_priority(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(SubmitTrack.choose_priority)
    await call.message.answer("Выберите приоритет:", reply_markup=priority_choice_kb(include_free=True))
