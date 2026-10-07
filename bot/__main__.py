from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .awg.mock import MockAwgServer
from .awg.server import AwgServer
from .config import Settings
from .db import Database
from .handlers import admin, user
from .handlers.common import ThrottleMiddleware, UserMiddleware
from .scheduler import start_background
from .service import VpnService
from .utils import set_timezone


def build_dispatcher(settings: Settings, db: Database, service: VpnService) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), settings=settings, db=db, service=service)
    throttle = ThrottleMiddleware(service)
    dp.message.outer_middleware(throttle)
    dp.callback_query.outer_middleware(throttle)
    mw = UserMiddleware(db, service)
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    dp.include_router(admin.router)
    dp.include_router(user.router)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    set_timezone(settings.timezone)
    if not settings.admin_ids:
        logging.warning("ADMIN_IDS не задан — некому подтверждать оплаты и управлять пользователями")

    db = Database(settings.db_path)
    await db.connect()

    if settings.awg_mock:
        server: AwgServer = MockAwgServer(os.path.join(os.path.dirname(settings.db_path) or ".", "mock-server"))
        settings.server_host = settings.server_host or "127.0.0.1"
    else:
        server = AwgServer(
            container=settings.awg_container,
            config_path=settings.awg_config_path,
            interface=settings.awg_interface,
            binary=settings.awg_bin,
            docker=settings.docker_bin,
        )
    service = VpnService(settings, db, server)
    await service.start()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(settings, db, service)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="profile", description="Профиль и статистика"),
            BotCommand(command="devices", description="Мои устройства"),
            BotCommand(command="plans", description="Тарифы"),
            BotCommand(command="ref", description="Пригласить друга"),
            BotCommand(command="help", description="Как подключиться"),
        ]
    )
    tasks = start_background(bot, service)
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
