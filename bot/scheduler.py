"""Фоновые задачи: автооплата ЮMoney, сбор трафика, окончание подписок, напоминания, проверка серверов, бэкап."""

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
from .handlers.keyboards import SlotCb, ikb, renew_kb, slot_kb
from .service import VpnService
from .utils import esc, fmt_dt, left_str
from .yoomoney import YooMoneyError

log = logging.getLogger(__name__)

# users.notified: 3 — сообщили об окончании подписки (напоминания до конца — в users.reminded)
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

    # Доп. слоты устройств
    s = service.settings
    for slot in await db.slots_expiring(now() + 3 * 86400):
        await db.update_slot(slot.id, notified=1)
        await _safe_send(
            bot,
            slot.tg_id,
            f"⏰ Дополнительный слот устройства заканчивается {fmt_dt(slot.until)}.\n"
            "Продлите его, чтобы все устройства продолжили работать.",
            reply_markup=slot_kb(slot.id, s.slot_price, s.currency),
        )
    for slot in await service.expire_slots():
        await _safe_send(
            bot,
            slot.tg_id,
            "⏳ Дополнительный слот устройства закончился. Если устройств больше, чем позволяет тариф, "
            "самые новые отключены (не удалены) — после покупки слота они снова заработают.",
            reply_markup=ikb([[(f"➕ Купить слот — {s.slot_price} {s.currency}", SlotCb(action="buy").pack())]]),
        )

    await remind_expiring(bot, service)


def remind_threshold(left: int, remind_days: tuple[int, ...]) -> int | None:
    """Самый близкий порог напоминания (в днях), в который уже попал остаток подписки."""
    hit = [d for d in remind_days if left <= d * 86400]
    return min(hit) if hit else None


async def remind_expiring(bot: Bot, service: VpnService) -> None:
    """Напоминания до конца подписки: по умолчанию за 7, 3, 2 и 1 день (REMIND_DAYS)."""
    days = service.settings.remind_days
    if not days:
        return
    t = now()
    for user in await service.db.users_expiring(t + max(days) * 86400):
        threshold = remind_threshold(user.sub_until - t, days)
        if threshold is None or (user.reminded is not None and user.reminded <= threshold):
            continue
        await service.db.update_user(user.tg_id, reminded=threshold)
        if service.is_admin(user.tg_id):
            continue
        left = left_str(user.sub_until, t)
        if threshold <= 1:
            head = f"⚠️ <b>Подписка заканчивается меньше чем через сутки</b> — {fmt_dt(user.sub_until)} ({left})."
        else:
            head = f"⏰ Подписка заканчивается {fmt_dt(user.sub_until)} ({left})."
        await _safe_send(
            bot,
            user.tg_id,
            head + "\nПродлите её заранее, чтобы VPN не отключился — новый срок добавится к текущему.",
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


async def check_payments(bot: Bot, service: VpnService) -> None:
    """Автооплата: ищет оплаченные счета в истории кошелька ЮMoney."""
    from .handlers.user import process_autopay

    try:
        results = await service.check_invoices()
    except YooMoneyError as e:
        log.warning("ЮMoney: %s", e)
        return
    await process_autopay(bot, service, results)


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
    tasks = []
    if service.yoomoney is not None:
        interval = service.settings.yoomoney_check_interval
        tasks.append(asyncio.create_task(_loop("yoomoney", interval, check_payments, bot, service)))
    return tasks + [
        asyncio.create_task(_loop("traffic", service.settings.stats_interval, service.collect_traffic)),
        asyncio.create_task(_loop("subscriptions", 60, check_subscriptions, bot, service)),
        asyncio.create_task(_loop("servers", 120, check_servers, bot, service)),
        asyncio.create_task(_loop("backup", 3600, send_backup, bot, service)),
    ]
