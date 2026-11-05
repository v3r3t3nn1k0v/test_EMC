"""Симулятор генератора: цикл машины, очередь на станции, перенаправление, поток.

Без брокера: тестируем _tick/_emit напрямую, сценарии детерминированы seed.
"""
from __future__ import annotations

import math

import pytest
from helpers import make_config

from vq.generator import GenSettings, Generator, _to_latlon, _to_xy

import random


@pytest.fixture
def cfg():
    return make_config()


def make_gen(cfg, **kw) -> Generator:
    settings = GenSettings(**kw)
    return Generator(cfg, settings, random.Random(7))


def run_until(gen: Generator, pred, limit=200_000) -> float:
    """Тикает симуляцию, пока pred не станет истинным; возвращает момент тика."""
    for _ in range(limit):
        t = gen.sim_t
        gen._tick(t)
        gen.sim_t = t + 1
        if pred():
            return t
    raise AssertionError("условие не достигнуто за пределы лимита")


def dist_to_station(gen: Generator, unit: str) -> float:
    car = gen.cars[unit]
    sx, sy = gen.stations[car.target].xy
    return math.hypot(sx - car.x, sy - car.y)


# --- проекция ------------------------------------------------------------------


def test_projection_round_trip(cfg):
    p = cfg.stations["S3"]
    x, y = _to_xy(p, cfg.unload_point)
    lat, lon = _to_latlon(x, y, cfg.unload_point)
    assert abs(lat - p.lat) < 1e-9
    assert abs(lon - p.lon) < 1e-9


def test_cars_start_between_unload_and_own_station(cfg):
    gen = make_gen(cfg)
    assert len(gen.cars) == len(cfg.assignments)
    for car in gen.cars.values():
        assert car.state == "to_station"
        assert car.target == car.own_station
        assert 0 < dist_to_station(gen, car.unit_uuid) < 5000


# --- цикл одной машины -----------------------------------------------------------


def test_single_car_releases_station_exactly_at_230(cfg):
    gen = make_gen(cfg)
    first = next(iter(gen.cars))
    gen.cars = {first: gen.cars[first]}  # одна машина на всю площадку

    occupy_t = run_until(gen, lambda: gen.stations[gen.cars[first].own_station].occupier == first)
    st = gen.stations[gen.cars[first].own_station]
    assert st.occupied_until == occupy_t + 230

    release_t = run_until(gen, lambda: st.occupier is None)
    assert release_t == occupy_t + 230           # освобождение по расписанию
    car = gen.cars[first]
    assert car.state == "maneuvering" or car.state == "to_unload"
    sx, sy = st.xy
    assert math.hypot(car.x - sx, car.y - sy) >= 49.5  # вышла из радиуса 50 м


def test_full_cycle_returns_to_own_station(cfg):
    gen = make_gen(cfg)
    first = next(iter(gen.cars))
    gen.cars = {first: gen.cars[first]}
    own = gen.cars[first].own_station

    run_until(gen, lambda: gen.stations[own].occupier == first)      # заняла
    run_until(gen, lambda: gen.stations[own].occupier is None)       # освободила
    run_until(gen, lambda: gen.cars[first].state == "to_unload")     # к разгрузке
    run_until(gen, lambda: gen.cars[first].state == "to_station")    # новый заезд
    assert gen.cars[first].target == own


# --- очередь на станции ------------------------------------------------------------


def test_second_car_waits_then_occupies(cfg):
    gen = make_gen(cfg)
    units = [u for u, s in cfg.assignments.items() if s == "S1"][:2]
    gen.cars = {u: gen.cars[u] for u in units}

    # момент: одна обслуживает, вторая ждёт
    both = run_until(
        gen,
        lambda: gen.stations["S1"].occupier is not None
        and len(gen.stations["S1"].waiting) == 1,
    )
    st = gen.stations["S1"]
    occupier, waiter = st.occupier, st.waiting[0]
    assert gen.cars[waiter].state == "waiting"

    # после освобождения займёт ждущая
    run_until(gen, lambda: st.occupier == waiter)
    assert both > 0


def test_waiting_cars_stay_inside_radius(cfg):
    gen = make_gen(cfg)
    units = [u for u, s in cfg.assignments.items() if s == "S1"][:4]
    gen.cars = {u: gen.cars[u] for u in units}

    run_until(gen, lambda: len(gen.stations["S1"].waiting) >= 2)
    sx, sy = gen.stations["S1"].xy
    for unit in gen.stations["S1"].waiting:
        car = gen.cars[unit]
        assert math.hypot(car.x - sx, car.y - sy) < 50.0


# --- перенаправление ----------------------------------------------------------------


def test_reroute_to_recommended_and_back_to_own(cfg):
    gen = make_gen(cfg)
    first = next(iter(gen.cars))
    gen.cars = {first: gen.cars[first]}
    car = gen.cars[first]

    # едет к своей — перенаправление работает только в этом состоянии
    assert gen.reroute(first, "S3") is True
    assert car.target == "S3"

    # обслужилась на S3, разгрузилась — снова к своей
    run_until(gen, lambda: gen.stations["S3"].occupier == first)
    run_until(gen, lambda: gen.stations["S3"].occupier is None)
    run_until(gen, lambda: car.state == "to_station")
    assert car.target == car.own_station


def test_reroute_rejected_while_servicing(cfg):
    gen = make_gen(cfg)
    first = next(iter(gen.cars))
    gen.cars = {first: gen.cars[first]}
    run_until(gen, lambda: gen.cars[first].state == "servicing")
    assert gen.reroute(first, "S3") is False


# --- поток телеметрии ----------------------------------------------------------------


def test_emit_buffers_all_cars_with_bounded_delay(cfg):
    gen = make_gen(cfg, duplicate_prob=0.0, late_max_s=25.0)
    gen._emit(1000)
    assert len(gen.buffer) == len(gen.cars)
    for send_at, key, payload in gen.buffer:
        assert 1000 <= send_at <= 1000 + 25.0
        assert payload["ts"] == 1000
        assert payload["unit_uuid"] == key
        assert {"unit_uuid", "ts", "lat", "lon", "speed_kmh"} == set(payload)


def test_duplicates_added(cfg):
    gen = make_gen(cfg, duplicate_prob=1.0)
    gen._emit(1000)
    assert len(gen.buffer) == 2 * len(gen.cars)  # каждая машина продублирована


def test_delay_walk_keeps_per_car_order(cfg):
    """Задержка блуждает плавно: сообщения одной машины приходят по порядку."""
    gen = make_gen(cfg, duplicate_prob=0.0, late_max_s=60.0)
    per_car: dict[str, list[tuple[float, int]]] = {}
    for t in range(1000, 1100):
        gen._emit(t)
        for send_at, key, payload in gen.buffer:
            per_car.setdefault(key, []).append((send_at, payload["ts"]))
        gen.buffer.clear()

    for key, msgs in per_car.items():
        sends = [s for s, _ in msgs]
        tss = [ts for _, ts in msgs]
        assert tss == sorted(tss) and len(set(tss)) == len(tss)  # ts строго растут
        assert sends == sorted(sends)  # и приходят в том же порядке — дедуп не убьёт


# --- сквозная сборка без брокера ---------------------------------------------------


def test_service_reconstructs_generator_picture(cfg):
    """Телеметрия генератора напрямую в процессор: занятость и ожидание
    восстанавливаются. Дубли и опоздания выключены — сравниваем честно."""
    import json

    from vq.serde import parse_telemetry
    from vq.service import Processor, Settings

    gen = make_gen(cfg, duplicate_prob=0.0, late_max_s=0.0)
    proc = Processor(cfg, Settings())

    for _ in range(600):  # прогрев генератора до смешанных фаз
        gen._tick(gen.sim_t)
        gen.sim_t += 1

    for _ in range(300):  # 5 минут симуляции сквозным потоком
        t = gen.sim_t
        gen._tick(t)
        gen._emit(t)
        msgs, gen.buffer = gen.buffer, []
        for _, _, payload in sorted(msgs, key=lambda m: m[1]):
            for out in proc.process_message(parse_telemetry(json.dumps(payload))):
                # замыкаем контур вручную: в реале это делает Kafka decision.v1
                if out.topic == "decision.v1" and out.payload["result"] == "recommended":
                    gen.reroute(out.payload["unit_uuid"], out.payload["to_station"])
        gen.sim_t += 1

    occupied = 0
    for s, gen_st in gen.stations.items():
        svc_st = proc.stations[s]
        if gen_st.occupier is not None:
            assert svc_st.occupied_by == gen_st.occupier, f"{s}: занявший не совпал"
            occupied += 1
        # все ждущие в генераторе видны сервису в очереди станции
        queue_units = {e.unit_uuid for e in proc._build_queues()[s].entries}
        assert set(gen_st.waiting) <= queue_units, f"{s}: потеряны ждущие"
    assert occupied > 0  # картинка живая: кто-то стоит на станциях
