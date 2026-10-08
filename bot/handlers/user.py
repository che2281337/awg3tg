from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..awg.server import AwgError
from ..db import PROTOCOLS, Database, User, now
from ..service import ServiceError, VpnService
from ..utils import (
    days_word,
    devices_word,
    esc,
    fmt_date,
    fmt_dt,
    human_bytes,
    left_str,
    month_start_day,
    plural,
    today,
)
from .common import deliver_key, edit_or_send, help_text, notify_admins
from .keyboards import (
    BTN_DEVICES,
    BTN_HELP,
    BTN_PLANS,
    BTN_PROFILE,
    BTN_REF,
    MENU_TEXTS,
    AU,
    APay,
    PROTO_BUTTONS,
    PROTO_ICONS,
    SlotCb,
    Buy,
    Dev,
    DevProto,
    Loc,
    Menu,
    back,
    ikb,
    main_menu,
)

log = logging.getLogger(__name__)
router = Router(name="user")

# Сколько неподтверждённых заявок на оплату может висеть у одного пользователя
MAX_PENDING_PAYMENTS = 2


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
    offer = await service.first_offer(user)
    if offer:
        plan, price = offer
        await message.answer(
            f"🔥 Для новых пользователей — <b>{esc(plan.title)} за {price} {service.settings.currency}</b> "
            f"вместо {plan.price} {service.settings.currency}!",
            reply_markup=ikb([[("💳 Оформить со скидкой", Buy(action="plan", plan_id=plan.id).pack())]]),
        )


async def slots_line(db: Database, service: VpnService, user: User) -> str:
    """«2 по тарифу + 1 доп. слот (до 07.11)» — если есть доп. слоты."""
    slots = await db.user_slots(user.tg_id, active_only=True)
    if not slots or service.is_admin(user.tg_id):
        return ""
    dates = ", ".join(fmt_date(s.until) for s in slots)
    return f" ({service.base_limit(user)} по тарифу + {plural(len(slots), 'доп. слот', 'доп. слота', 'доп. слотов')} до {dates})"


async def profile_view(db: Database, service: VpnService, user: User) -> tuple[str, object]:
    keys = await db.user_keys(user.tg_id)
    limit = await service.device_limit(user)
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
        f"Устройства: {len(keys)} из {limit}{await slots_line(db, service, user)}\n\n"
        f"📊 <b>Трафик</b> (скачано / отдано)\n"
        f"Сегодня: {human_bytes(t_today.tx)} / {human_bytes(t_today.rx)}\n"
        f"За месяц: {human_bytes(t_month.tx)} / {human_bytes(t_month.rx)}\n"
        f"Всего: {human_bytes(t_all.tx)} / {human_bytes(t_all.rx)}"
    )
    rows = [[("💳 Продлить подписку" if user.active else "💳 Оформить подписку", Menu(action="plans").pack())]]
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
    limit = await service.device_limit(user)
    lines = [f"🔑 <b>Мои устройства</b> ({len(keys)} из {limit}){await slots_line(db, service, user)}\n"]
    if not service.has_access(user):
        lines.append("⛔ Подписка не активна — устройства отключены.\n")
    online_border = now() - 180
    flags = {s.id: s.flag for s in await db.servers()}
    for k in keys:
        if not k.enabled:
            state = "⏸"
        elif k.last_handshake >= online_border:
            state = "🟢"
        else:
            state = "⚪️"
        lines.append(f"{state} {flags.get(k.server_id, '❔')} {esc(k.name)} · {k.protocol_name}")
    if not keys:
        lines.append("Устройств пока нет.")
    rows = [
        [(f"{flags.get(k.server_id, '❔')} {PROTO_ICONS.get(k.protocol, '')} {k.name}", Dev(action="view", id=k.id).pack())]
        for k in keys
    ]
    if service.has_access(user) and len(keys) < limit:
        rows.append([("➕ Добавить устройство", Menu(action="add").pack())])
    elif service.has_access(user):
        s = service.settings
        rows.append(
            [(f"➕ Ещё устройство — {s.slot_price} {s.currency} / {s.slot_days} дн.", SlotCb(action="buy").pack())]
        )
    else:
        rows.append([("💳 Оформить подписку", Menu(action="plans").pack())])
    return "\n".join(lines), ikb(rows)


@router.message(Command("devices"))
@router.message(F.text == BTN_DEVICES)
async def msg_devices(message: Message, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    await state.clear()
    text, kb = await devices_view(db, service, user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Menu.filter(F.action == "devices"))
async def cb_devices(call: CallbackQuery, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    await state.clear()
    await call.answer()
    text, kb = await devices_view(db, service, user)
    await edit_or_send(call, text, kb)


@router.callback_query(Menu.filter(F.action == "add"))
async def cb_add_device(call: CallbackQuery, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    if not service.has_access(user):
        await call.answer("Нет активной подписки", show_alert=True)
        return
    if len(await db.user_keys(user.tg_id)) >= await service.device_limit(user):
        await cb_slot_offer(call, SlotCb(action="buy"), service, user)
        return
    servers = await service.available_servers()
    if not servers:
        await call.answer("Сейчас нет доступных серверов, попробуйте чуть позже", show_alert=True)
        return
    await call.answer()
    if len(servers) == 1:  # выбирать не из чего — сразу к протоколу
        await _ask_protocol(call, state, servers[0])
        return
    rows = [[(s.title, Loc(action="new", key=0, sid=s.id).pack())] for s in servers]
    rows.append([back(Menu(action="devices"), "Отмена")])
    await edit_or_send(call, "🌍 Выберите страну сервера:", ikb(rows))


async def _ask_protocol(call: CallbackQuery, state: FSMContext, server) -> None:
    protocols = server.protocol_list
    if len(protocols) == 1:
        await _ask_device_name(call, state, server.id, protocols[0])
        return
    rows = [[(PROTO_BUTTONS[p], DevProto(sid=server.id, proto=p).pack())] for p in protocols]
    rows.append([back(Menu(action="devices"), "Отмена")])
    await edit_or_send(
        call,
        f"Сервер: {esc(server.title)}\n\n🔐 <b>Выберите протокол</b>\n\n"
        "🛡 <b>AmneziaWG</b> — быстрый, для приложения AmneziaVPN.\n"
        "⚡ <b>VLESS</b> — маскируется под обычный HTTPS-сайт, хорошо работает там, где блокируют VPN. "
        "Подходит для AmneziaVPN, v2rayNG, Hiddify, Streisand, FoXray.\n\n"
        "Если не уверены — начните с AmneziaWG, а при проблемах со связью добавьте VLESS.",
        ikb(rows),
    )


async def _ask_device_name(call: CallbackQuery, state: FSMContext, server_id: int, protocol: str) -> None:
    await state.set_state(UserStates.device_name)
    await state.update_data(server_id=server_id, protocol=protocol)
    await edit_or_send(
        call,
        "Введите название устройства, например «Мой айфон» или «Ноутбук».\nОтмена — /cancel",
        ikb([[back(Menu(action="devices"), "Отмена")]]),
    )


@router.callback_query(Loc.filter(F.action == "new"))
async def cb_new_location(call: CallbackQuery, callback_data: Loc, state: FSMContext, service: VpnService) -> None:
    servers = {s.id: s for s in await service.available_servers()}
    if callback_data.sid not in servers:
        await call.answer("Эта локация сейчас недоступна, выберите другую", show_alert=True)
        return
    await call.answer()
    await _ask_protocol(call, state, servers[callback_data.sid])


@router.callback_query(DevProto.filter())
async def cb_device_protocol(call: CallbackQuery, callback_data: DevProto, state: FSMContext, service: VpnService) -> None:
    servers = {s.id: s for s in await service.available_servers(callback_data.proto)}
    if callback_data.sid not in servers:
        await call.answer("Этот протокол сейчас недоступен на сервере", show_alert=True)
        return
    await call.answer()
    await _ask_device_name(call, state, callback_data.sid, callback_data.proto)


async def _create_and_send(
    bot: Bot,
    chat_id: int,
    db: Database,
    service: VpnService,
    user: User,
    name: str,
    server_id: int,
    protocol: str = "awg",
) -> None:
    existing = {k.name for k in await db.user_keys(user.tg_id)}
    final, n = name, 2
    while final in existing:
        final, n = f"{name} {n}", n + 1
    wait = await bot.send_message(chat_id, "⏳ Создаю ключ…")
    try:
        rk = await service.create_device(user, final, server_id, protocol)
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


@router.message(UserStates.device_name, F.text, ~F.text.in_(MENU_TEXTS), ~F.text.startswith("/"))
async def st_device_name(message: Message, bot: Bot, state: FSMContext, db: Database, service: VpnService, user: User) -> None:
    data = await state.get_data()
    await state.clear()
    name = message.text.strip()[:40] or "Устройство"
    server_id, protocol = data.get("server_id"), data.get("protocol", "awg")
    if server_id is None:
        servers = await service.available_servers(protocol)
        if not servers:
            await message.answer("Сейчас нет доступных серверов, попробуйте чуть позже.")
            return
        server_id = servers[0].id
    await _create_and_send(bot, message.chat.id, db, service, user, name, server_id, protocol)


async def _own_key(call: CallbackQuery, db: Database, user: User, key_id: int):
    key = await db.get_key(key_id)
    if key is None or key.tg_id != user.tg_id:
        await call.answer("Устройство не найдено", show_alert=True)
        return None
    return key


@router.callback_query(Dev.filter(F.action == "view"))
async def cb_device_view(call: CallbackQuery, callback_data: Dev, db: Database, service: VpnService, user: User) -> None:
    key = await _own_key(call, db, user, callback_data.id)
    if key is None:
        return
    await call.answer()
    t_today = await db.traffic(key_id=key.id, since=today())
    t_month = await db.traffic(key_id=key.id, since=month_start_day())
    t_all = await db.traffic(key_id=key.id)
    server = await db.get_server(key.server_id) if key.server_id else None
    location = server.title if server else "❔ сервер удалён — смените локацию"
    if server and not server.status_ok:
        location += " (🔴 временно недоступен)"
    status = "✅ активно" if key.enabled else "⏸ отключено (нет подписки или превышен лимит устройств)"
    text = (
        f"🔑 <b>{esc(key.name)}</b>\n\n"
        f"Локация: {esc(location)}\n"
        f"Протокол: {PROTO_ICONS.get(key.protocol, '')} {key.protocol_name}\n"
        f"Статус: {status}\n"
        f"Добавлено: {fmt_date(key.created_at)}\n"
        f"{'Последнее подключение' if key.protocol == 'awg' else 'Последняя активность'}: {fmt_dt(key.last_handshake)}\n\n"
        f"📊 Трафик (скачано / отдано)\n"
        f"Сегодня: {human_bytes(t_today.tx)} / {human_bytes(t_today.rx)}\n"
        f"За месяц: {human_bytes(t_month.tx)} / {human_bytes(t_month.rx)}\n"
        f"Всего: {human_bytes(t_all.tx)} / {human_bytes(t_all.rx)}"
    )
    others = [s for s in await service.available_servers(key.protocol) if s.id != key.server_id]
    kb = ikb(
        [
            [("📤 Получить ключ", Dev(action="key", id=key.id).pack())],
            [("🌍 Сменить локацию", Loc(action="pick", key=key.id, sid=0).pack())] if others else [],
            [("✏️ Переименовать", Dev(action="ren", id=key.id).pack()), ("🗑 Удалить", Dev(action="del", id=key.id).pack())],
            [back(Menu(action="devices"))],
        ]
    )
    await edit_or_send(call, text, kb)


@router.callback_query(Loc.filter(F.action == "pick"))
async def cb_pick_location(call: CallbackQuery, callback_data: Loc, db: Database, service: VpnService, user: User) -> None:
    key = await _own_key(call, db, user, callback_data.key)
    if key is None:
        return
    others = [s for s in await service.available_servers(key.protocol) if s.id != key.server_id]
    if not others:
        await call.answer("Других доступных локаций сейчас нет", show_alert=True)
        return
    await call.answer()
    rows = [[(s.title, Loc(action="move", key=key.id, sid=s.id).pack())] for s in others]
    rows.append([back(Dev(action="view", id=key.id))])
    await edit_or_send(
        call,
        f"🌍 Куда перенести <b>{esc(key.name)}</b>?\n\n"
        "После переноса придёт новый ключ — его нужно будет заново добавить в приложение, "
        "а старый удалить.",
        ikb(rows),
    )


@router.callback_query(Loc.filter(F.action == "move"))
async def cb_move_device(
    call: CallbackQuery, callback_data: Loc, bot: Bot, db: Database, service: VpnService, user: User
) -> None:
    key = await _own_key(call, db, user, callback_data.key)
    if key is None:
        return
    await call.answer("Переношу…")
    try:
        rk = await service.move_device(key, callback_data.sid)
    except ServiceError as e:
        await call.message.answer(f"❌ {e}")
        return
    except AwgError:
        log.exception("Ошибка переноса устройства")
        await call.message.answer("❌ Сервер не отвечает, попробуйте позже или выберите другую локацию.")
        return
    await edit_or_send(call, f"✅ Устройство перенесено: {esc(rk.location)}. Добавьте новый ключ в приложение:")
    await deliver_key(bot, call.from_user.id, rk)


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
    except ServiceError as e:
        await bot.send_message(call.from_user.id, f"❌ {e}")
        return
    except AwgError:
        log.exception("Ошибка чтения конфига сервера")
        await bot.send_message(
            call.from_user.id, "❌ Сервер недоступен, попробуйте позже или смените локацию в карточке устройства."
        )
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
    except (AwgError, ServiceError):
        log.exception("Ошибка удаления устройства")
        await call.answer("Сервер не отвечает, удалить не получилось — попробуйте позже", show_alert=True)
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
    rows = []
    discounted = False
    for p in plans:
        price = await service.price_for(user, p)
        if price < p.price:
            discounted = True
            label = f"🔥 {p.title} · {devices_word(p.devices)} · {price} {cur} (вместо {p.price})"
        else:
            label = f"{p.title} · {devices_word(p.devices)} · {p.price} {cur}"
        rows.append([(label, Buy(action="plan", plan_id=p.id).pack())])
    if discounted:
        text += "\n\n🔥 Скидка для новых пользователей на первую оплату."
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


# «srv» оставлен для старых кнопок в чатах (раньше при покупке выбирали сервер).
@router.callback_query(Buy.filter(F.action.in_({"plan", "srv"})))
async def cb_buy_plan(call: CallbackQuery, callback_data: Buy, db: Database, service: VpnService, user: User) -> None:
    """Шаг 2: реквизиты для оплаты. Сервер выбирается позже — при создании устройства."""
    plan = await db.get_plan(callback_data.plan_id)
    if plan is None or not plan.active:
        await call.answer("Тариф недоступен", show_alert=True)
        return
    await call.answer()
    s = service.settings
    price = await service.price_for(user, plan)
    note = f" <s>{plan.price} {s.currency}</s> — скидка новичка" if price < plan.price else ""
    text = (
        f"<b>{esc(plan.title)}</b>\n"
        f"Срок: {days_word(plan.days)}, {devices_word(plan.devices)}\n"
        f"К оплате: <b>{price} {s.currency}</b>{note}\n\n"
        f"{s.payment_details}\n\n"
        "После оплаты нажмите «✅ Я оплатил» и отправьте скриншот или чек."
    )
    await edit_or_send(
        call,
        text,
        ikb([[("✅ Я оплатил", Buy(action="paid", plan_id=plan.id).pack())], [back(Menu(action="plans"))]]),
    )


# ---------- доп. слоты устройств ----------


@router.callback_query(SlotCb.filter(F.action.in_({"buy", "renew"})))
async def cb_slot_offer(call: CallbackQuery, callback_data: SlotCb, service: VpnService, user: User) -> None:
    """Покупка ещё одного устройства сверх тарифа (или продление слота)."""
    s = service.settings
    if not service.has_access(user):
        await call.answer("Сначала оформите или продлите подписку в «💳 Тарифы»", show_alert=True)
        return
    slot = None
    if callback_data.action == "renew":
        slot = await service.db.get_slot(callback_data.id)
        if slot is None or slot.tg_id != user.tg_id:
            await call.answer("Слот не найден", show_alert=True)
            return
    await call.answer()
    if slot:
        head = (
            f"🔁 <b>Продление доп. слота устройства</b>\n\n"
            f"Сейчас действует до {fmt_dt(slot.until)}, продление добавит {days_word(s.slot_days)}.\n"
        )
    else:
        limit = await service.device_limit(user)
        head = (
            f"➕ <b>Ещё одно устройство</b>\n\n"
            f"Сейчас доступно {devices_word(limit)}. Дополнительный слот добавит ещё одно устройство "
            f"на {days_word(s.slot_days)} (подписка при этом не меняется).\n"
        )
    text = (
        head
        + f"К оплате: <b>{s.slot_price} {s.currency}</b>\n\n"
        f"{s.payment_details}\n\n"
        "После оплаты нажмите «✅ Я оплатил» и отправьте скриншот или чек."
    )
    await edit_or_send(
        call,
        text,
        ikb([[("✅ Я оплатил", SlotCb(action="paid", id=slot.id if slot else 0).pack())], [back(Menu(action="devices"))]]),
    )


@router.callback_query(SlotCb.filter(F.action == "paid"))
async def cb_slot_paid(call: CallbackQuery, callback_data: SlotCb, state: FSMContext, db: Database, user: User) -> None:
    if await db.pending_count(user.tg_id) >= MAX_PENDING_PAYMENTS:
        await call.answer("У вас уже есть заявки на проверке. Дождитесь решения администратора.", show_alert=True)
        return
    await call.answer()
    await state.set_state(UserStates.receipt)
    await state.update_data(kind="slot", slot_id=callback_data.id or None)
    await call.message.answer(
        "📎 Отправьте скриншот или чек об оплате (фото, файл или текстом).\n"
        "Чтобы отменить — /cancel"
    )


@router.callback_query(Buy.filter(F.action == "paid"))
async def cb_buy_paid(call: CallbackQuery, callback_data: Buy, state: FSMContext, db: Database, user: User) -> None:
    plan = await db.get_plan(callback_data.plan_id)
    if plan is None or not plan.active:
        await call.answer("Тариф недоступен", show_alert=True)
        return
    if await db.pending_count(user.tg_id) >= MAX_PENDING_PAYMENTS:
        await call.answer("У вас уже есть заявки на проверке. Дождитесь решения администратора.", show_alert=True)
        return
    await call.answer()
    await state.set_state(UserStates.receipt)
    await state.update_data(plan_id=callback_data.plan_id, server_id=callback_data.sid or None)
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
    await state.clear()
    if message.photo:
        rtype, receipt = "photo", message.photo[-1].file_id
    elif message.document:
        rtype, receipt = "document", message.document.file_id
    else:
        rtype, receipt = "text", (message.text or "")[:1000]
    if data.get("kind") == "slot":
        await _slot_receipt(message, bot, db, service, user, data.get("slot_id"), rtype, receipt)
        return
    plan = await db.get_plan(data.get("plan_id", 0))
    # Проверяем ещё раз: callback_data можно подделать и подсунуть скрытый тариф.
    if plan is None or not plan.active:
        await message.answer("Тариф не найден, выберите его заново в «💳 Тарифы».")
        return
    if await db.pending_count(user.tg_id) >= MAX_PENDING_PAYMENTS:
        await message.answer("У вас уже есть заявки на проверке. Дождитесь решения администратора.")
        return
    # Цена считается заново на сервере — скидку нельзя «принести» из старой кнопки.
    price = await service.price_for(user, plan)
    title = plan.title + (" (скидка новичка)" if price < plan.price else "")
    server_id = data.get("server_id")
    if server_id and await db.get_server(server_id) is None:
        server_id = None
    payment = await db.create_payment(
        user.tg_id, plan, rtype, receipt, amount=price, title=title, server_id=server_id
    )
    await message.answer(
        f"✅ Заявка на оплату №{payment.id} отправлена. Подписка активируется после проверки администратором — "
        "обычно это занимает немного времени. Я пришлю уведомление."
    )
    await send_payment_to_admins(bot, service, payment.id)


async def _slot_receipt(
    message: Message, bot: Bot, db: Database, service: VpnService, user: User, slot_id, rtype: str, receipt: str
) -> None:
    if await db.pending_count(user.tg_id) >= MAX_PENDING_PAYMENTS:
        await message.answer("У вас уже есть заявки на проверке. Дождитесь решения администратора.")
        return
    if slot_id:
        slot = await db.get_slot(slot_id)
        if slot is None or slot.tg_id != user.tg_id:  # чужой или удалённый слот — покупаем новый
            slot_id = None
    s = service.settings
    # Цена и срок берутся из настроек на сервере, а не из кнопки.
    payment = await db.create_slot_payment(user.tg_id, s.slot_price, s.slot_days, rtype, receipt, slot_id)
    await message.answer(
        f"✅ Заявка на оплату №{payment.id} отправлена. Слот добавится после проверки администратором — "
        "я пришлю уведомление."
    )
    await send_payment_to_admins(bot, service, payment.id)


async def send_payment_to_admins(bot: Bot, service: VpnService, payment_id: int) -> None:
    db = service.db
    p = await db.get_payment(payment_id)
    if p is None:
        return
    u = await db.get_user(p.tg_id)
    who = esc(u.title) if u else str(p.tg_id)
    server = await db.get_server(p.server_id) if p.server_id else None
    caption = (
        f"💳 <b>Оплата №{p.id}</b>\n"
        f"От: {who} (<code>{p.tg_id}</code>)\n"
        + (
            f"Тариф: {esc(p.title)} — {days_word(p.days)}, {devices_word(p.devices)}\n"
            if p.kind == "plan"
            else f"Покупка: {esc(p.title)} — +1 устройство на {days_word(p.days)}\n"
        )
        + (f"Сервер: {esc(server.title)}\n" if server else "")
        + f"Сумма: <b>{p.amount} {service.settings.currency}</b>"
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
