"""
Команды и логика, доступные только КРОСС-у.

Команды:
  /team — список монтёров и их текущая загрузка.
  /inbox — заявки, которые я (КРОСС) создал, и их статус.
  /new — короткая подсказка по созданию заявки (на деле она создаётся
         простым текстовым сообщением, как у монтёра).
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services import db
from bot.services.formatting import format_ticket
from bot.services.roles import dispatcher_ids, is_dispatcher

logger = logging.getLogger(__name__)
router = Router(name="dispatcher")


@router.message(Command("team"))
async def cmd_team(message: Message) -> None:
    """Список монтёров с загрузкой (открытые заявки)."""
    if message.from_user is None:
        return
    if not is_dispatcher(message.from_user.id):
        await message.answer("Эта команда — только для КРОСС.")
        return

    monteurs = await db.list_users_except(dispatcher_ids())
    if not monteurs:
        await message.answer(
            "Монтёров в системе нет. Попроси их написать боту /start."
        )
        return

    lines = ["👥 <b>Бригада</b>", ""]
    total_open = 0
    for m in monteurs:
        open_cnt = await db.count_open_tickets_for(m["id"])
        total_open += open_cnt
        status = (
            "🟢 свободен" if open_cnt == 0
            else f"🟡 {open_cnt} открыт."
        )
        username = f" @{m['username']}" if m["username"] else ""
        lines.append(
            f"• <b>{escape(m['full_name'], quote=False)}</b>{username} — {status}"
        )
    lines.append("")
    lines.append(f"📊 Всего открытых заявок: <b>{total_open}</b>")
    await message.answer("\n".join(lines))


@router.message(Command("inbox"))
async def cmd_inbox(message: Message) -> None:
    """Заявки, созданные текущим КРОСС-ом."""
    if message.from_user is None:
        return
    if not is_dispatcher(message.from_user.id):
        await message.answer("Эта команда — только для КРОСС.")
        return

    tickets = await db.list_dispatcher_inbox(message.from_user.id, limit=20)
    if not tickets:
        await message.answer("Ты ещё не создавал ни одной заявки.")
        return

    lines: list[str] = [f"📂 <b>Мои назначения</b> (всего: {len(tickets)})", ""]
    for t in tickets:
        executor = await db.get_user(t.user_id)
        executor_name = (
            escape(executor["full_name"], quote=False) if executor else f"id{t.user_id}"
        )
        status_icon = "✅" if t.work_done else "⏳"
        number = t.user_ticket_number or t.id
        lines.append(
            f"{status_icon} <b>{executor_name}</b> #{number} • "
            f"{escape(t.address[:60], quote=False)}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("new"))
async def cmd_new(message: Message) -> None:
    """Подсказка по созданию новой заявки."""
    if message.from_user is None:
        return
    if not is_dispatcher(message.from_user.id):
        await message.answer("Эта команда — только для КРОСС.")
        return
    await message.answer(
        "📝 <b>Новая заявка</b>\n\n"
        "Просто опиши её в свободной форме — текстом, голосом или с фото:\n\n"
        "<i>«Абилов, 7029583619, ул. Беркимбаева 102/13, нет интернета»</i>\n\n"
        "После проверки превью бот спросит, кому из монтёров отдать."
    )
