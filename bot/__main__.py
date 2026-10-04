from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .awg.server import AwgServer
from .config import Settings
from .db import Database
from .handlers import admin, user
from .service import KeyService


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if not settings.admin_ids:
        logging.warning("ADMIN_IDS не задан — некому одобрять заявки и управлять ключами")

    db = Database(settings.db_path)
    await db.connect()

    server = AwgServer(
        container=settings.awg_container,
        config_path=settings.awg_config_path,
        interface=settings.awg_interface,
        binary=settings.awg_bin,
        docker=settings.docker_bin,
    )
    service = KeyService(settings, db, server)
    await service.start()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(settings=settings, db=db, service=service)
    dp.include_router(admin.router)  # админский /keys должен перехватываться раньше
    dp.include_router(user.router)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="key", description="Получить ключ"),
            BotCommand(command="mykeys", description="Мои ключи"),
            BotCommand(command="help", description="Как подключиться"),
        ]
    )
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
