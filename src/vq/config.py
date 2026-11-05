"""Конфигурация площадки: станции, точка разгрузки, закрепления, константы.

Константы расчёта — поля Config (ТЗ: «Константы в конфигурации сервиса»).
Геометрия ниже — пример площадки для локального запуска и генератора;
тесты собирают свою Config с удобной геометрией.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float


# --- Пример площадки: 4 станции, точка разгрузки, 40 машин ---

STATIONS: dict[str, Point] = {
    "S1": Point(49.1288, 142.6601),
    "S2": Point(49.1300, 142.6650),
    "S3": Point(49.1250, 142.6550),
    "S4": Point(49.1320, 142.6620),
}

UNLOAD_POINT: Point = Point(49.1295, 142.6575)


def _default_assignments() -> dict[str, str]:
    """10 машин на каждую из 4 станций."""
    stations = list(STATIONS.keys())
    return {f"T-{i:02d}": stations[i % len(stations)] for i in range(40)}


@dataclass(frozen=True)
class Config:
    """Всё, что нужно расчёту: площадка и константы из ТЗ."""

    stations: dict[str, Point] = field(default_factory=lambda: dict(STATIONS))
    unload_point: Point = UNLOAD_POINT
    assignments: dict[str, str] = field(default_factory=_default_assignments)

    # Время занятия станции: 200 с обслуживания + 30 с манёвра
    service_time_s: int = 230
    # Расчётная скорость движения — 36 км/ч
    drive_speed_mps: float = 10.0
    # Радиус станции и точки разгрузки
    station_radius_m: float = 50.0
    # Порог «машина стоит»
    stop_speed_kmh: float = 1.0
    # Радиус принятия решения
    decision_radius_m: float = 1500.0
    # Горизонт очереди
    queue_horizon_s: int = 30 * 60
    # Позиция считается свежей не старше
    fresh_telemetry_s: int = 30
    # Минимальный выигрыш для рекомендации
    min_gain_s: int = 60
    # Допустимое опоздание сообщений (нужно генератору)
    late_tolerance_s: int = 60

    def own_station(self, unit_uuid: str) -> str | None:
        return self.assignments.get(unit_uuid)
