from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

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
    dp.errors.register(on_error)
    return dp


async def on_error(event: ErrorEvent) -> bool:
    """Любая ошибка в обработчике (битые данные, подделанная кнопка и т.п.) логируется,
    пользователь получает короткий ответ, а бот продолжает работать."""
    logging.getLogger("bot").error("Ошибка при обработке апдейта", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer("Произошла ошибка, попробуйте ещё раз", show_alert=True)
        elif update.message:
            await update.message.answer("Произошла ошибка, попробуйте ещё раз.")
    except Exception:
        pass
    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    set_timezone(settings.timezone)
    if not settings.admin_ids:
        logging.warning("ADMIN_IDS не задан — некому подтверждать оплаты и управлять пользователями")

    db = Database(settings.db_path)
    await db.connect()

    # Серверы хранятся в базе: при первом запуске бот добавит сервер, на котором запущен
    # (или демо-серверы при AWG_MOCK=1), остальные добавляются из админки.
    service = VpnService(settings, db)
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
        await service.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
