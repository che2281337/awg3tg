from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..awg.server import AwgError
from ..db import Database, User, now
from ..service import ServiceError, VpnService
from ..utils import days_word, devices_word, esc, fmt_date, fmt_dt, human_bytes, left_str, month_start_day, today
from .common import deliver_key, edit_or_send, help_text, notify_admins
from .keyboards import (
    BTN_DEVICES,
    BTN_HELP,
    BTN_PLANS,
    BTN_PROFILE,
    BTN_REF,
    DEVICE_TYPES,
    MENU_TEXTS,
    AU,
    APay,
    Buy,
    Dev,
    DevType,
    Menu,
    back,
    ikb,
    main_menu,
)

log = logging.getLogger(__name__)
router = Router(name="user")


class UserStates(StatesGroup):
    device_name = State()
    rename = State()
    receipt = State()


# ---------- старт и профиль ----------


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    bot: Bot,
    state: FSMContext,
    db: Database,
    service: VpnService,
    user: User,
    is_new: bool,
    is_admin: bool,
) -> None:
    await state.clear()
    payload = command.args or ""
    if is_new and payload.startswith("ref_") and payload[4:].isdigit():
        ref_id = int(payload[4:])
        referrer = await db.get_user(ref_id)
        if referrer and ref_id != user.tg_id:
            await db.update_user(user.tg_id, referrer_id=ref_id)
            try:
                await bot.send_message(
                    ref_id,
                    f"👥 По вашей ссылке зарегистрировался {esc(user.title)}. "
                    f"Когда он оплатит подписку, вы получите +{days_word(service.settings.ref_bonus_days)}.",
                )
            except Exception:
                pass

    text = (
        f"👋 Добро пожаловать в <b>{esc(service.settings.server_name)}</b>!\n\n"
        "Быстрый VPN на протоколе AmneziaWG, который не блокируется DPI.\n\n"
        "• «💳 Тарифы» — оформить или продлить подписку\n"
        "• «🔑 Мои устройства» — получить ключи для телефона, компьютера и т.д.\n"
        "• «👤 Профиль» — срок подписки и статистика трафика"
    )
    await message.answer(text, reply_markup=main_menu(is_admin))
    if service.trial_available(user):
        await message.answer(
            f"🎁 Попробуйте бесплатно: {days_word(service.settings.trial_days)}, "
            f"{devices_word(service.settings.trial_devices)}.",
            reply_markup=ikb([[("🎁 Активировать пробный период", Menu(action="trial").pack())]]),
        )


async def profile_view(db: Database, service: VpnService, user: User) -> tuple[str, object]:
    keys = await db.user_keys(user.tg_id)
    limit = service.device_limit(user)
    t_today = await db.traffic(tg_id=user.tg_id, since=today())
    t_month = await db.traffic(tg_id=user.tg_id, since=month_start_day())
    t_all = await db.traffic(tg_id=user.tg_id)
    if service.is_admin(user.tg_id):
        sub = "♾ администратор"
    elif user.active:
        sub = f"✅ активна до <b>{fmt_dt(user.sub_until)}</b> ({left_str(user.sub_until, now())})"
    elif user.sub_until:
        sub = f"⛔ закончилась {fmt_dt(user.sub_until)}"
    else:
        sub = "нет"
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"ID: <code>{user.tg_id}</code>\n"
        f"Подписка: {sub}\n"
        f"Устройства: {len(keys)} из {limit}\n\n"
        f"📊 <b>Трафик</b> (скачано / отдано)\n"
        f"Сегодня: {human_bytes(t_today.tx)} / {human_bytes(t_today.rx)}\n"
        f"За месяц: {human_bytes(t_month.tx)} / {human_bytes(t_month.rx)}\n"
        f"Всего: {human_bytes(t_all.tx)} / {human_bytes(t_all.rx)}"
    )
    rows = [[("💳 Продлить подписку" if user.active else "💳 Оформить подписку", Menu(action="plans").pack())]]
    if service.trial_available(user):
        rows.append([("🎁 Пробный период", Menu(action="trial").pack())])
    rows.append([("🔑 Мои устройства", Menu(action="devices").pack())])
    return text, ikb(rows)


@router.message(Command("profile"))
@router.message(F.text == BTN_PROFILE)
async def msg_profile(message: Message, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    await state.clear()
    text, kb = await profile_view(db, service, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Menu.filter(F.action == "profile"))
async def cb_profile(call: CallbackQuery, db: Database, service: VpnService, user: User) -> None:
    await call.answer()
    text, kb = await profile_view(db, service, user)
    await edit_or_send(call, text, kb)


@router.callback_query(Menu.filter(F.action == "trial"))
async def cb_trial(call: CallbackQuery, service: VpnService, user: User, is_admin: bool) -> None:
    try:
        user = await service.start_trial(user)
    except ServiceError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("Пробный период активирован!")
    await edit_or_send(
        call,
        f"🎁 Пробный период активирован до <b>{fmt_dt(user.sub_until)}</b>.\n\n"
        "Теперь добавьте устройство — придёт ключ для подключения.",
        ikb([[("➕ Добавить устройство", Menu(action="add").pack())]]),
    )


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def msg_help(message: Message, state: FSMContext, service: VpnService) -> None:
    await state.clear()
    await message.answer(help_text(service.settings.support), disable_web_page_preview=True)


@router.callback_query(Menu.filter(F.action == "help"))
async def cb_help(call: CallbackQuery, service: VpnService) -> None:
    await call.answer()
    await call.message.answer(help_text(service.settings.support), disable_web_page_preview=True)


# ---------- устройства ----------


async def devices_view(db: Database, service: VpnService, user: User) -> tuple[str, object]:
    keys = await db.user_keys(user.tg_id)
    limit = service.device_limit(user)
    lines = [f"🔑 <b>Мои устройства</b> ({len(keys)} из {limit})\n"]
    if not service.has_access(user):
        lines.append("⛔ Подписка не активна — устройства отключены.\n")
    online_border = now() - 180
    for k in keys:
        if not k.enabled:
            state = "⏸"
        elif k.last_handshake >= online_border:
            state = "🟢"
        else:
            state = "⚪️"
        lines.append(f"{state} {esc(k.name)}")
    if not keys:
        lines.append("Устройств пока нет.")
    rows = [[(f"{k.name}", Dev(action="view", id=k.id).pack())] for k in keys]
    if service.has_access(user) and len(keys) < limit:
        rows.append([("➕ Добавить устройство", Menu(action="add").pack())])
    elif not service.has_access(user):
        rows.append([("💳 Оформить подписку", Menu(action="plans").pack())])
    return "\n".join(lines), ikb(rows)


@router.message(Command("devices"))
@router.message(F.text == BTN_DEVICES)
async def msg_devices(message: Message, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    await state.clear()
    text, kb = await devices_view(db, service, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Menu.filter(F.action == "devices"))
async def cb_devices(call: CallbackQuery, db: Database, service: VpnService, user: User) -> None:
    await call.answer()
    text, kb = await devices_view(db, service, user)
    await edit_or_send(call, text, kb)


@router.callback_query(Menu.filter(F.action == "add"))
async def cb_add_device(call: CallbackQuery, db: Database, service: VpnService, user: User) -> None:
    if not service.has_access(user):
        await call.answer("Нет активной подписки", show_alert=True)
        return
    if len(await db.user_keys(user.tg_id)) >= service.device_limit(user):
        await call.answer("Достигнут лимит устройств вашего тарифа", show_alert=True)
        return
    await call.answer()
    rows = [[(title, DevType(kind=kind).pack())] for kind, title in DEVICE_TYPES.items()]
    rows.append([("✏️ Своё название", DevType(kind="custom").pack())])
    rows.append([back(Menu(action="devices"))])
    await edit_or_send(call, "Какое устройство подключаем?", ikb(rows))


async def _create_and_send(bot: Bot, chat_id: int, db: Database, service: VpnService, user: User, name: str) -> None:
    existing = {k.name for k in await db.user_keys(user.tg_id)}
    final, n = name, 2
    while final in existing:
        final, n = f"{name} {n}", n + 1
    wait = await bot.send_message(chat_id, "⏳ Создаю ключ…")
    try:
        rk = await service.create_device(user, final)
    except ServiceError as e:
        await wait.edit_text(f"❌ {e}")
        return
    except AwgError as e:
        log.exception("Ошибка создания устройства")
        await wait.edit_text("❌ Не удалось создать ключ, попробуйте позже.")
        await notify_admins(bot, service, f"❌ Ошибка выдачи ключа для {user.tg_id}: <code>{esc(e)}</code>")
        return
    await wait.delete()
    await deliver_key(bot, chat_id, rk)
    await bot.send_message(
        chat_id,
        "Готово! Ключ всегда можно получить повторно в «🔑 Мои устройства».",
        reply_markup=ikb([[("🔑 Мои устройства", Menu(action="devices").pack())]]),
    )


@router.callback_query(DevType.filter())
async def cb_device_type(
    call: CallbackQuery, callback_data: DevType, bot: Bot, state: FSMContext, db: Database, service: VpnService, user: User
) -> None:
    await call.answer()
    if callback_data.kind == "custom":
        await state.set_state(UserStates.device_name)
        await edit_or_send(call, "Введите название устройства (например, «Ноутбук мамы»):")
        return
    await call.message.delete()
    await _create_and_send(bot, call.from_user.id, db, service, user, DEVICE_TYPES.get(callback_data.kind, "Устройство"))


@router.message(UserStates.device_name, F.text, ~F.text.in_(MENU_TEXTS), ~F.text.startswith("/"))
async def st_device_name(message: Message, bot: Bot, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    await state.clear()
    name = message.text.strip()[:40] or "Устройство"
    await _create_and_send(bot, message.chat.id, db, service, user, name)


async def _own_key(call: CallbackQuery, db: Database, user: User, key_id: int):
    key = await db.get_key(key_id)
    if key is None or key.tg_id != user.tg_id:
        await call.answer("Устройство не найдено", show_alert=True)
        return None
    return key


@router.callback_query(Dev.filter(F.action == "view"))
async def cb_device_view(call: CallbackQuery, callback_data: Dev, db: Database, user: User) -> None:
    key = await _own_key(call, db, user, callback_data.id)
    if key is None:
        return
    await call.answer()
    t_today = await db.traffic(key_id=key.id, since=today())
    t_month = await db.traffic(key_id=key.id, since=month_start_day())
    t_all = await db.traffic(key_id=key.id)
    status = "✅ активно" if key.enabled else "⏸ отключено (нет подписки или превышен лимит устройств)"
    text = (
        f"🔑 <b>{esc(key.name)}</b>\n\n"
        f"Статус: {status}\n"
        f"Добавлено: {fmt_date(key.created_at)}\n"
        f"Последнее подключение: {fmt_dt(key.last_handshake)}\n\n"
        f"📊 Трафик (скачано / отдано)\n"
        f"Сегодня: {human_bytes(t_today.tx)} / {human_bytes(t_today.rx)}\n"
        f"За месяц: {human_bytes(t_month.tx)} / {human_bytes(t_month.rx)}\n"
        f"Всего: {human_bytes(t_all.tx)} / {human_bytes(t_all.rx)}"
    )
    kb = ikb(
        [
            [("📤 Получить ключ", Dev(action="key", id=key.id).pack())],
            [("✏️ Переименовать", Dev(action="ren", id=key.id).pack()), ("🗑 Удалить", Dev(action="del", id=key.id).pack())],
            [back(Menu(action="devices"))],
        ]
    )
    await edit_or_send(call, text, kb)


@router.callback_query(Dev.filter(F.action == "key"))
async def cb_device_key(call: CallbackQuery, callback_data: Dev, bot: Bot, db: Database, service: VpnService, user: User) -> None:
    key = await _own_key(call, db, user, callback_data.id)
    if key is None:
        return
    if not key.enabled:
        await call.answer("Устройство отключено. Продлите подписку, и ключ снова заработает.", show_alert=True)
    else:
        await call.answer()
    try:
        rk = await service.render(key)
    except AwgError:
        log.exception("Ошибка чтения конфига сервера")
        await bot.send_message(call.from_user.id, "❌ Сервер недоступен, попробуйте позже.")
        return
    await deliver_key(bot, call.from_user.id, rk)


@router.callback_query(Dev.filter(F.action == "ren"))
async def cb_device_rename(call: CallbackQuery, callback_data: Dev, state: FSMContext, db: Database, user: User) -> None:
    key = await _own_key(call, db, user, callback_data.id)
    if key is None:
        return
    await call.answer()
    await state.set_state(UserStates.rename)
    await state.update_data(key_id=key.id)
    await call.message.answer(f"Введите новое название для «{esc(key.name)}»:")


@router.message(UserStates.rename, F.text, ~F.text.in_(MENU_TEXTS), ~F.text.startswith("/"))
async def st_rename(message: Message, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    data = await state.get_data()
    await state.clear()
    key = await db.get_key(data.get("key_id", 0))
    if key is None or key.tg_id != user.tg_id:
        return
    await service.rename_device(key, message.text.strip() or key.name)
    await message.answer("✅ Переименовано.", reply_markup=ikb([[("🔑 Мои устройства", Menu(action="devices").pack())]]))


@router.callback_query(Dev.filter(F.action == "del"))
async def cb_device_del(call: CallbackQuery, callback_data: Dev, db: Database, user: User) -> None:
    key = await _own_key(call, db, user, callback_data.id)
    if key is None:
        return
    await call.answer()
    await edit_or_send(
        call,
        f"Удалить устройство <b>{esc(key.name)}</b>? Ключ перестанет работать, а статистика устройства будет удалена.",
        ikb([[("🗑 Да, удалить", Dev(action="delok", id=key.id).pack()), back(Dev(action="view", id=key.id), "Отмена")]]),
    )


@router.callback_query(Dev.filter(F.action == "delok"))
async def cb_device_delok(call: CallbackQuery, callback_data: Dev, db: Database, service: VpnService, user: User) -> None:
    key = await _own_key(call, db, user, callback_data.id)
    if key is None:
        return
    try:
        await service.delete_device(key)
    except AwgError:
        log.exception("Ошибка удаления устройства")
        await call.answer("Не удалось удалить, попробуйте позже", show_alert=True)
        return
    await call.answer("Устройство удалено")
    text, kb = await devices_view(db, service, user)
    await edit_or_send(call, text, kb)


# ---------- тарифы и оплата ----------


async def plans_view(db: Database, service: VpnService, user: User) -> tuple[str, object]:
    cur = service.settings.currency
    plans = await db.plans()
    if user.active:
        head = f"Подписка активна до <b>{fmt_dt(user.sub_until)}</b>. Продление добавится к текущему сроку.\n\n"
    else:
        head = ""
    text = head + "💳 <b>Выберите тариф:</b>"
    rows = [
        [(f"{p.title} · {devices_word(p.devices)} · {p.price} {cur}", Buy(action="plan", plan_id=p.id).pack())]
        for p in plans
    ]
    if service.trial_available(user):
        rows.append([("🎁 Бесплатный пробный период", Menu(action="trial").pack())])
    if not plans:
        text = "Тарифы пока не настроены. Напишите администратору."
    return text, ikb(rows)


@router.message(Command("plans"))
@router.message(F.text == BTN_PLANS)
async def msg_plans(message: Message, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    await state.clear()
    text, kb = await plans_view(db, service, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Menu.filter(F.action == "plans"))
async def cb_plans(call: CallbackQuery, db: Database, service: VpnService, user: User) -> None:
    await call.answer()
    text, kb = await plans_view(db, service, user)
    await edit_or_send(call, text, kb)


@router.callback_query(Buy.filter(F.action == "plan"))
async def cb_buy_plan(call: CallbackQuery, callback_data: Buy, db: Database, service: VpnService) -> None:
    plan = await db.get_plan(callback_data.plan_id)
    if plan is None or not plan.active:
        await call.answer("Тариф недоступен", show_alert=True)
        return
    await call.answer()
    s = service.settings
    text = (
        f"<b>{esc(plan.title)}</b>\n"
        f"Срок: {days_word(plan.days)}, {devices_word(plan.devices)}\n"
        f"К оплате: <b>{plan.price} {s.currency}</b>\n\n"
        f"{s.payment_details}\n\n"
        "После оплаты нажмите «✅ Я оплатил» и отправьте скриншот или чек."
    )
    await edit_or_send(
        call,
        text,
        ikb([[("✅ Я оплатил", Buy(action="paid", plan_id=plan.id).pack())], [back(Menu(action="plans"))]]),
    )


@router.callback_query(Buy.filter(F.action == "paid"))
async def cb_buy_paid(call: CallbackQuery, callback_data: Buy, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(UserStates.receipt)
    await state.update_data(plan_id=callback_data.plan_id)
    await call.message.answer(
        "📎 Отправьте скриншот или чек об оплате (фото, файл или текстом).\n"
        "Чтобы отменить — /cancel"
    )


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext, is_admin: bool) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu(is_admin))


@router.message(UserStates.receipt, F.photo | F.document | (F.text & ~F.text.in_(MENU_TEXTS) & ~F.text.startswith("/")))
async def st_receipt(
    message: Message, bot: Bot, state: FSMContext, db: Database, service: VpnService, user: User
) -> None:
    data = await state.get_data()
    plan = await db.get_plan(data.get("plan_id", 0))
    await state.clear()
    if plan is None:
        await message.answer("Тариф не найден, выберите его заново в «💳 Тарифы».")
        return
    if message.photo:
        rtype, receipt = "photo", message.photo[-1].file_id
    elif message.document:
        rtype, receipt = "document", message.document.file_id
    else:
        rtype, receipt = "text", (message.text or "")[:1000]
    payment = await db.create_payment(user.tg_id, plan, rtype, receipt)
    await message.answer(
        f"✅ Заявка на оплату №{payment.id} отправлена. Подписка активируется после проверки администратором — "
        "обычно это занимает немного времени. Я пришлю уведомление."
    )
    await send_payment_to_admins(bot, service, payment.id)


async def send_payment_to_admins(bot: Bot, service: VpnService, payment_id: int) -> None:
    db = service.db
    p = await db.get_payment(payment_id)
    if p is None:
        return
    u = await db.get_user(p.tg_id)
    who = esc(u.title) if u else str(p.tg_id)
    caption = (
        f"💳 <b>Оплата №{p.id}</b>\n"
        f"От: {who} (<code>{p.tg_id}</code>)\n"
        f"Тариф: {esc(p.title)} — {days_word(p.days)}, {devices_word(p.devices)}\n"
        f"Сумма: <b>{p.amount} {service.settings.currency}</b>"
    )
    if p.receipt_type == "text":
        caption += f"\n\nКомментарий: {esc(p.receipt)}"
    kb = ikb(
        [
            [("✅ Подтвердить", APay(action="ok", id=p.id).pack()), ("❌ Отклонить", APay(action="no", id=p.id).pack())],
            [("👤 Пользователь", AU(action="card", uid=p.tg_id).pack())],
        ]
    )
    for admin_id in service.settings.admin_ids:
        try:
            if p.receipt_type == "photo":
                await bot.send_photo(admin_id, p.receipt, caption=caption, reply_markup=kb)
            elif p.receipt_type == "document":
                await bot.send_document(admin_id, p.receipt, caption=caption, reply_markup=kb)
            else:
                await bot.send_message(admin_id, caption, reply_markup=kb)
        except Exception:
            log.warning("Не удалось отправить платёж админу %s", admin_id)


# ---------- рефералы ----------


@router.message(Command("ref"))
@router.message(F.text == BTN_REF)
async def msg_ref(message: Message, bot: Bot, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    await state.clear()
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref_{user.tg_id}"
    total, paid = await db.referrals_count(user.tg_id)
    bonus = service.settings.ref_bonus_days
    await message.answer(
        "👥 <b>Пригласите друга</b>\n\n"
        f"За каждого друга, который оплатит подписку, вы получите <b>+{days_word(bonus)}</b>.\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {total}, оплатили: {paid}"
    )
