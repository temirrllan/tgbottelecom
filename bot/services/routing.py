"""
Простое планирование маршрута монтёра.

Для 5–10 точек nearest-neighbor даёт почти оптимальный порядок объезда.
Для больших списков нужен полноценный TSP — пока не требуется.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from bot.services.geocode import Coords


@dataclass
class RoutePoint:
    """Точка маршрута с привязкой к заявке."""
    ticket_id: int
    address: str
    coords: Coords
    distance_from_prev_km: float = 0.0


def haversine_km(a: Coords, b: Coords) -> float:
    """Расстояние «по прямой» между двумя координатами в километрах."""
    R = 6371.0
    lat1, lon1 = math.radians(a.lat), math.radians(a.lng)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lng)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(h))


def plan_route(
    start: Optional[Coords],
    points: list[RoutePoint],
) -> list[RoutePoint]:
    """
    Сортирует точки методом ближайшего соседа.
    Если start не передан — стартуем с первой точки.
    Заполняет distance_from_prev_km у каждой точки.
    """
    if not points:
        return []

    remaining = list(points)
    ordered: list[RoutePoint] = []
    current = start

    while remaining:
        if current is None:
            next_point = remaining.pop(0)
            next_point.distance_from_prev_km = 0.0
        else:
            next_point = min(remaining, key=lambda p: haversine_km(current, p.coords))
            remaining.remove(next_point)
            next_point.distance_from_prev_km = haversine_km(current, next_point.coords)
        ordered.append(next_point)
        current = next_point.coords

    return ordered


def total_distance_km(ordered: list[RoutePoint]) -> float:
    """Сумма дистанций по упорядоченному маршруту."""
    return sum(p.distance_from_prev_km for p in ordered)
