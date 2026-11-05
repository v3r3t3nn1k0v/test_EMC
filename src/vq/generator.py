"""Генератор телеметрии: симулятор площадки с замкнутым контуром рекомендаций.

Симулятор держит собственную «правду» — кто где едет, кто занимает станцию
и кто ждёт, — и раз в секунду публикует телеметрию всех машин в
telemetry.v1. Из этого потока сервис восстанавливает виртуальные очереди.

Модель цикла машины (по ТЗ):
- едет к станции с расчётной скоростью 36 км/ч;
- станция занята — останавливается в радиусе 50 м и ждёт своей очереди;
- обслуживание: 200 с стоит + 30 с манёвра; скорость манёвра подобрана
  так, что машина пересекает границу радиуса ровно к концу 230-й секунды —
  станция освобождается по расписанию;
- едет к точке разгрузки, разгрузка мгновенная, возвращается к станции.

Замкнутый контур: генератор читает decision.v1 и перенаправляет машину к
рекомендованной станции с текущей позиции; после обслуживания и разгрузки
машина снова едет к своей.

Реализм потока (ТЗ «дубли и опоздания до 60 секунд»): задержка отправки
каждой машины блуждает случайным шагом в [0, 60] с — порядок сообщений
одной машины сохраняется (дедупликация их не убивает), а общий поток
не отсортирован по времени. Дубли — малой вероятностью на каждое сообщение.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
from dataclasses import dataclass, field

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .config import Config, Point

logger = logging.getLogger("vq.generator")

DT = 1.0                 # шаг симуляции, с
STAND_S = 200.0          # стоит на станции
MANEUVER_S = 30.0        # манёвр внутри радиуса
SERVICE_SLOT_M = 10.0    # точка обслуживания: 10 м от центра станции
WAIT_BASE_M = 15.0       # место ожидания первой машины
WAIT_STEP_M = 10.0       # шаг между ждущими
WAIT_MAX_M = 45.0        # не дальше — все ждущие внутри радиуса 50 м
LAT_M = 111_320.0        # метров на градус широты — локальная проекция


@dataclass(frozen=True)
class GenSettings:
    bootstrap: str = "localhost:9092"
    telemetry_topic: str = "telemetry.v1"
    decision_topic: str = "decision.v1"
    group_id: str = "vq-generator"
    speedup: float = 10.0
    duplicate_prob: float = 0.005
    late_max_s: float = 60.0
    seed: int = 42
    warmup_ticks: int = 600

    @classmethod
    def from_env(cls) -> "GenSettings":
        def f(name: str, default: float) -> float:
            return float(os.environ.get(name, default))

        return cls(
            bootstrap=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", cls.bootstrap),
            telemetry_topic=os.environ.get("TELEMETRY_TOPIC", cls.telemetry_topic),
            decision_topic=os.environ.get("DECISION_TOPIC", cls.decision_topic),
            group_id=os.environ.get("CONSUMER_GROUP", cls.group_id),
            speedup=f("GEN_SPEEDUP", cls.speedup),
            duplicate_prob=f("GEN_DUPLICATE_PROB", cls.duplicate_prob),
            late_max_s=f("GEN_LATE_MAX_S", cls.late_max_s),
            seed=int(os.environ.get("GEN_SEED", cls.seed)),
            warmup_ticks=int(os.environ.get("GEN_WARMUP", cls.warmup_ticks)),
        )


def _to_xy(p: Point, origin: Point) -> tuple[float, float]:
    """Широта/долгота → локальные метры (x на восток, y на север)."""
    k = LAT_M * math.cos(math.radians(origin.lat))
    return ((p.lon - origin.lon) * k, (p.lat - origin.lat) * LAT_M)


def _to_latlon(x: float, y: float, origin: Point) -> tuple[float, float]:
    k = LAT_M * math.cos(math.radians(origin.lat))
    return (origin.lat + y / LAT_M, origin.lon + x / k)

#станция симуляции 
@dataclass
class SimStation:
    uuid: str
    xy: tuple[float, float]
    occupier: str | None = None
    occupied_until: float = 0.0
    waiting: list[str] = field(default_factory=list)

# машина симуляции 
@dataclass
class SimCar:
    unit_uuid: str
    own_station: str
    target: str                     # цель заезда: своя или рекомендованная
    x: float
    y: float
    state: str = "to_station"       # to_station|waiting|servicing|maneuvering|to_unload
    service_end: float = 0.0
    maneuver_end: float = 0.0
    maneuver_v: float = 0.0
    maneuver_dir: tuple[float, float] = (0.0, 0.0)
    delay: float = 0.0              # текущая задержка отправки, с


class Generator:
    def __init__(self, cfg: Config, settings: GenSettings, rng: random.Random):
        self.cfg = cfg
        self.settings = settings
        self.rng = rng
        self.origin = cfg.unload_point
        self.unload_xy = _to_xy(cfg.unload_point, self.origin)
        self.stations = {
            s: SimStation(s, _to_xy(p, self.origin)) for s, p in cfg.stations.items()
        }
        self.cars: dict[str, SimCar] = {}
        self.buffer: list[tuple[float, str, dict]] = []  # (send_at, key, payload)
        self.sim_t = 0
        self._init_cars()

    # --- инициализация ---

    def _init_cars(self) -> None:
        """Все машины стартуют по пути к своей станции со случайной фазой.

        Смешанное состояние (обслуживание, ожидание, разгрузка) получается
        прогревом: warmup_ticks тактов без отправки телеметрии.
        """
        for unit, own in self.cfg.assignments.items():
            st = self.stations[own]
            frac = self.rng.uniform(0.05, 0.95)
            x = self.unload_xy[0] + (st.xy[0] - self.unload_xy[0]) * frac
            y = self.unload_xy[1] + (st.xy[1] - self.unload_xy[1]) * frac
            self.cars[unit] = SimCar(
                unit_uuid=unit, own_station=own, target=own, x=x, y=y,
                delay=self.rng.uniform(0.0, self.settings.late_max_s),
            )

    # --- симуляция ---

    def _tick(self, t: float) -> None:
        for car in self.cars.values():
            self._step_car(car, t)
        self._step_stations(t)

    def _step_car(self, car: SimCar, t: float) -> None:
        st = self.stations[car.target]
        sx, sy = st.xy

        if car.state == "to_station":
            d = math.hypot(sx - car.x, sy - car.y)
            slot = SERVICE_SLOT_M if st.occupier is None else min(
                WAIT_BASE_M + WAIT_STEP_M * len(st.waiting), WAIT_MAX_M,
            )
            if d <= slot + 0.5:
                car.state = "waiting"          # занять очередь; продвижение — в _step_stations
                st.waiting.append(car.unit_uuid)
            else:
                # ехать к точке остановки: не до центра, а до slot метров до него
                self._move_toward(car, sx - (sx - car.x) / d * slot,
                                  sy - (sy - car.y) / d * slot, self.cfg.drive_speed_mps)
        elif car.state == "waiting":
            pass  # стоит в радиусе; займёт станцию, когда она освободится
        elif car.state == "servicing":
            if t >= car.service_end:
                # манёвр: скорость подобрана под пересечение границы радиуса
                # ровно через 30 с — станция освободится по расписанию +230
                d = math.hypot(sx - car.x, sy - car.y)
                car.maneuver_v = max((self.cfg.station_radius_m - d) / MANEUVER_S, 0.05)
                ux, uy = self.unload_xy[0] - sx, self.unload_xy[1] - sy
                un = math.hypot(ux, uy) or 1.0
                car.maneuver_dir = (ux / un, uy / un)
                car.maneuver_end = t + MANEUVER_S
                car.state = "maneuvering"
        elif car.state == "maneuvering":
            car.x += car.maneuver_dir[0] * car.maneuver_v * DT
            car.y += car.maneuver_dir[1] * car.maneuver_v * DT
            if t >= car.maneuver_end:
                car.state = "to_unload"
                car.target = car.own_station   # следующий заезд — снова к своей
        elif car.state == "to_unload":
            if self._move_toward(car, *self.unload_xy, self.cfg.drive_speed_mps):
                car.state = "to_station"       # разгрузка мгновенная

    def _step_stations(self, t: float) -> None:
        total = self.cfg.service_time_s
        stand = STAND_S
        for st in self.stations.values():
            if st.occupier is not None and t >= st.occupied_until:
                st.occupier = None
            if st.occupier is None and st.waiting:
                unit = st.waiting.pop(0)
                car = self.cars[unit]
                if car.state != "waiting":
                    continue  # защита; перенаправления происходят только в to_station
                st.occupier = unit
                st.occupied_until = t + total
                car.state = "servicing"
                car.service_end = t + stand

    @staticmethod
    def _move_toward(car: SimCar, tx: float, ty: float, v: float) -> bool:
        dx, dy = tx - car.x, ty - car.y
        d = math.hypot(dx, dy)
        step = v * DT
        if d <= step:
            car.x, car.y = tx, ty
            return True
        car.x += dx / d * step
        car.y += dy / d * step
        return False

    def _speed_kmh(self, car: SimCar) -> float:
        if car.state in ("waiting", "servicing"):
            return 0.0
        if car.state == "maneuvering":
            return round(car.maneuver_v * 3.6, 1)
        return 36.0

    # --- рекомендации (замкнутый контур) ---

    def reroute(self, unit_uuid: str, to_station: str) -> bool:
        """Перенаправить машину к рекомендованной станции с текущей позиции."""
        car = self.cars.get(unit_uuid)
        if car is None or car.state != "to_station" or car.target != car.own_station:
            return False
        car.target = to_station
        logger.info("%s перенаправлена: %s → %s", unit_uuid, car.own_station, to_station)
        return True

    # --- телеметрия ---

    def _emit(self, t: float) -> None:
        """Буферизует сообщения всех машин с их текущими задержками."""
        late_max = self.settings.late_max_s
        for car in self.cars.values():
            car.delay = min(late_max, max(0.0, car.delay + self.rng.uniform(-0.4, 0.4)))
            lat, lon = _to_latlon(car.x, car.y, self.origin)
            payload = {
                "unit_uuid": car.unit_uuid,
                "ts": int(t),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "speed_kmh": self._speed_kmh(car),
            }
            self.buffer.append((t + car.delay, car.unit_uuid, payload))
            if self.rng.random() < self.settings.duplicate_prob:
                self.buffer.append((t + car.delay + self.rng.uniform(0.5, 2.0),
                                    car.unit_uuid, payload))

    async def _flush(self, t: float, producer: AIOKafkaProducer) -> None:
        due = [m for m in self.buffer if m[0] <= t + 1e-9]
        if due:
            for _, key, payload in due:
                producer.send(
                    self.settings.telemetry_topic,
                    key=key.encode(),
                    value=json.dumps(payload).encode(),
                )
            await producer.flush()
        self.buffer = [m for m in self.buffer if m[0] > t + 1e-9]

    # --- запуск ---

    async def run(self) -> None:
        st = self.settings
        producer = AIOKafkaProducer(
            bootstrap_servers=st.bootstrap, acks="1", linger_ms=5,
        )
        consumer = AIOKafkaConsumer(
            st.decision_topic,
            bootstrap_servers=st.bootstrap,
            group_id=st.group_id,
            auto_offset_reset="latest",
        )
        await producer.start()
        await consumer.start()
        decisions = asyncio.create_task(self._consume_decisions(consumer))
        logger.info(
            "старт: %d машин, %d станций, ускорение x%s, брокер %s",
            len(self.cars), len(self.stations), st.speedup, st.bootstrap,
        )
        try:
            for _ in range(st.warmup_ticks):  # прогрев: смешанные фазы без отправки
                self._tick(self.sim_t)
                self.sim_t += 1
            period = 1.0 / st.speedup
            while True:
                t = self.sim_t
                self._tick(t)
                self._emit(t)
                await self._flush(t, producer)
                self.sim_t += 1
                await asyncio.sleep(period)
        finally:
            decisions.cancel()
            await producer.stop()
            await consumer.stop()

    async def _consume_decisions(self, consumer: AIOKafkaConsumer) -> None:
        async for msg in consumer:
            try:
                obj = json.loads(msg.value)
                if obj.get("result") == "recommended":
                    self.reroute(obj["unit_uuid"], obj["to_station"])
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("битое решение на оффсете %s", msg.offset)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = GenSettings.from_env()
    gen = Generator(Config(), settings, random.Random(settings.seed))
    asyncio.run(gen.run())


if __name__ == "__main__":
    main()
