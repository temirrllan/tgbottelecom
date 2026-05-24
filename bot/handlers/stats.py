"""Команды и форматирование статистики."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from typing import Optional

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.services import db

logger = logging.getLogger(__name__)
router = Router(name="stats")


# --- Период / интервалы -----------------------------------------------------

def _period_bounds(period: str) -> tuple[datetime, datetime, datetime, str]:
    """
    Возвращает (since, prev_since, prev_until, label) для периода.
    prev_* — предыдущий такой же отрезок для сравнения.
    """
    now = datetime.now().astimezone()
    if period == "today":
        since = datetime.combine(now.date(), datetime.min.time()).astimezone()
        prev_until = since
        prev_since = since - timedelta(days=1)
        label = "сегодня"
    elif period == "month":
        since = now - timedelta(days=30)
        prev_since = now - timedelta(days=60)
        prev_until = since
        label = "месяц"
    else:  # week — дефолт
        since = now - timedelta(days=7)
        prev_since = now - timedelta(days=14)
        prev_until = since
        label = "неделю"
    return since, prev_since, prev_until, label


# --- Команда /stats ---------------------------------------------------------

@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject) -> None:
    """Сводная статистика: /stats [today|week|month]."""
    if message.from_user is None:
        return
    arg = (command.args or "").strip().lower()
    period = arg if arg in ("today", "week", "month") else "week"
    text = await build_stats_text(message.from_user.id, period)
    await message.answer(text)


# --- Сборка текста статистики ----------------------------------------------

async def build_stats_text(user_id: int, period: str) -> str:
    """Собирает текст статистики за период."""
    since, prev_since, prev_until, label = _period_bounds(period)

    count = await db.count_tickets_since(user_id, since)
    prev_count = await db.count_tickets_between(user_id, prev_since, prev_until)
    repeats = await db.count_repeats_since(user_id, since)
    with_photos = await db.count_with_photos_since(user_id, since)
    with_act = await db.count_with_act_since(user_id, since)
    top_mats = await db.top_materials_since(user_id, since, limit=5)
    top_addrs = await db.top_addresses_since(user_id, since, limit=5)
    by_hour = await db.hour_distribution_since(user_id, since)

    lines: list[str] = [f"📊 <b>Статистика за {label}</b>", ""]

    # Основные счётчики с трендом
    trend = _trend_arrow(count, prev_count)
    lines.append(f"📋 Заявок: <b>{count}</b> {trend}")
    if count > 0:
        repeat_pct = round(repeats * 100 / count) if count else 0
        lines.append(f"🔁 Повторных: {repeats} ({repeat_pct}%)")
        lines.append(f"📷 С фото: {with_photos}")
        lines.append(f"📄 С актом: {with_act}")

    # Топ материалов с ASCII-баром
    if top_mats:
        lines.append("")
        lines.append("🏆 <b>Топ материалов:</b>")
        max_total = max(_to_float(m["total"]) for m in top_mats) or 1
        for m in top_mats:
            qty = _to_float(m["total"])
            bar = _bar(qty, max_total, width=10)
            name = escape(m["name"], quote=False).ljust(10)[:10]
            unit = escape(m["unit"], quote=False)
            lines.append(
                f"<code>{name} {bar} {_fmt_qty(qty)}{unit}</code>"
            )

    # Распределение по часам
    if by_hour:
        lines.append("")
        lines.append("⏰ <b>Часы работы:</b>")
        max_cnt = max(h["count"] for h in by_hour) or 1
        # Показываем только активные часы
        for h in by_hour:
            bar = _bar(h["count"], max_cnt, width=10)
            lines.append(
                f"<code>{h['hour']:02d}:00 {bar} {h['count']}</code>"
            )

    # Топ адресов
    if top_addrs:
        lines.append("")
        lines.append("📍 <b>Топ адресов:</b>")
        for a in top_addrs:
            addr = escape(a["address"], quote=False)
            # Адреса бывают длинные — обрежем для читаемости
            if len(addr) > 50:
                addr = addr[:47] + "..."
            lines.append(f"• {addr} ({a['count']})")

    if count == 0:
        lines.append("")
        lines.append("За этот период заявок ещё не было.")

    return "\n".join(lines)


# --- Вспомогательные функции ------------------------------------------------

def _bar(value: float, max_value: float, width: int = 10) -> str:
    """ASCII-индикатор прогресса: ██████░░░░"""
    if max_value <= 0:
        return "░" * width
    filled = int(round(value / max_value * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _trend_arrow(current: int, previous: int) -> str:
    """Стрелочка тренда с процентом."""
    if previous == 0:
        if current == 0:
            return ""
        return "🆕"
    diff_pct = round((current - previous) * 100 / previous)
    if diff_pct > 0:
        return f"📈 +{diff_pct}% к прошлому периоду"
    if diff_pct < 0:
        return f"📉 {diff_pct}% к прошлому периоду"
    return "➖ как в прошлый раз"


def _to_float(value) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0)


def _fmt_qty(value: float) -> str:
    """1.00 → 1, 1.50 → 1.5"""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")
