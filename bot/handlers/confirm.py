"""Подтверждение / редактирование заявки перед сохранением."""
from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from pydantic import ValidationError

from bot.models.schemas import TicketIn
from bot.services import ai, db
from bot.services.formatting import format_ticket

logger = logging.getLogger(__name__)
router = Router(name="confirm")


# --- FSM ---------------------------------------------------------------------

class TicketConfirm(StatesGroup):
    """Пользователь увидел превью и должен подтвердить / отменить / поправить."""
    waiting = State()    # ждём нажатия кнопки или правки текстом
    editing = State()    # нажал «Изменить», ждём текста правки


# --- Callback data -----------------------------------------------------------

class TicketCB(CallbackData, prefix="tc"):
    """Действие пользователя по кнопке."""
    action: str  # "save" | "cancel" | "edit"


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Сохранить",
                callback_data=TicketCB(action="save").pack(),
            ),
            InlineKeyboardButton(
                text="✏️ Изменить",
                callback_data=TicketCB(action="edit").pack(),
            ),
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data=TicketCB(action="cancel").pack(),
            ),
        ]]
    )


# --- Превью черновика --------------------------------------------------------

def _e(value) -> str:
    return escape(str(value), quote=False)


def _format_preview(t: TicketIn) -> str:
    """Форматирует ещё не сохранённую заявку (без id)."""
    lines = ["📋 <b>Превью заявки</b>"]
    lines.append(f"📍 Адрес: {_e(t.address)}")
    if t.visit_date:
        lines.append(f"🕐 Время: {t.visit_date.strftime('%d.%m.%Y %H:%M')}")
    if t.problem_description:
        lines.append(f"🔧 Проблема: {_e(t.problem_description)}")
    if t.work_done:
        lines.append(f"✅ Что сделал: {_e(t.work_done)}")
    if t.materials:
        mats = ", ".join(
            f"{_e(m.name)} {_fmt_qty(m.quantity)}{_e(m.unit)}"
            for m in t.materials
        )
        lines.append(f"📦 Материалы: {mats}")
    if t.act_number:
        lines.append(f"📄 Акт: №{_e(t.act_number)}")
    if t.photos:
        lines.append(f"📷 Фото: {len(t.photos)}")
    if t.is_repeat_visit:
        lines.append("🔁 Повторная: да")
    return "\n".join(lines)


async def send_photos(message: Message, file_ids: list[str]) -> None:
    """
    Отправляет фотографии заявки. До 10 шт в одном альбоме,
    Telegram режет больше. Если фото одно — отправляем как обычное.
    """
    if not file_ids or not message.bot:
        return
    if len(file_ids) == 1:
        try:
            await message.bot.send_photo(message.chat.id, file_ids[0])
        except TelegramBadRequest:
            logger.exception("Не удалось отправить фото")
        return
    # Telegram разрешает максимум 10 элементов в media group
    for chunk_start in range(0, len(file_ids), 10):
        chunk = file_ids[chunk_start:chunk_start + 10]
        media = [InputMediaPhoto(media=fid) for fid in chunk]
        try:
            await message.bot.send_media_group(message.chat.id, media=media)
        except TelegramBadRequest:
            logger.exception("Не удалось отправить альбом фото")


def _fmt_qty(q) -> str:
    s = format(q, "f") if hasattr(q, "is_finite") else str(q)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# --- Публичный API: показать превью ----------------------------------------

async def show_preview(
    message: Message,
    state: FSMContext,
    ticket: TicketIn,
    intro: str = "",
) -> None:
    """Сохраняет черновик в FSM и присылает превью с кнопками."""
    await state.set_state(TicketConfirm.waiting)
    await state.update_data(pending_ticket=ticket.model_dump(mode="json"))

    head = intro or "Проверь данные перед сохранением:"
    text = f"{head}\n\n{_format_preview(ticket)}\n\nСохранить?"
    sent = await message.answer(text, reply_markup=_keyboard())
    await state.update_data(preview_message_id=sent.message_id)


async def _disable_old_preview(message: Message, state: FSMContext) -> None:
    """Снимает кнопки со старого превью, чтобы не было путаницы."""
    data = await state.get_data()
    old_id = data.get("preview_message_id")
    if not old_id or not message.bot:
        return
    try:
        await message.bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=old_id,
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass


# --- Кнопка «Сохранить» -----------------------------------------------------

@router.callback_query(TicketCB.filter(F.action == "save"))
async def on_save(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.from_user is None or cb.message is None:
        await cb.answer()
        return

    data = await state.get_data()
    pending = data.get("pending_ticket")
    if not pending:
        await cb.answer("Нечего сохранять", show_alert=True)
        await state.clear()
        return

    try:
        ticket_in = TicketIn.model_validate(pending)
    except ValidationError:
        logger.warning("Поломанный черновик в FSM: %s", pending)
        await cb.answer("Данные черновика повреждены", show_alert=True)
        await state.clear()
        return

    ticket_id = await db.create_ticket(cb.from_user.id, ticket_in)
    saved = await db.get_ticket(cb.from_user.id, ticket_id)
    await state.clear()

    # Убираем кнопки с превью
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    # Сначала фото, потом текстовая карточка
    if saved and saved.photos:
        await send_photos(cb.message, saved.photos)

    text = "✅ Заявка сохранена!"
    if saved is not None:
        text += "\n\n" + format_ticket(saved)
    await cb.message.answer(text)
    await cb.answer("Сохранил")


# --- Кнопка «Отмена» --------------------------------------------------------

@router.callback_query(TicketCB.filter(F.action == "cancel"))
async def on_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cb.message is not None:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await cb.message.answer("❌ Отменил. Заявка не сохранена.")
    await cb.answer("Отмена")


# --- Кнопка «Изменить» ------------------------------------------------------

@router.callback_query(TicketCB.filter(F.action == "edit"))
async def on_edit(cb: CallbackQuery, state: FSMContext) -> None:
    if cb.message is None:
        await cb.answer()
        return
    await state.set_state(TicketConfirm.editing)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await cb.message.answer(
        "✏️ Напиши, что исправить или дополнить.\n"
        "<i>Например: «акт №321», «адрес Сейфуллина 10», "
        "«добавь патчкорд 2шт».</i>"
    )
    await cb.answer()


# --- Текст в режиме подтверждения/правки → мерж -----------------------------

@router.message(
    StateFilter(TicketConfirm.waiting, TicketConfirm.editing),
    F.text & ~F.text.startswith("/"),
)
async def handle_edit_text(message: Message, state: FSMContext) -> None:
    """Слияние правки от пользователя с черновиком в FSM."""
    if message.from_user is None or not message.text:
        return

    data = await state.get_data()
    pending = data.get("pending_ticket")
    if not pending:
        await state.clear()
        await message.answer("Черновик потерян. Опиши заявку заново.")
        return

    merged_dict = await ai.merge_ticket(message.text, pending)
    try:
        merged_ticket = TicketIn.model_validate(merged_dict)
    except ValidationError:
        logger.warning("Слитый черновик не прошёл валидацию: %s", merged_dict)
        await message.answer(
            "Не получилось применить правку — поломались данные. "
            "Попробуй сформулировать иначе или нажми ❌ Отмена."
        )
        return

    # Снимаем кнопки со старого превью и показываем новое
    await _disable_old_preview(message, state)
    await show_preview(
        message, state, merged_ticket,
        intro="✏️ Обновил черновик:",
    )
