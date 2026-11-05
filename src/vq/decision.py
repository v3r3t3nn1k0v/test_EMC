"""Решатель рекомендаций: куда ехать машине, подъезжающей к своей станции.

Считается один раз за заезд — по сигналу CROSSED_DECISION_RADIUS от FSM
(машина в состоянии «к станции» впервые оказалась ближе 1500 м к своей).

Правила (ТЗ):
- ожидание на станции = момент освобождения − время приезда машины к ней,
  но не меньше нуля; освобождение = free_at последней машины в очереди
  станции, которая приедет раньше нашей (занявшая станцию с eta=null
  считается приехавшей), нет таких — станция свободна;
- выбор: наименьшее ожидание; при равенстве — своя станция, затем ближайшая;
- выигрыш = ожидание у своей − ожидание у выбранной; меньше 60 с — отказ
  no_gain; сообщение, вызвавшее расчёт, старее «сейчас» на 30 с — отказ
  stale_telemetry (проверяется до расчёта: протухшее сообщение делает
  сравнение недостоверным — решение из README);
- после рекомендации машина до конца обслуживания едет к рекомендованной
  станции (переключением car.recommended_station), из очереди своей
  станции она исключается автоматически — очередь строится по цели заезда.
"""
from __future__ import annotations

from .car_fsm import FsmEvent
from .config import Config, Point
from .geo import distance_m
from .models import CarRuntime, Decision, FsmEventKind, StationQueue


class DecisionEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def decide(
        self,
        car: CarRuntime,
        ev: FsmEvent,
        queues: dict[str, StationQueue],
        now: int,
    ) -> Decision:
        """Расчёт по сигналу пересечения радиуса решения.

        `queues` — очереди всех станций, снятые ДО решения: машина ещё
        едет к своей станции. Свои записи из очередей исключаем сами.
        """
        assert ev.kind is FsmEventKind.CROSSED_DECISION_RADIUS # надо ли принимать решение ы
        car.decision_made = True

        if ev.stale:
            return Decision(
                car.unit_uuid, now, "rejected",
                from_station=car.own_station, reason="stale_telemetry",
            )

        pos = Point(*car.last_pos)

        # ожидание на каждой станции
        waits: dict[str, int] = {}
        for st_uuid in self.cfg.stations:
            st = self.cfg.stations[st_uuid]
            arrival = now + distance_m(pos, st) / self.cfg.drive_speed_mps
            waits[st_uuid] = self._wait_seconds(queues[st_uuid], car.unit_uuid, arrival)

        # выбор: минимум ожидания; при равенстве — своя, затем ближайшая
        def rank(item: tuple[str, int]):
            st_uuid, wait = item
            return (
                wait,
                st_uuid != car.own_station,                  # своя выигрывает ничью
                distance_m(pos, self.cfg.stations[st_uuid]),  # затем ближайшая
            )

        selected, selected_wait = min(waits.items(), key=rank)
        gain = waits[car.own_station] - selected_wait

        if gain < self.cfg.min_gain_s:
            return Decision(
                car.unit_uuid, now, "rejected",
                from_station=car.own_station, reason="no_gain",
            )

        # рекомендация принята: до конца обслуживания едет к рекомендованной
        car.recommended_station = selected 
        return Decision(
            car.unit_uuid, now, "recommended",
            from_station=car.own_station, to_station=selected, gain_seconds=gain,
        )

    @staticmethod
    def _wait_seconds(queue: StationQueue, unit_uuid: str, arrival: float) -> int:
        """Ожидание машины на станции: освобождение − приезд, но не меньше нуля.

        Освобождение — free_at последней машины очереди, приедущей раньше
        нашей (entries упорядочены по приезду, free_at вдоль очереди не
        убывает, поэтому последнее подходящее звено и есть искомное).
        """
        arrival_s = int(round(arrival))
        release = 0
        for entry in queue.entries:
            if entry.unit_uuid == unit_uuid:
                continue  # себя не учитываем
            if entry.eta is None or entry.eta < arrival_s:
                release = entry.free_at
        return max(0, release - arrival_s)
