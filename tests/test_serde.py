"""Сериализация: форматы сообщений из ТЗ, ISO 8601, защита от битого входа."""
from __future__ import annotations

from datetime import datetime, timezone

from vq.models import Decision, QueueEntry, StationQueue
from vq.serde import decision_to_json, iso_z, parse_telemetry, queue_to_json

# 2026-09-15T12:00:00Z — дата из примеров ТЗ
T0 = int(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp())


def test_iso_z():
    """epoch-секунды → «2026-09-15T12:00:00Z»: формат времени из ТЗ, до буквы Z."""
    assert iso_z(T0) == "2026-09-15T12:00:00Z"
    assert iso_z(T0 + 170) == "2026-09-15T12:02:50Z"


def test_parse_telemetry_valid():
    """Образцовое сообщение из раздела «Данные» ТЗ разбирается без потерь."""
    raw = b'{"unit_uuid": "T-12", "ts": 1757930400, "lat": 49.1288, "lon": 142.6601, "speed_kmh": 21.4}'
    tel = parse_telemetry(raw)
    assert tel is not None
    assert tel.unit_uuid == "T-12"
    assert tel.ts == 1757930400
    assert tel.lat == 49.1288
    assert tel.speed_kmh == 21.4


def test_parse_telemetry_garbage_returns_none():
    """Битое сообщение не роняет сервис: parse отдаёт None, вызывающий пропустит.

    Четыре вида мусора: не-JSON вовсе, пустой объект, нечисловой ts,
    отсутствие полей. Одно такое сообщение в потоке не должно остановить
    обработку остальных.
    """
    assert parse_telemetry(b"not json") is None
    assert parse_telemetry(b"{}") is None  # нет обязательных полей
    assert parse_telemetry(b'{"unit_uuid": "T", "ts": "abc", "lat": 1, "lon": 1, "speed_kmh": 1}') is None
    assert parse_telemetry(b'{"unit_uuid": "T"}') is None


def test_queue_json_matches_tz_example():
    """Выход queue.v1 повторяет пример из ТЗ дословно.

    null в eta — только у обслуживаемой машины, времена — ISO 8601 UTC,
    wait_seconds — целые секунды. Потребитель топика ожидает ровно эту
    форму, любое лишнее поле — уже несовместимость.
    """
    q = StationQueue("S1", T0, entries=(
        QueueEntry("T1", None, T0 - 60, T0 + 170, 0),
        QueueEntry("T2", T0 + 120, T0 + 170, T0 + 400, 50),
        QueueEntry("T3", T0 + 300, T0 + 400, T0 + 630, 100),
    ))
    assert queue_to_json(q) == {
        "station_uuid": "S1",
        "at": "2026-09-15T12:00:00Z",
        "queue": [
            {
                "unit_uuid": "T1", "eta": None,
                "service_start": "2026-09-15T11:59:00Z",
                "free_at": "2026-09-15T12:02:50Z", "wait_seconds": 0,
            },
            {
                "unit_uuid": "T2", "eta": "2026-09-15T12:02:00Z",
                "service_start": "2026-09-15T12:02:50Z",
                "free_at": "2026-09-15T12:06:40Z", "wait_seconds": 50,
            },
            {
                "unit_uuid": "T3", "eta": "2026-09-15T12:05:00Z",
                "service_start": "2026-09-15T12:06:40Z",
                "free_at": "2026-09-15T12:10:30Z", "wait_seconds": 100,
            },
        ],
    }


def test_decision_json_recommended():
    """Формат рекомендации — как в примере ТЗ: from/to/gain_seconds."""
    d = Decision("T4", T0, "recommended",
                 from_station="S1", to_station="S2", gain_seconds=80)
    assert decision_to_json(d) == {
        "unit_uuid": "T4",
        "at": "2026-09-15T12:00:00Z",
        "result": "recommended",
        "from_station": "S1",
        "to_station": "S2",
        "gain_seconds": 80,
    }


def test_decision_json_rejected():
    """Формат отказа минимален, как в примере ТЗ: только причина.

    Внутри у Decision есть from_station и для отказов — полезно для логов,
    но наружу в decision.v1 он не идёт: контракт есть контракт.
    """
    d = Decision("T4", T0, "rejected", from_station="S1", reason="no_gain")
    assert decision_to_json(d) == {
        "unit_uuid": "T4",
        "at": "2026-09-15T12:00:00Z",
        "result": "rejected",
        "reason": "no_gain",
    }
