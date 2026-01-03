import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .config import load_settings
from .services.trackrater_api import TrackRaterAPI
from .handlers import start, submit, payments, raise_priority

logging.basicConfig(level=logging.INFO)

async def main():
    settings = load_settings()
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    api = TrackRaterAPI(settings.trackrater_base_url, settings.trackrater_bot_token)

    # Inject deps
    dp["settings"] = settings
    dp["api"] = api

    dp.include_router(start.router)
    dp.include_router(submit.router)
    dp.include_router(raise_priority.router)
    dp.include_router(payments.router)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
