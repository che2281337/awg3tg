"""Фоновые задачи: сбор трафика, окончание подписок, напоминания, проверка серверов, бэкап."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .db import now
from .handlers.keyboards import renew_kb
from .service import VpnService
from .utils import esc, fmt_dt, left_str

log = logging.getLogger(__name__)

# Этапы users.notified
STAGE_3_DAYS = 1
STAGE_1_DAY = 2
STAGE_EXPIRED = 3


async def _safe_send(bot: Bot, chat_id: int, text: str, **kw) -> None:
    try:
        await bot.send_message(chat_id, text, **kw)
    except Exception as e:  # пользователь заблокировал бота и т.п.
        log.info("Не удалось отправить сообщение %s: %s", chat_id, e)


async def check_subscriptions(bot: Bot, service: VpnService) -> None:
    db = service.db
    await service.expire_subscriptions()

    for user in await db.expired_unnotified():
        await db.update_user(user.tg_id, notified=STAGE_EXPIRED)
        if service.is_admin(user.tg_id):
            continue
        await _safe_send(
            bot,
            user.tg_id,
            "⛔ Ваша подписка закончилась, устройства отключены.\n"
            "Продлите подписку — ключи заработают снова, перенастраивать ничего не нужно.",
            reply_markup=renew_kb(),
        )

    for days, stage in ((1, STAGE_1_DAY), (3, STAGE_3_DAYS)):
        for user in await db.users_expiring(now() + days * 86400):
            if user.notified >= stage:
                continue
            await db.update_user(user.tg_id, notified=stage)
            await _safe_send(
                bot,
                user.tg_id,
                f"⏰ Подписка заканчивается {fmt_dt(user.sub_until)} ({left_str(user.sub_until, now())}).\n"
                "Продлите её заранее, чтобы VPN не отключился.",
                reply_markup=renew_kb(),
            )


async def check_servers(bot: Bot, service: VpnService) -> None:
    """Проверка доступности серверов; админам — уведомление при смене статуса."""
    for c in await service.check_servers():
        if not c.changed:
            continue
        if c.ok:
            text = f"✅ Сервер {esc(c.server.title)} снова доступен."
        else:
            text = (
                f"🔴 Сервер {esc(c.server.title)} недоступен!\n<code>{esc(c.error[:500])}</code>\n\n"
                "Новым клиентам эта локация не предлагается, пока сервер не поднимется."
            )
        for admin_id in service.settings.admin_ids:
            await _safe_send(bot, admin_id, text)


def make_backup(service: VpnService) -> tuple[str, bytes]:
    """Архив с копией базы и SSH-ключом бота — всё, что нужно, чтобы поднять бота заново."""
    data_dir = os.path.dirname(service.settings.db_path) or "."
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        db_copy = os.path.join(tmp, "bot.db")
        src = sqlite3.connect(service.settings.db_path)
        dst = sqlite3.connect(db_copy)
        with dst:
            src.backup(dst)  # консистентная копия даже во время работы бота
        src.close()
        dst.close()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(db_copy, "data/bot.db")
            ssh_dir = os.path.join(data_dir, "ssh")
            if os.path.isdir(ssh_dir):
                for name in os.listdir(ssh_dir):
                    z.write(os.path.join(ssh_dir, name), f"data/ssh/{name}")
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    return f"backup_{stamp}.zip", buf.getvalue()


async def send_backup(bot: Bot, service: VpnService, force: bool = False) -> bool:
    """Раз в BACKUP_HOURS часов отправляет админам архив с базой."""
    hours = service.settings.backup_hours
    if hours <= 0 and not force:
        return False
    marker = os.path.join(os.path.dirname(service.settings.db_path) or ".", ".last_backup")
    try:
        last = os.path.getmtime(marker)
    except OSError:
        last = 0
    if not force and time.time() - last < hours * 3600:
        return False
    name, data = await asyncio.to_thread(make_backup, service)
    for admin_id in service.settings.admin_ids:
        try:
            await bot.send_document(
                admin_id,
                BufferedInputFile(data, filename=name),
                caption="💾 Резервная копия бота (база + SSH-ключ). Храните в надёжном месте — "
                "в архиве ключи клиентов. Восстановление: распакуйте в папку бота с заменой.",
                disable_notification=True,
            )
        except Exception as e:
            log.warning("Не удалось отправить бэкап админу %s: %s", admin_id, e)
    with open(marker, "w") as f:
        f.write(str(int(time.time())))
    return True


async def _loop(name: str, interval: int, func, *args) -> None:
    while True:
        try:
            await func(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в фоновой задаче %s", name)
        await asyncio.sleep(interval)


def start_background(bot: Bot, service: VpnService) -> list[asyncio.Task]:
    return [
        asyncio.create_task(_loop("traffic", service.settings.stats_interval, service.collect_traffic)),
        asyncio.create_task(_loop("subscriptions", 60, check_subscriptions, bot, service)),
        asyncio.create_task(_loop("servers", 120, check_servers, bot, service)),
        asyncio.create_task(_loop("backup", 3600, send_backup, bot, service)),
    ]
