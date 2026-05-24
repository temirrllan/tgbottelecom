"""Подтверждение / редактирование заявки перед сохранением."""
from __future__ import annotations

import logging
from html import escape
from typing import Optional

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

from bot.models.schemas import Ticket, TicketIn
from bot.services import ai, db
from bot.services.formatting import format_ticket
from bot.services.roles import is_dispatcher
from bot.services.tz import to_local

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


class AssignCB(CallbackData, prefix="as"):
    """Выбор монтёра-исполнителя из меню КРОСС."""
    monteur_id: int  # 0 — отмена


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
    if t.crm_ticket_number:
        lines.append(f"🆔 CRM: {_e(t.crm_ticket_number)}")
    lines.append(f"📍 Адрес: {_e(t.address)}")
    if t.visit_date:
        lines.append(f"🕐 Время: {to_local(t.visit_date).strftime('%d.%m.%Y %H:%M')}")
    if t.customer_name:
        lines.append(f"👤 Абонент: {_e(t.customer_name)}")
    if t.customer_phone:
        lines.append(f"📞 Тел: {_e(t.customer_phone)}")
    if t.license_account:
        lines.append(f"💳 Лиц.счёт: {_e(t.license_account)}")
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

    # КРОСС: вместо мгновенной записи показываем выбор монтёра-исполнителя.
    if is_dispatcher(cb.from_user.id):
        await _show_monteur_picker(cb, state)
        return

    # Монтёр: сохраняет на себя
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

    # Алерт повторного визита: бот сам считает прошлые выезды на этот адрес
    if saved is not None:
        prev_visits = await db.count_recent_visits_at_address(
            cb.from_user.id,
            saved.address,
            days=30,
            exclude_ticket_id=saved.id,
        )
        if prev_visits > 0 and not saved.is_repeat_visit:
            await cb.message.answer(
                f"⚠️ <b>Внимание:</b> ты уже был на этом адресе "
                f"{prev_visits} {_visits_word(prev_visits)} за 30 дней. "
                f"Возможно, это повторная заявка — посмотри /find {_e(saved.address[:30])}"
            )

    await cb.answer("Сохранил")


# --- Выбор монтёра для КРОСС ------------------------------------------------

async def _show_monteur_picker(cb: CallbackQuery, state: FSMContext) -> None:
    """Показывает inline-клавиатуру со списком монтёров и их загрузкой."""
    from bot.services.roles import dispatcher_ids
    monteurs = await db.list_users_except(dispatcher_ids())
    if not monteurs:
        await cb.answer(
            "Нет ни одного монтёра в системе. "
            "Попроси их написать боту /start.",
            show_alert=True,
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for m in monteurs:
        open_count = await db.count_open_tickets_for(m["id"])
        load = (
            "свободен" if open_count == 0
            else f"{open_count} откр."
        )
        label = f"👷 {m['full_name']} • {load}"
        btn = InlineKeyboardButton(
            text=label,
            callback_data=AssignCB(monteur_id=m["id"]).pack(),
        )
        current_row.append(btn)
        if len(current_row) == 1:  # одна кнопка в ряд — широкий лейбл
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=AssignCB(monteur_id=0).pack(),
        ),
    ])

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await cb.message.answer(
        "👥 <b>Кому отправить заявку?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(AssignCB.filter())
async def on_assign(
    cb: CallbackQuery,
    callback_data: AssignCB,
    state: FSMContext,
) -> None:
    """Обработка выбора монтёра-исполнителя."""
    if cb.from_user is None or cb.message is None:
        await cb.answer()
        return

    if callback_data.monteur_id == 0:
        await state.clear()
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await cb.message.answer("❌ Назначение отменено.")
        await cb.answer("Отмена")
        return

    data = await state.get_data()
    pending = data.get("pending_ticket")
    if not pending:
        await cb.answer("Черновик потерян", show_alert=True)
        await state.clear()
        return

    try:
        ticket_in = TicketIn.model_validate(pending)
    except ValidationError:
        await cb.answer("Данные повреждены", show_alert=True)
        await state.clear()
        return

    monteur = await db.get_user(callback_data.monteur_id)
    if monteur is None:
        await cb.answer("Монтёр не найден", show_alert=True)
        return

    # Создаём заявку: исполнитель — выбранный монтёр, автор — КРОСС
    ticket_id = await db.create_ticket(
        user_id=monteur["id"],
        data=ticket_in,
        created_by_id=cb.from_user.id,
    )
    saved = await db.get_ticket(monteur["id"], ticket_id)
    dispatcher = await db.get_user(cb.from_user.id)
    await state.clear()

    # Убираем кнопки выбора
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    # Подтверждение КРОСС-у — показываем личный номер монтёра
    display_num = (saved.user_ticket_number if saved else None) or ticket_id
    await cb.message.answer(
        f"✅ Заявка <b>{_e(monteur['full_name'])} #{display_num}</b> "
        f"назначена."
    )
    await cb.answer("Отправил монтёру")

    # Пуш монтёру
    if saved is not None and cb.bot is not None:
        await notify_monteur(cb.bot, monteur["id"], saved, dispatcher)


async def notify_monteur(bot, monteur_id: int, ticket: Ticket, dispatcher: Optional[dict]) -> None:
    """Шлёт монтёру уведомление о новой назначенной заявке."""
    who = (
        f"от <b>{_e(dispatcher['full_name'])}</b> (КРОСС)"
        if dispatcher else "от КРОСС"
    )
    text = f"🆕 <b>Новая заявка {who}</b>\n\n" + format_ticket(ticket)
    try:
        # Сначала фото, если есть
        if ticket.photos:
            await send_photos_to_chat(bot, monteur_id, ticket.photos)
        await bot.send_message(monteur_id, text)
    except Exception as e:
        logger.warning("Не удалось доставить заявку %s монтёру %s: %s",
                       ticket.id, monteur_id, e)


async def send_photos_to_chat(bot, chat_id: int, file_ids: list[str]) -> None:
    """Отправляет фото в произвольный чат (для уведомлений)."""
    if not file_ids:
        return
    if len(file_ids) == 1:
        try:
            await bot.send_photo(chat_id, file_ids[0])
        except TelegramBadRequest:
            logger.exception("Не удалось отправить фото в %s", chat_id)
        return
    for chunk_start in range(0, len(file_ids), 10):
        chunk = file_ids[chunk_start:chunk_start + 10]
        media = [InputMediaPhoto(media=fid) for fid in chunk]
        try:
            await bot.send_media_group(chat_id, media=media)
        except TelegramBadRequest:
            logger.exception("Не удалось отправить альбом в %s", chat_id)


def _visits_word(n: int) -> str:
    """Склонение «раз»."""
    n10 = n % 10
    n100 = n % 100
    if n10 == 1 and n100 != 11:
        return "раз"
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return "раза"
    return "раз"


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


# --- Применение текстовой правки к черновику --------------------------------

async def apply_text_edit(message: Message, state: FSMContext, text: str) -> None:
    """
    Сливает текстовую правку с черновиком и показывает обновлённое превью.
    Используется и из текстового хендлера, и из голосового.
    """
    if message.from_user is None or not text:
        return

    data = await state.get_data()
    pending = data.get("pending_ticket")
    if not pending:
        await state.clear()
        await message.answer("Черновик потерян. Опиши заявку заново.")
        return

    merged_dict = await ai.merge_ticket(text, pending)
    try:
        merged_ticket = TicketIn.model_validate(merged_dict)
    except ValidationError:
        logger.warning("Слитый черновик не прошёл валидацию: %s", merged_dict)
        await message.answer(
            "Не получилось применить правку — поломались данные. "
            "Попробуй сформулировать иначе или нажми ❌ Отмена."
        )
        return

    await _disable_old_preview(message, state)
    await show_preview(
        message, state, merged_ticket,
        intro="✏️ Обновил черновик:",
    )


@router.message(
    StateFilter(TicketConfirm.waiting, TicketConfirm.editing),
    F.text & ~F.text.startswith("/"),
)
async def handle_edit_text(message: Message, state: FSMContext) -> None:
    """Текст в состоянии подтверждения → применяем как правку."""
    if message.text:
        await apply_text_edit(message, state, message.text)
