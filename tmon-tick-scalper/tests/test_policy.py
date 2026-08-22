"""Тесты решающего слоя. Каждый закрывает конкретную ловушку из FEASIBILITY.md."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tmon_bot import Book, Cancel, FairValue, Order, Place, PolicyConfig, Position, Side, decide

PRICE = 163.00
DAY = 86400.0


def fair(ticks_per_day: float = 5.8) -> FairValue:
    """F(t), проходящая через 163,00 в момент t=0 с заданным дрейфом."""
    return FairValue(a=PRICE, b=ticks_per_day * 0.01 / DAY)


def book(bid: float = 163.00, ask: float = 163.01, ts: float = 0.0) -> Book:
    return Book(ts=ts, bid=bid, ask=ask, bid_qty=40000, ask_qty=40000)


def cfg(**kw) -> PolicyConfig:
    return PolicyConfig(sleeve=10, **kw)


def at_phase(p: float, f: FairValue) -> float:
    """Момент времени, когда фаза режима равна p."""
    return p * 0.01 / f.b


def places(intents) -> list[Place]:
    return [i for i in intents if isinstance(i, Place)]


# --- модель справедливой цены -------------------------------------------------

def test_regime_length_equals_tick_cost():
    """Длина режима и стоимость тика в удержании — одно и то же число."""
    assert fair().regime_minutes == pytest.approx(248, abs=1)


def test_phase_runs_from_zero_to_one_across_regime():
    f = fair()
    assert f.phase(0.0, PRICE) == pytest.approx(0.0, abs=1e-6)
    assert f.phase(at_phase(0.5, f), PRICE) == pytest.approx(0.5, abs=1e-3)
    assert f.phase(at_phase(1.5, f), PRICE) == pytest.approx(0.999)


def test_calibrate_recovers_drift_from_level_shifts():
    f = fair()
    shifts = [(at_phase(k, f), PRICE + k * 0.01) for k in range(1, 5)]
    assert FairValue.calibrate(shifts).ticks_per_day == pytest.approx(5.8, rel=1e-6)


# --- базовое состояние «в фонде» ----------------------------------------------

def test_holding_rests_passive_sell_at_ask_not_take_profit():
    """Выход — лимит по офферу. Тейк-профит сработал бы по цене последней сделки,
    а она скачет между бидом и оффером, и продажа ушла бы по цене входа."""
    out = places(decide(book(), fair(), Position(shares=10, base=0), [], 0.0, cfg()))
    assert len(out) == 1
    assert (out[0].side, out[0].price, out[0].aggressive) == (Side.SELL, 163.01, False)


def test_flat_rests_passive_buy_at_bid_early_in_regime():
    f = fair()
    out = places(decide(book(), f, Position(0, 0), [], at_phase(0.2, f), cfg()))
    assert len(out) == 1
    assert (out[0].side, out[0].price, out[0].aggressive) == (Side.BUY, 163.00, False)


def test_no_order_is_ever_marketable_while_passive():
    """Пассивная покупка не должна стоять по офферу, продажа — по биду."""
    f = fair()
    for pos in (Position(0, 0), Position(10, 0)):
        for p in places(decide(book(), f, pos, [], at_phase(0.3, f), cfg())):
            if p.aggressive:
                continue
            assert (p.price < 163.01) if p.side is Side.BUY else (p.price > 163.00)


# --- выкуп до смены уровня ----------------------------------------------------

def test_late_regime_switches_to_aggressive_rebuy():
    """К концу режима оффер сравнялся с F: переход спреда почти бесплатен, а не
    выкупиться до сдвига значит получить новый бид, равный цене продажи, и ноль."""
    f = fair()
    out = places(decide(book(), f, Position(0, 0), [], at_phase(0.9, f), cfg()))
    assert len(out) == 1
    assert (out[0].side, out[0].price, out[0].aggressive) == (Side.BUY, 163.01, True)


def test_long_flat_switches_to_aggressive_rebuy():
    """Один тик стоит ~248 минут начисления: более долгий флэт съедает цикл."""
    f = fair()
    out = places(decide(book(), f, Position(0, 0), [], 60.0, cfg(), flat_since=-13000.0))
    assert out and out[0].aggressive


def test_stale_passive_buy_is_repriced_after_level_shift():
    f = fair()
    stale = Order(Side.BUY, 162.99, 10, placed_ts=0.0)
    out = decide(book(), f, Position(0, 0), [stale], at_phase(0.2, f), cfg())
    assert any(isinstance(i, Cancel) and i.order is stale for i in out)


# --- защита базы и границы сессии ---------------------------------------------

def test_base_position_is_never_offered():
    """Слив 10 бумаг при позиции 50 и базе 40 — продаём ровно слив."""
    out = places(decide(book(), fair(), Position(shares=50, base=40), [], 0.0, cfg()))
    assert len(out) == 1 and out[0].side is Side.SELL and out[0].qty == 10


def test_session_end_forces_rebuy_and_pulls_the_offer():
    """Ночь вне позиции стоит 2,5 цикла, выходные — 17."""
    f = fair()
    now = 1000.0
    sell = Order(Side.SELL, 163.01, 10, placed_ts=0.0)
    out = decide(book(ts=now), f, Position(0, 0), [sell], now, cfg(session_end_ts=now + 300))
    assert any(isinstance(i, Cancel) and i.order is sell for i in out)
    buy = places(out)
    assert len(buy) == 1 and buy[0].side is Side.BUY and buy[0].aggressive


def test_session_end_leaves_holding_position_alone():
    f = fair()
    now = 1000.0
    out = decide(book(ts=now), f, Position(10, 0), [], now, cfg(session_end_ts=now + 300))
    assert places(out) == []


def test_wide_spread_pulls_everything():
    """Спред шире тика — модель режимов не описывает рынок, уходим из стакана."""
    resting = [Order(Side.SELL, 163.05, 10, 0.0)]
    out = decide(book(bid=163.00, ask=163.05), fair(), Position(10, 0), resting, 0.0, cfg())
    assert out == [Cancel(resting[0], "спред не равен одному тику")]


# --- вырожденный случай: начисление приходит разрывом, а не внутри сессии ------

def flat_fair() -> FairValue:
    """F(t) без дрейфа внутри сессии: весь рост приходит гэпом между сессиями."""
    return FairValue(a=PRICE + 0.005, b=0.0)


def test_no_intraday_drift_means_no_regime_structure():
    assert flat_fair().regime_minutes == float("inf")


def test_no_drift_does_not_trigger_aggressive_rebuy_on_phase():
    """Фаза при нулевом дрейфе — шум, торопиться с переходом спреда незачем."""
    out = places(decide(book(), flat_fair(), Position(0, 0), [], 0.0, cfg()))
    assert len(out) == 1
    assert (out[0].side, out[0].price, out[0].aggressive) == (Side.BUY, 163.00, False)


def test_no_drift_makes_long_flat_free():
    """Справедливая цена стоит на месте — простой внутри дня ничего не стоит."""
    out = places(decide(book(), flat_fair(), Position(0, 0), [], 60.0, cfg(),
                        flat_since=-99000.0))
    assert len(out) == 1 and not out[0].aggressive


def test_no_drift_still_forces_rebuy_before_close():
    """Гэп между сессиями достаётся только тому, кто в позиции на закрытии."""
    now = 1000.0
    out = places(decide(book(ts=now), flat_fair(), Position(0, 0), [], now,
                        cfg(session_end_ts=now + 300)))
    assert len(out) == 1 and out[0].side is Side.BUY and out[0].aggressive
