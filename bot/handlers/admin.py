from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..awg.conf import protocol_version
from ..awg.server import AwgError
from ..db import Database, User, now
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
from .keyboards import AK, AU, BTN_ADMIN, MENU_TEXTS, Adm, APay, APlan, Bcast, UList, back, ikb, main_menu

log = logging.getLogger(__name__)
router = Router(name="admin")

PAGE = 10
MAX_PLAN_DAYS = 3650
MAX_PRICE = 10_000_000
FILTERS = {"all": "Все", "active": "С подпиской", "expired": "Без подписки", "banned": "Бан"}
BCAST_TARGETS = {"all": "всем", "active": "с активной подпиской", "expired": "без подписки"}


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


def panel_kb(pending: int):
    return ikb(
        [
            [("👥 Пользователи", UList(flt="all").pack()), (f"💳 Платежи ({pending})", Adm(action="payments").pack())],
            [("📊 Статистика", Adm(action="stats").pack()), ("📦 Тарифы", Adm(action="plans").pack())],
            [("📢 Рассылка", Adm(action="bcast").pack()), ("🖥 Сервер", Adm(action="server").pack())],
            [("🧹 Неактивные", Adm(action="inactive", arg=30).pack()), ("🔎 Поиск", Adm(action="find").pack())],
        ]
    )


PANEL_TEXT = (
    "🛠 <b>Админ-панель</b>\n\n"
    "Команды: /user <code>ID|@ник</code>, /extend <code>ID дни</code>, /ban <code>ID</code>, "
    "/unban <code>ID</code>, /msg <code>ID текст</code>, /inactive <code>дни</code>"
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
    except AwgError as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)
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
    lines = [f"🔑 <b>Устройства</b> {uid}\n"]
    for k in keys:
        t = await db.traffic(key_id=k.id, since=month_start_day())
        state = "⏸" if not k.enabled else ("🟢" if k.last_handshake >= now() - 180 else "⚪️")
        lines.append(
            f"{state} #{k.id} {esc(k.name)} · {k.ip} · был {fmt_dt(k.last_handshake)} · за месяц {human_bytes(t.total)}"
        )
    if not keys:
        lines.append("Устройств нет.")
    rows = [[(f"#{k.id} {k.name}", AK(action="view", id=k.id).pack())] for k in keys]
    rows.append([("➕ Создать устройство", AU(action="addkey", uid=uid).pack())])
    rows.append([back(AU(action="card", uid=uid))])
    await edit_or_send(call, "\n".join(lines), ikb(rows))


@router.callback_query(AU.filter(F.action == "addkey"))
async def cb_user_addkey(call: CallbackQuery, callback_data: AU, bot: Bot, db: Database, service: VpnService) -> None:
    u = await db.get_user(callback_data.uid)
    if u is None:
        await call.answer("Не найден", show_alert=True)
        return
    n = len(await db.user_keys(u.tg_id)) + 1
    try:
        rk = await service.create_device(u, f"Устройство {n}")
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
    text = (
        f"🔑 <b>#{key.id} {esc(key.name)}</b>\n"
        f"Владелец: <code>{key.tg_id}</code>\nIP: {key.ip}\n"
        f"Статус: {'✅ активен' if key.enabled else '⏸ отключён'}\n"
        f"Создан: {fmt_dt(key.created_at)}\nПоследнее подключение: {fmt_dt(key.last_handshake)}\n"
        f"Трафик за месяц: ↓{human_bytes(t_month.tx)} ↑{human_bytes(t_month.rx)}\n"
        f"Всего: ↓{human_bytes(t_all.tx)} ↑{human_bytes(t_all.rx)}\n"
        f"Публичный ключ: <code>{esc(key.public_key)}</code>"
    )
    rows = [
        [("📤 Получить ключ", AK(action="key", id=key.id).pack()), ("🗑 Удалить", AK(action="del", id=key.id).pack())],
    ]
    if key.tg_id:
        rows.append([back(AU(action="keys", uid=key.tg_id))])
    await edit_or_send(call, text, ikb(rows))


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
    except AwgError as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)
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
        "🏆 <b>Топ за месяц:</b>",
    ]
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


@router.callback_query(Adm.filter(F.action == "bcast"))
async def cb_bcast(call: CallbackQuery) -> None:
    await call.answer()
    rows = [[(f"Отправить {title}", Bcast(action="target", target=t).pack())] for t, title in BCAST_TARGETS.items()]
    rows.append([back(Adm(action="panel"))])
    await edit_or_send(call, "📢 <b>Рассылка</b>\nКому отправить?", ikb(rows))


@router.callback_query(Bcast.filter(F.action == "target"))
async def cb_bcast_target(call: CallbackQuery, callback_data: Bcast, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.broadcast)
    await state.update_data(target=callback_data.target)
    await call.message.answer(
        f"Отправьте сообщение для рассылки ({BCAST_TARGETS[callback_data.target]}): текст, фото, видео. /cancel — отмена."
    )


@router.message(AdminStates.broadcast, ~F.text.in_(MENU_TEXTS))
async def st_bcast(message: Message, state: FSMContext, db: Database) -> None:
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    target = data.get("target", "all")
    ids = await db.all_user_ids(target)
    await state.update_data(msg_id=message.message_id, chat_id=message.chat.id)
    await message.answer(
        f"Отправить это сообщение {len(ids)} пользователям ({BCAST_TARGETS[target]})?",
        reply_markup=ikb(
            [[("✅ Отправить", Bcast(action="send", target=target).pack()), ("Отмена", Bcast(action="cancel").pack())]]
        ),
    )


@router.callback_query(Bcast.filter(F.action == "cancel"))
async def cb_bcast_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    await edit_or_send(call, "Рассылка отменена.")


@router.callback_query(Bcast.filter(F.action == "send"))
async def cb_bcast_send(call: CallbackQuery, callback_data: Bcast, bot: Bot, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    await state.clear()
    if "msg_id" not in data:
        await call.answer("Сообщение не найдено, начните заново", show_alert=True)
        return
    await call.answer("Рассылка запущена")
    await edit_or_send(call, "📢 Рассылка идёт…")
    ids = await db.all_user_ids(callback_data.target)
    ok = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, data["chat_id"], data["msg_id"])
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)  # лимит Telegram ~30 сообщений/сек
    await bot.send_message(call.from_user.id, f"📢 Рассылка завершена: доставлено {ok} из {len(ids)}.")


# ---------- сервер ----------


@router.callback_query(Adm.filter(F.action == "server"))
async def cb_server(call: CallbackQuery, db: Database, service: VpnService) -> None:
    await call.answer()
    try:
        cfg = await service.server.load_config()
        info = await service.server.server_info(cfg)
        stats = await service.server.stats()
    except AwgError as e:
        await edit_or_send(call, f"❌ Сервер недоступен: <code>{esc(e)}</code>", ikb([[back(Adm(action="panel"))]]))
        return
    version = protocol_version(info.awg_params) or "1.0 / WireGuard"
    online = sum(1 for s in stats.values() if s.latest_handshake and now() - s.latest_handshake < 180)
    params = "\n".join(f"  {k} = {esc(v if len(v) < 60 else v[:57] + '…')}" for k, v in info.awg_params.items())
    known = {k.public_key for k in await db.all_keys()}
    foreign = sum(1 for p in cfg.peers if p.get("PublicKey") not in known)
    text = (
        f"🖥 <b>Сервер</b>\n"
        f"Контейнер: <code>{esc(info.container)}</code>\n"
        f"Адрес: <code>{esc(service.host)}:{info.port}</code>\n"
        f"Протокол AmneziaWG: <b>{version}</b>\n"
        f"Подсеть: {info.subnet_address}/{info.subnet_cidr}\n"
        f"Пиров на сервере: {len(cfg.peers)} (из них созданы не ботом: {foreign}), онлайн: {online}\n\n"
        f"<b>Параметры обфускации:</b>\n<pre>{params or '—'}</pre>"
    )
    await edit_or_send(call, text, ikb([[back(Adm(action="panel"))]]))


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
        except AwgError:
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


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("Меню обновлено.", reply_markup=main_menu(True))
