"""
Обработка голосовых сообщений.

Логика:
  1. Скачиваем OGG/Opus из Telegram.
  2. Транскрибируем через Gemini.
  3. Показываем монтёру распознанный текст.
  4. Дальше отправляем по тому же маршруту, что и обычный текст:
     - если есть открытый черновик → ai.merge_ticket → новое превью,
     - иначе → ai.analyze_message → SAVE / QUERY / EDIT / CHAT.
"""
from __future__ import annotations

import io
import logging
from html import escape
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.handlers.chat import process_user_text
from bot.handlers.confirm import TicketConfirm, apply_text_edit
from bot.services import ai, db

logger = logging.getLogger(__name__)
router = Router(name="voice")


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None or message.voice is None:
        return

    await db.upsert_user(user.id, user.username, user.full_name)
    await db.touch_user(user.id)

    # Telegram-индикатор «печатает...» пока обрабатываем
    if message.bot:
        try:
            await message.bot.send_chat_action(message.chat.id, "typing")
        except Exception:
            pass

    audio_bytes = await _download_voice(message)
    if audio_bytes is None:
        await message.answer("Не смог скачать голосовое. Попробуй ещё раз.")
        return

    text = await ai.transcribe_voice(audio_bytes)
    if not text:
        await message.answer(
            "🎤 Не получилось распознать голосовое. "
            "Скажи чётче или напиши текстом."
        )
        return

    # Показываем монтёру, что распознали (важно для контроля)
    await message.answer(f"🎤 <i>«{escape(text, quote=False)}»</i>")

    # Маршрутизация: если есть открытый черновик — мержим, иначе — обычная логика
    current_state = await state.get_state()
    in_draft = current_state in (
        TicketConfirm.waiting.state,
        TicketConfirm.editing.state,
    )

    if in_draft:
        await apply_text_edit(message, state, text)
    else:
        await process_user_text(message, state, text)


async def _download_voice(message: Message) -> Optional[bytes]:
    """Качает voice-файл из Telegram и возвращает байты."""
    bot = message.bot
    if bot is None or message.voice is None:
        return None
    try:
        tg_file = await bot.get_file(message.voice.file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        return buf.getvalue()
    except Exception:
        logger.exception("Не удалось скачать голосовое из Telegram")
        return None
