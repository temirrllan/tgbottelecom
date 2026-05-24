"""Точка входа Telegram-бота монтёра Казактелекома."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from bot.handlers import chat, commands, confirm, photo, voice
from bot.services import db

logger = logging.getLogger(__name__)


REMINDER_TEXT = (
    "⏰ Не забудь зафиксировать заявки! "
    "Просто напиши, что сделал — я сохраню."
)


async def reminder_loop(bot: Bot) -> None:
    """
    Фоновая задача: раз в час шлёт напоминания тем,
    кто не писал боту больше 4 часов в рабочее время.
    """
    work_start = int(os.getenv("REMINDER_HOUR_START", "8"))
    work_end = int(os.getenv("REMINDER_HOUR_END", "18"))
    interval_seconds = 60 * 60  # раз в час

    while True:
        try:
            user_ids = await db.get_idle_users(
                idle_hours=4,
                work_hour_start=work_start,
                work_hour_end=work_end,
            )
            for uid in user_ids:
                try:
                    await bot.send_message(uid, REMINDER_TEXT)
                    # Чтобы не спамить — сдвигаем last_active_at
                    await db.touch_user(uid)
                except Exception as e:
                    logger.warning("Не удалось отправить напоминание %s: %s", uid, e)
        except Exception:
            logger.exception("Ошибка в цикле напоминаний")

        await asyncio.sleep(interval_seconds)


async def main() -> None:
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    token = os.environ["BOT_TOKEN"]
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Порядок важен:
    #   1) команды,
    #   2) подтверждение заявки (перехватывает текст в FSM-состоянии),
    #   3) фотографии,
    #   4) голосовые сообщения,
    #   5) свободный чат с ИИ.
    dp.include_router(commands.router)
    dp.include_router(confirm.router)
    dp.include_router(photo.router)
    dp.include_router(voice.router)
    dp.include_router(chat.router)

    # Инициализируем БД и применяем миграции
    await db.init_db()

    # Запускаем фоновый таск напоминаний
    reminder_task = asyncio.create_task(reminder_loop(bot))

    try:
        logger.info("Бот стартует...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        reminder_task.cancel()
        with suppress(asyncio.CancelledError):
            await reminder_task
        await db.close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
