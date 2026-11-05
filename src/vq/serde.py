
from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import Decision, StationQueue, Telemetry


def iso_z(ts: int) -> str:
    """epoch-секунды → ISO 8601 UTC, например 2026-09-15T12:00:00Z."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_telemetry(raw: bytes | str) -> Telemetry | None:
    """JSON-сообщение telemetry.v1 → Telemetry; None для битого."""
    try:
        obj = json.loads(raw)
        return Telemetry(
            unit_uuid=str(obj["unit_uuid"]),
            ts=int(obj["ts"]),
            lat=float(obj["lat"]),
            lon=float(obj["lon"]),
            speed_kmh=float(obj["speed_kmh"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def queue_to_json(q: StationQueue) -> dict:
    """Очередь станции → payload для queue.v1 (ключ station_uuid)."""
    return {
        "station_uuid": q.station_uuid,
        "at": iso_z(q.at),
        "queue": [
            {
                "unit_uuid": e.unit_uuid,
                "eta": None if e.eta is None else iso_z(e.eta),
                "service_start": iso_z(e.service_start),
                "free_at": iso_z(e.free_at),
                "wait_seconds": e.wait_seconds,
            }
            for e in q.entries
        ],
    }


def decision_to_json(d: Decision) -> dict:
    """Решение → payload для decision.v1 (ключ unit_uuid), формат из ТЗ."""
    if d.result == "recommended":
        return {
            "unit_uuid": d.unit_uuid,
            "at": iso_z(d.at),
            "result": "recommended",
            "from_station": d.from_station,
            "to_station": d.to_station,
            "gain_seconds": d.gain_seconds,
        }
    return {
        "unit_uuid": d.unit_uuid,
        "at": iso_z(d.at),
        "result": "rejected",
        "reason": d.reason,
    }
