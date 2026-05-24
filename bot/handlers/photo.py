"""
Обработка фотографий.

Логика:
  1. Сначала пробуем распознать скриншот CRM-заявки через Gemini Vision.
     Если получилось — создаём черновик с извлечёнными полями.
  2. Если Vision не распознал (это фото акта / работы / просто картинка)
     и у пользователя открыт черновик — прикрепляем фото к нему.
  3. Если есть подпись к фото и черновика нет — парсим подпись как описание
     новой заявки, фото прикрепляем как доказательство.
  4. Иначе — просим монтёра описать заявку текстом.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from pydantic import ValidationError

from bot.handlers.confirm import TicketConfirm, show_preview
from bot.models.schemas import TicketIn
from bot.services import ai, db
from bot.services.tz import local_now

logger = logging.getLogger(__name__)
router = Router(name="photo")


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None or not message.photo:
        return

    # Максимальный размер фото — лучшее качество для Vision
    file_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()

    await db.upsert_user(user.id, user.username, user.full_name)
    await db.touch_user(user.id)

    # Шаг 1. Пытаемся распознать CRM-скриншот через Vision
    image_bytes = await _download_photo(message, file_id)
    crm_data: Optional[dict] = None
    if image_bytes is not None:
        crm_data = await ai.analyze_crm_photo(image_bytes)

    if crm_data:
        await _new_ticket_from_crm_photo(message, state, file_id, crm_data, caption)
        return

    # Шаг 2. Vision не распознал CRM — смотрим состояние пользователя
    current_state = await state.get_state()
    has_draft = current_state in (
        TicketConfirm.waiting.state,
        TicketConfirm.editing.state,
    )

    if has_draft:
        await _attach_to_draft(message, state, file_id, caption)
        return

    # Шаг 3. Подпись к фото — описание новой заявки
    if caption:
        await _new_ticket_from_caption(message, state, file_id, caption)
        return

    # Шаг 4. Контекста нет — просим описание
    await message.answer(
        "📷 Фото получил, но это не похоже на скриншот заявки.\n"
        "Опиши, к какой заявке его прикрепить — или пришли скриншот из CRM."
    )


# --- Скачивание фото из Telegram --------------------------------------------

async def _download_photo(message: Message, file_id: str) -> Optional[bytes]:
    """Скачивает фотографию по file_id и возвращает её как bytes."""
    bot = message.bot
    if bot is None:
        return None
    try:
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        return buf.getvalue()
    except Exception:
        logger.exception("Не удалось скачать фото из Telegram")
        return None


# --- Новый черновик из CRM-скриншота ----------------------------------------

async def _new_ticket_from_crm_photo(
    message: Message,
    state: FSMContext,
    file_id: str,
    crm_data: dict,
    caption: str,
) -> None:
    """Создаёт черновик из данных, распознанных на скриншоте CRM."""
    data = dict(crm_data)
    data["photos"] = [file_id]

    try:
        ticket_in = TicketIn.model_validate(data)
    except ValidationError as e:
        logger.warning("Vision дал невалидные поля: %s — %s", crm_data, e)
        await message.answer(
            "🔍 Распознал что-то на скриншоте, но не смог собрать заявку. "
            "Уточни данные текстом."
        )
        return

    # Если есть подпись — применяем её как правку поверх распознанного
    if caption:
        merged = await ai.merge_ticket(caption, ticket_in.model_dump(mode="json"))
        try:
            ticket_in = TicketIn.model_validate(merged)
        except ValidationError:
            logger.warning("Не удалось применить подпись к Vision-данным")

    await show_preview(
        message, state, ticket_in,
        intro="🔍 Распознал данные из CRM-скриншота:",
    )


# --- Старая логика: прикрепление к черновику --------------------------------

async def _attach_to_draft(
    message: Message,
    state: FSMContext,
    file_id: str,
    caption: str,
) -> None:
    """Добавляет фото к существующему черновику."""
    data = await state.get_data()
    pending = dict(data.get("pending_ticket") or {})

    photos = list(pending.get("photos") or [])
    photos.append(file_id)
    pending["photos"] = photos

    if caption:
        pending = await ai.merge_ticket(caption, pending)

    try:
        merged_ticket = TicketIn.model_validate(pending)
    except ValidationError:
        logger.warning("Черновик с фото не прошёл валидацию: %s", pending)
        await message.answer(
            "Фото добавил, но правка не применилась. Попробуй ещё раз."
        )
        return

    await show_preview(
        message, state, merged_ticket,
        intro=f"📷 Фото прикреплено (всего: {len(merged_ticket.photos)}).",
    )


# --- Новый черновик из подписи к фото (не-CRM фото) -------------------------

async def _new_ticket_from_caption(
    message: Message,
    state: FSMContext,
    file_id: str,
    caption: str,
) -> None:
    """Создаёт черновик из подписи к обычному фото (фото становится доказательством)."""
    user = message.from_user
    if user is None:
        return

    history = await db.get_recent_history(user.id, limit=10)
    try:
        ai_response = await ai.analyze_message(
            user_text=caption,
            history=history,
            now=local_now(),
        )
    except Exception:
        logger.exception("Ошибка при разборе caption фото")
        await message.answer("Не получилось понять подпись к фото.")
        return

    if ai_response.action != "SAVE_TICKET":
        await message.answer(
            ai_response.reply
            or "📷 Фото получил. Опиши заявку — приложу его к ней."
        )
        return

    data = dict(ai_response.data or {})
    data["photos"] = [file_id]
    try:
        ticket_in = TicketIn.model_validate(data)
    except ValidationError as e:
        logger.warning("ИИ вернул некорректные данные по подписи фото: %s", e)
        await message.answer(
            ai_response.reply
            or "Не хватает данных. Укажи хотя бы адрес и что делал."
        )
        return

    await show_preview(
        message, state, ticket_in,
        intro=(ai_response.reply or "") + "\n📷 Фото прикреплено.",
    )
