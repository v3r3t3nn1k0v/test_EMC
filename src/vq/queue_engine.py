"""Движок очереди: занятость станций и расчёт очередей на 30 минут вперёд.

Две ответственности:
- apply_event — bookkeeping занятости по сигналам FSM; решения принимаются
  во времени события (ev.ts), а не во времени обработки;
- build_queue — чистый расчёт очереди станции на момент «сейчас», без
  побочных эффектов: одна и та же картина машин даёт одну и ту же очередь.

Правила (ТЗ):
- машина занимает станцию в момент первого сообщения «на станции», когда
  станция свободна; освобождение — через 230 с после занятия либо в момент
  выхода занявшей машины из радиуса 50 м, что наступит раньше;
- машина в радиусе занятой станции ждёт: её приезд — момент входа в радиус;
- занявшая машина стоит в очереди первой, её free_at = занятие + 230 с;
- остальные выстраиваются по времени приезда, каждая занимает станцию на
  230 с с max(приезд, освобождение предыдущей); ожидание = начало − приезд;
- приезд позже горизонта 30 минут — машина в очередь не попадает;
- последняя позиция старше 30 с — машина исключается, кроме занявшей:
  станция остаётся занятой до расчётного освобождения.

Уточнение к неоднозначности ТЗ (отражено в README): машина, отслужившая
и оставшаяся стоять в радиусе (освобождение по расписанию уже наступило),
станцию повторно не занимает — «первое сообщение» уже было, — и в очередь
не попадает: признак — заполненный car.station_occupied_at.
"""
from __future__ import annotations

from .car_fsm import FsmEvent
from .config import Config, Point
from .geo import distance_m
from .models import (
    CarRuntime,
    CarState,
    FsmEventKind,
    QueueEntry,
    StationQueue,
    StationState,
)


def _sec(value: float) -> int:
    """Секунды epoch целыми — выход ТЗ в целых секундах."""
    return int(round(value))


class QueueEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # --- занятость станций ---

    def apply_event(self, ev: FsmEvent, car: CarRuntime, station: StationState) -> None:
        """Обновляет занятость станции по одному сигналу FSM."""
        if ev.station is None or ev.kind is FsmEventKind.IGNORED:
            return

        if ev.kind is FsmEventKind.LEFT_STATION_RADIUS:
            # занявшая машина вышла из радиуса — освободить станцию раньше срока
            if station.occupied_by == ev.unit_uuid and station.released_at is not None:
                station.released_at = min(station.released_at, ev.ts)
            return

        # Любое учтённое сообщение машины «на станции» может стать занятием —
        # станция могла освободиться по расписанию уже после того, как машина
        # встала и начала ждать (ТЗ: «момент первого сообщения, где она в
        # состоянии „на станции“, а станция свободна»).
        if (
            car.state is CarState.AT_STATION
            and station.is_free(ev.ts)
            and station.occupied_by != ev.unit_uuid
        ):
            station.occupied_by = ev.unit_uuid
            station.occupied_at = ev.ts
            station.released_at = ev.ts + self.cfg.service_time_s
            car.station_occupied_at = ev.ts

    # --- расчёт очереди ---

    def build_queue(
        self,
        station_uuid: str,
        station: StationState,
        cars: dict[str, CarRuntime],
        now: int,
    ) -> StationQueue:
        
        """Очередь к станции на текущий момент времени. функция от состояния системы."""
        cfg = self.cfg
        entries: list[QueueEntry] = []


        free_boundary = station.released_at # момент, когда станция освободится 

        if free_boundary is not None and free_boundary > now:
            occupier = cars.get(station.occupied_by) # машина, которая заняла станцию
            if occupier is not None and occupier.state is CarState.AT_STATION:
                free_at = station.occupied_at + cfg.service_time_s # время освобождения станции 
                entries.append(QueueEntry(
                    occupier.unit_uuid, None, station.occupied_at, free_at, 0,
                ))

       
        st_point = cfg.stations[station_uuid] # Точка станции 
        unload = cfg.unload_point # точка разгрузки

        candidates: list[tuple[float, str, CarRuntime]] = []
        for car in cars.values():
            if car.last_pos is None:
                continue
            arrival = self._arrival_ts(car, station_uuid, st_point, unload, now) # время приезда машины к станции. Есть обработчик, который вернет None для не подходящих машин (не про эту станцию, не в очереди и т.д.)
            if arrival is None:
                continue
            if now - car.last_ts > cfg.fresh_telemetry_s:
                continue  # позиция машины неактуальна (старше константного значения)
            if arrival > now + cfg.queue_horizon_s:
                continue  # приезд позже горизонта 30 минут
            candidates.append((arrival, car.unit_uuid, car))

        # 3) раскладка: по приезду, каждая на 230 с с max(приезд, free_at предыдущей)
        for arrival, _, car in sorted(candidates, key=lambda c: (c[0], c[1])):
            start = arrival if free_boundary is None else max(arrival, free_boundary)
            free_at = start + cfg.service_time_s
            entries.append(QueueEntry(
                car.unit_uuid,
                _sec(arrival),
                _sec(start),
                _sec(free_at),
                _sec(start - arrival),
            ))
            free_boundary = free_at

        return StationQueue(station_uuid, now, tuple(entries))

    def _arrival_ts(
        self,
        car: CarRuntime,
        station_uuid: str,
        st_point: Point,
        unload: Point,
        now: int,
    ) -> float | None:
        """Время приезда машины к станции; None, если она не про эту очередь."""
        pos = Point(*car.last_pos)
        speed = self.cfg.drive_speed_mps

        if car.state is CarState.AT_STATION:
            if car.target_station != station_uuid:
                return None # не та станция 
            
            # отслужившая машина, задержавшаяся в радиусе, — не в очереди
            if car.station_occupied_at is not None:
                return None
            
            # ждёт в радиусе занятой станции: приезд — момент входа в радиус
            entered = car.entered_radius_at if car.entered_radius_at is not None else car.last_ts
            return float(entered)

        if car.state is CarState.TO_STATION:
            if car.target_station != station_uuid:
                return None
            return now + distance_m(pos, st_point) / speed

        if car.state is CarState.TO_UNLOAD:
            # к разгрузке: после неё возвращается к своей станции
            if car.own_station != station_uuid:
                return None
            return now + (distance_m(pos, unload) + distance_m(unload, st_point)) / speed

        return None
