"""Форматирование заявок и сводок для вывода в Telegram."""
from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import Iterable

from bot.models.schemas import Ticket


def _e(value) -> str:
    """Экранирует пользовательский текст для HTML-режима Telegram."""
    return escape(str(value), quote=False)


def format_ticket(t: Ticket) -> str:
    """Красиво форматирует одну заявку с эмодзи."""
    lines = [f"📋 Заявка #{t.id}"]
    lines.append(f"📍 Адрес: {_e(t.address)}")
    lines.append(f"🕐 Время: {t.visit_date.strftime('%d.%m.%Y %H:%M')}")

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
