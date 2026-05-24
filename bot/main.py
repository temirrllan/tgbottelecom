"""Точка входа Telegram-бота монтёра Казактелекома."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from bot.handlers import chat, commands, confirm, dispatcher, photo, route, stats, voice
from bot.handlers.stats import build_stats_text
from bot.services import db
from bot.services.tz import LOCAL_TZ, local_now

logger = logging.getLogger(__name__)


REMINDER_TEXT = (
    "⏰ Не забудь зафиксировать заявки! "
    "Просто напиши, что сделал — я сохраню."
)


async def evening_summary_loop(bot: Bot) -> None:
    """
    Раз в день в 19:00 (пн–сб) рассылает вечернюю сводку каждому активному монтёру:
    сколько заявок закрыл, сколько материалов потратил.
    """
    summary_hour = int(os.getenv("EVENING_SUMMARY_HOUR", "19"))
    fired_dates: set = set()

    while True:
        try:
            now = local_now()
            today = now.date()
            # Чистим старые отметки, оставляем только сегодняшнюю
            fired_dates = {d for d in fired_dates if d == today}

            in_window = (
                now.weekday() < 6
                and now.hour == summary_hour
                and now.minute < 10  # 10-минутное окно срабатывания
                and today not in fired_dates
            )
            if in_window:
                fired_dates.add(today)
                await _send_evening_summaries(bot)
        except Exception:
            logger.exception("Ошибка в цикле вечерней сводки")

        await asyncio.sleep(60)


async def _send_evening_summaries(bot: Bot) -> None:
    """Рассылает сводку всем монтёрам, у которых сегодня была активность."""
    pool = db._get_pool()
    today = local_now().date()
    rows = await pool.fetch(
        """
        SELECT DISTINCT user_id FROM tickets
        WHERE (visit_date AT TIME ZONE $1)::date = $2
        """,
        str(LOCAL_TZ), today,
    )
    for row in rows:
        uid = int(row["user_id"])
        try:
            text = await build_stats_text(uid, "today")
            await bot.send_message(uid, "🌆 <b>Вечерняя сводка</b>\n\n" + text)
        except Exception as e:
            logger.warning("Не удалось отправить сводку %s: %s", uid, e)


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
    #   1) команды (/start, /help, /today …),
    #   2) статистика,
    #   3) маршрут (включая F.location),
    #   4) подтверждение заявки (перехватывает текст в FSM-состоянии),
    #   5) фотографии,
    #   6) голосовые сообщения,
    #   7) свободный чат с ИИ.
    dp.include_router(commands.router)
    dp.include_router(dispatcher.router)
    dp.include_router(stats.router)
    dp.include_router(route.router)
    dp.include_router(confirm.router)
    dp.include_router(photo.router)
    dp.include_router(voice.router)
    dp.include_router(chat.router)

    # Инициализируем БД и применяем миграции
    await db.init_db()

    # Фоновые таски: напоминания и вечерняя сводка
    reminder_task = asyncio.create_task(reminder_loop(bot))
    evening_task = asyncio.create_task(evening_summary_loop(bot))

    try:
        logger.info("Бот стартует...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in (reminder_task, evening_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await db.close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
