from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from ..states import SubmitTrack
from ..keyboards.priority import priority_choice_kb
from ..keyboards.payments import payment_method_kb
from ..keyboards.main import main_menu_kb, check_sub_kb
from ..keyboards.common import cancel_kb
from ..services.subscription_check import check_subscription
from ..services.trackrater_api import TrackRaterAPI
from ..config import Settings

router = Router()

def _normalize_ext(filename: str) -> str:
    if not filename:
        return ""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower().strip()

async def _require_sub(call_or_msg, settings: Settings) -> bool:
    ok = await check_subscription(call_or_msg.bot, call_or_msg.from_user.id, settings.required_chat_ids)
    if ok:
        return True
    await call_or_msg.answer("Перед загрузкой подпишитесь на каналы антигазовцев:", reply_markup=check_sub_kb(settings.sponsor_links))
    return False

@router.callback_query(F.data == "menu:submit")
async def start_submit(call: CallbackQuery, state: FSMContext, settings: Settings):
    await call.answer()
    if not await _require_sub(call, settings):
        return
    await state.clear()
    await state.set_state(SubmitTrack.waiting_file)
    await call.message.answer(
        "Отправьте аудиофайл (audio или document).",
        reply_markup=cancel_kb(),
    )

@router.message(SubmitTrack.waiting_file, F.audio | F.document)
async def got_file(message: Message, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    if not await _require_sub(message, settings):
        return

    # pick file object
    doc = message.audio or message.document
    assert doc
    filename = getattr(doc, "file_name", None) or "track"
    ext = _normalize_ext(filename)

    # If audio, Telegram may provide performer/title
    performer = getattr(message.audio, "performer", None) if message.audio else None
    title_meta = getattr(message.audio, "title", None) if message.audio else None

    if ext not in settings.allowed_exts:
        await message.answer("❌ Неподдерживаемый формат. Разрешены: " + ", ".join(settings.allowed_exts))
        return

    # download file bytes
    file = await message.bot.get_file(doc.file_id)
    file_bytes = await message.bot.download_file(file.file_path)

    data = await api.create_submission(
        tg_user_id=message.from_user.id,
        tg_username=message.from_user.username,
        filename=filename,
        ext=ext,
        file_bytes=file_bytes.read(),
    )
    submission_id = int(data["submission_id"])
    await state.update_data(submission_id=submission_id)

    # Prefill if we have both
    if performer and title_meta:
        await state.update_data(artist=performer, title=title_meta)
        await message.answer(
            f"Нашёл метаданные:\n\n🎤 Исполнитель: {performer}\n🎵 Название: {title_meta}\n\n"
            "Если ок — просто отправьте любое слово 'да', или напишите исправленный ИСПОЛНИТЕЛЬ.",
            reply_markup=cancel_kb(),
        )
        await state.set_state(SubmitTrack.waiting_artist)
        return

    await message.answer("Введите исполнителя:", reply_markup=cancel_kb())
    await state.set_state(SubmitTrack.waiting_artist)

@router.message(SubmitTrack.waiting_artist)
async def got_artist(message: Message, state: FSMContext):
    artist = (message.text or "").strip()
    if not artist:
        await message.answer("Введите исполнителя текстом.")
        return
    await state.update_data(artist=artist)
    await message.answer("Введите название трека:", reply_markup=cancel_kb())
    await state.set_state(SubmitTrack.waiting_title)

@router.message(SubmitTrack.waiting_title)
async def got_title(message: Message, state: FSMContext, api: TrackRaterAPI):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Введите название трека текстом.")
        return
    data = await state.get_data()
    submission_id = int(data["submission_id"])
    artist = data.get("artist") or ""
    await state.update_data(title=title)

    # persist metadata
    await api.set_metadata(submission_id, artist=artist, title=title)

    await message.answer(
        f"✅ Принято:\n\n🎤 {artist}\n🎵 {title}\n\nВыберите приоритет:",
        reply_markup=priority_choice_kb(include_free=True),
    )
    await state.set_state(SubmitTrack.choose_priority)

@router.callback_query(SubmitTrack.choose_priority, F.data.startswith("prio:"))
async def choose_priority(call: CallbackQuery, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    await call.answer()
    if not await _require_sub(call, settings):
        return
    prio = int(call.data.split(":")[1])
    data = await state.get_data()
    submission_id = int(data["submission_id"])

    if prio == 0:
        payload = await api.enqueue_free(submission_id)
        pos = payload.get("position")
        await state.clear()
        await call.message.answer(f"✅ Успешно! Ваш трек добавлен в очередь.\nПозиция: {pos}", reply_markup=main_menu_kb())
        return

    await api.set_waiting_payment(submission_id, priority=prio)
    await state.update_data(priority=prio)
    await call.message.answer(
        f"Чтобы отправить трек в платную очередь, оплатите {prio}.\nВыберите способ оплаты:",
        reply_markup=payment_method_kb(submission_id, prio),
    )
    await state.set_state(SubmitTrack.choose_payment_method)


@router.callback_query(SubmitTrack.choose_payment_method, F.data.startswith("nav:prio:"))
async def back_to_priority(call: CallbackQuery, state: FSMContext, settings: Settings):
    await call.answer()
    if not await _require_sub(call, settings):
        return
    # just show priority choice again
    await state.set_state(SubmitTrack.choose_priority)
    await call.message.answer("Выберите приоритет:", reply_markup=priority_choice_kb(include_free=True))
