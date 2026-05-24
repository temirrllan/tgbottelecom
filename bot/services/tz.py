"""
Единая работа с часовым поясом приложения.

Все «локальные» отсчёты времени и преобразования идут через этот модуль,
чтобы не зависеть от системной TZ контейнера (на Railway по умолчанию UTC).
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

# По умолчанию — Алматы. Переопределяется переменной окружения TZ.
_TZ_NAME = os.getenv("TZ") or "Asia/Almaty"
try:
    LOCAL_TZ = ZoneInfo(_TZ_NAME)
except Exception:
    LOCAL_TZ = ZoneInfo("Asia/Almaty")


def local_now() -> datetime:
    """Текущий момент в локальной TZ."""
    return datetime.now(LOCAL_TZ)


def to_local(dt: datetime) -> datetime:
    """Переводит aware datetime в локальную TZ для отображения."""
    if dt.tzinfo is None:
        # Наивные значения трактуем как локальные
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def normalize_for_db(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Готовит datetime для записи в TIMESTAMPTZ:
    - None → None
    - наивный → считается локальным временем монтёра
    - aware → как есть (asyncpg сам приведёт к UTC при записи)
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt
