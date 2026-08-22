"""Тесты симулятора очереди — ядро ответа «сколько тиков реально снимается»."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tmon_bot import FairValue, PolicyConfig, Side
from tmon_bot.simulator import simulate

PRICE = 163.00
DAY = 86400.0
SLEEVE = 10


def fair(ticks_per_day: float = 5.8) -> FairValue:
    return FairValue(a=PRICE, b=ticks_per_day * 0.01 / DAY)


def book(ts: float, bid_qty: int = 0, ask_qty: int = 0, bid: float = 163.00) -> dict:
    return {"t": "book", "ts": ts, "bid": bid, "ask": round(bid + 0.01, 2),
            "bid_qty": bid_qty, "ask_qty": ask_qty}


def trade(ts: float, price: float, qty: int) -> dict:
    return {"t": "trade", "ts": ts, "price": price, "qty": qty, "side": "B", "no": int(ts)}


def cfg(**kw) -> PolicyConfig:
    return PolicyConfig(sleeve=SLEEVE, **kw)


def test_empty_queue_completes_a_full_cycle():
    """Очередь пуста, встречный поток есть — продали по офферу, выкупили по биду."""
    recs = [book(0), trade(1, 163.01, SLEEVE), book(2), trade(3, 163.00, SLEEVE), book(4)]
    r = simulate(recs, fair(), cfg())
    assert r.cycles == 1
    assert [(f.side, f.price) for f in r.fills] == [(Side.SELL, 163.01), (Side.BUY, 163.00)]
    assert r.ticks_per_share == pytest.approx(1.0, abs=0.01)


def test_queue_ahead_blocks_the_fill():
    """Пока объём впереди не выбран, исполнения нет — сколько бы ни было сделок."""
    recs = [book(0, ask_qty=50_000), trade(1, 163.01, 1_000), book(2)]
    r = simulate(recs, fair(), cfg())
    assert r.fills == [] and r.cycles == 0


def test_fill_starts_only_after_the_queue_is_cleared():
    """Сделка, перекрывающая очередь, исполняет нас на остаток."""
    recs = [book(0, ask_qty=100), trade(1, 163.01, 104), book(2)]
    r = simulate(recs, fair(), cfg())
    assert [(f.side, f.qty) for f in r.fills] == [(Side.SELL, 4)]


def test_premium_is_exactly_cancelled_by_a_long_flat():
    """Главное тождество проекта: продажа выше справедливой цены даёт премию сразу,
    а простой съедает её по мере того, как справедливая цена догоняет. Ровно через
    один режим — 248 минут — счёт сравнивается, дальше уходит в минус."""
    f = fair()
    regime_s = f.regime_minutes * 60
    tail = [book(t) for t in range(60, int(2 * regime_s), 600)]
    r = simulate([book(0), trade(1, 163.01, SLEEVE)] + tail,
                 f, cfg(max_flat_minutes=10**6, rebuy_phase=2.0))
    assert r.cycles == 0
    assert r.flat_share > 0.9
    # захватили один тик, простояли два режима — минус примерно один тик на бумагу
    assert r.ticks_per_share == pytest.approx(-1.0, abs=0.05)


def test_break_even_flat_equals_one_regime():
    """Простой ровно в один режим обнуляет захваченный тик."""
    f = fair()
    tail = [book(t) for t in range(60, int(f.regime_minutes * 60), 600)]
    r = simulate([book(0), trade(1, 163.01, SLEEVE)] + tail,
                 f, cfg(max_flat_minutes=10**6, rebuy_phase=2.0))
    assert r.ticks_per_share == pytest.approx(0.0, abs=0.05)


def test_aggressive_rebuy_closes_the_cycle_near_the_level_change():
    """К концу режима переход спреда почти бесплатен — выкупаемся рыночной."""
    f = fair()
    late = 0.9 * 0.01 / f.b
    recs = [book(0), trade(1, 163.01, SLEEVE), book(late)]
    r = simulate(recs, f, cfg())
    assert r.cycles == 1
    assert r.fills[-1].aggressive and r.fills[-1].side is Side.BUY


def test_annualised_percent_scales_with_capital_not_sleeve_size():
    """Метрика на капитал не должна зависеть от того, сколько бумаг в обороте."""
    recs = [book(0), trade(1, 163.01, 100), book(2), trade(3, 163.00, 100), book(4 * 3600)]
    small = simulate(recs, fair(), PolicyConfig(sleeve=10))
    big = simulate(recs, fair(), PolicyConfig(sleeve=100))
    assert small.annualised_pct(PRICE) == pytest.approx(big.annualised_pct(PRICE), rel=1e-6)
