"""Базовые типы рынка и модель справедливой цены TMON."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

TICK = 0.01
MINUTES_PER_DAY = 1440.0


@dataclass(frozen=True)
class Book:
    """Верхушка стакана."""

    ts: float
    bid: float
    ask: float
    bid_qty: int = 0
    ask_qty: int = 0

    @property
    def spread_ticks(self) -> int:
        return round((self.ask - self.bid) / TICK)


@dataclass(frozen=True)
class Position:
    """Позиция делится на неприкосновенную базу и оборотный слив.

    База никогда не продаётся: она приносит доходность фонда и круглосуточно, и в
    выходные. Бот гоняет только слив.
    """

    shares: int
    base: int

    @property
    def sleeve(self) -> int:
        return self.shares - self.base


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Order:
    side: Side
    price: float
    qty: int
    placed_ts: float
    order_id: str = ""


class FairValue:
    """F(t) = a + b·t.

    Пай денежного рынка растёт на ставку РЕПО детерминированно, поэтому справедливая
    цена считается вперёд. Всё остальное в стратегии — производные от неё.
    """

    def __init__(self, a: float, b: float) -> None:
        self.a = a
        self.b = b

    @classmethod
    def from_model(cls, price: float, annual_rate: float, ts: float) -> "FairValue":
        """Оценка до калибровки: из цены и нетто-доходности фонда."""
        per_second = price * annual_rate / 365 / 86400
        return cls(a=price - per_second * ts, b=per_second)

    @classmethod
    def calibrate(cls, shifts: list[tuple[float, float]]) -> "FairValue":
        """Прямая по моментам смены уровня: в момент сдвига F совпадает с новым бидом."""
        if len(shifts) < 2:
            raise ValueError("нужно минимум две смены уровня")
        n = len(shifts)
        mx = sum(x for x, _ in shifts) / n
        my = sum(y for _, y in shifts) / n
        var = sum((x - mx) ** 2 for x, _ in shifts)
        if var == 0:
            raise ValueError("все сдвиги в один момент времени")
        b = sum((x - mx) * (y - my) for x, y in shifts) / var
        return cls(a=my - b * mx, b=b)

    def at(self, ts: float) -> float:
        return self.a + self.b * ts

    def phase(self, ts: float, bid: float) -> float:
        """Фаза режима: 0 — уровень только что открылся, 1 — вот-вот сменится.

        Единственный вход торговой логики. В начале режима оффер на тик выше
        справедливой цены, в конце — бид на тик ниже.
        """
        return min(max((self.at(ts) - bid) / TICK, 0.0), 0.999)

    @property
    def ticks_per_day(self) -> float:
        return self.b * 86400 / TICK

    @property
    def regime_minutes(self) -> float:
        """Сколько живёт один ценовой уровень. Оно же — стоимость тика в удержании."""
        return MINUTES_PER_DAY / self.ticks_per_day
