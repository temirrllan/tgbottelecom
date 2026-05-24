"""
Геокодинг адресов через Nominatim (OpenStreetMap).

Бесплатно, без API-ключей, лимит 1 запрос/сек.
Результаты кэшируются в БД — адреса не двигаются, поэтому кэш бессрочный.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from bot.services import db

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "KaztelecomMonterBot/1.0 (+https://github.com/temirrllan/tgbottelecom)"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Соблюдаем лимит Nominatim: не больше 1 запроса в секунду
_request_lock = asyncio.Lock()
_last_request_ts = 0.0


@dataclass
class Coords:
    lat: float
    lng: float
    display: str = ""


async def geocode(address: str) -> Optional[Coords]:
    """
    Возвращает координаты адреса.
    Сначала проверяет кэш в БД, потом ходит в Nominatim.
    None — если адрес не нашёлся.
    """
    if not address or not address.strip():
        return None
    addr = address.strip()

    cached = await db.get_cached_geocode(addr)
    if cached:
        return Coords(lat=cached["lat"], lng=cached["lng"], display=cached.get("display_name", ""))

    coords = await _fetch_nominatim(addr)
    if coords is None:
        return None

    await db.save_cached_geocode(addr, coords.lat, coords.lng, coords.display)
    return coords


async def _fetch_nominatim(address: str) -> Optional[Coords]:
    """Запрос к Nominatim с соблюдением rate-limit."""
    global _last_request_ts

    async with _request_lock:
        # Минимум 1 секунда между запросами
        loop = asyncio.get_running_loop()
        now = loop.time()
        gap = now - _last_request_ts
        if gap < 1.0:
            await asyncio.sleep(1.0 - gap)

        params = {
            "q": address,
            "format": "json",
            "limit": 1,
            "accept-language": "ru",
        }
        headers = {"User-Agent": USER_AGENT}

        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.get(NOMINATIM_URL, params=params, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning("Nominatim вернул %s для адреса %s", resp.status, address)
                        return None
                    data = await resp.json()
        except Exception:
            logger.exception("Ошибка запроса к Nominatim для %s", address)
            return None
        finally:
            _last_request_ts = loop.time()

    if not data:
        return None
    item = data[0]
    try:
        return Coords(
            lat=float(item["lat"]),
            lng=float(item["lon"]),
            display=item.get("display_name", ""),
        )
    except (KeyError, ValueError):
        return None
