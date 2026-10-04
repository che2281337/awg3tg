from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile, KeyboardButton, ReplyKeyboardMarkup
from aiogram.types import User as TgUser

from ..config import Settings
from ..db import Database, User
from ..service import RenderedKey

log = logging.getLogger(__name__)

BTN_GET = "🔑 Получить ключ"
BTN_MY = "📋 Мои ключи"
BTN_HELP = "❓ Как подключиться"

MAX_TEXT = 4000

HELP_TEXT = (
    "<b>Как подключиться</b>\n\n"
    "1. Установите <b>AmneziaVPN</b> (5.0.1.5 или новее — нужна поддержка AmneziaWG 3.x): "
    "https://amnezia.org/downloads или из магазина приложений.\n"
    "2. Нажмите «🔑 Получить ключ» и скопируйте ключ <code>vpn://…</code>.\n"
    "3. В AmneziaVPN: <b>➕ / «Добавить сервер»</b> → «Вставить ключ» (или «Файл с настройками» "
    "для .conf, или «QR-код»).\n"
    "4. Подключитесь.\n\n"
    "Файл <b>.conf</b> подходит и для приложения <b>AmneziaWG</b>, если оно поддерживает AmneziaWG 3.x.\n"
    "Не передавайте ключ другим людям: один ключ — одно устройство."
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_GET)], [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_HELP)]],
        resize_keyboard=True,
    )


def is_admin(settings: Settings, tg_id: int) -> bool:
    return tg_id in settings.admin_ids


def user_title(u: TgUser) -> str:
    return f"@{u.username}" if u.username else u.full_name


def esc(text: str | None) -> str:
    return html.escape(text or "")


async def ensure_user(db: Database, settings: Settings, tg_user: TgUser) -> tuple[User, bool]:
    """Регистрирует пользователя. Возвращает (user, создан_ли_только_что)."""
    existing = await db.get_user(tg_user.id)
    if existing is None:
        status = "approved" if settings.access_mode == "open" or is_admin(settings, tg_user.id) else "pending"
    else:
        status = existing.status
    user = await db.upsert_user(tg_user.id, tg_user.username, tg_user.full_name, status)
    return user, existing is None


def can_use(settings: Settings, user: User) -> bool:
    return is_admin(settings, user.tg_id) or user.status == "approved"


async def send_long(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    """Отправка длинного текста по кускам (по строкам)."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > MAX_TEXT:
            await bot.send_message(chat_id, chunk, **kwargs)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await bot.send_message(chat_id, chunk, **kwargs)


async def deliver_key(bot: Bot, chat_id: int, rk: RenderedKey) -> None:
    header = f"🔑 Ключ <b>{esc(rk.key.name)}</b> (IP {rk.key.ip})\n\n"
    instruction = "Скопируйте ключ и вставьте его в AmneziaVPN (➕ → «Вставить ключ»):\n\n"
    body = f"<code>{esc(rk.vpn_url)}</code>"
    if len(header) + len(instruction) + len(body) <= MAX_TEXT:
        await bot.send_message(chat_id, header + instruction + body)
    else:
        await bot.send_message(chat_id, header + "Ключ длинный, поэтому он во вложении — откройте файл и скопируйте текст.")
        await bot.send_document(
            chat_id, BufferedInputFile(rk.vpn_url.encode(), filename="amnezia-key.txt"), caption="Ключ vpn://"
        )

    await bot.send_document(
        chat_id,
        BufferedInputFile(rk.conf.encode(), filename=rk.filename),
        caption="Файл конфигурации для AmneziaVPN / AmneziaWG",
    )
    png = rk.qr_png()
    if png:
        await bot.send_photo(
            chat_id, BufferedInputFile(png, filename="qr.png"), caption="QR-код для сканирования в приложении"
        )
