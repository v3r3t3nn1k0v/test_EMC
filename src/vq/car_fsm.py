"""Машина-автомат: состояние одной машины по одному сообщению телеметрии.

FSM сознательно не знает про очереди и занятость станций: она отвечает
только за состояние машины (таблица из ТЗ) и точечные сигналы — вход/выход
радиуса станции, пересечение радиуса решения, начало нового заезда. Занятие
станции — факт станции: движок очереди выводит его из сигнала
AT_STATION_ENTERED и расписания станции.

Правила (ТЗ):
- порядок и дубли: сообщение с ts <= последнего учтённого ts той же машины
  игнорируется — это покрывает и дубли, и нарушение порядка; опоздавшее
  сообщение с более новым ts учитывается;
- «на станции»: скорость < 1 км/ч и расстояние до целевой станции < 50 м;
- «к разгрузке»: вышла из радиуса 50 м станции после обслуживания;
- «к станции»: вышла из радиуса 50 м точки разгрузки; это же состояние
  по умолчанию для первого сообщения не «на станции».

Два уточнения к неоднозначностям ТЗ (отражены в README):
- расстояния считаются до целевой станции (рекомендованная, если есть,
  иначе своя): после рекомендации машина обслуживается на рекомендованной;
- переходы по радиусам — по пересечению границы, с гистерезисом:
  «на станции» держится, пока машина в радиусе (манёвр), а «к станции»
  машина переходит, только побывав в радиусе разгрузки и выйдя из него, —
  иначе машина в 100 м от станции сразу считалась бы «к станции».
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config, Point
from .geo import distance_m
from .models import CarRuntime, CarState, FsmEventKind, Telemetry


@dataclass
class FsmEvent:
    """Сигнал по одной машине — вход для движка очереди и решателя."""

    unit_uuid: str
    ts: int
    kind: FsmEventKind
    station: str | None = None   # целевая станция; своя — для радиуса решения
    stale: bool = False          # сообщение старее «сейчас» больше чем на 30 с


class CarFsm:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def apply(self, car: CarRuntime, msg: Telemetry, now_ts: int) -> list[FsmEvent]:
        """Учитывает сообщение, мутирует car, возвращает сигналы."""
        cfg = self.cfg

        # случай, когда пришло сообщение с моментом времени меньшим уже имеющегося
        if msg.ts <= car.last_ts:
            return [FsmEvent(car.unit_uuid, msg.ts, FsmEventKind.IGNORED)]

        prev_state = car.state
        target = cfg.stations[car.target_station] # рекомендованная станция, если есть, иначе своя
        point = Point(msg.lat, msg.lon)

        dist_target = distance_m(point, target)          # до целевой станции
        dist_unload = distance_m(point, cfg.unload_point)  # до точки разгрузки



        # храним для отслежвивания пересечения радиусов станции и разгрузки
        prev_dist_target = (
            distance_m(Point(*car.last_pos), target) 
            if car.last_pos is not None else None
        ) # расстояние до рекомендованной станции до последнего полученного сообщения
        prev_dist_unload = (
            distance_m(Point(*car.last_pos), cfg.unload_point)
            if car.last_pos is not None else None
        ) # расстояние до точки разгрузки до последнего полученного сообщения

        
        in_station_radius = dist_target < cfg.station_radius_m # машина находится в радиусе станции
        in_unload_radius = dist_unload < cfg.station_radius_m # машина находится в радиусе точки разгрузки
        stopped = msg.speed_kmh < cfg.stop_speed_kmh # машина остановилась (можно проверять по конкретному значению с телеметрии <1 км.ч) 
       
        if prev_state is CarState.AT_STATION:
            new_state = CarState.AT_STATION if in_station_radius else CarState.TO_UNLOAD # на станции или дивгается к зангрузке
        elif stopped and in_station_radius:
            new_state = CarState.AT_STATION  # приехала и встала у станции 
        elif (
            prev_state is CarState.TO_UNLOAD
            and not in_unload_radius
            and prev_dist_unload is not None
            and prev_dist_unload < cfg.station_radius_m
        ):
            new_state = CarState.TO_STATION  # побывала у разгрузки и вышла из радиуса
        else:
            # продолжает путь; это же дефолт для первого сообщения
            new_state = (
                CarState.TO_UNLOAD if prev_state is CarState.TO_UNLOAD
                else CarState.TO_STATION
            )

        events: list[FsmEvent] = []
        stale = (now_ts - msg.ts) > cfg.fresh_telemetry_s

        # 3) сигналы по смене состояния
        if new_state is not prev_state:
            if new_state is CarState.AT_STATION: # машина приехала на станцию 
                events.append(self._ev(car, msg, stale,
                                       FsmEventKind.AT_STATION_ENTERED, car.target_station))
            if prev_state is CarState.AT_STATION: # машина покинула станцию 
                events.append(self._ev(car, msg, stale,
                                       FsmEventKind.LEFT_STATION_RADIUS, car.target_station))
            if prev_state is CarState.TO_UNLOAD and new_state is CarState.TO_STATION: # от загрузки к станции. новая поездка
                self._reset_trip(car)
                events.append(self._ev(car, msg, stale,
                                       FsmEventKind.NEW_TRIP, car.own_station))

        # Радиус решения: один раз за заезд, только пока едет к своей станции.
        dist_own = (
            dist_target if car.target_station == car.own_station
            else distance_m(point, cfg.stations[car.own_station])
        )
        if (
            new_state is CarState.TO_STATION
            and car.recommended_station is None
            and not car.was_within_decision_radius
            and dist_own < cfg.decision_radius_m
        ):
            car.was_within_decision_radius = True
            events.append(self._ev(car, msg, stale,
                                   FsmEventKind.CROSSED_DECISION_RADIUS, car.own_station))

        
        if in_station_radius and (
            prev_dist_target is None or prev_dist_target >= cfg.station_radius_m
        ):
            car.entered_radius_at = msg.ts
        elif not in_station_radius:
            car.entered_radius_at = None

        # маппинг сообщения в CarRuntime
        car.last_ts = msg.ts
        car.last_pos = (msg.lat, msg.lon)
        car.last_speed_kmh = msg.speed_kmh
        car.state = new_state

        if not events:
            events.append(self._ev(car, msg, stale, FsmEventKind.UPDATED, car.target_station))
        return events

    @staticmethod
    def _reset_trip(car: CarRuntime) -> None:
        """Новый заезд: машина снова едет к своей станции, флаги сбрасываются."""
        car.recommended_station = None
        car.entered_radius_at = None
        car.was_within_decision_radius = False
        car.decision_made = False
        car.station_occupied_at = None

    @staticmethod
    def _ev(car: CarRuntime, msg: Telemetry, stale: bool,
            kind: FsmEventKind, station: str | None) -> FsmEvent:
        return FsmEvent(car.unit_uuid, msg.ts, kind, station=station, stale=stale)
