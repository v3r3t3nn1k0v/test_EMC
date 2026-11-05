"""Фикстуры тестов."""
from __future__ import annotations

import pytest

from helpers import make_config
from vq.config import Config
from vq.models import CarRuntime


@pytest.fixture
def cfg() -> Config:
    return make_config()


def make_car(cfg: Config, unit: str = "T1") -> CarRuntime:
    return CarRuntime(unit_uuid=unit, own_station=cfg.own_station(unit))
