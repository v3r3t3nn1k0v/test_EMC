"""Расстояния между точками. ТЗ: «по прямой» — используем haversine
(на масштабе площадки ~км эквивалентно плоскости, но корректнее)."""
from __future__ import annotations

import math

from .config import Point

EARTH_RADIUS_M = 6_371_000.0


def distance_m(a: Point, b: Point) -> float:

    """Расстояние по прямой между двумя точками"""

    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)

    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def eta_seconds(distance: float, speed_mps: float) -> float:
    """Время в пути при постоянной скорости."""
    if speed_mps <= 0:
        raise ValueError("speed must be > 0")
    return distance / speed_mps