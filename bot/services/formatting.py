"""Форматирование заявок и сводок для вывода в Telegram."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from html import escape
from typing import Iterable

from bot.models.schemas import Ticket
from bot.services.tz import to_local


def _e(value) -> str:
    """Экранирует пользовательский текст для HTML-режима Telegram."""
    return escape(str(value), quote=False)


def format_ticket(t: Ticket) -> str:
    """Красиво форматирует одну заявку с эмодзи."""
    number = t.user_ticket_number or t.id
    lines = [f"📋 Заявка #{number}"]
    if t.crm_ticket_number:
        lines.append(f"🆔 CRM: {_e(t.crm_ticket_number)}")
    lines.append(f"📍 Адрес: {_e(t.address)}")
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
        materials_str = ", ".join(
            f"{_e(m.name)} {_fmt_qty(m.quantity)}{_e(m.unit)}" for m in t.materials
        )
        lines.append(f"📦 Материалы: {materials_str}")
    if t.act_number:
        lines.append(f"📄 Акт: №{_e(t.act_number)}")
    if t.photos:
        lines.append(f"📷 Фото: {len(t.photos)}")
    lines.append(f"🔁 Повторная: {'да' if t.is_repeat_visit else 'нет'}")
    return "\n".join(lines)


def format_tickets_list(tickets: Iterable[Ticket], header: str = "") -> str:
    """Форматирует список заявок."""
    tickets = list(tickets)
    if not tickets:
        return (_e(header) + "\n\nЗаявок не найдено.").strip()

    parts: list[str] = []
    if header:
        parts.append(f"<b>{_e(header)}</b> (всего: {len(tickets)})")
    for t in tickets:
        parts.append(format_ticket(t))
    return "\n\n".join(parts)


_WEEKDAY_RU = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]
_MONTH_RU = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def format_tickets_by_day(
    tickets: Iterable[Ticket],
    header: str = "",
) -> str:
    """
    Группирует заявки по дням недели. Внутри дня — хронологически (по времени).
    Используется для /week — чтобы было видно, что сделано в каждый из дней.
    """
    tickets = list(tickets)
    if not tickets:
        return (_e(header) + "\n\nЗаявок не найдено.").strip()

    # Группировка по локальной дате визита
    by_day: dict[date, list[Ticket]] = {}
    for t in tickets:
        day = to_local(t.visit_date).date()
        by_day.setdefault(day, []).append(t)

    # Хронологически (понедельник → воскресенье)
    days_ordered = sorted(by_day.keys())

    parts: list[str] = []
    if header:
        parts.append(f"<b>{_e(header)}</b> (всего: {len(tickets)})")

    for day in days_ordered:
        day_tickets = sorted(by_day[day], key=lambda x: x.visit_date)
        day_name = _WEEKDAY_RU[day.weekday()]
        date_str = f"{day.day} {_MONTH_RU[day.month]}"
        parts.append(
            f"━━━━━━━━━━━━━━━\n"
            f"📅 <b>{day_name}, {date_str}</b> — {len(day_tickets)} "
            f"{_ticket_word(len(day_tickets))}"
        )
        for t in day_tickets:
            parts.append(format_ticket(t))

    return "\n\n".join(parts)


def _ticket_word(n: int) -> str:
    """Склонение «заявка»."""
    n10 = n % 10
    n100 = n % 100
    if n10 == 1 and n100 != 11:
        return "заявка"
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return "заявки"
    return "заявок"


def format_materials_summary(rows: list[dict], header: str = "Материалы за период") -> str:
    """Форматирует сводку по материалам."""
    if not rows:
        return f"<b>{_e(header)}</b>\n\nМатериалов не списано."
    lines = [f"<b>{_e(header)}</b>"]
    for r in rows:
        lines.append(f"• {_e(r['name'])}: {_fmt_qty(r['total'])} {_e(r['unit'])}")
    return "\n".join(lines)


def _fmt_qty(q) -> str:
    """Убирает лишние нули у Decimal: 10.00 → 10, 1.50 → 1.5."""
    if isinstance(q, Decimal):
        q = q.normalize()
        # Decimal('1E+1') → '10'
        s = format(q, "f")
    else:
        s = str(q)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
