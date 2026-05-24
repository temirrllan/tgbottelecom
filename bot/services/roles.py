"""
Роли пользователей.

Простая реализация через переменную окружения DISPATCHER_IDS —
список Telegram-id девушек из отдела КРОСС, разделённый запятыми.

Все остальные пользователи автоматически считаются монтёрами.
"""
from __future__ import annotations

import os


def dispatcher_ids() -> set[int]:
    """Читает Telegram-id диспетчеров из env."""
    raw = os.getenv("DISPATCHER_IDS", "").strip()
    if not raw:
        return set()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def is_dispatcher(user_id: int) -> bool:
    """True если этот пользователь — КРОСС."""
    return user_id in dispatcher_ids()


def is_monteur(user_id: int) -> bool:
    """True если пользователь — обычный монтёр (не КРОСС)."""
    return user_id not in dispatcher_ids()


def role_label(user_id: int) -> str:
    """Человекочитаемая роль для вывода."""
    return "КРОСС" if is_dispatcher(user_id) else "монтёр"
