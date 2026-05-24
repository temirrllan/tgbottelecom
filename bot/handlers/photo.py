"""Обработка фотографий: прикрепление к черновику или создание нового."""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from pydantic import ValidationError

from bot.handlers.confirm import TicketConfirm, show_preview
from bot.models.schemas import TicketIn
from bot.services import ai, db

logger = logging.getLogger(__name__)
router = Router(name="photo")


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    """
    Логика:
      - Если у пользователя есть черновик заявки (FSM в waiting/editing) —
        прикрепляем фото к черновику, при наличии caption применяем как правку.
      - Иначе если есть caption — парсим caption как новую заявку и
        автоматически прикрепляем фото.
      - Иначе просим монтёра описать заявку текстом.
    """
    user = message.from_user
    if user is None or not message.photo:
        return

    # Берём максимальный размер фото — он лучшего качества
    file_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()

    await db.upsert_user(user.id, user.username, user.full_name)
    await db.touch_user(user.id)

    current_state = await state.get_state()
    has_draft = current_state in (
        TicketConfirm.waiting.state,
        TicketConfirm.editing.state,
    )

    if has_draft:
        await _attach_to_draft(message, state, file_id, caption)
        return

    if caption:
        await _new_ticket_from_caption(message, state, file_id, caption)
        return

    # Нет ни черновика, ни caption — фото без контекста
    await message.answer(
        "📷 Фото получил, но я пока не знаю, к какой заявке его прикрепить.\n"
        "Опиши заявку (адрес, что сделал) и приложи фото с подписью — "
        "или сначала пришли описание, а фото добавишь после."
    )


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

    # Если есть подпись — применяем её как правку через ИИ
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


async def _new_ticket_from_caption(
    message: Message,
    state: FSMContext,
    file_id: str,
    caption: str,
) -> None:
    """Создаёт новый черновик из подписи к фото."""
    user = message.from_user
    if user is None:
        return

    history = await db.get_recent_history(user.id, limit=10)
    try:
        ai_response = await ai.analyze_message(
            user_text=caption,
            history=history,
            now=datetime.now().astimezone(),
        )
    except Exception:
        logger.exception("Ошибка при разборе caption фото")
        await message.answer("Не получилось понять подпись к фото, попробуй ещё раз.")
        return

    if ai_response.action != "SAVE_TICKET":
        # ИИ не распознал в подписи заявку — просто подтверждаем приём фото
        await message.answer(
            ai_response.reply
            or "📷 Фото получил. Опиши заявку (адрес, что сделал) — приложу его к ней."
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
