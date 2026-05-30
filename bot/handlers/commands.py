"""Хендлеры команд бота."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.services import db
from bot.services.formatting import (
    format_materials_summary,
    format_tickets_by_day,
    format_tickets_list,
)
from bot.services.roles import is_dispatcher

logger = logging.getLogger(__name__)
router = Router(name="commands")


WELCOME_MONTEUR = (
    "👋 Привет! Я — ИИ-ассистент монтёра Казактелекома.\n\n"
    "Просто пиши мне в свободной форме, что сделал — я сам разберу и сохраню:\n"
    "<i>«Был на Абая 45 кв 12, заменил кабель 10м, акт №321»</i>\n\n"
    "🎤 Можно говорить голосом — я распознаю.\n"
    "📷 Можно слать фото — приложу к заявке.\n\n"
    "А ещё можно спрашивать про свои заявки:\n"
    "<i>«Что я делал сегодня?», «Сколько кабеля потратил за месяц?»</i>\n\n"
    "Команды:\n"
    "/today — заявки за сегодня\n"
    "/week — заявки за неделю\n"
    "/month — заявки за месяц + материалы\n"
    "/stats — статистика и графики\n"
    "/route — маршрут по открытым заявкам\n"
    "/find [адрес] — поиск\n"
    "/edit — редактировать последнюю заявку\n"
    "/photos [id] — фото заявки\n"
    "/help — справка\n"
    "/cancel — отмена"
)

WELCOME_DISPATCHER = (
    "👋 Привет! Ты КРОСС в системе бота.\n\n"
    "Тут ты создаёшь заявки и распределяешь их монтёрам.\n\n"
    "📝 <b>Как создать заявку:</b>\n"
    "1. Опиши её текстом или голосом:\n"
    "<i>«Абилов, 7029583619, ул. Беркимбаева 102/13, нет интернета»</i>\n"
    "2. Когда увидишь превью — при желании пришли <b>фото</b> "
    "(скриншот заявки, фасад дома, что угодно). Прикрепится к заявке.\n"
    "3. Нажми ✅ Сохранить → выбери монтёра.\n"
    "4. Монтёру улетит уведомление с фото и карточкой.\n\n"
    "Команды:\n"
    "/new — подсказка по созданию заявки\n"
    "/team — список монтёров и загрузка\n"
    "/inbox — мои назначенные заявки (с кнопками 🔄 Передать / 🗑 Удалить)\n"
    "/msg — написать монтёру лично (текст, голос, фото)\n"
    "/find [адрес] — поиск по адресу\n"
    "/help — справка\n"
    "/cancel — отмена"
)


HELP_TEXT = (
    "<b>Что я умею:</b>\n\n"
    "📝 <b>Сохранение заявок</b>\n"
    "Опиши заявку обычным языком — я извлеку адрес, материалы, акт и сохраню.\n"
    "Перед сохранением покажу превью с кнопками ✅ / ✏️ / ❌.\n\n"
    "📷 <b>Фото</b>\n"
    "Пришли скриншот CRM-заявки — я сам прочитаю адрес, ФИО, телефон, проблему.\n"
    "Или приложи фото к черновику — оно сохранится как доказательство.\n"
    "Просмотр фото заявки: /photos [id].\n\n"
    "🎤 <b>Голосовые</b>\n"
    "Скажи всё то же самое голосом — я транскрибирую и обработаю.\n"
    "Удобно, когда руки заняты.\n\n"
    "📊 <b>Статистика</b>\n"
    "/stats — сводка за неделю с графиками.\n"
    "/stats today / /stats month — за день / месяц.\n"
    "В 19:00 (пн–сб) бот сам пришлёт вечернюю сводку.\n\n"
    "🗺 <b>Маршрут на день</b>\n"
    "/route — собирает все открытые заявки (без work_done), "
    "выстраивает оптимальный порядок объезда и даёт ссылки на 2GIS.\n"
    "После /route можно нажать «📍 Я тут» — пересоберёт от твоей точки.\n\n"
    "⚠️ <b>Алерт повторного визита</b>\n"
    "При сохранении заявки бот предупредит, если ты уже был "
    "на этом адресе за последние 30 дней.\n\n"
    "🔍 <b>Поиск и отчёты</b>\n"
    "Спрашивай: «что было вчера», «сколько патчкордов за неделю», "
    "«заявки по Абая».\n\n"
    "✏️ <b>Редактирование</b>\n"
    "Только заявки за сегодня — напиши «исправь адрес на …» или /edit.\n\n"
    "⏰ <b>Напоминания</b>\n"
    "В рабочее время напомню, если долго не писал."
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """/start — регистрация пользователя."""
    await state.clear()
    user = message.from_user
    if user is None:
        return
    await db.upsert_user(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )
    welcome = WELCOME_DISPATCHER if is_dispatcher(user.id) else WELCOME_MONTEUR
    await message.answer(welcome)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if message.from_user and is_dispatcher(message.from_user.id):
        await message.answer(WELCOME_DISPATCHER)
        return
    await message.answer(HELP_TEXT)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    """Заявки за сегодня."""
    if message.from_user is None:
        return
    tickets = await db.list_tickets(message.from_user.id, period="today")
    await message.answer(
        format_tickets_list(tickets, header="📅 Заявки за сегодня"),
    )


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    """Заявки за неделю, сгруппированные по дням."""
    if message.from_user is None:
        return
    tickets = await db.list_tickets(message.from_user.id, period="week")
    await message.answer(
        format_tickets_by_day(tickets, header="📅 Заявки за неделю"),
    )


@router.message(Command("month"))
async def cmd_month(message: Message) -> None:
    """Заявки за месяц + сводка материалов."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    tickets = await db.list_tickets(user_id, period="month")
    summary = await db.materials_summary(user_id, period="month")

    text = format_tickets_list(tickets, header="📅 Заявки за месяц")
    text += "\n\n" + format_materials_summary(summary, header="📦 Итого материалов за месяц")
    await message.answer(text)


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject) -> None:
    """Поиск по адресу: /find Абая 5."""
    if message.from_user is None:
        return
    address = (command.args or "").strip()
    if not address:
        await message.answer("Укажи адрес после команды, например: <code>/find Абая 5</code>")
        return
    tickets = await db.list_tickets(
        message.from_user.id, search_address=address, limit=20,
    )
    await message.answer(
        format_tickets_list(tickets, header=f"🔍 Поиск: {address}"),
    )


@router.message(Command("edit"))
async def cmd_edit(message: Message) -> None:
    """Подсказка по редактированию — фактическое редактирование делает ИИ."""
    if message.from_user is None:
        return
    last = await db.get_last_ticket_today(message.from_user.id)
    if last is None:
        await message.answer(
            "За сегодня заявок ещё нет. Редактировать можно только сегодняшние."
        )
        return
    from bot.services.formatting import format_ticket
    await message.answer(
        "✏️ Последняя заявка за сегодня:\n\n"
        + format_ticket(last)
        + "\n\nНапиши, что в ней исправить — например: "
        "<i>«поменяй адрес на ул. Сейфуллина 12»</i> или "
        "<i>«добавь акт №555»</i>."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Сбрасывает текущее состояние."""
    await state.clear()
    await message.answer("Окей, отменил. Что дальше?")


@router.message(Command("photos"))
async def cmd_photos(message: Message, command: CommandObject) -> None:
    """Показывает фото конкретной заявки: /photos 123."""
    if message.from_user is None:
        return
    arg = (command.args or "").strip()
    if not arg or not arg.isdigit():
        await message.answer(
            "Укажи номер заявки: <code>/photos 5</code>"
        )
        return
    ticket = await db.get_ticket_by_number(message.from_user.id, int(arg))
    if ticket is None:
        await message.answer("Не нашёл такой заявки.")
        return
    number = ticket.user_ticket_number or ticket.id
    if not ticket.photos:
        await message.answer(f"К заявке #{number} фото не прикреплено.")
        return

    from bot.handlers.confirm import send_photos
    await message.answer(f"📷 Фото заявки #{number} ({len(ticket.photos)} шт):")
    await send_photos(message, ticket.photos)
