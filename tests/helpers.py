"""Общие помощники тестов: площадка-линейка и телеметрия.

Все точки на одном меридиане: расстояние по haversine между ними равно
разности широт, поэтому дистанции задаются в метрах точно, без погрешности.
Схема (метры к северу от S1):

    разгрузка(-2000) ... S1(0) ... S2(+2000) ... S3(+4000) ... S4(+6000)

Разгрузка вынесена за радиус решения 1500 м от S1, чтобы новый заезд
не пересекал его в тот же миг.
"""
from __future__ import annotations

import math

from vq.car_fsm import CarFsm
from vq.config import Config, Point
from vq.decision import DecisionEngine
from vq.geo import EARTH_RADIUS_M
from vq.models import CarRuntime, StationState, Telemetry
from vq.queue_engine import QueueEngine

BASE_LAT = 49.0
LON = 142.0
M_PER_DEG = math.pi * EARTH_RADIUS_M / 180.0  # метров на градус широты


def lat_at(meters_from_s1: float) -> float:
    """Широта точки в `meters_from_s1` метрах к северу от S1."""
    return BASE_LAT + meters_from_s1 / M_PER_DEG


def point_at(meters_from_s1: float) -> Point:
    return Point(lat_at(meters_from_s1), LON)


def make_config() -> Config:
    """Тестовая площадка: S1 в нуле, станции через 2000 м, разгрузка южнее."""
    return Config(
        stations={
            "S1": point_at(0),
            "S2": point_at(2000),
            "S3": point_at(4000),
            "S4": point_at(6000),
        },
        unload_point=point_at(-2000),
        assignments={"T1": "S1", "T2": "S1", "T3": "S1", "T4": "S1", "U1": "S2"},
    )


def tel(unit_uuid: str, ts: int, meters_from_s1: float,
        speed_kmh: float = 36.0) -> Telemetry:
    """Сообщение телеметрии из точки в `meters_from_s1` метрах от S1."""
    return Telemetry(
        unit_uuid=unit_uuid, ts=ts,
        lat=lat_at(meters_from_s1), lon=LON, speed_kmh=speed_kmh,
    )


def kinds(events) -> list:
    """Виды событий одной строкой — для читаемых assert'ов."""
    return [e.kind for e in events]


class Yard:
    """Мини-процессор для тестов: FSM + занятость станций, без Kafka.

    Тот же конвейер, что и в сервисе: каждое сообщение — сначала FSM
    машины, затем сигналы FSM обновляют занятость станций.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.fsm = CarFsm(cfg)
        self.engine = QueueEngine(cfg)
        self.decider = DecisionEngine(cfg)
        self.cars: dict[str, CarRuntime] = {}
        self.stations: dict[str, StationState] = {
            s: StationState(s) for s in cfg.stations
        }

    def car(self, unit_uuid: str) -> CarRuntime:
        if unit_uuid not in self.cars:
            own = self.cfg.own_station(unit_uuid)
            self.cars[unit_uuid] = CarRuntime(unit_uuid=unit_uuid, own_station=own)
        return self.cars[unit_uuid]

    def apply(self, msg: Telemetry, now_ts: int | None = None):
        """Прогоняет сообщение через FSM и движок занятости."""
        car = self.car(msg.unit_uuid)
        events = self.fsm.apply(car, msg, now_ts if now_ts is not None else msg.ts)
        for ev in events:
            if ev.station is not None:
                self.engine.apply_event(ev, car, self.stations[ev.station])
        return events

    def queue(self, station_uuid: str, now: int):
        return self.engine.build_queue(
            station_uuid, self.stations[station_uuid], self.cars, now,
        )
