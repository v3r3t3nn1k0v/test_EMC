"""FSM машины: таблица состояний из ТЗ, дубли и порядок, радиусы, сброс заезда."""
from __future__ import annotations

from conftest import make_car
from helpers import kinds, tel

from vq.car_fsm import CarFsm
from vq.models import CarState
from vq.models import FsmEventKind as K


# --- дубли, порядок, свежесть ---------------------------------------------


def test_duplicate_and_out_of_order_ignored(cfg):
    """Дубли и старые сообщения молча выбрасываются.

    T1 прислала сообщение с тем же ts, что уже учли, а затем — с меньшим
    (классическое опоздание «не в том порядке»). Оба должны уйти в IGNORED,
    причём позиция машины остаётся от последнего учтённого сообщения —
    дублем нельзя откатить машину назад. Сообщение с новым ts снова
    принимается без последствий.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    fsm.apply(car, tel("T1", 100, -3000), now_ts=100)

    assert kinds(fsm.apply(car, tel("T1", 100, -2900), now_ts=101)) == [K.IGNORED]  # дубль ts
    assert kinds(fsm.apply(car, tel("T1", 99, -2800), now_ts=101)) == [K.IGNORED]   # старое ts
    assert car.last_ts == 100
    assert car.last_pos[0] == tel("T1", 0, -3000).lat  # позиция не перезаписана

    assert kinds(fsm.apply(car, tel("T1", 101, -2900), now_ts=101)) == [K.UPDATED]
    assert car.last_ts == 101


def test_late_message_with_newer_ts_is_applied_and_marked_stale(cfg):
    """Опоздавшее сообщение с новым ts — не дубль: оно учитывается, но со знаком «протухшее».

    «Сейчас» уже убежало на 200 с, а сообщение пришло с ts=101. По ТЗ такое
    сообщение нужно обработать (его ts новее последнего учтённого), но все
    события от него помечаются stale — решатель по ним откажет с
    stale_telemetry, не доверяя старой позиции.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    fsm.apply(car, tel("T1", 100, -3000), now_ts=100)

    events = fsm.apply(car, tel("T1", 101, -2950), now_ts=200)  # опоздало, но ts новый
    assert kinds(events) == [K.UPDATED]
    assert all(e.stale for e in events)
    assert car.last_ts == 101


def test_stale_boundary_is_30_seconds(cfg):
    """Граница свежести — ровно 30 секунд, сравнение строгое.

    Сообщение возрастом 30 с ещё свежее, 31 с — уже протухшее. Тест стоит
    на страже off-by-one: «старее 30 с» не значит «30 с и старше».
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    assert not fsm.apply(car, tel("T1", 100, -3000), now_ts=130)[0].stale  # ровно 30 с
    assert fsm.apply(car, tel("T1", 131, -2950), now_ts=162)[0].stale     # 31 с


# --- таблица состояний ------------------------------------------------------


def test_first_message_far_defaults_to_station(cfg):
    """Первое сообщение машины вдали от станции: состояние по умолчанию «к станции».

    Сервис только что подключился к потоку, истории по машине нет — но уже
    по одному сообщению должно быть понятно, куда она движется.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    events = fsm.apply(car, tel("T1", 100, -3000, speed_kmh=36), now_ts=100)
    assert kinds(events) == [K.UPDATED]
    assert car.state is CarState.TO_STATION


def test_first_message_stopped_at_station(cfg):
    """Первое же сообщение — машина уже стоит у станции.

    Она мгновенно «на станции», и момент входа в радиус совпадает с ts
    этого сообщения (предыдущей позиции не было — считаем, что вошла сейчас).
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    events = fsm.apply(car, tel("T1", 100, 10, speed_kmh=0), now_ts=100)
    assert kinds(events) == [K.AT_STATION_ENTERED]
    assert car.state is CarState.AT_STATION
    assert car.entered_radius_at == 100


def test_entered_radius_recorded_even_while_moving(cfg):
    """Приезд ждущей машины — момент входа в радиус, даже если она вошла на ходу.

    T1 въехала в радиус 50 м на скорости 36 км/ч (101-я секунда) и лишь
    секундой позже остановилась. Если фиксировать только остановку, её
    ожидание «похорошеет» на секунду — а очередь должна считать от входа.
    Пока машина стоит в радиусе, момент входа не затирается.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    fsm.apply(car, tel("T1", 100, -100), now_ts=100)   # едет, до станции 100 м
    assert car.entered_radius_at is None

    fsm.apply(car, tel("T1", 101, -40), now_ts=101)    # вошла в радиус 50 м на ходу
    assert car.entered_radius_at == 101
    assert car.state is CarState.TO_STATION            # но не остановилась

    events = fsm.apply(car, tel("T1", 102, -30, speed_kmh=0), now_ts=102)
    assert kinds(events) == [K.AT_STATION_ENTERED]

    fsm.apply(car, tel("T1", 103, -30, speed_kmh=0), now_ts=103)
    assert car.entered_radius_at == 101                # момент входа не затёрся


def test_slow_maneuver_inside_radius_keeps_at_station(cfg):
    """Манёвр: машина уже не «стоит», но ещё внутри радиуса — она всё ещё «на станции».

    После обслуживания машина ползёт к выезду со скоростью 5 км/ч: порог
    «стоит» пройден, но граница 50 м — нет. Если бросить состояние на
    halfway, сервис решит, что машина уехала, и освободит станцию раньше
    времени.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    fsm.apply(car, tel("T1", 100, -30, speed_kmh=0), now_ts=100)

    events = fsm.apply(car, tel("T1", 101, -20, speed_kmh=5), now_ts=101)  # манёвр
    assert kinds(events) == [K.UPDATED]
    assert car.state is CarState.AT_STATION


def test_exit_station_radius_goes_to_unload(cfg):
    """Выход из радиуса 50 м — обслуживание закончено, машина «к разгрузке».

    Вместе со сменой состояния уходит и момент входа в радиус: он был нужен
    только для этой явки на станцию.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    fsm.apply(car, tel("T1", 100, -30, speed_kmh=0), now_ts=100)

    events = fsm.apply(car, tel("T1", 101, -100, speed_kmh=36), now_ts=101)
    assert kinds(events) == [K.LEFT_STATION_RADIUS]
    assert car.state is CarState.TO_UNLOAD
    assert car.entered_radius_at is None


def test_trip_through_unload_starts_new_trip_and_resets_flags(cfg):
    """Новый заезд начинается на выезде из радиуса разгрузки — и всё заездовое обнуляется.

    Машина прошла через разгрузку и снова едет к станции. Рекомендованная
    станция прошлого заезда больше не действует, «решение сделано» и
    «пересекала 1500 м» сбрасываются: следующий заезд считается начисто,
    и машина снова едет к своей. Без этого сброса рекомендация сработала
    бы один раз на всю жизнь машины.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    fsm.apply(car, tel("T1", 100, -30, speed_kmh=0), now_ts=100)          # на станции
    car.recommended_station = "S2"        # флаги «использованного» заезда
    car.decision_made = True
    car.was_within_decision_radius = True

    fsm.apply(car, tel("T1", 101, -100, speed_kmh=36), now_ts=101)        # → к разгрузке
    assert car.state is CarState.TO_UNLOAD

    assert kinds(fsm.apply(car, tel("T1", 102, -1960), now_ts=102)) == [K.UPDATED]  # в радиусе разгрузки
    events = fsm.apply(car, tel("T1", 103, -2060, speed_kmh=36), now_ts=103)       # вышла из радиуса
    assert kinds(events) == [K.NEW_TRIP]
    assert car.state is CarState.TO_STATION

    assert car.recommended_station is None
    assert not car.decision_made
    assert not car.was_within_decision_radius
    assert car.entered_radius_at is None


def test_no_station_transition_without_visiting_unload(cfg):
    """Выехала из радиуса станции — ещё не значит «к станции».

    Машина в 100 м от станции и в 100 м до разгрузки, но в радиусе разгрузки
    не была. Переключить её в «к станции» сейчас — значит посчитать возврат,
    которого не было: она должна оставаться «к разгрузке», пока реально
    не проедет через точку разгрузки.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    fsm.apply(car, tel("T1", 100, -30, speed_kmh=0), now_ts=100)

    fsm.apply(car, tel("T1", 101, -100, speed_kmh=36), now_ts=101)   # к разгрузке
    events = fsm.apply(car, tel("T1", 102, -2100), now_ts=102)       # 100 м не доезжая
    assert kinds(events) == [K.UPDATED]
    assert car.state is CarState.TO_UNLOAD   # в радиусе разгрузки не была


# --- целевая станция после рекомендации -------------------------------------


def test_arrival_detected_at_recommended_station_not_own(cfg):
    """После рекомендации расстояния меряются до рекомендованной станции.

    Машина перенаправлена на S2. Она останавливается в 20 м от СВОЕЙ S1 —
    это не «на станции»; останавливается в 10 м от S2 — вот теперь да.
    Тест охраняет баг первой версии FSM: там состояние проверялось только
    против своей станции, и перенаправленная машина зависала в «к станции»
    навсегда, никогда не заняв S2.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    car.recommended_station = "S2"

    events = fsm.apply(car, tel("T1", 100, 20, speed_kmh=0), now_ts=100)  # у своей S1
    assert kinds(events) == [K.UPDATED]
    assert car.state is CarState.TO_STATION            # не «на станции»

    events = fsm.apply(car, tel("T1", 101, 1990, speed_kmh=0), now_ts=101)  # 10 м от S2
    assert kinds(events) == [K.AT_STATION_ENTERED]
    assert car.state is CarState.AT_STATION


def test_no_decision_crossing_while_heading_to_recommended(cfg):
    """Едущей к рекомендованной станции радиус решения своей не интересен.

    Машина проходит в 1000 м от своей S1 — формально внутри радиуса 1500 м,
    но рекомендация в этом заезде уже принята. Второй расчёт перевернул бы
    её маршрут посреди дороги.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    car.recommended_station = "S2"

    # в 1000 м от своей S1 (внутри радиуса решения), но едет к S2
    events = fsm.apply(car, tel("T1", 100, 1000, speed_kmh=36), now_ts=100)
    assert kinds(events) == [K.UPDATED]


# --- радиус решения ----------------------------------------------------------


def test_decision_radius_crossed_once_per_trip(cfg):
    """Пересечение радиуса 1500 м — событие строго одно на заезд.

    Снаружи радиуса — тишина; первое сообщение внутри — сигнал для решателя;
    все следующие внутри — снова тишина. Без флага сигнал стрелял бы на
    каждом сообщении, а решатель — пересчитывал рекомендацию каждую секунду.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    fsm.apply(car, tel("T1", 100, -2000), now_ts=100)

    assert K.CROSSED_DECISION_RADIUS not in kinds(fsm.apply(car, tel("T1", 101, -1600), now_ts=101))
    events = fsm.apply(car, tel("T1", 102, -1400), now_ts=102)
    assert kinds(events) == [K.CROSSED_DECISION_RADIUS]
    assert events[0].station == "S1"

    # повторно за тот же заезд не срабатывает
    assert K.CROSSED_DECISION_RADIUS not in kinds(fsm.apply(car, tel("T1", 103, -1300), now_ts=103))


def test_first_message_inside_decision_radius_fires(cfg):
    """Машина появилась в потоке уже внутри радиуса 1500 м.

    Сервис перезапустили, а машины ездили: первого сообщения от машины нет
    «снаружи» радиуса. Её «впервые ближе 1500 м» — это первое же сообщение,
    и решатель должен получить сигнал сразу.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    events = fsm.apply(car, tel("T1", 100, -1000), now_ts=100)
    assert kinds(events) == [K.CROSSED_DECISION_RADIUS]


def test_no_crossing_while_at_station(cfg):
    """Стоящую на станции машину рекомендация не касается.

    Радиус решения пересекает только машина в состоянии «к станции»:
    та, что уже стоит на станции, никакого выбора не делает.
    """
    car, fsm = make_car(cfg), CarFsm(cfg)
    events = fsm.apply(car, tel("T1", 100, 10, speed_kmh=0), now_ts=100)  # сразу у станции
    assert kinds(events) == [K.AT_STATION_ENTERED]
    assert K.CROSSED_DECISION_RADIUS not in kinds(events)
