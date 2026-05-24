"""
Команда /route — планирует маршрут по открытым заявкам монтёра.

Использует геокодинг через Nominatim (OSM) и сортировку nearest-neighbor.
По умолчанию маршрут начинается с первой заявки. Если монтёр поделится
текущей геопозицией — пересоберём маршрут с учётом старта от него.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.services import db
from bot.services.geocode import Coords, geocode
from bot.services.routing import RoutePoint, plan_route, total_distance_km

logger = logging.getLogger(__name__)
router = Router(name="route")


# --- Команда /route ---------------------------------------------------------

@router.message(Command("route"))
async def cmd_route(message: Message, state: FSMContext) -> None:
    """Строит маршрут по всем открытым заявкам."""
    if message.from_user is None:
        return
    await _send_route(message, start=None)


# --- Обработка геопозиции --------------------------------------------------

@router.message(F.location)
async def handle_location(message: Message, state: FSMContext) -> None:
    """Принимает текущее положение монтёра и пересчитывает маршрут от него."""
    if message.from_user is None or message.location is None:
        return
    start = Coords(
        lat=message.location.latitude,
        lng=message.location.longitude,
    )
    await message.answer(
        "📍 Получил твою точку. Пересобираю маршрут от тебя…",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _send_route(message, start=start)


# --- Основная логика --------------------------------------------------------

async def _send_route(message: Message, start: Coords | None) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id

    open_tickets = await db.list_open_tickets(user_id, limit=30)
    if not open_tickets:
        await message.answer(
            "Открытых заявок нет — выезжать некуда 🎉",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    notice = await message.answer(
        f"🗺 Получил {len(open_tickets)} открытых заявок. Гео-кодирую адреса…"
    )

    points: list[RoutePoint] = []
    failed: list[str] = []
    for t in open_tickets:
        coords = await geocode(t.address)
        if coords is None:
            failed.append(t.address)
            continue
        points.append(RoutePoint(
            ticket_id=t.id,
            address=t.address,
            coords=coords,
        ))

    # Удаляем уведомление о прогрессе, чтобы не засорять чат
    try:
        if message.bot:
            await message.bot.delete_message(message.chat.id, notice.message_id)
    except Exception:
        pass

    if not points:
        await message.answer(
            "❌ Не смог найти координаты ни одного адреса. "
            "Возможно, адреса записаны нестандартно — попробуй позже."
        )
        return

    ordered = plan_route(start, points)
    text = _format_route(ordered, start, failed)

    # Просим поделиться местоположением — для пересчёта с учётом старта
    if start is None:
        kb = ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(text="📍 Я тут — пересобрать", request_location=True),
            ]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await message.answer(
            text,
            reply_markup=ReplyKeyboardRemove(),
            disable_web_page_preview=True,
        )


def _format_route(
    ordered: list[RoutePoint],
    start: Coords | None,
    failed: list[str],
) -> str:
    """Формирует текст маршрута со ссылками на 2GIS."""
    header = "🗺 <b>Маршрут на день</b>"
    if start is not None:
        header += " (от твоей точки)"
    lines: list[str] = [header, ""]

    for i, p in enumerate(ordered, 1):
        # 2GIS принимает порядок lng,lat
        link = f"https://2gis.kz/geo/{p.coords.lng:.6f},{p.coords.lat:.6f}"
        address = escape(p.address, quote=False)
        dist_str = (
            f" <i>(+{p.distance_from_prev_km:.1f} км)</i>"
            if (i > 1 or start is not None) and p.distance_from_prev_km > 0
            else ""
        )
        lines.append(
            f"<b>{i}.</b> <a href=\"{link}\">{address}</a>"
            f" • заявка #{p.ticket_id}{dist_str}"
        )

    total = total_distance_km(ordered)
    if total > 0:
        lines.append("")
        lines.append(f"📏 Общий пробег: <b>{total:.1f} км</b>")

    if failed:
        lines.append("")
        lines.append(f"⚠️ Не смог найти координаты ({len(failed)}):")
        for addr in failed[:5]:
            lines.append(f"• {escape(addr, quote=False)}")
        if len(failed) > 5:
            lines.append(f"• …и ещё {len(failed) - 5}")

    if start is None:
        lines.append("")
        lines.append(
            "<i>Хочешь маршрут с учётом твоей точки? Нажми кнопку «📍 Я тут».</i>"
        )

    return "\n".join(lines)
