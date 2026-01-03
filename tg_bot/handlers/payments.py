from aiogram import Router, F
from aiogram.types import CallbackQuery, LabeledPrice, PreCheckoutQuery, Message
from aiogram.fsm.context import FSMContext
import secrets


from ..services.trackrater_api import TrackRaterAPI
from ..config import Settings
from ..keyboards.main import main_menu_kb

router = Router()

def _da_link(base: str) -> str:
    return base or ""


@router.callback_query(F.data.startswith("pay:stars:"))
async def pay_stars(call: CallbackQuery, state: FSMContext):
    await call.answer()
    _, _, sid, prio = call.data.split(":")
    sid_i = int(sid); prio_i=int(prio)

    payload = f"TR:{sid_i}:P{prio_i}"
    prices = [LabeledPrice(label=f"Приоритет {prio_i}", amount=prio_i)]

    await call.bot.send_invoice(
        chat_id=call.from_user.id,
        title="TrackRater — приоритет трека",
        description=f"Оплата приоритета {prio_i} (Stars). Код: {payload}",
        payload=payload,
        provider_token="",  # empty for Stars
        currency="XTR",
        prices=prices,
        start_parameter="trackrater_priority",
    )
    await call.message.answer("⭐ Инвойс отправлен. После оплаты я автоматически добавлю/подниму трек.")

@router.pre_checkout_query()
async def pre_checkout(pre: PreCheckoutQuery):
    # Accept all payloads that look like ours
    ok = bool(pre.invoice_payload and pre.invoice_payload.startswith("TR:"))
    await pre.answer(ok=ok, error_message=None if ok else "Некорректный платёж.")

@router.message(F.successful_payment)
async def successful_payment(msg: Message, api: TrackRaterAPI):
    sp = msg.successful_payment
    payload = sp.invoice_payload or ""
    # payload format: TR:<id>:P<prio>
    try:
        parts = payload.split(":")
        sid = int(parts[1])
        prio = int(parts[2].lstrip("P"))
    except Exception:
        await msg.answer("Платёж получен, но не смог распознать заявку. Напишите администратору.")
        return

    provider_ref = sp.telegram_payment_charge_id
    result = await api.mark_paid(sid, provider="stars", provider_ref=provider_ref, amount=prio)
    pos = result.get("position")
    await msg.answer(f"✅ Оплата прошла! Трек добавлен/поднят.\nПозиция: {pos}", reply_markup=main_menu_kb())

@router.callback_query(F.data.startswith("pay:da:"))
async def pay_da(call: CallbackQuery, state: FSMContext, settings: Settings, api: TrackRaterAPI):
    await call.answer()
    _, _, sid, prio = call.data.split(":")
    sid_i=int(sid); prio_i=int(prio)
    code = f"TR-{sid_i}-P{prio_i}-" + secrets.token_hex(3).upper()
    await api.set_waiting_payment(sid_i, priority=prio_i, provider="donationalerts", provider_ref=code)
    link = _da_link(settings.donationalerts_base_url)

    text = (
        f"💸 DonationAlerts:\n\n"
        f"Сумма: {prio_i}\n"
        f"Комментарий: {code}\n\n"
        f"⚠️ Внимание! Не меняйте сумму и комментарий, иначе бот не распознает оплату.\n"
    )
    if link:
        text += f"\nСсылка: {link}"
    else:
        text += "\n(Ссылка не настроена: установите DONATIONALERTS_URL в .env)"
    await call.message.answer(text)
