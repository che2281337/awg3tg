from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..awg.server import AwgError
from ..config import Settings
from ..db import Database
from ..service import KeyService, LimitReached, human_bytes
from .common import (
    BTN_GET,
    BTN_HELP,
    BTN_MY,
    HELP_TEXT,
    can_use,
    deliver_key,
    ensure_user,
    esc,
    is_admin,
    main_menu,
    user_title,
)

log = logging.getLogger(__name__)
router = Router(name="user")


def approve_kb(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm:approve:{tg_id}"),
                InlineKeyboardButton(text="⛔ Отклонить", callback_data=f"adm:reject:{tg_id}"),
            ]
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    assert message.from_user
    user, created = await ensure_user(db, settings, message.from_user)

    if can_use(settings, user):
        await message.answer(
            "👋 Привет! Здесь можно получить ключ для AmneziaVPN (протокол AmneziaWG).\n\n"
            "Нажмите «🔑 Получить ключ».",
            reply_markup=main_menu(),
        )
        return
    if user.status == "banned":
        await message.answer("⛔ Доступ к боту заблокирован.")
        return
    if settings.access_mode == "closed":
        await message.answer(
            f"🔒 Бот закрытый. Передайте администратору ваш ID: <code>{user.tg_id}</code>"
        )
        return

    await message.answer("📨 Заявка на доступ отправлена администратору. Я напишу, когда её одобрят.")
    if created:
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"🆕 Заявка на доступ: {esc(user_title(message.from_user))} (<code>{user.tg_id}</code>)",
                    reply_markup=approve_kb(user.tg_id),
                )
            except Exception:
                log.warning("Не удалось уведомить админа %s", admin_id)


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, disable_web_page_preview=True)


@router.message(Command("key"))
@router.message(F.text == BTN_GET)
async def get_key(message: Message, bot: Bot, db: Database, settings: Settings, service: KeyService) -> None:
    assert message.from_user
    user, _ = await ensure_user(db, settings, message.from_user)
    if not can_use(settings, user):
        await message.answer("🔒 У вас пока нет доступа. Отправьте /start.")
        return

    keys = await db.user_keys(user.tg_id)
    limit = None if is_admin(settings, user.tg_id) else service.user_limit(user.key_limit)
    if limit is not None and len(keys) >= limit:
        await message.answer(
            f"У вас уже {len(keys)} из {limit} доступных ключей. "
            "Откройте «📋 Мои ключи», чтобы получить ключ повторно или удалить ненужный."
        )
        return

    wait = await message.answer("⏳ Создаю ключ…")
    name = f"TG {user_title(message.from_user)} #{len(keys) + 1}"
    try:
        rk = await service.issue(user.tg_id, name, key_limit=limit)
    except LimitReached:
        await wait.edit_text("Лимит ключей исчерпан.")
        return
    except AwgError as e:
        log.exception("Ошибка выдачи ключа")
        await wait.edit_text("❌ Не удалось создать ключ, попробуйте позже.")
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, f"❌ Ошибка выдачи ключа для {user.tg_id}: <code>{esc(str(e))}</code>")
            except Exception:
                pass
        return
    await wait.delete()
    await deliver_key(bot, message.chat.id, rk)


def keys_kb(key_ids: list[int]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=f"📤 Показать #{kid}", callback_data=f"key:show:{kid}"),
            InlineKeyboardButton(text=f"🗑 Удалить #{kid}", callback_data=f"key:del:{kid}"),
        ]
        for kid in key_ids
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fmt_handshake(ts: int) -> str:
    if not ts:
        return "ещё не подключался"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


@router.message(Command("mykeys"))
@router.message(F.text == BTN_MY)
async def my_keys(message: Message, db: Database, settings: Settings, service: KeyService) -> None:
    assert message.from_user
    user, _ = await ensure_user(db, settings, message.from_user)
    if not can_use(settings, user):
        await message.answer("🔒 У вас пока нет доступа. Отправьте /start.")
        return
    keys = await db.user_keys(user.tg_id)
    if not keys:
        await message.answer("У вас пока нет ключей. Нажмите «🔑 Получить ключ».")
        return
    try:
        stats = await service.server.stats()
    except AwgError:
        stats = {}
    lines = ["<b>Ваши ключи:</b>\n"]
    for k in keys:
        st = stats.get(k.public_key)
        extra = ""
        if st:
            extra = f"\n   последнее подключение: {fmt_handshake(st.latest_handshake)}, ↓{human_bytes(st.tx)} ↑{human_bytes(st.rx)}"
        lines.append(f"#{k.id} <b>{esc(k.name)}</b> — {k.ip}{extra}")
    await message.answer("\n".join(lines), reply_markup=keys_kb([k.id for k in keys]))


async def _own_key(call: CallbackQuery, db: Database, settings: Settings, key_id: int):
    key = await db.get_key(key_id)
    if key is None or (key.tg_id != call.from_user.id and not is_admin(settings, call.from_user.id)):
        await call.answer("Ключ не найден", show_alert=True)
        return None
    return key


@router.callback_query(F.data.startswith("key:show:"))
async def cb_show(call: CallbackQuery, bot: Bot, db: Database, settings: Settings, service: KeyService) -> None:
    key = await _own_key(call, db, settings, int(call.data.split(":")[2]))
    if key is None:
        return
    await call.answer()
    try:
        rk = await service.render(key)
    except AwgError:
        log.exception("Ошибка чтения конфига сервера")
        await bot.send_message(call.from_user.id, "❌ Сервер недоступен, попробуйте позже.")
        return
    await deliver_key(bot, call.from_user.id, rk)


@router.callback_query(F.data.startswith("key:del:"))
async def cb_delete(call: CallbackQuery, db: Database, settings: Settings) -> None:
    key = await _own_key(call, db, settings, int(call.data.split(":")[2]))
    if key is None:
        return
    await call.answer()
    await call.message.answer(
        f"Удалить ключ #{key.id} <b>{esc(key.name)}</b>? Устройства с этим ключом перестанут подключаться.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"key:delok:{key.id}"),
                    InlineKeyboardButton(text="Отмена", callback_data="key:cancel"),
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("key:delok:"))
async def cb_delete_ok(call: CallbackQuery, db: Database, settings: Settings, service: KeyService) -> None:
    key = await _own_key(call, db, settings, int(call.data.split(":")[2]))
    if key is None:
        return
    try:
        await service.revoke(key)
    except AwgError:
        log.exception("Ошибка удаления ключа")
        await call.answer("Не удалось удалить ключ, попробуйте позже", show_alert=True)
        return
    await call.answer("Ключ удалён")
    await call.message.edit_text(f"🗑 Ключ #{key.id} удалён.")


@router.callback_query(F.data == "key:cancel")
async def cb_cancel(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.delete()
