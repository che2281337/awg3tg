"""Фоновые задачи: сбор трафика, окончание подписок, напоминания."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from .db import now
from .handlers.keyboards import renew_kb
from .service import VpnService
from .utils import fmt_dt, left_str

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
    ]
