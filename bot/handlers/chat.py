"""Основной чат-хендлер — обработка сообщений через ИИ."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from aiogram import Router, F
from aiogram.types import Message
from pydantic import ValidationError

from bot.models.schemas import AIResponse, TicketIn, TicketUpdate
from bot.services import ai, db
from bot.services.formatting import (
    format_materials_summary,
    format_ticket,
    format_tickets_list,
)

logger = logging.getLogger(__name__)
router = Router(name="chat")


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    """Любое текстовое сообщение, кроме команд, идёт через ИИ."""
    user = message.from_user
    if user is None or not message.text:
        return

    # Регистрируем пользователя на лету, если он ещё не сделал /start
    await db.upsert_user(user.id, user.username, user.full_name)
    await db.touch_user(user.id)

    history = await db.get_recent_history(user.id, limit=10)

    try:
        ai_response = await ai.analyze_message(
            user_text=message.text,
            history=history,
            now=datetime.now().astimezone(),
        )
    except Exception:
        logger.exception("Ошибка при вызове ИИ")
        await message.answer("Что-то пошло не так, попробуй ещё раз.")
        return

    # Сохраняем оба сообщения в историю
    await db.add_history(user.id, "user", message.text)
    if ai_response.reply:
        await db.add_history(user.id, "assistant", ai_response.reply)
    await db.trim_history(user.id, keep=20)

    # Маршрутизируем по action
    if ai_response.action == "SAVE_TICKET":
        await _handle_save(message, ai_response)
    elif ai_response.action == "QUERY":
        await _handle_query(message, ai_response)
    elif ai_response.action == "EDIT_TICKET":
        await _handle_edit(message, ai_response)
    else:
        await message.answer(ai_response.reply or "Понял.")


# --- SAVE_TICKET ------------------------------------------------------------

async def _handle_save(message: Message, ai_response: AIResponse) -> None:
    """Сохраняет новую заявку в БД."""
    if message.from_user is None:
        return
    try:
        ticket_in = TicketIn.model_validate(ai_response.data)
    except ValidationError as e:
        logger.warning("ИИ вернул некорректные данные заявки: %s", e)
        await message.answer(
            ai_response.reply
            or "Не хватает данных для заявки. Уточни, пожалуйста, адрес и что делал."
        )
        return

    ticket_id = await db.create_ticket(message.from_user.id, ticket_in)
    saved = await db.get_ticket(message.from_user.id, ticket_id)
    if saved is None:
        await message.answer("Сохранил, но не смог прочитать обратно. Странно.")
        return

    reply = ai_response.reply or "✅ Заявка сохранена!"
    await message.answer(f"{reply}\n\n{format_ticket(saved)}")


# --- QUERY ------------------------------------------------------------------

async def _handle_query(message: Message, ai_response: AIResponse) -> None:
    """Отвечает на вопрос про заявки/материалы."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    data = ai_response.data or {}
    query_type = data.get("query_type", "last_tickets")
    period = data.get("period")
    address = data.get("address")
    limit = data.get("limit") or 5

    if query_type == "materials_summary":
        rows = await db.materials_summary(user_id, period=period or "month")
        header = f"📦 Материалы за {_period_label(period)}"
        text = format_materials_summary(rows, header=header)

    elif query_type == "search_address":
        tickets = await db.list_tickets(
            user_id, search_address=address or "", limit=20,
        )
        text = format_tickets_list(tickets, header=f"🔍 Поиск: {address}")

    elif query_type == "list_tickets":
        tickets = await db.list_tickets(user_id, period=period)
        text = format_tickets_list(
            tickets, header=f"📅 Заявки за {_period_label(period)}",
        )

    else:  # last_tickets и фоллбэк
        tickets = await db.list_tickets(user_id, limit=int(limit))
        text = format_tickets_list(tickets, header="📋 Последние заявки")

    if ai_response.reply:
        text = f"{ai_response.reply}\n\n{text}"
    await message.answer(text)


# --- EDIT_TICKET ------------------------------------------------------------

async def _handle_edit(message: Message, ai_response: AIResponse) -> None:
    """Редактирует заявку, созданную сегодня."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    data = ai_response.data or {}
    ticket_id: Optional[int] = data.get("ticket_id")
    changes = data.get("changes") or {}

    if not ticket_id:
        last = await db.get_last_ticket_today(user_id)
        if last is None:
            await message.answer(
                "Не нашёл сегодняшней заявки для редактирования. "
                "Менять можно только заявки за сегодня."
            )
            return
        ticket_id = last.id

    # Проверяем, что заявка действительно сегодняшняя
    existing = await db.get_ticket(user_id, ticket_id)
    if existing is None:
        await message.answer("Не нашёл такой заявки.")
        return
    if existing.created_at.date() != datetime.now().astimezone().date():
        await message.answer(
            "Редактировать можно только заявки, созданные сегодня."
        )
        return

    try:
        upd = TicketUpdate.model_validate(changes)
    except ValidationError as e:
        logger.warning("Некорректные данные для редактирования: %s", e)
        await message.answer("Не понял, что именно исправить. Уточни?")
        return

    ok = await db.update_ticket(user_id, ticket_id, upd)
    if not ok:
        await message.answer("Не удалось обновить заявку.")
        return

    updated = await db.get_ticket(user_id, ticket_id)
    reply = ai_response.reply or "✏️ Изменил."
    await message.answer(f"{reply}\n\n{format_ticket(updated)}")


# --- Утилиты ----------------------------------------------------------------

def _period_label(period: Optional[str]) -> str:
    return {
        "today": "сегодня",
        "week": "неделю",
        "month": "месяц",
    }.get(period or "", "всё время")
