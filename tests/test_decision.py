"""Решатель рекомендаций: пример T4 из ТЗ, отказы, ничья, переключение очередей.

Момент 12:00:00 = 43200. Площадка из helpers: S1 в нуле, S2/S3/S4 через
2000 м к северу, разгрузка в 2000 м южнее S1.
"""
from __future__ import annotations

import pytest
from helpers import Yard, make_config, point_at, tel

from vq.config import Config
from vq.models import Decision, FsmEventKind as K


@pytest.fixture
def cfg():
    return make_config()


@pytest.fixture
def yard(cfg):
    return Yard(cfg)


def crossed(events):
    return next(e for e in events if e.kind is K.CROSSED_DECISION_RADIUS)


def queues_of(yard, cfg, now):
    return {s: yard.queue(s, now=now) for s in cfg.stations}


# --- пример из ТЗ: T4 → S2, выигрыш 80 с --------------------------------------


def test_tz_example_t4_recommended(cfg, yard):
    # очередь S1 из примера ТЗ: T1 заняла в 11:59:00, T2 и T3 едут
    yard.apply(tel("T1", 43140, -15, speed_kmh=0), now_ts=43140)
    yard.apply(tel("T2", 43200, -1200), now_ts=43200)
    yard.apply(tel("T3", 43200, -3000), now_ts=43200)

    # T4: первое сообщение в 900 м от S1 — внутри радиуса решения 1500 м
    events = yard.apply(tel("T4", 43200, -900), now_ts=43200)
    decision = yard.decider.decide(
        yard.cars["T4"], crossed(events), queues_of(yard, cfg, 43200), now=43200,
    )

    # ожидание у S1: освобождение 43370 (после T1) − приезд 43290 = 80 с;
    # у S2/S3/S4 очередей нет — ожидание 0; ближе всех свободная S2
    assert decision == Decision(
        "T4", 43200, "recommended", from_station="S1", to_station="S2", gain_seconds=80,
    )
    assert yard.cars["T4"].recommended_station == "S2"

    # после решения очередь перестраивается: T4 ушла из S1, встала в S2
    q1 = yard.queue("S1", now=43200)
    q2 = yard.queue("S2", now=43200)
    assert [e.unit_uuid for e in q1.entries] == ["T1", "T2", "T3"]
    assert [e.unit_uuid for e in q2.entries] == ["T4"]
    assert q2.entries[0].eta == 43200 + 290  # до S2 2900 м / 10 м/с


def test_t4_wait_not_grown_by_later_cars(cfg, yard):
    """T2 и T3 приедут после T4 — они не влияют на её ожидание у S1."""
    yard.apply(tel("T1", 43140, -15, speed_kmh=0), now_ts=43140)
    yard.apply(tel("T2", 43200, -1200), now_ts=43200)
    yard.apply(tel("T3", 43200, -3000), now_ts=43200)
    events = yard.apply(tel("T4", 43200, -900), now_ts=43200)
    decision = yard.decider.decide(
        yard.cars["T4"], crossed(events), queues_of(yard, cfg, 43200), now=43200,
    )
    assert decision.gain_seconds == 80  # не 260: T2/T3 позади T4 в очереди


# --- отказы ---------------------------------------------------------------------


def test_no_gain_when_other_station_wait_is_close():
    """У S2 ожидание 40 с — выигрыш 80 − 40 = 40 < 60 → отказ."""
    cfg2 = Config(  # компактная площадка: всего две станции
        stations={"S1": point_at(0), "S2": point_at(2000)},
        unload_point=point_at(-2000),
        assignments={"T1": "S1", "T2": "S1", "T3": "S1", "T4": "S1", "U1": "S2"},
    )
    yard = Yard(cfg2)
    yard.apply(tel("T1", 43140, -15, speed_kmh=0), now_ts=43140)
    yard.apply(tel("T2", 43200, -1200), now_ts=43200)
    yard.apply(tel("T3", 43200, -3000), now_ts=43200)
    # U1 едет к S2 (1000 м): займёт её в 43300 и освободит в 43530
    yard.apply(tel("U1", 43200, 3000), now_ts=43200)

    events = yard.apply(tel("T4", 43200, -900), now_ts=43200)
    decision = yard.decider.decide(
        yard.cars["T4"], crossed(events), queues_of(yard, cfg2, 43200), now=43200,
    )
    assert decision.result == "rejected"
    assert decision.reason == "no_gain"
    assert yard.cars["T4"].recommended_station is None
    assert yard.cars["T4"].decision_made

    # отказ не выкинул машину из очереди своей станции
    q1 = yard.queue("S1", now=43200)
    assert [e.unit_uuid for e in q1.entries] == ["T1", "T4", "T2", "T3"]


def test_stale_telemetry_rejected_before_calculation(cfg, yard):
    yard.apply(tel("T1", 43140, -15, speed_kmh=0), now_ts=43140)
    yard.apply(tel("T2", 43200, -1200), now_ts=43200)  # «сейчас» = 43200

    # опоздавшее сообщение T4: ts 43100 — старее «сейчас» на 100 с
    events = yard.apply(tel("T4", 43100, -900), now_ts=43200)
    decision = yard.decider.decide(
        yard.cars["T4"], crossed(events), queues_of(yard, cfg, 43200), now=43200,
    )
    # выигрыш был бы 80, но расчёт по протухшему сообщению недостоверен
    assert decision.result == "rejected"
    assert decision.reason == "stale_telemetry"
    assert yard.cars["T4"].recommended_station is None


# --- выбор при равенстве ---------------------------------------------------------


def test_tie_between_free_stations_picks_nearest():
    """S2 и S3 свободны — ожидание одинаковое, ближе та, что меньше круг."""
    cfg2 = Config(
        stations={"S1": point_at(0), "S2": point_at(2000), "S3": point_at(900)},
        unload_point=point_at(-2000),
        assignments={"T1": "S1", "T4": "S1"},
    )
    yard = Yard(cfg2)
    yard.apply(tel("T1", 43140, -15, speed_kmh=0), now_ts=43140)
    events = yard.apply(tel("T4", 43200, -900), now_ts=43200)
    decision = yard.decider.decide(
        yard.cars["T4"], crossed(events), queues_of(yard, cfg2, 43200), now=43200,
    )
    # до S3 1800 м, до S2 2900 м — при равном ожидании выбирается ближняя
    assert decision.to_station == "S3"
    assert decision.gain_seconds == 80


def test_own_station_wins_tie_yields_no_gain(cfg, yard):
    """Все станции свободны: ничья решается в пользу своей → выигрыш 0."""
    events = yard.apply(tel("T4", 43200, -900), now_ts=43200)
    decision = yard.decider.decide(
        yard.cars["T4"], crossed(events), queues_of(yard, cfg, 43200), now=43200,
    )
    assert decision.result == "rejected"
    assert decision.reason == "no_gain"
