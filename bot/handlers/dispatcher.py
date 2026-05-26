"""
Команды и логика, доступные только КРОСС-у.

Команды:
  /team — список монтёров и их текущая загрузка.
  /inbox — заявки, которые я (КРОСС) создал, и их статус.
  /new — короткая подсказка по созданию заявки (на деле она создаётся
         простым текстовым сообщением, как у монтёра).
  /msg — отправить произвольное сообщение конкретному монтёру.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.services import db
from bot.services.formatting import format_ticket
from bot.services.roles import dispatcher_ids, is_dispatcher

logger = logging.getLogger(__name__)
router = Router(name="dispatcher")


class CrossMessage(StatesGroup):
    """Кросс пишет произвольное сообщение монтёру."""
    waiting_text = State()      # ждём текст сообщения
    waiting_monteur = State()   # ждём выбора монтёра-получателя


class CrossMsgCB(CallbackData, prefix="cm"):
    """Выбор адресата для произвольного сообщения от КРОСС."""
    monteur_id: int  # 0 — отмена


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


# --- Произвольное сообщение монтёру ----------------------------------------

@router.message(Command("msg"))
async def cmd_msg(message: Message, state: FSMContext) -> None:
    """Старт диалога: КРОСС пишет сообщение, потом выбирает монтёра."""
    if message.from_user is None:
        return
    if not is_dispatcher(message.from_user.id):
        await message.answer("Эта команда — только для КРОСС.")
        return

    await state.clear()
    await state.set_state(CrossMessage.waiting_text)
    await message.answer(
        "💬 <b>Сообщение монтёру</b>\n\n"
        "Напиши текст — потом выберешь, кому отправить.\n"
        "Чтобы отменить — /cancel."
    )


@router.message(
    StateFilter(CrossMessage.waiting_text),
    F.text & ~F.text.startswith("/"),
)
async def on_msg_text(message: Message, state: FSMContext) -> None:
    """Получили текст сообщения — показываем выбор монтёра."""
    if message.from_user is None or not message.text:
        return
    if not is_dispatcher(message.from_user.id):
        await state.clear()
        return

    text = message.text.strip()
    if not text:
        await message.answer("Пустое сообщение не отправлю. Напиши текст или /cancel.")
        return

    monteurs = await db.list_users_except(dispatcher_ids())
    if not monteurs:
        await state.clear()
        await message.answer(
            "Нет ни одного монтёра в системе. "
            "Попроси их написать боту /start."
        )
        return

    await state.update_data(message_text=text)
    await state.set_state(CrossMessage.waiting_monteur)

    rows: list[list[InlineKeyboardButton]] = []
    for m in monteurs:
        rows.append([
            InlineKeyboardButton(
                text=f"👷 {m['full_name']}",
                callback_data=CrossMsgCB(monteur_id=m["id"]).pack(),
            ),
        ])
    rows.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=CrossMsgCB(monteur_id=0).pack(),
        ),
    ])

    preview = text if len(text) <= 200 else text[:200] + "…"
    await message.answer(
        f"📨 <b>Кому отправить?</b>\n\n<i>{escape(preview, quote=False)}</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(CrossMsgCB.filter())
async def on_msg_assign(
    cb: CallbackQuery,
    callback_data: CrossMsgCB,
    state: FSMContext,
) -> None:
    """КРОСС выбрал монтёра — отправляем ему сообщение."""
    if cb.from_user is None or cb.message is None:
        await cb.answer()
        return
    if not is_dispatcher(cb.from_user.id):
        await cb.answer()
        return

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    if callback_data.monteur_id == 0:
        await state.clear()
        await cb.message.answer("❌ Отправка отменена.")
        await cb.answer("Отмена")
        return

    data = await state.get_data()
    text = (data.get("message_text") or "").strip()
    await state.clear()

    if not text:
        await cb.message.answer("Текст сообщения потерян. Начни заново: /msg")
        await cb.answer()
        return

    monteur = await db.get_user(callback_data.monteur_id)
    if monteur is None:
        await cb.message.answer("Монтёр не найден.")
        await cb.answer("Не нашёл", show_alert=True)
        return

    sender = await db.get_user(cb.from_user.id)
    sender_name = (sender or {}).get("full_name") or "КРОСС"
    notice = (
        f"💬 <b>Сообщение от {escape(sender_name, quote=False)} (КРОСС)</b>\n\n"
        f"{escape(text, quote=False)}"
    )

    if cb.bot is None:
        await cb.message.answer("Не получилось доставить — нет бот-сессии.")
        await cb.answer()
        return

    try:
        await cb.bot.send_message(monteur["id"], notice)
    except Exception as e:
        logger.warning("Не удалось доставить сообщение %s: %s", monteur["id"], e)
        await cb.message.answer(
            f"⚠️ Не удалось доставить сообщение <b>{escape(monteur['full_name'], quote=False)}</b>."
        )
        await cb.answer("Ошибка", show_alert=True)
        return

    await cb.message.answer(
        f"✅ Отправил <b>{escape(monteur['full_name'], quote=False)}</b>."
    )
    await cb.answer("Отправлено")
