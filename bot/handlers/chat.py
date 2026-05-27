"""Основной чат-хендлер — обработка сообщений через ИИ."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from pydantic import ValidationError

from bot.handlers.confirm import show_preview
from bot.models.schemas import AIResponse, TicketIn, TicketUpdate
from bot.services import ai, db
from bot.services.formatting import (
    format_materials_summary,
    format_ticket,
    format_tickets_by_day,
    format_tickets_list,
)
from bot.services.roles import is_dispatcher
from bot.services.tz import local_now

logger = logging.getLogger(__name__)
router = Router(name="chat")


async def process_user_text(
    message: Message,
    state: FSMContext,
    text: str,
) -> None:
    """
    Универсальная обработка пользовательского текста через ИИ.
    Используется и для обычных сообщений, и для транскрипций голосовых.
    """
    user = message.from_user
    if user is None or not text:
        return

    # Регистрируем пользователя на лету, если он ещё не сделал /start
    await db.upsert_user(user.id, user.username, user.full_name)
    await db.touch_user(user.id)

    history = await db.get_recent_history(user.id, limit=5)
    # КРОСС не имеет «своих» открытых заявок в роли исполнителя — не передаём контекст
    is_kross = is_dispatcher(user.id)
    open_context = [] if is_kross else await _build_open_tickets_context(user.id)
    role_label = "КРОСС" if is_kross else "монтёр"

    try:
        ai_response = await ai.analyze_message(
            user_text=text,
            history=history,
            now=local_now(),
            open_tickets=open_context,
            user_role=role_label,
        )
    except Exception:
        logger.exception("Ошибка при вызове ИИ")
        await message.answer("Что-то пошло не так, попробуй ещё раз.")
        return

    # Сохраняем оба сообщения в историю
    await db.add_history(user.id, "user", text)
    if ai_response.reply:
        await db.add_history(user.id, "assistant", ai_response.reply)
    await db.trim_history(user.id, keep=20)

    # Маршрутизируем по action
    if ai_response.action == "SAVE_TICKET":
        await _handle_save(message, ai_response, state)
    elif ai_response.action == "QUERY":
        await _handle_query(message, ai_response)
    elif ai_response.action == "EDIT_TICKET":
        await _handle_edit(message, ai_response)
    else:
        await message.answer(ai_response.reply or "Понял.")


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext) -> None:
    """Любое текстовое сообщение, кроме команд, идёт через ИИ."""
    if message.text:
        await process_user_text(message, state, message.text)


# --- SAVE_TICKET ------------------------------------------------------------

async def _handle_save(
    message: Message,
    ai_response: AIResponse,
    state: FSMContext,
) -> None:
    """Готовит черновик заявки и показывает превью с кнопками подтверждения."""
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

    await show_preview(message, state, ticket_in, intro=ai_response.reply or "")


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
        header = f"📅 Заявки за {_period_label(period)}"
        # Для недели — группируем по дням, чтобы видеть «понедельник / вторник...»
        if period == "week":
            text = format_tickets_by_day(tickets, header=header)
        else:
            text = format_tickets_list(tickets, header=header)

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

    existing = None
    if not ticket_id:
        # Без номера — берём последнюю заявку текущего дня
        existing = await db.get_last_ticket_today(user_id)
        if existing is None:
            await message.answer(
                "Не нашёл сегодняшней заявки для редактирования. "
                "Укажи номер: «по 5 ...»"
            )
            return
    else:
        # Пользователь дал номер — пробуем личный номер монтёра
        existing = await db.get_ticket_by_number(user_id, int(ticket_id))
        # Если не нашли — может это длинный номер из CRM
        if existing is None:
            existing = await db.find_ticket_by_crm(user_id, str(ticket_id))

    if existing is None:
        await message.answer("Не нашёл такой заявки.")
        return
    ticket_id = existing.id  # дальше работаем по внутреннему id
    # Правило: редактировать можно либо сегодняшние, либо ещё «открытые»
    # (без work_done). Это позволяет закрывать назначенные КРОСС-ом заявки
    # на следующий день.
    from bot.services.tz import to_local
    same_day = to_local(existing.created_at).date() == local_now().date()
    is_open = not existing.work_done
    if not same_day and not is_open:
        await message.answer(
            "Закрытые заявки прошлых дней редактировать нельзя."
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

    # Если заявку создавал КРОСС и сейчас монтёр впервые проставил work_done,
    # это значит «закрытие» — уведомим КРОСС.
    if (
        updated is not None
        and updated.created_by_id
        and updated.created_by_id != user_id
        and not existing.work_done
        and updated.work_done
        and message.bot is not None
    ):
        monteur = await db.get_user(user_id)
        monteur_name = (monteur or {}).get("full_name", f"монтёр {user_id}")
        display_num = updated.user_ticket_number or updated.id
        try:
            await message.bot.send_message(
                updated.created_by_id,
                f"✅ <b>{monteur_name}</b> закрыл заявку #{display_num}\n\n"
                + format_ticket(updated),
            )
        except Exception as e:
            logger.warning("Не удалось уведомить КРОСС %s: %s",
                           updated.created_by_id, e)


# --- Утилиты ----------------------------------------------------------------

async def _build_open_tickets_context(user_id: int) -> list[dict]:
    """Собирает компактный список открытых заявок для подсказки ИИ."""
    open_tickets = await db.list_open_tickets(user_id, limit=10)
    result: list[dict] = []
    for t in open_tickets:
        info: dict = {
            "number": t.user_ticket_number or t.id,
            "address": t.address,
        }
        if t.problem_description:
            info["problem"] = t.problem_description[:120]
        if t.created_by_id and t.created_by_id != user_id:
            creator = await db.get_user(t.created_by_id)
            if creator:
                info["from_dispatcher"] = creator["full_name"]
        result.append(info)
    return result


def _period_label(period: Optional[str]) -> str:
    return {
        "today": "сегодня",
        "week": "неделю",
        "month": "месяц",
    }.get(period or "", "всё время")
