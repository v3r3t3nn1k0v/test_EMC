"""Доменные модели: телеметрия, состояние машины, сигнал FSM."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CarState(str, Enum):
    AT_STATION = "at_station"    # на станции: обслуживается или ждёт в радиусе
    TO_UNLOAD = "to_unload"      # к точке разгрузки
    TO_STATION = "to_station"    # к станции


@dataclass(frozen=True)
class Telemetry:
    unit_uuid: str
    ts: int          # секунды epoch по часам машины
    lat: float
    lon: float
    speed_kmh: float


@dataclass
class CarRuntime:
    """Всё, что сервис знает о машине по телеметрии."""

    unit_uuid: str
    own_station: str
    last_ts: int = -1
    last_pos: tuple[float, float] | None = None
    last_speed_kmh: float = 0.0
    state: CarState = CarState.TO_STATION

    # приоритет - рекомендованная станция, если такая есть, иначе своя 

    recommended_station: str | None = None

    # Момент входа в радиус 50 м целевой станции: приезд ждущей машины
    
    entered_radius_at: int | None = None

    # Была ли ближе 1500 м к своей станции в этом заезде — рекомендация
    # считается один раз за заезд.
    was_within_decision_radius: bool = False

    # Считалась ли рекомендация в этом заезде (заполняет решатель, не FSM).
    decision_made: bool = False

    # Момент занятия станции (заполняет движок очереди, не FSM).
    station_occupied_at: int | None = None

    @property
    def target_station(self) -> str:
        """Станция, к которой машина едет в текущем заезде.
        Правило: если есть рекомендованная станция, то к ней, иначе к своей."""
        return self.recommended_station or self.own_station


class FsmEventKind(str, Enum):
    IGNORED = "ignored"                          # дубль или старое ts — учтено ранее
    UPDATED = "updated"                          # позиция учтена, сигналов нет
    AT_STATION_ENTERED = "at_station_entered"    # первое сообщение «на станции»
    LEFT_STATION_RADIUS = "left_station_radius"  # вышла из радиуса 50 м станции
    NEW_TRIP = "new_trip"                        # новый заезд: выехала к своей станции
    CROSSED_DECISION_RADIUS = "crossed_decision_radius"  # впервые ближе 1500 м к своей


@dataclass
class StationState:
    """Занятость станции: кто и когда занял, когда освободится (расчёт).

    Освобождение — занятие + 230 с, либо раньше, если занявшая машина
    вышла из радиуса 50 м (ТЗ, п. 2 занятия/освобождения).
    """

    station_uuid: str
    occupied_by: str | None = None
    occupied_at: int | None = None   # момент занятия
    released_at: int | None = None   # расчётный момент освобождения

    def is_free(self, at_ts: int) -> bool:
        return self.released_at is None or self.released_at <= at_ts


@dataclass(frozen=True)
class QueueEntry:
    """Строка очереди к станции — выход queue.v1.

    Время — epoch-секунды; сериализация в ISO 8601 делается на выходе.
    eta=None у машины, занявшей станцию (обслуживается).
    """

    unit_uuid: str
    eta: int | None
    service_start: int
    free_at: int
    wait_seconds: int


@dataclass(frozen=True)
class StationQueue:
    """Очередь к станции, рассчитанная на момент `at` («сейчас»)."""

    station_uuid: str
    at: int
    entries: tuple[QueueEntry, ...]


@dataclass(frozen=True)
class Decision:
    """Решение по машине — выход decision.v1.

    result = "recommended": заполнены from_station, to_station, gain_seconds.
    result = "rejected": заполнен reason — no_gain | stale_telemetry.
    """

    unit_uuid: str
    at: int
    result: str
    from_station: str | None = None
    to_station: str | None = None
    gain_seconds: int | None = None
    reason: str | None = None
