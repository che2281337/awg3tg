from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from ..db import Database
from ..service import RenderedKey, VpnService
from ..utils import esc

log = logging.getLogger(__name__)

MAX_TEXT = 4000


def help_text(support: str) -> str:
    text = (
        "<b>Как подключиться</b>\n\n"
        "1. Установите <b>AmneziaVPN</b> 5.0.1.5 или новее: https://amnezia.org/downloads "
        "(или из App Store / Google Play).\n"
        "2. Оформите подписку в разделе «💳 Тарифы».\n"
        "3. В «🔑 Мои устройства» добавьте устройство и введите его название — придёт ключ <code>vpn://…</code>, файл и QR-код.\n"
        "4. В AmneziaVPN: <b>«Добавить сервер» → «Вставить ключ»</b> (или «Файл с настройками» / «QR-код»).\n"
        "5. Подключитесь.\n\n"
        "Один ключ — одно устройство. Для телефона и компьютера добавьте отдельные устройства.\n"
        "Если подписка закончилась, ключи отключаются, а после продления снова работают — "
        "заново ничего настраивать не нужно."
    )
    if support:
        text += f"\n\nПоддержка: {esc(support)}"
    return text


class UserMiddleware(BaseMiddleware):
    """Создаёт аккаунт при первом обращении, обновляет last_seen, отсекает забаненных."""

    def __init__(self, db: Database, service: VpnService) -> None:
        self.db = db
        self.service = service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = getattr(event, "from_user", None)
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)
        user, created = await self.db.touch_user(tg_user.id, tg_user.username, tg_user.full_name)
        data["user"] = user
        data["is_new"] = created
        data["is_admin"] = self.service.is_admin(user.tg_id)
        if user.banned and not data["is_admin"]:
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Аккаунт заблокирован", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("⛔ Ваш аккаунт заблокирован. Обратитесь к администратору.")
            return None
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Анти-флуд: не чаще одного действия в `rate` секунд от пользователя (админов не ограничивает)."""

    def __init__(self, service: VpnService, rate: float = 0.5) -> None:
        self.service = service
        self.rate = rate
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = getattr(event, "from_user", None)
        if tg_user is not None and not self.service.is_admin(tg_user.id):
            now_ = time.monotonic()
            if now_ - self._last.get(tg_user.id, 0.0) < self.rate:
                if isinstance(event, CallbackQuery):
                    await event.answer("Слишком часто, подождите секунду")
                return None
            self._last[tg_user.id] = now_
            if len(self._last) > 10000:  # не копим память бесконечно
                border = now_ - 60
                self._last = {k: v for k, v in self._last.items() if v > border}
        return await handler(event, data)


async def edit_or_send(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение с кнопками; если нельзя (фото, старое) — шлёт новое."""
    msg = call.message
    try:
        if isinstance(msg, Message) and msg.text is not None:
            await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
    await call.bot.send_message(call.from_user.id, text, reply_markup=kb, disable_web_page_preview=True)


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
    header = f"🔑 <b>{esc(rk.key.name)}</b>\n\n"
    instruction = "Скопируйте ключ (нажмите на него) и вставьте в AmneziaVPN → «Добавить сервер» → «Вставить ключ»:\n\n"
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
        await bot.send_photo(chat_id, BufferedInputFile(png, filename="qr.png"), caption="QR-код для сканирования")


async def notify_admins(bot: Bot, service: VpnService, text: str, **kwargs) -> None:
    for admin_id in service.settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, **kwargs)
        except Exception:
            log.warning("Не удалось уведомить админа %s", admin_id)
