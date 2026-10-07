from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..awg.server import AwgError
from ..db import PROTOCOLS, Database, User, now
from ..service import MAX_DAYS, MAX_DEVICES, ServiceError, VpnService
from ..utils import (
    day_start_ts,
    days_word,
    devices_word,
    esc,
    fmt_date,
    fmt_dt,
    human_bytes,
    left_str,
    month_start_day,
    month_start_ts,
    plural,
    today,
)
from .common import deliver_key, edit_or_send, send_long
from .keyboards import (
    AK,
    AU,
    BTN_ADMIN,
    MENU_TEXTS,
    AAdd,
    Adm,
    AMove,
    APay,
    APlan,
    ASrv,
    Bcast,
    UList,
    back,
    ikb,
    main_menu,
)

log = logging.getLogger(__name__)
router = Router(name="admin")

PAGE = 10
MAX_PLAN_DAYS = 3650
MAX_PRICE = 10_000_000
FILTERS = {"all": "Все", "active": "С подпиской", "expired": "Без подписки", "banned": "Бан"}
BCAST_TARGETS = {"all": "всем", "active": "с активной подпиской", "expired": "без подписки"}
PROTO_SHORT = {"awg": "AWG", "vless": "VLESS"}


def _is_admin(event: TelegramObject, service: VpnService) -> bool:
    user = getattr(event, "from_user", None)
    return user is not None and service.is_admin(user.id)


router.message.filter(_is_admin)
router.callback_query.filter(_is_admin)

_INPUT = (F.text & ~F.text.in_(MENU_TEXTS) & ~F.text.startswith("/"))


class AdminStates(StatesGroup):
    find = State()
    extend = State()
    message = State()
    broadcast = State()
    plan = State()
    server = State()


def panel_kb(pending: int):
    return ikb(
        [
            [("👥 Пользователи", UList(flt="all").pack()), (f"💳 Платежи ({pending})", Adm(action="payments").pack())],
            [("📊 Статистика", Adm(action="stats").pack()), ("📦 Тарифы", Adm(action="plans").pack())],
            [("📢 Оповещение всем", Adm(action="bcast").pack()), ("🖥 Серверы", ASrv(action="list").pack())],
            [("🧹 Неактивные", Adm(action="inactive", arg=30).pack()), ("🔎 Поиск", Adm(action="find").pack())],
        ]
    )


PANEL_TEXT = (
    "🛠 <b>Админ-панель</b>\n\n"
    "Команды: /user <code>ID|@ник</code>, /extend <code>ID дни</code>, /ban <code>ID</code>, "
    "/unban <code>ID</code>, /msg <code>ID текст</code>, /inactive <code>дни</code>, "
    "/backup — резервная копия базы"
)


@router.message(Command("admin"))
@router.message(F.text == BTN_ADMIN)
async def cmd_admin(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    pending = len(await db.pending_payments())
    await message.answer(PANEL_TEXT, reply_markup=panel_kb(pending))


@router.callback_query(Adm.filter(F.action == "panel"))
async def cb_panel(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await call.answer()
    await edit_or_send(call, PANEL_TEXT, panel_kb(len(await db.pending_payments())))


# ---------- список и поиск пользователей ----------


def _user_line(u: User, service: VpnService) -> str:
    if u.banned:
        mark = "⛔"
    elif u.active:
        mark = "✅"
    else:
        mark = "▫️"
    sub = f"до {fmt_date(u.sub_until)}" if u.active else "нет подписки"
    return f"{mark} {u.title} · {sub}"


@router.callback_query(UList.filter())
async def cb_users(call: CallbackQuery, callback_data: UList, db: Database, service: VpnService) -> None:
    await call.answer()
    flt, page = callback_data.flt, callback_data.page
    users, total = await db.list_users(flt, page * PAGE, PAGE)
    pages = max(1, (total + PAGE - 1) // PAGE)
    rows = [[(("• " if f == flt else "") + title, UList(flt=f).pack()) for f, title in FILTERS.items()]]
    rows += [[(_user_line(u, service), AU(action="card", uid=u.tg_id).pack())] for u in users]
    nav = []
    if page > 0:
        nav.append(("◀️", UList(flt=flt, page=page - 1).pack()))
    nav.append((f"{page + 1}/{pages}", UList(flt=flt, page=page).pack()))
    if page + 1 < pages:
        nav.append(("▶️", UList(flt=flt, page=page + 1).pack()))
    rows.append(nav)
    rows.append([("🔎 Поиск", Adm(action="find").pack()), back(Adm(action="panel"))])
    await edit_or_send(call, f"👥 <b>Пользователи</b> — {FILTERS[flt].lower()}: {total}", ikb(rows))


@router.callback_query(Adm.filter(F.action == "find"))
async def cb_find(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.find)
    await call.message.answer("Введите ID, @username или часть имени:")


async def _show_found(message: Message, db: Database, service: VpnService, query: str) -> None:
    users = await db.find_users(query)
    if not users:
        await message.answer("Никого не нашёл.")
    elif len(users) == 1:
        text, kb = await user_card(db, service, users[0].tg_id)
        await message.answer(text, reply_markup=kb)
    else:
        rows = [[(_user_line(u, service), AU(action="card", uid=u.tg_id).pack())] for u in users]
        await message.answer(f"Найдено: {len(users)}", reply_markup=ikb(rows))


@router.message(AdminStates.find, _INPUT)
async def st_find(message: Message, state: FSMContext, db: Database, service: VpnService) -> None:
    await state.clear()
    await _show_found(message, db, service, message.text)


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, db: Database, service: VpnService) -> None:
    if not command.args:
        await message.answer("Использование: /user ID или /user @username")
        return
    await _show_found(message, db, service, command.args)


# ---------- карточка пользователя ----------


async def user_card(db: Database, service: VpnService, uid: int):
    u = await db.get_user(uid)
    if u is None:
        return "Пользователь не найден (возможно, удалён).", ikb([[back(UList(flt="all"))]])
    keys = await db.user_keys(uid)
    t_today = await db.traffic(tg_id=uid, since=today())
    t_month = await db.traffic(tg_id=uid, since=month_start_day())
    t_all = await db.traffic(tg_id=uid)
    refs_total, refs_paid = await db.referrals_count(uid)
    payments = await db.user_payments(uid, 100)
    paid = [p for p in payments if p.status == "paid"]
    referrer = await db.get_user(u.referrer_id) if u.referrer_id else None
    if u.active:
        sub = f"✅ до <b>{fmt_dt(u.sub_until)}</b> ({left_str(u.sub_until, now())})"
    elif u.sub_until:
        sub = f"⛔ закончилась {fmt_dt(u.sub_until)}"
    else:
        sub = "нет"
    online = sum(1 for k in keys if k.enabled and k.last_handshake >= now() - 180)
    text = (
        f"👤 <b>{esc(u.full_name or u.title)}</b>{' ⛔ БАН' if u.banned else ''}\n"
        f"ID: <code>{u.tg_id}</code>{' · @' + esc(u.username) if u.username else ''}\n"
        f"Регистрация: {fmt_dt(u.created_at)}, был в боте: {fmt_dt(u.last_seen)}\n"
        f"Подписка: {sub}\n"
        f"Устройства: {len(keys)} из {service.device_limit(u)} (онлайн {online})\n"
        f"Скидка новичка: {'использована' if paid else 'доступна'}\n"
        f"Оплат: {len(paid)} на {sum(p.amount for p in paid)} {service.settings.currency}\n"
        f"Рефералов: {refs_total} (оплатили {refs_paid})"
        + (f", пригласил: {esc(referrer.title)}" if referrer else "")
        + "\n\n"
        f"📊 Трафик (скачано / отдано)\n"
        f"Сегодня: {human_bytes(t_today.tx)} / {human_bytes(t_today.rx)}\n"
        f"Месяц: {human_bytes(t_month.tx)} / {human_bytes(t_month.rx)}\n"
        f"Всего: {human_bytes(t_all.tx)} / {human_bytes(t_all.rx)}"
    )
    a = lambda action, arg=0: AU(action=action, uid=uid, arg=arg).pack()  # noqa: E731
    kb = ikb(
        [
            [("+7 дн", a("ext", 7)), ("+30 дн", a("ext", 30)), ("+90 дн", a("ext", 90)), ("± N дн", a("extc"))],
            [("📱 −1", a("dev", -1)), (f"Лимит: {u.device_limit or service.settings.default_devices}", a("card")), ("📱 +1", a("dev", 1))],
            [("⏹ Обнулить подписку", a("sub0"))],
            [(f"🔑 Устройства ({len(keys)})", a("keys")), (f"💳 Платежи ({len(payments)})", a("pays"))],
            [("✉️ Написать", a("msg")), ("✅ Разбанить" if u.banned else "⛔ Забанить", a("unban" if u.banned else "ban"))],
            [("🗑 Удалить аккаунт", a("del"))],
            [back(UList(flt="all"), "⬅️ К списку")],
        ]
    )
    return text, kb


async def _refresh_card(call: CallbackQuery, db: Database, service: VpnService, uid: int) -> None:
    text, kb = await user_card(db, service, uid)
    await edit_or_send(call, text, kb)


async def _notify(bot: Bot, uid: int, text: str) -> None:
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass


@router.callback_query(AU.filter(F.action == "card"))
async def cb_card(call: CallbackQuery, callback_data: AU, db: Database, service: VpnService) -> None:
    await call.answer()
    await _refresh_card(call, db, service, callback_data.uid)


async def _extend(bot: Bot, service: VpnService, uid: int, days: int) -> str:
    user = await service.db.get_user(uid)
    if user is None:
        return "Пользователь не найден"
    if days >= 0:
        user = await service.extend(uid, days)
        await _notify(bot, uid, f"🎉 Подписка продлена на {days_word(days)} — до {fmt_dt(user.sub_until)}.")
    else:
        if days < -MAX_DAYS:
            raise ServiceError(f"Срок должен быть от -{MAX_DAYS} до {MAX_DAYS} дней.")
        until = (user.sub_until or now()) + days * 86400
        await service.set_sub_until(uid, until)
        user = await service.db.get_user(uid)
    return f"Подписка до {fmt_dt(user.sub_until)}"


@router.callback_query(AU.filter(F.action == "ext"))
async def cb_extend(call: CallbackQuery, callback_data: AU, bot: Bot, db: Database, service: VpnService) -> None:
    try:
        result = await _extend(bot, service, callback_data.uid, callback_data.arg)
    except (ServiceError, AwgError) as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)
        return
    await call.answer(result)
    await _refresh_card(call, db, service, callback_data.uid)


@router.callback_query(AU.filter(F.action == "extc"))
async def cb_extend_custom(call: CallbackQuery, callback_data: AU, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.extend)
    await state.update_data(uid=callback_data.uid)
    await call.message.answer("На сколько дней продлить? Отрицательное число — уменьшить срок (например, -5).")


@router.message(AdminStates.extend, _INPUT)
async def st_extend(message: Message, bot: Bot, state: FSMContext, db: Database, service: VpnService) -> None:
    data = await state.get_data()
    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer("Нужно целое число дней, например 30 или -5.")
        return
    await state.clear()
    try:
        result = await _extend(bot, service, data["uid"], days)
    except (ServiceError, AwgError) as e:
        await message.answer(f"❌ {esc(e)}")
        return
    text, kb = await user_card(db, service, data["uid"])
    await message.answer(f"✅ {result}\n\n{text}", reply_markup=kb)


@router.message(Command("extend"))
async def cmd_extend(message: Message, command: CommandObject, bot: Bot, service: VpnService) -> None:
    args = (command.args or "").split()
    try:
        uid, days = int(args[0]), int(args[1])
    except (IndexError, ValueError):
        await message.answer("Использование: /extend ID дни (отрицательное число — уменьшить)")
        return
    try:
        await message.answer(f"✅ {await _extend(bot, service, uid, days)}")
    except (ServiceError, AwgError) as e:
        await message.answer(f"❌ {esc(e)}")


@router.callback_query(AU.filter(F.action == "sub0"))
async def cb_sub0(call: CallbackQuery, callback_data: AU, db: Database, service: VpnService) -> None:
    await service.set_sub_until(callback_data.uid, now())
    await call.answer("Подписка обнулена, устройства отключены")
    await _refresh_card(call, db, service, callback_data.uid)


@router.callback_query(AU.filter(F.action == "dev"))
async def cb_dev_limit(call: CallbackQuery, callback_data: AU, db: Database, service: VpnService) -> None:
    u = await db.get_user(callback_data.uid)
    if u is None:
        await call.answer("Не найден", show_alert=True)
        return
    current = u.device_limit or service.settings.default_devices
    new = max(0, min(current + callback_data.arg, MAX_DEVICES))
    await service.set_device_limit(u.tg_id, new)
    await call.answer(f"Лимит устройств: {new}")
    await _refresh_card(call, db, service, u.tg_id)


@router.callback_query(AU.filter(F.action.in_({"ban", "unban"})))
async def cb_ban(call: CallbackQuery, callback_data: AU, bot: Bot, db: Database, service: VpnService) -> None:
    if service.is_admin(callback_data.uid):
        await call.answer("Нельзя забанить администратора", show_alert=True)
        return
    if callback_data.action == "ban":
        await service.ban(callback_data.uid)
        await call.answer("Забанен, устройства отключены")
    else:
        await service.unban(callback_data.uid)
        await call.answer("Разбанен")
        await _notify(bot, callback_data.uid, "✅ Ваш аккаунт разблокирован.")
    await _refresh_card(call, db, service, callback_data.uid)


@router.message(Command("ban", "unban"))
async def cmd_ban(message: Message, command: CommandObject, bot: Bot, service: VpnService) -> None:
    try:
        uid = int((command.args or "").split()[0])
    except (IndexError, ValueError):
        await message.answer(f"Использование: /{command.command} ID")
        return
    if await service.db.get_user(uid) is None:
        await message.answer("Пользователь не найден.")
        return
    if command.command == "ban":
        if service.is_admin(uid):
            await message.answer("Нельзя забанить администратора.")
            return
        await service.ban(uid)
        await message.answer(f"⛔ {uid} забанен, устройства отключены.")
    else:
        await service.unban(uid)
        await _notify(bot, uid, "✅ Ваш аккаунт разблокирован.")
        await message.answer(f"✅ {uid} разбанен.")


@router.callback_query(AU.filter(F.action == "del"))
async def cb_del_user(call: CallbackQuery, callback_data: AU, db: Database) -> None:
    await call.answer()
    u = await db.get_user(callback_data.uid)
    name = esc(u.title) if u else callback_data.uid
    await edit_or_send(
        call,
        f"Удалить аккаунт {name}? Все его устройства будут удалены с сервера, статистика стёрта. "
        "История платежей сохранится.",
        ikb(
            [
                [
                    ("🗑 Да, удалить", AU(action="delok", uid=callback_data.uid).pack()),
                    back(AU(action="card", uid=callback_data.uid), "Отмена"),
                ]
            ]
        ),
    )


@router.callback_query(AU.filter(F.action == "delok"))
async def cb_del_user_ok(call: CallbackQuery, callback_data: AU, service: VpnService) -> None:
    if service.is_admin(callback_data.uid):
        await call.answer("Нельзя удалить администратора", show_alert=True)
        return
    try:
        await service.delete_user(callback_data.uid)
    except (AwgError, ServiceError) as e:
        await call.answer(f"Сервер с устройствами пользователя не отвечает: {e}"[:190], show_alert=True)
        return
    await call.answer("Аккаунт удалён")
    await edit_or_send(call, f"🗑 Аккаунт {callback_data.uid} удалён.", ikb([[back(UList(flt="all"), "⬅️ К списку")]]))


@router.callback_query(AU.filter(F.action == "msg"))
async def cb_msg(call: CallbackQuery, callback_data: AU, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.message)
    await state.update_data(uid=callback_data.uid)
    await call.message.answer("Отправьте сообщение для пользователя (текст, фото, файл). /cancel — отмена.")


@router.message(AdminStates.message, ~F.text.in_(MENU_TEXTS))
async def st_msg(message: Message, bot: Bot, state: FSMContext) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    await state.clear()
    try:
        await bot.send_message(data["uid"], "✉️ <b>Сообщение от администратора:</b>")
        await message.copy_to(data["uid"])
        await message.answer("✅ Отправлено.")
    except Exception as e:
        await message.answer(f"❌ Не доставлено: {esc(e)}")


@router.message(Command("msg"))
async def cmd_msg(message: Message, command: CommandObject, bot: Bot) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Использование: /msg ID текст")
        return
    try:
        await bot.send_message(int(parts[0]), f"✉️ <b>Сообщение от администратора:</b>\n\n{esc(parts[1])}")
        await message.answer("✅ Отправлено.")
    except Exception as e:
        await message.answer(f"❌ Не доставлено: {esc(e)}")


@router.callback_query(AU.filter(F.action == "pays"))
async def cb_user_pays(call: CallbackQuery, callback_data: AU, db: Database, service: VpnService) -> None:
    await call.answer()
    pays = await db.user_payments(callback_data.uid, 20)
    icons = {"paid": "✅", "pending": "⏳", "rejected": "❌"}
    lines = [f"💳 <b>Платежи пользователя</b> {callback_data.uid}\n"]
    for p in pays:
        lines.append(f"{icons.get(p.status, '?')} №{p.id} {fmt_dt(p.created_at)} · {esc(p.title)} · {p.amount} {service.settings.currency}")
    if not pays:
        lines.append("Платежей нет.")
    rows = [[(f"⏳ №{p.id} — открыть", APay(action="view", id=p.id).pack())] for p in pays if p.status == "pending"]
    rows.append([back(AU(action="card", uid=callback_data.uid))])
    await edit_or_send(call, "\n".join(lines), ikb(rows))


# ---------- устройства пользователя ----------


@router.callback_query(AU.filter(F.action == "keys"))
async def cb_user_keys(call: CallbackQuery, callback_data: AU, db: Database, service: VpnService) -> None:
    await call.answer()
    uid = callback_data.uid
    keys = await db.user_keys(uid)
    flags = {s.id: s.flag for s in await db.servers()}
    lines = [f"🔑 <b>Устройства</b> {uid}\n"]
    for k in keys:
        t = await db.traffic(key_id=k.id, since=month_start_day())
        state = "⏸" if not k.enabled else ("🟢" if k.last_handshake >= now() - 180 else "⚪️")
        lines.append(
            f"{state} {flags.get(k.server_id, '❔')} #{k.id} {esc(k.name)} · {PROTO_SHORT.get(k.protocol, k.protocol)}"
            f" · был {fmt_dt(k.last_handshake)} · за месяц {human_bytes(t.total)}"
        )
    if not keys:
        lines.append("Устройств нет.")
    rows = [
        [(f"{flags.get(k.server_id, '❔')} #{k.id} {PROTO_SHORT.get(k.protocol, '')} {k.name}", AK(action="view", id=k.id).pack())]
        for k in keys
    ]
    rows.append([("➕ Создать устройство", AAdd(uid=uid).pack())])
    rows.append([back(AU(action="card", uid=uid))])
    await edit_or_send(call, "\n".join(lines), ikb(rows))


@router.callback_query(AAdd.filter())
async def cb_user_addkey(call: CallbackQuery, callback_data: AAdd, bot: Bot, db: Database, service: VpnService) -> None:
    u = await db.get_user(callback_data.uid)
    if u is None:
        await call.answer("Не найден", show_alert=True)
        return
    if not callback_data.sid:  # 1) сервер
        servers = [s for s in await db.servers() if s.status_ok and s.protocol_list]
        if not servers:
            await call.answer("Нет работающих серверов", show_alert=True)
            return
        await call.answer()
        rows = [[(s.title, AAdd(uid=u.tg_id, sid=s.id).pack())] for s in servers]
        rows.append([back(AU(action="keys", uid=u.tg_id))])
        await edit_or_send(call, "На каком сервере создать устройство?", ikb(rows))
        return
    server = await db.get_server(callback_data.sid)
    if server is None:
        await call.answer("Сервер не найден", show_alert=True)
        return
    if not callback_data.proto:  # 2) протокол
        if len(server.protocol_list) == 1:
            callback_data = AAdd(uid=u.tg_id, sid=server.id, proto=server.protocol_list[0])
        else:
            await call.answer()
            rows = [[(PROTOCOLS[p], AAdd(uid=u.tg_id, sid=server.id, proto=p).pack())] for p in server.protocol_list]
            rows.append([back(AAdd(uid=u.tg_id))])
            await edit_or_send(call, f"Протокол для устройства на {esc(server.title)}:", ikb(rows))
            return
    n = len(await db.user_keys(u.tg_id)) + 1
    try:
        rk = await service.create_device(u, f"Устройство {n}", server.id, callback_data.proto, force=True)
    except (ServiceError, AwgError) as e:
        await call.answer(str(e)[:190], show_alert=True)
        return
    await call.answer("Создано")
    await call.message.answer("Ключ создан. Можно переслать его пользователю:")
    await deliver_key(bot, call.from_user.id, rk)
    await _notify(bot, u.tg_id, "🔑 Администратор добавил вам устройство — смотрите «🔑 Мои устройства».")


@router.callback_query(AK.filter(F.action == "view"))
async def cb_key_view(call: CallbackQuery, callback_data: AK, db: Database) -> None:
    key = await db.get_key(callback_data.id)
    if key is None:
        await call.answer("Ключ не найден", show_alert=True)
        return
    await call.answer()
    t_month = await db.traffic(key_id=key.id, since=month_start_day())
    t_all = await db.traffic(key_id=key.id)
    server = await db.get_server(key.server_id) if key.server_id else None
    text = (
        f"🔑 <b>#{key.id} {esc(key.name)}</b>\n"
        f"Владелец: <code>{key.tg_id}</code>\n"
        f"Сервер: {esc(server.title) if server else '❔ удалён'}\n"
        f"Протокол: {key.protocol_name}" + (f", IP {key.ip}" if key.ip else "") + "\n"
        f"Статус: {'✅ активен' if key.enabled else '⏸ отключён'}\n"
        f"Создан: {fmt_dt(key.created_at)}\nПоследняя активность: {fmt_dt(key.last_handshake)}\n"
        f"Трафик за месяц: ↓{human_bytes(t_month.tx)} ↑{human_bytes(t_month.rx)}\n"
        f"Всего: ↓{human_bytes(t_all.tx)} ↑{human_bytes(t_all.rx)}\n"
        f"{'UUID' if key.protocol == 'vless' else 'Публичный ключ'}: <code>{esc(key.public_key)}</code>"
    )
    rows = [
        [("📤 Получить ключ", AK(action="key", id=key.id).pack()), ("🗑 Удалить", AK(action="del", id=key.id).pack())],
        [("🌍 Перенести на другой сервер", AMove(key=key.id).pack())],
    ]
    if key.tg_id:
        rows.append([back(AU(action="keys", uid=key.tg_id))])
    await edit_or_send(call, text, ikb(rows))


@router.callback_query(AMove.filter())
async def cb_key_move(call: CallbackQuery, callback_data: AMove, bot: Bot, db: Database, service: VpnService) -> None:
    key = await db.get_key(callback_data.key)
    if key is None:
        await call.answer("Ключ не найден", show_alert=True)
        return
    if not callback_data.sid:
        targets = [
            s for s in await db.servers() if s.id != key.server_id and s.status_ok and key.protocol in s.protocol_list
        ]
        if not targets:
            await call.answer(f"Нет других работающих серверов с {key.protocol_name}", show_alert=True)
            return
        await call.answer()
        rows = [[(s.title, AMove(key=key.id, sid=s.id).pack())] for s in targets]
        rows.append([back(AK(action="view", id=key.id))])
        await edit_or_send(call, f"Куда перенести #{key.id} {esc(key.name)}?", ikb(rows))
        return
    try:
        rk = await service.move_device(key, callback_data.sid, force=True)
    except (ServiceError, AwgError) as e:
        await call.answer(str(e)[:190], show_alert=True)
        return
    await call.answer("Перенесено")
    await edit_or_send(call, f"✅ #{key.id} перенесён: {esc(rk.location)}", ikb([[back(AK(action="view", id=key.id))]]))
    if key.tg_id:
        await _notify(
            bot,
            key.tg_id,
            f"🌍 Ваше устройство «{esc(key.name)}» перенесено на сервер {esc(rk.location)}.\n"
            "Получите новый ключ в «🔑 Мои устройства» и добавьте его в приложение.",
        )


@router.callback_query(AK.filter(F.action == "key"))
async def cb_key_send(call: CallbackQuery, callback_data: AK, bot: Bot, db: Database, service: VpnService) -> None:
    key = await db.get_key(callback_data.id)
    if key is None:
        await call.answer("Ключ не найден", show_alert=True)
        return
    await call.answer()
    await deliver_key(bot, call.from_user.id, await service.render(key))


@router.callback_query(AK.filter(F.action == "del"))
async def cb_key_del(call: CallbackQuery, callback_data: AK) -> None:
    await call.answer()
    await edit_or_send(
        call,
        f"Удалить ключ #{callback_data.id}?",
        ikb([[("🗑 Да", AK(action="delok", id=callback_data.id).pack()), back(AK(action="view", id=callback_data.id), "Отмена")]]),
    )


@router.callback_query(AK.filter(F.action == "delok"))
async def cb_key_delok(call: CallbackQuery, callback_data: AK, bot: Bot, db: Database, service: VpnService) -> None:
    key = await db.get_key(callback_data.id)
    if key is None:
        await call.answer("Уже удалён", show_alert=True)
        return
    try:
        await service.delete_device(key)
    except (AwgError, ServiceError) as e:
        await call.answer(f"Ошибка: {e}"[:190], show_alert=True)
        return
    await call.answer("Ключ удалён")
    if key.tg_id:
        await _notify(bot, key.tg_id, f"🗑 Устройство «{esc(key.name)}» удалено администратором.")
        await _refresh_card(call, db, service, key.tg_id)


# ---------- платежи ----------


@router.callback_query(Adm.filter(F.action == "payments"))
async def cb_payments(call: CallbackQuery, db: Database, service: VpnService) -> None:
    await call.answer()
    pays = await db.pending_payments()
    rows = []
    for p in pays[:30]:
        u = await db.get_user(p.tg_id)
        who = u.title if u else str(p.tg_id)
        rows.append([(f"№{p.id} · {who} · {p.amount} {service.settings.currency}", APay(action="view", id=p.id).pack())])
    rows.append([back(Adm(action="panel"))])
    text = f"💳 <b>Ожидают проверки:</b> {len(pays)}" if pays else "💳 Новых платежей нет."
    await edit_or_send(call, text, ikb(rows))


@router.callback_query(APay.filter(F.action == "view"))
async def cb_payment_view(call: CallbackQuery, callback_data: APay, bot: Bot, service: VpnService) -> None:
    await call.answer()
    from .user import send_payment_to_admins

    p = await service.db.get_payment(callback_data.id)
    if p is None or p.status != "pending":
        await call.message.answer("Платёж уже обработан.")
        return
    await send_payment_to_admins(bot, service, p.id)


async def _mark_payment_message(call: CallbackQuery, suffix: str) -> None:
    msg = call.message
    if not isinstance(msg, Message):
        return
    try:
        if msg.caption is not None:
            await msg.edit_caption(caption=f"{msg.html_text}\n\n{suffix}", reply_markup=None)
        else:
            await msg.edit_text(f"{msg.html_text}\n\n{suffix}", reply_markup=None)
    except Exception:
        pass


@router.callback_query(APay.filter(F.action == "ok"))
async def cb_payment_ok(call: CallbackQuery, callback_data: APay, bot: Bot, service: VpnService) -> None:
    try:
        res = await service.confirm_payment(callback_data.id, call.from_user.id)
    except ServiceError as e:
        await call.answer(str(e), show_alert=True)
        await _mark_payment_message(call, "<i>уже обработан</i>")
        return
    except AwgError as e:
        log.exception("Ошибка при активации подписки")
        await call.answer(f"Платёж принят, но сервер ответил ошибкой: {e}"[:190], show_alert=True)
        return
    p, u = res.payment, res.user
    await call.answer("Подтверждено")
    await _mark_payment_message(call, f"✅ <b>Подтверждено</b> ({esc(call.from_user.full_name)}), подписка до {fmt_dt(u.sub_until)}")
    await _notify(
        bot,
        u.tg_id,
        f"✅ Оплата №{p.id} подтверждена!\nПодписка активна до <b>{fmt_dt(u.sub_until)}</b>, "
        f"доступно {devices_word(service.device_limit(u))}.\n\nДобавьте устройства в «🔑 Мои устройства».",
    )
    if res.referrer:
        await _notify(
            bot,
            res.referrer.tg_id,
            f"🎁 Ваш друг оплатил подписку — вам начислено +{days_word(service.settings.ref_bonus_days)}. "
            f"Подписка до {fmt_dt(res.referrer.sub_until)}.",
        )


@router.callback_query(APay.filter(F.action == "no"))
async def cb_payment_no(call: CallbackQuery, callback_data: APay, bot: Bot, service: VpnService) -> None:
    try:
        p = await service.reject_payment(callback_data.id, call.from_user.id)
    except ServiceError as e:
        await call.answer(str(e), show_alert=True)
        await _mark_payment_message(call, "<i>уже обработан</i>")
        return
    await call.answer("Отклонено")
    await _mark_payment_message(call, f"❌ <b>Отклонено</b> ({esc(call.from_user.full_name)})")
    support = f" Если это ошибка, напишите {esc(service.settings.support)}." if service.settings.support else ""
    await _notify(bot, p.tg_id, f"❌ Оплата №{p.id} не подтверждена.{support}")


# ---------- статистика ----------


@router.callback_query(Adm.filter(F.action == "stats"))
async def cb_stats(call: CallbackQuery, db: Database, service: VpnService) -> None:
    await call.answer()
    s = await db.stats(day_start_ts(), month_start_ts())
    t_today = await db.traffic(since=today())
    t_month = await db.traffic(since=month_start_day())
    t_all = await db.traffic()
    cur = service.settings.currency
    lines = [
        "📊 <b>Статистика</b>\n",
        f"Пользователей: {s['users']} (сегодня +{s['new_today']}, за месяц +{s['new_month']})",
        f"С активной подпиской: {s['active']}, из них платных: {s['paying']}",
        f"Забанено: {s['banned']}",
        f"Устройств: {s['keys']} (активных {s['keys_enabled']}, онлайн {s['online']})",
        "",
        f"💰 Выручка за месяц: <b>{s['revenue_month']} {cur}</b> ({plural(s['payments_month'], 'оплата', 'оплаты', 'оплат')})",
        f"Выручка всего: {s['revenue_total']} {cur}",
        f"Ожидают проверки: {s['pending']}",
        "",
        "📶 Трафик (скачано клиентами / отдано)",
        f"Сегодня: {human_bytes(t_today.tx)} / {human_bytes(t_today.rx)}",
        f"Месяц: {human_bytes(t_month.tx)} / {human_bytes(t_month.rx)}",
        f"Всего: {human_bytes(t_all.tx)} / {human_bytes(t_all.rx)}",
        "",
        "🖥 <b>По серверам</b> (устройств · онлайн · трафик за месяц):",
    ]
    counts = await db.server_key_counts()
    for srv in await db.servers():
        total, _, online = counts.get(srv.id, (0, 0, 0))
        t = await db.traffic(server_id=srv.id, since=month_start_day())
        mark = "🟢" if srv.status_ok else "🔴"
        lines.append(f"{mark} {esc(srv.title)}: {total} · {online} · {human_bytes(t.total)}")
    lines += ["", "🏆 <b>Топ за месяц:</b>"]
    top = await db.top_traffic(month_start_day())
    rows = []
    for i, (uid, t) in enumerate(top, 1):
        u = await db.get_user(uid)
        name = u.title if u else str(uid)
        lines.append(f"{i}. {esc(name)} — {human_bytes(t.total)}")
        rows.append([(f"{i}. {name}", AU(action="card", uid=uid).pack())])
    if not top:
        lines.append("пока нет данных")
    rows.append([back(Adm(action="panel"))])
    await edit_or_send(call, "\n".join(lines), ikb(rows))


# ---------- тарифы ----------


@router.callback_query(Adm.filter(F.action == "plans"))
async def cb_plans(call: CallbackQuery, db: Database, service: VpnService) -> None:
    await call.answer()
    plans = await db.plans(only_active=False)
    cur = service.settings.currency
    rows = [
        [
            (
                f"{'' if p.active else '🙈 '}{p.title} · {p.days} дн · {p.devices} устр · {p.price} {cur}",
                APlan(action="view", id=p.id).pack(),
            )
        ]
        for p in plans
    ]
    rows.append([("➕ Добавить тариф", APlan(action="add").pack())])
    rows.append([back(Adm(action="panel"))])
    await edit_or_send(call, "📦 <b>Тарифы</b> (🙈 — скрыт от пользователей)", ikb(rows))


@router.callback_query(APlan.filter(F.action == "view"))
async def cb_plan_view(call: CallbackQuery, callback_data: APlan, db: Database, service: VpnService) -> None:
    p = await db.get_plan(callback_data.id)
    if p is None:
        await call.answer("Не найден", show_alert=True)
        return
    await call.answer()
    text = (
        f"📦 <b>{esc(p.title)}</b>\n"
        f"Срок: {days_word(p.days)}\nУстройств: {p.devices}\nЦена: {p.price} {service.settings.currency}\n"
        f"Статус: {'показывается' if p.active else 'скрыт'}"
    )
    rows = [
        [("✏️ Изменить", APlan(action="edit", id=p.id).pack()), ("🙈 Скрыть" if p.active else "👁 Показать", APlan(action="toggle", id=p.id).pack())],
        [("🗑 Удалить", APlan(action="del", id=p.id).pack())],
        [back(Adm(action="plans"))],
    ]
    await edit_or_send(call, text, ikb(rows))


PLAN_FORMAT = (
    "Отправьте тариф одной строкой в формате:\n"
    "<code>Название | дней | устройств | цена</code>\n\n"
    "Например: <code>1 месяц | 30 | 2 | 150</code>"
)


@router.callback_query(APlan.filter(F.action.in_({"add", "edit"})))
async def cb_plan_edit(call: CallbackQuery, callback_data: APlan, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.plan)
    await state.update_data(plan_id=callback_data.id if callback_data.action == "edit" else 0)
    await call.message.answer(PLAN_FORMAT)


@router.message(AdminStates.plan, _INPUT)
async def st_plan(message: Message, state: FSMContext, db: Database) -> None:
    parts = [p.strip() for p in message.text.split("|")]
    try:
        title, days, devices, price = parts[0][:64], int(parts[1]), int(parts[2]), int(parts[3])
        if not title or not 0 < days <= MAX_PLAN_DAYS or not 0 < devices <= MAX_DEVICES or not 0 <= price <= MAX_PRICE:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer(
            f"Не получилось разобрать. Допустимо: дней 1–{MAX_PLAN_DAYS}, устройств 1–{MAX_DEVICES}, "
            f"цена 0–{MAX_PRICE}, название до 64 символов.\n\n" + PLAN_FORMAT
        )
        return
    data = await state.get_data()
    await state.clear()
    if data.get("plan_id"):
        await db.update_plan(data["plan_id"], title=title, days=days, devices=devices, price=price)
    else:
        await db.add_plan(title, days, devices, price)
    await message.answer("✅ Сохранено.", reply_markup=ikb([[("📦 Тарифы", Adm(action="plans").pack())]]))


@router.callback_query(APlan.filter(F.action == "toggle"))
async def cb_plan_toggle(call: CallbackQuery, callback_data: APlan, db: Database, service: VpnService) -> None:
    p = await db.get_plan(callback_data.id)
    if p:
        await db.update_plan(p.id, active=0 if p.active else 1)
    await cb_plan_view(call, callback_data, db, service)


@router.callback_query(APlan.filter(F.action == "del"))
async def cb_plan_del(call: CallbackQuery, callback_data: APlan, db: Database, service: VpnService) -> None:
    await db.delete_plan(callback_data.id)
    await cb_plans(call, db, service)


# ---------- рассылка ----------


#
# Режим оповещения: админ выбирает аудиторию, и дальше КАЖДОЕ его сообщение
# (текст, фото, видео, файл) бот сразу пересылает всем. Удобно, когда, например,
# сервер заблокировали и нужно быстро объяснить клиентам, что происходит.

_BROADCAST_TASKS: set[asyncio.Task] = set()


async def _audience(db: Database, target: str) -> tuple[list[int], str]:
    if target.startswith("s") and target[1:].isdigit():
        server = await db.get_server(int(target[1:]))
        title = f"клиентам сервера {server.title}" if server else "клиентам удалённого сервера"
        return await db.server_user_ids(int(target[1:])), title
    return await db.all_user_ids(target), BCAST_TARGETS.get(target, target)


def _stop_kb():
    return ikb([[("⏹ Выйти из режима оповещения", Bcast(action="stop").pack())]])


@router.callback_query(Adm.filter(F.action == "bcast"))
async def cb_bcast(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    rows = [[(f"📢 {title.capitalize()}", Bcast(action="target", target=t).pack())] for t, title in BCAST_TARGETS.items()]
    for s in await db.servers():
        rows.append([(f"📢 Клиентам {s.title}", Bcast(action="target", target=f"s{s.id}").pack())])
    rows.append([back(Adm(action="panel"))])
    await edit_or_send(
        call,
        "📢 <b>Оповещение</b>\n\nКому пишем? После выбора все ваши сообщения будут сразу пересылаться "
        "выбранным пользователям — пока не нажмёте «⏹ Выйти».\n\n"
        "«Клиентам сервера» — тем, у кого есть устройства на этом сервере или кто выбрал его при покупке "
        "(удобно, если заблокировали конкретный сервер).",
        ikb(rows),
    )


@router.callback_query(Bcast.filter(F.action == "target"))
async def cb_bcast_target(call: CallbackQuery, callback_data: Bcast, state: FSMContext, db: Database) -> None:
    ids, title = await _audience(db, callback_data.target)
    await call.answer()
    await state.set_state(AdminStates.broadcast)
    await state.update_data(target=callback_data.target)
    await call.message.answer(
        f"📢 <b>Режим оповещения включён</b> — {esc(title)} ({len(ids)} чел.).\n\n"
        "Пишите сообщения — текст, фото, видео, файлы. Каждое сразу уйдёт всем адресатам.\n"
        "Выйти — кнопкой ниже или /cancel.",
        reply_markup=_stop_kb(),
    )


@router.message(AdminStates.broadcast, ~F.text.in_(MENU_TEXTS))
async def st_bcast(message: Message, bot: Bot, state: FSMContext, db: Database) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("📢 Режим оповещения выключен.")
        return
    data = await state.get_data()
    ids, title = await _audience(db, data.get("target", "all"))
    ids = [uid for uid in ids if uid != message.chat.id]
    if not ids:
        await message.answer("Некому отправлять — в этой аудитории нет пользователей.", reply_markup=_stop_kb())
        return
    status = await message.answer(f"📤 Отправляю {len(ids)} пользователям ({esc(title)})…")

    async def run() -> None:
        ok = 0
        for uid in ids:
            try:
                await bot.copy_message(uid, message.chat.id, message.message_id)
                ok += 1
            except Exception:
                pass  # заблокировал бота и т.п.
            await asyncio.sleep(0.05)  # лимит Telegram ~30 сообщений/сек
        try:
            await status.edit_text(
                f"✅ Доставлено {ok} из {len(ids)} ({esc(title)}). Можно писать следующее сообщение.",
                reply_markup=_stop_kb(),
            )
        except Exception:
            pass

    # Отправка идёт в фоне: бот не «замирает», пока рассылка не закончится.
    task = asyncio.create_task(run())
    _BROADCAST_TASKS.add(task)
    task.add_done_callback(_BROADCAST_TASKS.discard)


@router.callback_query(Bcast.filter(F.action == "stop"))
async def cb_bcast_stop(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Режим оповещения выключен")
    await call.message.answer("📢 Режим оповещения выключен.", reply_markup=main_menu(True))


# ---------- серверы ----------


def _conn_label(conn: str) -> str:
    if conn == "local":
        return "этот же сервер (local)"
    if conn.startswith("mock:"):
        return "демо-эмуляция"
    return f"SSH {conn}"


@router.callback_query(ASrv.filter(F.action == "list"))
async def cb_servers(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await call.answer()
    servers = await db.servers()
    counts = await db.server_key_counts()
    lines = ["🖥 <b>Серверы</b>\n", "🟢 работает · 🔴 недоступен · 🙈 скрыт для новых устройств\n"]
    rows = []
    for s in servers:
        total, _, online = counts.get(s.id, (0, 0, 0))
        mark = ("🟢" if s.status_ok else "🔴") + ("" if s.active else "🙈")
        protos = "+".join(PROTO_SHORT[p] for p in s.protocol_list) or "—"
        label = f"{mark} {s.title} · {protos} · {total} устр. · онлайн {online}"
        lines.append(esc(label))
        rows.append([(label, ASrv(action="view", id=s.id).pack())])
    if not servers:
        lines.append("Серверов пока нет — добавьте первый.")
    rows.append([("➕ Добавить сервер", ASrv(action="add").pack())])
    rows.append([("🔄 Проверить все", ASrv(action="checkall").pack()), back(Adm(action="panel"))])
    await edit_or_send(call, "\n".join(lines), ikb(rows))


@router.callback_query(ASrv.filter(F.action == "checkall"))
async def cb_servers_check(call: CallbackQuery, state: FSMContext, db: Database, service: VpnService) -> None:
    await call.answer("Проверяю…")
    await service.check_servers()
    await cb_servers(call, state, db)


async def server_card(db: Database, service: VpnService, row) -> tuple[str, object]:
    counts = await db.server_key_counts()
    total, enabled, online_db = counts.get(row.id, (0, 0, 0))
    t_today = await db.traffic(server_id=row.id, since=today())
    t_month = await db.traffic(server_id=row.id, since=month_start_day())
    lines = [
        f"🖥 <b>{esc(row.title)}</b>",
        f"Статус: {'🟢 работает' if row.status_ok else '🔴 недоступен'}"
        + ("" if row.active else ", 🙈 скрыт для новых устройств"),
        f"Адрес для клиентов: <code>{esc(row.host)}</code>",
        f"Подключение: {esc(_conn_label(row.conn))}",
        f"Проверен: {fmt_dt(row.last_check)}",
    ]
    if row.last_error:
        lines.append(f"Ошибка: <code>{esc(row.last_error[:400])}</code>")
    lines.append(f"\nУстройств в боте: {total} (активных {enabled})")
    details = await service.server_details(row) if row.status_ok else {}
    lines.append("\n<b>Протоколы:</b>")
    for proto, title in PROTOCOLS.items():
        if proto in details:
            lines.append(f"✅ {title}: {esc(details[proto])}")
        else:
            lines.append(f"— {title}: не найден на сервере")
    lines += [
        f"\n📶 Трафик сегодня: {human_bytes(t_today.total)}, за месяц: {human_bytes(t_month.total)}",
    ]
    a = lambda action, arg=0: ASrv(action=action, id=row.id, arg=arg).pack()  # noqa: E731
    rows = [
        [("🔄 Проверить", a("check")), ("✏️ Изменить", a("edit"))],
        [("🙈 Скрыть для новых" if row.active else "👁 Показывать клиентам", a("toggle"))],
        [("🚚 Перенести всех клиентов", a("moveall"))] if total else [],
        [("🔑 Сбросить SSH-ключ сервера", a("resetkey"))] if not row.conn.startswith(("local", "mock:")) else [],
        [("🗑 Удалить сервер", a("del"))],
        [back(ASrv(action="list"))],
    ]
    return "\n".join(lines), ikb(rows)


async def _server_or_alert(call: CallbackQuery, db: Database, server_id: int):
    row = await db.get_server(server_id)
    if row is None:
        await call.answer("Сервер не найден", show_alert=True)
    return row


@router.callback_query(ASrv.filter(F.action == "view"))
async def cb_server_view(call: CallbackQuery, callback_data: ASrv, db: Database, service: VpnService) -> None:
    row = await _server_or_alert(call, db, callback_data.id)
    if row is None:
        return
    await call.answer()
    text, kb = await server_card(db, service, row)
    await edit_or_send(call, text, kb)


@router.callback_query(ASrv.filter(F.action == "check"))
async def cb_server_check(call: CallbackQuery, callback_data: ASrv, db: Database, service: VpnService) -> None:
    row = await _server_or_alert(call, db, callback_data.id)
    if row is None:
        return
    res = await service.check_server(row)
    await call.answer("✅ Сервер отвечает" if res.ok else f"🔴 {res.error[:180]}", show_alert=not res.ok)
    row = await db.get_server(row.id)
    text, kb = await server_card(db, service, row)
    await edit_or_send(call, text, kb)


@router.callback_query(ASrv.filter(F.action == "toggle"))
async def cb_server_toggle(call: CallbackQuery, callback_data: ASrv, db: Database, service: VpnService) -> None:
    row = await _server_or_alert(call, db, callback_data.id)
    if row is None:
        return
    await db.update_server(row.id, active=0 if row.active else 1)
    await call.answer("Сервер скрыт для новых устройств" if row.active else "Сервер снова доступен клиентам")
    text, kb = await server_card(db, service, await db.get_server(row.id))
    await edit_or_send(call, text, kb)


@router.callback_query(ASrv.filter(F.action == "resetkey"))
async def cb_server_resetkey(call: CallbackQuery, callback_data: ASrv, db: Database, service: VpnService) -> None:
    from ..awg.runner import parse_ssh_target

    row = await _server_or_alert(call, db, callback_data.id)
    if row is None:
        return
    try:
        _, host, port = parse_ssh_target(row.conn)
    except ValueError:
        await call.answer("Это не SSH-сервер", show_alert=True)
        return
    service.pool.ssh.forget(host, port)
    await service.pool.drop(row.id)
    await call.answer("Ключ сервера забыт — при следующем подключении будет запомнен новый", show_alert=True)


def _ssh_help(pubkey: str) -> str:
    return (
        "➕ <b>Добавление сервера</b>\n\n"
        "1. Установите на сервер AmneziaWG и/или XRay (VLESS) через приложение AmneziaVPN (как обычно).\n"
        "2. Разрешите боту вход по SSH — выполните на этом сервере одну команду:\n"
        f"<pre>mkdir -p ~/.ssh &amp;&amp; echo '{esc(pubkey)}' &gt;&gt; ~/.ssh/authorized_keys</pre>\n"
        "3. Отправьте сюда строку:\n"
        "<code>Название | флаг | IP сервера</code>\n"
        "например: <code>Нидерланды | 🇳🇱 | 5.6.7.8</code>\n\n"
        "Если SSH не на 22 порту или пользователь не root, добавьте четвёртым полем подключение:\n"
        "<code>Нидерланды | 🇳🇱 | 5.6.7.8 | admin@5.6.7.8:2222</code>\n"
        "(у не-root пользователя должен быть sudo без пароля для docker).\n\n"
        "Для сервера, на котором запущен сам бот, укажите <code>local</code>:\n"
        "<code>Германия | 🇩🇪 | 1.2.3.4 | local</code>\n\n"
        "Отмена — /cancel"
    )


@router.callback_query(ASrv.filter(F.action.in_({"add", "edit"})))
async def cb_server_add(call: CallbackQuery, callback_data: ASrv, state: FSMContext, db: Database, service: VpnService) -> None:
    await call.answer()
    await state.set_state(AdminStates.server)
    await state.update_data(server_id=callback_data.id if callback_data.action == "edit" else 0)
    try:
        pubkey = await asyncio.to_thread(service.pool.ssh.ensure_key)
    except Exception as e:  # нет asyncssh и т.п.
        pubkey = f"(не удалось создать SSH-ключ: {e})"
    text = _ssh_help(pubkey)
    if callback_data.action == "edit":
        row = await db.get_server(callback_data.id)
        if row:
            text = (
                f"✏️ Текущие данные:\n<code>{esc(row.name)} | {esc(row.flag)} | {esc(row.host)} | {esc(row.conn)}</code>\n\n"
                + text.replace("➕ <b>Добавление сервера</b>\n\n", "")
            )
    await call.message.answer(text)


def _parse_server_line(text: str) -> tuple[str, str, str, str]:
    parts = [p.strip() for p in text.split("|")]
    if len(parts) not in (3, 4) or not all(parts[:3]):
        raise ValueError
    name, flag, host = parts[0][:40], parts[1][:8], parts[2][:255]
    conn = parts[3] if len(parts) == 4 and parts[3] else f"root@{host}"
    return name, flag, host, conn


@router.message(AdminStates.server, _INPUT)
async def st_server(message: Message, state: FSMContext, db: Database, service: VpnService) -> None:
    try:
        name, flag, host, conn = _parse_server_line(message.text)
    except ValueError:
        await message.answer("Не получилось разобрать. Формат: <code>Название | флаг | IP</code> (или /cancel)")
        return
    data = await state.get_data()
    wait = await message.answer("⏳ Подключаюсь к серверу…")
    try:
        if data.get("server_id"):
            row = await db.get_server(data["server_id"])
            if row is None:
                await state.clear()
                await wait.edit_text("Сервер не найден.")
                return
            from ..service import validate_conn

            await service.update_server(row, name=name, flag=flag, host=host, conn=validate_conn(conn))
            row = await db.get_server(row.id)
            res = await service.check_server(row)
            result = "✅ Сохранено, сервер отвечает." if res.ok else f"⚠️ Сохранено, но сервер не отвечает:\n<code>{esc(res.error)}</code>"
        else:
            row = await service.add_server(name, flag, host, conn)
            result = f"✅ Сервер {esc(row.title)} добавлен и доступен клиентам."
    except (ServiceError, ValueError) as e:
        await wait.edit_text(f"❌ {esc(e)}\n\nИсправьте и отправьте строку ещё раз (или /cancel).")
        return
    await state.clear()
    await wait.edit_text(result, reply_markup=ikb([[("🖥 Серверы", ASrv(action="list").pack())]]))


@router.callback_query(ASrv.filter(F.action == "del"))
async def cb_server_del(call: CallbackQuery, callback_data: ASrv, db: Database) -> None:
    row = await _server_or_alert(call, db, callback_data.id)
    if row is None:
        return
    if await db.server_keys(row.id):
        await call.answer("На сервере есть устройства — сначала перенесите клиентов", show_alert=True)
        return
    await call.answer()
    await edit_or_send(
        call,
        f"Удалить сервер {esc(row.title)} из бота? На самом сервере ничего не изменится.",
        ikb([[("🗑 Да, удалить", ASrv(action="delok", id=row.id).pack()), back(ASrv(action="view", id=row.id), "Отмена")]]),
    )


@router.callback_query(ASrv.filter(F.action == "delok"))
async def cb_server_delok(call: CallbackQuery, callback_data: ASrv, state: FSMContext, db: Database, service: VpnService) -> None:
    row = await _server_or_alert(call, db, callback_data.id)
    if row is None:
        return
    try:
        await service.delete_server(row)
    except ServiceError as e:
        await call.answer(str(e), show_alert=True)
        return
    await cb_servers(call, state, db)


@router.callback_query(ASrv.filter(F.action == "moveall"))
async def cb_server_moveall(call: CallbackQuery, callback_data: ASrv, db: Database) -> None:
    row = await _server_or_alert(call, db, callback_data.id)
    if row is None:
        return
    targets = [s for s in await db.servers() if s.id != row.id and s.status_ok]
    if not targets:
        await call.answer("Нет других работающих серверов", show_alert=True)
        return
    await call.answer()
    n = len(await db.server_keys(row.id))
    rows = [[(s.title, ASrv(action="moveallok", id=row.id, arg=s.id).pack())] for s in targets]
    rows.append([back(ASrv(action="view", id=row.id))])
    await edit_or_send(
        call,
        f"🚚 Перенести {devices_word(n)} с {esc(row.title)} на другой сервер?\n\n"
        "Клиентам придёт уведомление — им нужно будет заново получить ключ в «🔑 Мои устройства».",
        ikb(rows),
    )


@router.callback_query(ASrv.filter(F.action == "moveallok"))
async def cb_server_moveallok(call: CallbackQuery, callback_data: ASrv, bot: Bot, db: Database, service: VpnService) -> None:
    src = await _server_or_alert(call, db, callback_data.id)
    dst = await db.get_server(callback_data.arg)
    if src is None or dst is None:
        return
    await call.answer("Переношу…")
    await edit_or_send(call, f"🚚 Переношу устройства {esc(src.title)} → {esc(dst.title)}…")
    ok = failed = 0
    for key in await db.server_keys(src.id):
        try:
            await service.move_device(key, dst.id, force=True)
            ok += 1
        except (AwgError, ServiceError) as e:
            failed += 1
            log.warning("Не удалось перенести ключ #%s: %s", key.id, e)
            continue
        if key.tg_id:
            await _notify(
                bot,
                key.tg_id,
                f"🌍 Ваше устройство «{esc(key.name)}» перенесено на сервер {esc(dst.title)}.\n"
                "Получите новый ключ в «🔑 Мои устройства» и добавьте его в приложение.",
            )
    await bot.send_message(
        call.from_user.id,
        f"🚚 Готово: перенесено {ok}" + (f", не удалось {failed} (см. логи)" if failed else "") + ".",
        reply_markup=ikb([[("🖥 Серверы", ASrv(action="list").pack())]]),
    )


# ---------- неактивные аккаунты ----------


async def _inactive_view(db: Database, days: int):
    users = await db.inactive_users(days)
    lines = [
        f"🧹 <b>Неактивные аккаунты</b> — нет подписки, не заходили в бота и не подключались {days_word(days)}: {len(users)}\n"
    ]
    for u in users[:30]:
        lines.append(f"• {esc(u.title)} (<code>{u.tg_id}</code>) — был {fmt_date(u.last_seen)}")
    if len(users) > 30:
        lines.append(f"… и ещё {len(users) - 30}")
    rows = [[(f"{d} дн", Adm(action="inactive", arg=d).pack()) for d in (14, 30, 60, 90)]]
    if users:
        rows.append([(f"🗑 Удалить все ({len(users)})", Adm(action="inactdel", arg=days).pack())])
    rows.append([back(Adm(action="panel"))])
    return "\n".join(lines), ikb(rows)


@router.callback_query(Adm.filter(F.action == "inactive"))
async def cb_inactive(call: CallbackQuery, callback_data: Adm, db: Database) -> None:
    await call.answer()
    text, kb = await _inactive_view(db, callback_data.arg or 30)
    await edit_or_send(call, text, kb)


@router.message(Command("inactive"))
async def cmd_inactive(message: Message, command: CommandObject, db: Database) -> None:
    days = int(command.args) if command.args and command.args.strip().isdigit() else 30
    text, kb = await _inactive_view(db, days)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Adm.filter(F.action == "inactdel"))
async def cb_inactive_del(call: CallbackQuery, callback_data: Adm) -> None:
    await call.answer()
    await edit_or_send(
        call,
        f"Точно удалить все неактивные аккаунты ({days_word(callback_data.arg)})? Их ключи будут удалены с сервера.",
        ikb([[("🗑 Да, удалить", Adm(action="inactdelok", arg=callback_data.arg).pack()), back(Adm(action="inactive", arg=callback_data.arg), "Отмена")]]),
    )


@router.callback_query(Adm.filter(F.action == "inactdelok"))
async def cb_inactive_delok(call: CallbackQuery, callback_data: Adm, bot: Bot, db: Database, service: VpnService) -> None:
    await call.answer("Удаляю…")
    users = [u for u in await db.inactive_users(callback_data.arg) if not service.is_admin(u.tg_id)]
    deleted = 0
    for u in users:
        try:
            await service.delete_user(u.tg_id)
            deleted += 1
        except (AwgError, ServiceError):
            log.exception("Не удалось удалить %s", u.tg_id)
    await edit_or_send(call, f"🗑 Удалено аккаунтов: {deleted}.", ikb([[back(Adm(action="panel"))]]))


# список всех ключей — пригодится для поиска «чужих» ключей
@router.message(Command("keys"))
async def cmd_keys(message: Message, bot: Bot, db: Database) -> None:
    keys = await db.all_keys()
    if not keys:
        await message.answer("Ключей пока нет.")
        return
    lines = ["<b>Все ключи:</b>"]
    for k in keys:
        lines.append(
            f"{'✅' if k.enabled else '⏸'} #{k.id} {esc(k.name)} (<code>{k.tg_id}</code>) {k.ip} — {fmt_dt(k.last_handshake)}"
        )
    await send_long(bot, message.chat.id, "\n".join(lines))


@router.message(Command("backup"))
async def cmd_backup(message: Message, bot: Bot, service: VpnService) -> None:
    from ..scheduler import send_backup

    await message.answer("💾 Готовлю резервную копию…")
    await send_backup(bot, service, force=True)


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("Меню обновлено.", reply_markup=main_menu(True))
