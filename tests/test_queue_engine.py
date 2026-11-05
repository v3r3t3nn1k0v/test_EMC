"""Движок очереди: занятие/освобождение, раскладка, горизонт, протухшие.

Центральный тест — пример из ТЗ: очередь к S1 на 12:00:00 из машин
T1 (заняла в 11:59:00), T2 (приезд через 120 с), T3 (через 300 с).
Момент 12:00:00 = 43200; времена в тестах — epoch-секунды.
"""
from __future__ import annotations

import pytest
from helpers import Yard, make_config, tel

from vq.models import QueueEntry


@pytest.fixture
def yard(cfg):
    return Yard(cfg)


# --- пример из ТЗ ------------------------------------------------------------


def test_tz_example_full_queue(cfg, yard):
    # T1: стоит в 15 м от S1, первое такое сообщение в 11:59:00 (= 43140)
    yard.apply(tel("T1", 43140, -15, speed_kmh=0), now_ts=43140)
    assert yard.stations["S1"].occupied_by == "T1"
    assert yard.stations["S1"].occupied_at == 43140

    # T2: в 1200 м от S1 едет к станции; T3: в 3000 м. Момент расчёта 12:00:00.
    yard.apply(tel("T2", 43200, -1200), now_ts=43200)
    yard.apply(tel("T3", 43200, -3000), now_ts=43200)

    q = yard.queue("S1", now=43200)
    assert q.entries == (
        #        eta    start  free   wait
        QueueEntry("T1", None, 43140, 43370, 0),    # заняла в 11:59:00
        QueueEntry("T2", 43320, 43370, 43600, 50),  # приезд 12:02:00, ждёт 50 с
        QueueEntry("T3", 43500, 43600, 43830, 100),  # приезд 12:05:00, ждёт 100 с
    )


# --- занятие и освобождение ---------------------------------------------------


def test_station_busy_no_second_occupier(yard):
    yard.apply(tel("T1", 100, -15, speed_kmh=0), now_ts=100)   # T1 заняла до 330
    yard.apply(tel("T2", 106, -35, speed_kmh=0), now_ts=106)   # T2 встала рядом
    assert yard.stations["S1"].occupied_by == "T1"             # станцией владеет T1


def test_waiter_arrival_is_radius_entry_moment(yard):
    yard.apply(tel("T1", 100, -15, speed_kmh=0), now_ts=100)   # заняла до 330
    yard.apply(tel("T2", 104, -60), now_ts=104)                # едет, вне радиуса
    yard.apply(tel("T2", 105, -40), now_ts=105)                # вошла в радиус на ходу
    yard.apply(tel("T2", 106, -35, speed_kmh=0), now_ts=106)   # встала — ждёт

    q = yard.queue("S1", now=106)
    assert q.entries == (
        QueueEntry("T1", None, 100, 330, 0),
        QueueEntry("T2", 105, 330, 560, 225),  # приезд = вход в радиус, не остановка
    )


def test_waiter_occupies_when_station_frees_by_schedule(yard):
    yard.apply(tel("T1", 100, -15, speed_kmh=0), now_ts=100)   # освободится в 330
    yard.apply(tel("T2", 105, -40), now_ts=105)
    yard.apply(tel("T2", 106, -35, speed_kmh=0), now_ts=106)   # ждёт с 105

    # первое сообщение T2 «на станции» после освобождения — в 331
    yard.apply(tel("T2", 331, -35, speed_kmh=0), now_ts=331)
    assert yard.stations["S1"].occupied_by == "T2"
    assert yard.cars["T2"].station_occupied_at == 331

    q = yard.queue("S1", now=331)
    assert q.entries == (QueueEntry("T2", None, 331, 561, 0),)  # T1 уже отслужила


def test_early_release_when_occupier_exits_radius(yard):
    yard.apply(tel("T1", 100, -15, speed_kmh=0), now_ts=100)   # заняла до 330
    yard.apply(tel("T2", 105, -40), now_ts=105)
    yard.apply(tel("T2", 106, -35, speed_kmh=0), now_ts=106)   # ждёт с 105

    yard.apply(tel("T1", 150, -100), now_ts=150)               # T1 вышла из радиуса
    assert yard.stations["S1"].released_at == 150
    yard.apply(tel("T2", 149, -35, speed_kmh=0), now_ts=149)   # T2 всё ещё ждёт

    q = yard.queue("S1", now=150)
    assert q.entries == (
        # T2 начинает в момент фактического освобождения станции
        QueueEntry("T2", 105, 150, 380, 45),
        # T1 уехала к разгрузке, но вернётся к S1: 1900 м до разгрузки + 2000 м
        # от неё до станции — приезд через 390 с, следующая в очереди
        QueueEntry("T1", 540, 540, 770, 0),
    )


def test_served_occupier_lingering_neither_reoccupies_nor_queues(yard):
    yard.apply(tel("T1", 100, -15, speed_kmh=0), now_ts=100)   # до 330
    yard.apply(tel("T1", 400, -15, speed_kmh=0), now_ts=400)   # отслужила, но стоит

    assert yard.stations["S1"].occupied_at == 100              # занятие не продлилось
    assert yard.queue("S1", now=400).entries == ()             # и в очереди её нет


# --- очередь: состав -----------------------------------------------------------


def test_order_by_arrival(yard):
    yard.apply(tel("T3", 100, -1000), now_ts=100)   # приезд через 100 с
    yard.apply(tel("T2", 100, -500), now_ts=100)    # приезд через 50 с
    q = yard.queue("S1", now=100)
    assert [e.unit_uuid for e in q.entries] == ["T2", "T3"]


def test_horizon_excludes_late_arrivals(yard):
    yard.apply(tel("T2", 43200, -18100), now_ts=43200)   # приезд через 1810 с > 1800
    yard.apply(tel("T3", 43200, -17000), now_ts=43200)   # приезд через 1700 с
    q = yard.queue("S1", now=43200)
    assert [e.unit_uuid for e in q.entries] == ["T3"]


def test_stale_position_excluded_but_occupier_kept(yard):
    yard.apply(tel("T1", 43140, -15, speed_kmh=0), now_ts=43140)  # заняла до 43370
    yard.apply(tel("T2", 43100, -1200), now_ts=43100)             # T2 замолчала

    # обе позиции старше 30 с, но занявшая машина не исключается (ТЗ, п. 5)
    q = yard.queue("S1", now=43200)
    assert [e.unit_uuid for e in q.entries] == ["T1"]


def test_to_unload_returns_via_unload_point(cfg, yard):
    # U1 закреплена за S2: обслужилась, вышла из радиуса, едет к разгрузке
    yard.apply(tel("U1", 100, 1990, speed_kmh=0), now_ts=100)
    yard.apply(tel("U1", 101, 1900), now_ts=101)
    yard.apply(tel("U1", 102, 1800), now_ts=102)

    q = yard.queue("S2", now=102)
    # путь: до разгрузки 3800 м + от разгрузки до S2 4000 м = 7800 м / 10 = 780 с
    assert q.entries == (QueueEntry("U1", 102 + 780, 102 + 780, 102 + 1010, 0),)


def test_recommended_car_switches_queues(yard):
    yard.apply(tel("T4", 100, -1400), now_ts=100)      # едет к S1
    yard.cars["T4"].recommended_station = "S2"         # приняла рекомендацию

    q1 = yard.queue("S1", now=100)
    q2 = yard.queue("S2", now=100)
    assert all(e.unit_uuid != "T4" for e in q1.entries)   # из своей очереди ушла
    assert q2.entries == (
        QueueEntry("T4", 100 + 340, 100 + 340, 100 + 570, 0),  # до S2 3400 м
    )


def test_empty_station_queue(yard):
    assert yard.queue("S4", now=100).entries == ()
