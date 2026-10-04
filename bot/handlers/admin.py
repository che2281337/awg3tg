from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..awg.conf import protocol_version
from ..awg.server import AwgError
from ..config import Settings
from ..db import Database
from ..service import KeyService, human_bytes
from .common import deliver_key, esc, main_menu, send_long

log = logging.getLogger(__name__)
router = Router(name="admin")


def _admin_filter(event: TelegramObject, settings: Settings) -> bool:
    user = getattr(event, "from_user", None)
    return user is not None and user.id in settings.admin_ids


router.message.filter(_admin_filter)
router.callback_query.filter(_admin_filter)

ADMIN_HELP = (
    "<b>Команды администратора</b>\n\n"
    "/server — состояние AWG-сервера\n"
    "/users — пользователи\n"
    "/approve <code>ID</code> — выдать доступ\n"
    "/ban <code>ID</code> — заблокировать и отозвать все ключи\n"
    "/limit <code>ID</code> <code>N|default</code> — лимит ключей пользователя\n"
    "/keys — все ключи со статистикой\n"
    "/revoke <code>KEY_ID</code> — отозвать ключ\n"
    "/issue <code>имя</code> — создать ключ без привязки к пользователю\n"
)


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await message.answer(ADMIN_HELP, reply_markup=main_menu())


@router.message(Command("server"))
async def cmd_server(message: Message, service: KeyService, db: Database) -> None:
    try:
        cfg = await service.server.load_config()
        info = await service.server.server_info(cfg)
        stats = await service.server.stats()
    except AwgError as e:
        await message.answer(f"❌ Сервер недоступен: <code>{esc(str(e))}</code>")
        return
    version = protocol_version(info.awg_params) or "1.0 / WireGuard"
    online = sum(1 for s in stats.values() if s.latest_handshake and datetime.now().timestamp() - s.latest_handshake < 180)
    params = "\n".join(
        f"  {k} = {esc(v if len(v) < 60 else v[:57] + '…')}" for k, v in info.awg_params.items()
    )
    await message.answer(
        f"<b>Сервер</b>\n"
        f"Контейнер: <code>{esc(info.container)}</code>\n"
        f"Адрес: <code>{esc(service.host)}:{info.port}</code>\n"
        f"Протокол AmneziaWG: <b>{version}</b>\n"
        f"Подсеть: {info.subnet_address}/{info.subnet_cidr}\n"
        f"Пиров в конфиге: {len(cfg.peers)}, ключей в боте: {len(await db.all_keys())}, онлайн (3 мин): {online}\n\n"
        f"<b>Параметры обфускации:</b>\n<pre>{params or '—'}</pre>"
    )


@router.message(Command("users"))
async def cmd_users(message: Message, bot: Bot, db: Database, service: KeyService) -> None:
    users = await db.list_users()
    if not users:
        await message.answer("Пользователей пока нет.")
        return
    icons = {"approved": "✅", "pending": "⏳", "banned": "⛔"}
    lines = ["<b>Пользователи:</b>"]
    for u in users:
        n = len(await db.user_keys(u.tg_id))
        limit = service.user_limit(u.key_limit)
        lines.append(f"{icons.get(u.status, '?')} <code>{u.tg_id}</code> {esc(u.title)} — ключей {n}/{limit}")
    await send_long(bot, message.chat.id, "\n".join(lines))


def _parse_id(command: CommandObject) -> int | None:
    try:
        return int((command.args or "").split()[0])
    except (ValueError, IndexError):
        return None


async def _approve(bot: Bot, db: Database, tg_id: int) -> bool:
    if not await db.set_status(tg_id, "approved"):
        return False
    try:
        await bot.send_message(tg_id, "✅ Доступ одобрен! Нажмите «🔑 Получить ключ».", reply_markup=main_menu())
    except Exception:
        log.warning("Не удалось уведомить пользователя %s", tg_id)
    return True


async def _ban(db: Database, service: KeyService, tg_id: int) -> tuple[bool, int]:
    if not await db.set_status(tg_id, "banned"):
        return False, 0
    keys = await db.user_keys(tg_id)
    for k in keys:
        await service.revoke(k)
    return True, len(keys)


@router.message(Command("approve"))
async def cmd_approve(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    tg_id = _parse_id(command)
    if tg_id is None:
        await message.answer("Использование: /approve ID")
        return
    if await db.get_user(tg_id) is None:
        # Пользователь ещё не писал боту (режим closed) — создаём запись заранее.
        await db.upsert_user(tg_id, None, None, "approved")
        await message.answer(f"✅ {tg_id} добавлен. Пусть отправит боту /start.")
        return
    await _approve(bot, db, tg_id)
    await message.answer(f"✅ {tg_id} одобрен.")


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, db: Database, service: KeyService) -> None:
    tg_id = _parse_id(command)
    if tg_id is None:
        await message.answer("Использование: /ban ID")
        return
    try:
        ok, n = await _ban(db, service, tg_id)
    except AwgError as e:
        await message.answer(f"❌ Ошибка: <code>{esc(str(e))}</code>")
        return
    await message.answer(f"⛔ {tg_id} заблокирован, отозвано ключей: {n}." if ok else "Пользователь не найден.")


@router.message(Command("limit"))
async def cmd_limit(message: Message, command: CommandObject, db: Database) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit() or not (args[1].isdigit() or args[1] == "default"):
        await message.answer("Использование: /limit ID N  или  /limit ID default")
        return
    limit = None if args[1] == "default" else int(args[1])
    ok = await db.set_limit(int(args[0]), limit)
    await message.answer("Готово." if ok else "Пользователь не найден.")


@router.message(Command("keys"))
async def cmd_keys(message: Message, bot: Bot, db: Database, service: KeyService) -> None:
    keys = await db.all_keys()
    if not keys:
        await message.answer("Ключей пока нет.")
        return
    try:
        stats = await service.server.stats()
    except AwgError:
        stats = {}
    lines = ["<b>Все ключи:</b>"]
    for k in keys:
        st = stats.get(k.public_key)
        if st is None:
            state = "⚠️ нет на сервере"
        else:
            state = f"{_fmt_ts(st.latest_handshake)} ↓{human_bytes(st.tx)} ↑{human_bytes(st.rx)}"
        owner = f"<code>{k.tg_id}</code>" if k.tg_id else "админ"
        lines.append(f"#{k.id} {esc(k.name)} ({owner}) {k.ip} — {state}")
    await send_long(bot, message.chat.id, "\n".join(lines))


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, bot: Bot, db: Database, service: KeyService) -> None:
    key_id = _parse_id(command)
    key = await db.get_key(key_id) if key_id is not None else None
    if key is None:
        await message.answer("Использование: /revoke KEY_ID (номер из /keys)")
        return
    try:
        await service.revoke(key)
    except AwgError as e:
        await message.answer(f"❌ Ошибка: <code>{esc(str(e))}</code>")
        return
    await message.answer(f"🗑 Ключ #{key.id} отозван.")
    if key.tg_id and key.tg_id != message.chat.id:
        try:
            await bot.send_message(key.tg_id, f"Ваш ключ #{key.id} ({esc(key.name)}) был отозван администратором.")
        except Exception:
            pass


@router.message(Command("issue"))
async def cmd_issue(message: Message, command: CommandObject, bot: Bot, service: KeyService) -> None:
    name = (command.args or "").strip()
    if not name:
        await message.answer("Использование: /issue имя_ключа")
        return
    try:
        rk = await service.issue(None, name[:64])
    except AwgError as e:
        await message.answer(f"❌ Ошибка: <code>{esc(str(e))}</code>")
        return
    await deliver_key(bot, message.chat.id, rk)


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(call: CallbackQuery, bot: Bot, db: Database, service: KeyService) -> None:
    _, action, raw_id = call.data.split(":")
    tg_id = int(raw_id)
    if action == "approve":
        await _approve(bot, db, tg_id)
        result = "✅ одобрен"
    else:
        await db.set_status(tg_id, "banned")
        result = "⛔ отклонён"
        try:
            await bot.send_message(tg_id, "⛔ Заявка на доступ отклонена.")
        except Exception:
            pass
    await call.answer(result)
    await call.message.edit_text(f"{call.message.html_text}\n\n<b>{result}</b> ({esc(call.from_user.full_name)})")
