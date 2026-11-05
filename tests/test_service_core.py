"""Ядро сервиса без брокера: process_message чист и возвращает исходящие."""
from __future__ import annotations

import pytest
from helpers import make_config, tel

from vq.serde import iso_z
from vq.service import Processor, Settings


@pytest.fixture
def cfg():
    return make_config()


@pytest.fixture
def proc(cfg):
    return Processor(cfg, Settings())


def topics(out) -> list[tuple[str, str]]:
    return [(o.topic, o.key) for o in out]


def test_unknown_unit_ignored(proc):
    assert proc.process_message(tel("X-99", 100, -3000)) == []


def test_first_message_publishes_station_queue(proc):
    out = proc.process_message(tel("T2", 43200, -3000))  # вне радиуса решения
    assert topics(out) == [("queue.v1", "S1")]  # пустые станции не публикуются
    entry = out[0].payload["queue"][0]
    assert entry["unit_uuid"] == "T2"
    assert entry["eta"] == iso_z(43500)  # 3000 м / 10 м/с = 300 с


def test_no_republish_when_queue_unchanged(proc):
    proc.process_message(tel("T2", 43200, -3000))
    assert proc.process_message(tel("T2", 43201, -2990)) == []  # едет — очередь та же
    assert proc.process_message(tel("T2", 43201, -2990)) == []  # дубль ts


def test_tz_example_t4_flow(proc):
    proc.process_message(tel("T1", 43140, -15, speed_kmh=0))
    proc.process_message(tel("T2", 43200, -1200))
    proc.process_message(tel("T3", 43200, -3000))

    out = proc.process_message(tel("T4", 43200, -900))
    # решение опубликовано; S1 не изменилась (T4 в неё так и не попала),
    # S2 получила новую очередь с T4
    assert topics(out) == [("decision.v1", "T4"), ("queue.v1", "S2")]
    assert out[0].payload["result"] == "recommended"
    assert out[0].payload["to_station"] == "S2"
    assert out[0].payload["gain_seconds"] == 80
    assert out[1].payload["queue"][0]["unit_uuid"] == "T4"


def test_stale_message_rejected_and_car_excluded_until_fresh(proc):
    proc.process_message(tel("T1", 43140, -15, speed_kmh=0))
    proc.process_message(tel("T2", 43200, -1200))       # «сейчас» = 43200

    out = proc.process_message(tel("T4", 43100, -900))  # опоздавшее на 100 с
    decisions = [o for o in out if o.topic == "decision.v1"]
    assert decisions[0].payload["reason"] == "stale_telemetry"

    # протухшая позиция не попадает в очередь: T4 в S1 нет
    assert all(o.key != "S1" for o in out if o.topic == "queue.v1")

    # свежая позиция возвращает машину в очередь
    out = proc.process_message(tel("T4", 43201, -890))
    s1 = [o for o in out if o.topic == "queue.v1" and o.key == "S1"]
    assert s1 and any(e["unit_uuid"] == "T4" for e in s1[0].payload["queue"])
