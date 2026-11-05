"""Сервис виртуальных очередей: Kafka-обвязка вокруг расчётного ядра.

Один процесс, одна консьюмер-группа, всё состояние в памяти. Телеметрия
приходит по машинам (ключ unit_uuid), очередь считается по станциям, а
рекомендация требует очередей всех станций сразу — поэтому процессор один:
шардирование по станциям потребовало бы обмена состоянием между шардами.

Обработка сообщения синхронна и без побочных эффектов на сторону Kafka:
process_message возвращает исходящие сообщения, и это же ядро тестируется
без брокера. run() только переносит поток:

    consume → process_message → produce → commit (после каждого сообщения)

Гарантии: at-least-once. Оффсет коммитится после публикации выходов,
при падении переигрывается максимум одно сообщение; идемпотентность
на уровне контента — одинаковые подряд очереди не публикуются, решения
детерминированы временем событий (см. ARCHITECTURE.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from . import serde
from .car_fsm import CarFsm
from .config import Config
from .decision import DecisionEngine
from .models import CarRuntime, FsmEventKind, StationState, Telemetry
from .queue_engine import QueueEngine

logger = logging.getLogger("vq.service")


@dataclass(frozen=True)
class Settings:
    """Настройки брокера и топиков — только env-переменные."""

    bootstrap: str = "localhost:9092"
    telemetry_topic: str = "telemetry.v1"
    queue_topic: str = "queue.v1"
    decision_topic: str = "decision.v1"
    group_id: str = "vq-processor"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bootstrap=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", cls.bootstrap),
            telemetry_topic=os.environ.get("TELEMETRY_TOPIC", cls.telemetry_topic),
            queue_topic=os.environ.get("QUEUE_TOPIC", cls.queue_topic),
            decision_topic=os.environ.get("DECISION_TOPIC", cls.decision_topic),
            group_id=os.environ.get("CONSUMER_GROUP", cls.group_id),
        )


@dataclass(frozen=True)
class Outbound:
    """Исходящее сообщение, подготовленное процессором."""
    topic: str
    key: str
    payload: dict


class Processor:
    """Состояние сервиса и синхронная обработка потока телеметрии."""

    def __init__(self, cfg: Config, settings: Settings):
        self.cfg = cfg
        self.settings = settings
        self.fsm = CarFsm(cfg)
        self.engine = QueueEngine(cfg)
        self.decider = DecisionEngine(cfg)
        self.cars: dict[str, CarRuntime] = {}
        self.stations: dict[str, StationState] = {s: StationState(s) for s in cfg.stations}
        self.now = 0  # «сейчас» — наибольший ts среди учтённых сообщений
        # последние опубликованные очереди; пустая очередь — стартовое
        # состояние станции, сама по себе не событие
        self.last_queue_entries: dict[str, tuple] = {s: tuple() for s in cfg.stations}

    def process_message(self, tel: Telemetry) -> list[Outbound]:
        """Учитывает сообщение телеметрии, возвращает что опубликовать."""
        if self.cfg.own_station(tel.unit_uuid) is None:
            logger.warning("телеметрия незакреплённой машины %s — игнорирую", tel.unit_uuid)
            return []

        car = self._car(tel.unit_uuid)
        if tel.ts <= car.last_ts:
            return []  # дубль или нарушение порядка — игнорируется

        if tel.ts > self.now:
            self.now = tel.ts

        events = self.fsm.apply(car, tel, self.now) # события, которые породило новое сообщение из телеметрии 
        for ev in events:
            if ev.station is not None:
                self.engine.apply_event(ev, car, self.stations[ev.station])

        out: list[Outbound] = []

        # рекомендация: один расчёт за заезд, по сигналу пересечения 1500 м;
        # вход — очереди ДО решения (машина ещё едет к своей станции)
        for ev in events:
            if ev.kind is FsmEventKind.CROSSED_DECISION_RADIUS: # принимаем решение, если находимся в радиусе 1500м от запланированной станции 
                queues = self._build_queues()
                decision = self.decider.decide(car, ev, queues, self.now)
                out.append(Outbound(
                    self.settings.decision_topic,
                    decision.unit_uuid,
                    serde.decision_to_json(decision),
                ))
                logger.info("решение %s: %s", decision.unit_uuid, decision)
                break

        # очереди: пересчёт всех станций, публикация только изменившихся
        for station_uuid, queue in self._build_queues().items():
            if self.last_queue_entries.get(station_uuid) == queue.entries:
                continue
            self.last_queue_entries[station_uuid] = queue.entries
            out.append(Outbound(
                self.settings.queue_topic,
                station_uuid,
                serde.queue_to_json(queue),
            ))
        return out

    def _car(self, unit_uuid: str) -> CarRuntime:
        if unit_uuid not in self.cars:
            self.cars[unit_uuid] = CarRuntime(
                unit_uuid=unit_uuid,
                own_station=self.cfg.own_station(unit_uuid),
            )
        return self.cars[unit_uuid]

    def _build_queues(self) -> dict:
        return {
            s: self.engine.build_queue(s, self.stations[s], self.cars, self.now)
            for s in self.cfg.stations
        }

    # --- Kafka I/O ---

    async def run(self) -> None:
        st = self.settings
        consumer = AIOKafkaConsumer(
            st.telemetry_topic,
            bootstrap_servers=st.bootstrap,
            group_id=st.group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        producer = AIOKafkaProducer(
            bootstrap_servers=st.bootstrap,
            acks="all",
            linger_ms=5,
        )
        await consumer.start()
        await producer.start()
        logger.info(
            "старт: %s → %s + %s, брокер %s, группа %s",
            st.telemetry_topic, st.queue_topic, st.decision_topic,
            st.bootstrap, st.group_id,
        )
        try:
            async for msg in consumer:
                await self._handle(consumer, producer, msg)
        finally:
            await producer.stop()
            await consumer.stop()

    async def _handle(self, consumer: AIOKafkaConsumer,
                      producer: AIOKafkaProducer, msg) -> None:
        tel = serde.parse_telemetry(msg.value)
        if tel is None:
            logger.warning("битое сообщение %s[%s] — пропускаю", msg.topic, msg.offset)
        else:
            for out in self.process_message(tel):
                await producer.send_and_wait(
                    out.topic,
                    key=out.key.encode(),
                    value=json.dumps(out.payload).encode(),
                )
        # коммит после каждого сообщения (включая пропущенное битое),
        # чтобы poison-message не останавливал поток
        await consumer.commit()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(Processor(Config(), Settings.from_env()).run())


if __name__ == "__main__":
    main()
