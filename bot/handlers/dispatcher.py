"""
Команды и логика, доступные только КРОСС-у.

Команды:
  /team — список монтёров и их текущая загрузка.
  /inbox — заявки, которые я (КРОСС) создал, и их статус.
           Открытые заявки имеют кнопки «🔄 Передать» и «🗑 Удалить».
  /new — короткая подсказка по созданию заявки.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Optional

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


# --- Callback data ----------------------------------------------------------

class InboxActionCB(CallbackData, prefix="iax"):
    """Нажатие на «Передать» или «Удалить» в /inbox."""
    action: str  # "reassign" | "delete"
    ticket_id: int


class ReassignToCB(CallbackData, prefix="rto"):
    """Выбор нового монтёра при переназначении."""
    ticket_id: int
    monteur_id: int  # 0 = отмена


class DeleteConfirmCB(CallbackData, prefix="dcf"):
    """Подтверждение удаления."""
    ticket_id: int
    confirm: int  # 0 = отмена, 1 = удалить


class MsgToCB(CallbackData, prefix="mto"):
    """Выбор монтёра для прямого сообщения."""
    monteur_id: int  # 0 = отмена


class DispatcherMessaging(StatesGroup):
    """FSM КРОСС, когда она набирает сообщение монтёру через /msg."""
    composing = State()


# --- /team ------------------------------------------------------------------

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


# --- /inbox -----------------------------------------------------------------

@router.message(Command("inbox"))
async def cmd_inbox(message: Message) -> None:
    """Заявки, созданные текущим КРОСС-ом, с кнопками управления."""
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
    kb_rows: list[list[InlineKeyboardButton]] = []

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
        # Кнопки управления — только для открытых заявок
        if not t.work_done:
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"🔄 Передать #{number}",
                    callback_data=InboxActionCB(
                        action="reassign", ticket_id=t.id,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=f"🗑 Удалить #{number}",
                    callback_data=InboxActionCB(
                        action="delete", ticket_id=t.id,
                    ).pack(),
                ),
            ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await message.answer("\n".join(lines), reply_markup=kb)


# --- /new -------------------------------------------------------------------

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


# --- Передать заявку --------------------------------------------------------

@router.callback_query(InboxActionCB.filter(F.action == "reassign"))
async def on_reassign_ask(
    cb: CallbackQuery,
    callback_data: InboxActionCB,
) -> None:
    """Спрашивает у КРОСС — кому передать выбранную заявку."""
    if cb.from_user is None or cb.message is None:
        await cb.answer()
        return
    if not is_dispatcher(cb.from_user.id):
        await cb.answer("Только для КРОСС", show_alert=True)
        return

    ticket = await db.get_ticket_for_dispatcher(callback_data.ticket_id, cb.from_user.id)
    if ticket is None:
        await cb.answer("Заявка не найдена или не твоя", show_alert=True)
        return
    if ticket.work_done:
        await cb.answer("Закрытую заявку нельзя передать", show_alert=True)
        return

    monteurs = await db.list_users_except(dispatcher_ids())
    if not monteurs:
        await cb.answer("Нет монтёров в системе", show_alert=True)
        return

    rows: list[list[InlineKeyboardButton]] = []
    for m in monteurs:
        if m["id"] == ticket.user_id:
            continue  # пропускаем текущего исполнителя
        open_cnt = await db.count_open_tickets_for(m["id"])
        load = "свободен" if open_cnt == 0 else f"{open_cnt} откр."
        rows.append([InlineKeyboardButton(
            text=f"👷 {m['full_name']} • {load}",
            callback_data=ReassignToCB(
                ticket_id=ticket.id, monteur_id=m["id"],
            ).pack(),
        )])
    rows.append([InlineKeyboardButton(
        text="❌ Отмена",
        callback_data=ReassignToCB(ticket_id=ticket.id, monteur_id=0).pack(),
    )])

    current_executor = await db.get_user(ticket.user_id)
    current_name = (
        escape(current_executor["full_name"], quote=False)
        if current_executor else f"id{ticket.user_id}"
    )
    number = ticket.user_ticket_number or ticket.id

    await cb.message.answer(
        f"🔄 <b>Передать заявку #{number}</b>\n"
        f"Сейчас у: <b>{current_name}</b>\n"
        f"📍 {escape(ticket.address[:80], quote=False)}\n\n"
        f"Кому передать?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(ReassignToCB.filter())
async def on_reassign_do(
    cb: CallbackQuery,
    callback_data: ReassignToCB,
) -> None:
    """Выполняет переназначение и шлёт уведомления обоим монтёрам."""
    if cb.from_user is None or cb.message is None:
        await cb.answer()
        return
    if not is_dispatcher(cb.from_user.id):
        await cb.answer("Только для КРОСС", show_alert=True)
        return

    # Снимаем кнопки выбора
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    if callback_data.monteur_id == 0:
        await cb.message.answer("❌ Передача отменена.")
        await cb.answer("Отмена")
        return

    # Берём данные ДО переназначения — нужны для уведомления старому
    ticket_before = await db.get_ticket_for_dispatcher(
        callback_data.ticket_id, cb.from_user.id,
    )
    if ticket_before is None:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    old_number = ticket_before.user_ticket_number or ticket_before.id

    success, old_user_id, new_number = await db.reassign_ticket(
        callback_data.ticket_id,
        callback_data.monteur_id,
        cb.from_user.id,
    )
    if not success:
        await cb.message.answer(
            "Не удалось передать — возможно, заявка закрыта или уже у этого монтёра."
        )
        await cb.answer()
        return

    new_monteur = await db.get_user(callback_data.monteur_id)
    new_name = (
        new_monteur["full_name"] if new_monteur else f"id{callback_data.monteur_id}"
    )
    dispatcher = await db.get_user(cb.from_user.id)
    dispatcher_name = (
        dispatcher["full_name"] if dispatcher else "КРОСС"
    )

    # Подтверждение КРОСС-у
    await cb.message.answer(
        f"✅ Заявка передана <b>{escape(new_name, quote=False)} #{new_number}</b>"
    )
    await cb.answer("Передал")

    # Старому монтёру — уведомление о снятии
    if old_user_id and old_user_id != callback_data.monteur_id and cb.bot is not None:
        try:
            await cb.bot.send_message(
                old_user_id,
                f"❌ <b>Заявка #{old_number} снята с тебя</b> — "
                f"передана монтёру <b>{escape(new_name, quote=False)}</b>\n"
                f"📍 {escape(ticket_before.address, quote=False)}",
            )
        except Exception as e:
            logger.warning("Не удалось уведомить старого монтёра %s: %s", old_user_id, e)

    # Новому монтёру — уведомление с карточкой
    new_ticket = await db.get_ticket(callback_data.monteur_id, callback_data.ticket_id)
    if new_ticket is not None and cb.bot is not None:
        from bot.handlers.confirm import notify_monteur
        await notify_monteur(cb.bot, callback_data.monteur_id, new_ticket, dispatcher)


# --- Удалить заявку ---------------------------------------------------------

@router.callback_query(InboxActionCB.filter(F.action == "delete"))
async def on_delete_ask(
    cb: CallbackQuery,
    callback_data: InboxActionCB,
) -> None:
    """Спрашивает подтверждение удаления."""
    if cb.from_user is None or cb.message is None:
        await cb.answer()
        return
    if not is_dispatcher(cb.from_user.id):
        await cb.answer("Только для КРОСС", show_alert=True)
        return

    ticket = await db.get_ticket_for_dispatcher(callback_data.ticket_id, cb.from_user.id)
    if ticket is None:
        await cb.answer("Заявка не найдена или не твоя", show_alert=True)
        return

    executor = await db.get_user(ticket.user_id)
    executor_name = (
        escape(executor["full_name"], quote=False)
        if executor else f"id{ticket.user_id}"
    )
    number = ticket.user_ticket_number or ticket.id

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🗑 Да, удалить",
            callback_data=DeleteConfirmCB(ticket_id=ticket.id, confirm=1).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=DeleteConfirmCB(ticket_id=ticket.id, confirm=0).pack(),
        ),
    ]])

    await cb.message.answer(
        f"🗑 <b>Удалить заявку #{number}?</b>\n"
        f"Монтёр: <b>{executor_name}</b>\n"
        f"📍 {escape(ticket.address[:80], quote=False)}\n\n"
        f"⚠️ Заявка будет удалена безвозвратно вместе с фото и материалами.\n"
        f"Монтёр получит уведомление.",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(DeleteConfirmCB.filter())
async def on_delete_confirm(
    cb: CallbackQuery,
    callback_data: DeleteConfirmCB,
) -> None:
    """Удаляет заявку (если подтверждено) и шлёт пуш монтёру."""
    if cb.from_user is None or cb.message is None:
        await cb.answer()
        return
    if not is_dispatcher(cb.from_user.id):
        await cb.answer("Только для КРОСС", show_alert=True)
        return

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    if callback_data.confirm == 0:
        await cb.message.answer("❌ Удаление отменено.")
        await cb.answer("Отмена")
        return

    # Запоминаем адрес ДО удаления — для уведомления
    ticket_before = await db.get_ticket_for_dispatcher(
        callback_data.ticket_id, cb.from_user.id,
    )
    address = ticket_before.address if ticket_before else "?"

    success, executor_id, number = await db.delete_ticket(
        callback_data.ticket_id, cb.from_user.id,
    )
    if not success:
        await cb.message.answer("Не удалось удалить заявку.")
        await cb.answer()
        return

    await cb.message.answer(f"🗑 Заявка #{number} удалена.")
    await cb.answer("Удалил")

    # Уведомление монтёру
    if executor_id and cb.bot is not None:
        dispatcher = await db.get_user(cb.from_user.id)
        dispatcher_name = dispatcher["full_name"] if dispatcher else "КРОСС"
        try:
            await cb.bot.send_message(
                executor_id,
                f"🗑 <b>Заявка #{number} отменена</b>\n"
                f"КРОСС: <b>{escape(dispatcher_name, quote=False)}</b>\n"
                f"📍 {escape(address, quote=False)}\n\n"
                f"Можешь не выезжать.",
            )
        except Exception as e:
            logger.warning(
                "Не удалось уведомить монтёра %s об удалении: %s",
                executor_id, e,
            )


# --- /msg — прямое сообщение монтёру ---------------------------------------

@router.message(Command("msg"))
async def cmd_msg(message: Message, state: FSMContext) -> None:
    """Открывает выбор монтёра для отправки прямого сообщения."""
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

    rows: list[list[InlineKeyboardButton]] = []
    for m in monteurs:
        rows.append([InlineKeyboardButton(
            text=f"👷 {m['full_name']}",
            callback_data=MsgToCB(monteur_id=m["id"]).pack(),
        )])
    rows.append([InlineKeyboardButton(
        text="❌ Отмена",
        callback_data=MsgToCB(monteur_id=0).pack(),
    )])

    await message.answer(
        "📩 <b>Кому отправить сообщение?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(MsgToCB.filter())
async def on_msg_to_chosen(
    cb: CallbackQuery,
    callback_data: MsgToCB,
    state: FSMContext,
) -> None:
    """КРОСС выбрала монтёра — теперь ждём само сообщение."""
    if cb.from_user is None or cb.message is None:
        await cb.answer()
        return
    if not is_dispatcher(cb.from_user.id):
        await cb.answer("Только для КРОСС", show_alert=True)
        return

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    if callback_data.monteur_id == 0:
        await state.clear()
        await cb.message.answer("❌ Отмена.")
        await cb.answer()
        return

    monteur = await db.get_user(callback_data.monteur_id)
    if monteur is None:
        await cb.answer("Монтёр не найден", show_alert=True)
        return

    await state.set_state(DispatcherMessaging.composing)
    await state.update_data(target_monteur_id=callback_data.monteur_id)
    await cb.message.answer(
        f"✍️ Напиши сообщение для <b>{escape(monteur['full_name'], quote=False)}</b>.\n"
        f"Можно текст, голосовое или фото.\n\n"
        f"<i>/cancel — отмена</i>"
    )
    await cb.answer()


# --- Перехват сообщений КРОСС в режиме compose ------------------------------

async def _resolve_target(state: FSMContext) -> Optional[dict]:
    """Достаёт из FSM выбранного монтёра."""
    data = await state.get_data()
    tid = data.get("target_monteur_id")
    if not tid:
        return None
    return await db.get_user(int(tid))


def _msg_header(sender_name: str, kind: str) -> str:
    """Префикс пересланного сообщения. kind: 'сообщение' / 'голосовое' / 'фото'."""
    return f"📩 <b>{kind.capitalize()} от {escape(sender_name, quote=False)} (КРОСС)</b>"


@router.message(
    StateFilter(DispatcherMessaging.composing),
    F.text & ~F.text.startswith("/"),
)
async def on_msg_text(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text or message.bot is None:
        return
    target = await _resolve_target(state)
    if target is None:
        await state.clear()
        await message.answer("Получатель потерян, попробуй /msg заново.")
        return

    sender = await db.get_user(message.from_user.id)
    sender_name = (sender or {}).get("full_name", "КРОСС")

    body = escape(message.text, quote=False)
    try:
        await message.bot.send_message(
            target["id"],
            f"{_msg_header(sender_name, 'сообщение')}\n\n{body}",
        )
        await message.answer(
            f"✅ Доставил <b>{escape(target['full_name'], quote=False)}</b>:\n"
            f"<i>«{body[:200]}»</i>"
        )
    except Exception as e:
        logger.warning("Не доставил текст %s: %s", target["id"], e)
        await message.answer(
            "Не удалось доставить — возможно, монтёр ещё не нажимал /start."
        )
    await state.clear()


@router.message(
    StateFilter(DispatcherMessaging.composing),
    F.voice,
)
async def on_msg_voice(message: Message, state: FSMContext) -> None:
    if message.from_user is None or message.voice is None or message.bot is None:
        return
    target = await _resolve_target(state)
    if target is None:
        await state.clear()
        await message.answer("Получатель потерян, попробуй /msg заново.")
        return

    sender = await db.get_user(message.from_user.id)
    sender_name = (sender or {}).get("full_name", "КРОСС")

    try:
        await message.bot.send_message(
            target["id"],
            _msg_header(sender_name, "голосовое"),
        )
        await message.bot.send_voice(target["id"], message.voice.file_id)
        await message.answer(
            f"✅ Голосовое отправлено <b>{escape(target['full_name'], quote=False)}</b>."
        )
    except Exception as e:
        logger.warning("Не доставил voice %s: %s", target["id"], e)
        await message.answer("Не удалось переслать голосовое.")
    await state.clear()


@router.message(
    StateFilter(DispatcherMessaging.composing),
    F.photo,
)
async def on_msg_photo(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.photo or message.bot is None:
        return
    target = await _resolve_target(state)
    if target is None:
        await state.clear()
        await message.answer("Получатель потерян, попробуй /msg заново.")
        return

    sender = await db.get_user(message.from_user.id)
    sender_name = (sender or {}).get("full_name", "КРОСС")
    caption = (message.caption or "").strip()

    try:
        header = _msg_header(sender_name, "фото")
        if caption:
            header += f"\n\n{escape(caption, quote=False)}"
        await message.bot.send_message(target["id"], header)
        await message.bot.send_photo(target["id"], message.photo[-1].file_id)
        await message.answer(
            f"✅ Фото отправлено <b>{escape(target['full_name'], quote=False)}</b>."
        )
    except Exception as e:
        logger.warning("Не доставил фото %s: %s", target["id"], e)
        await message.answer("Не удалось переслать фото.")
    await state.clear()
