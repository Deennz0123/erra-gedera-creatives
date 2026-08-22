"""Симулятор исполнения: прогоняет политику по собранному стакану.

Отвечает на главный вопрос проекта — сколько тиков за день реально снимается, —
и делает это единственным корректным способом: моделируя очередь.

Ключевая механика в том, что пассивная заявка исполняется не когда цена «дошла»,
а когда встречный поток выбрал весь объём, стоявший перед нами на этом уровне.
Поэтому у каждой нашей заявки есть счётчик `ahead`: сколько бумаг надо исполнить
до нас. Печати сделок по нашей цене его уменьшают, и только после обнуления
начинаем исполняться сами.

Бэктест по свечам этого не воспроизводит и потому показывает прибыль, которой нет.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .market import TICK, Book, FairValue, Order, Position, Side
from .policy import Cancel, Place, PolicyConfig, decide


@dataclass
class Fill:
    ts: float
    side: Side
    price: float
    qty: int
    aggressive: bool


@dataclass
class Resting:
    order: Order
    ahead: int
    """Объём впереди нас в очереди на этом уровне."""
    left: int
    """Сколько наших бумаг ещё не исполнено."""


@dataclass
class SimResult:
    fills: list[Fill] = field(default_factory=list)
    cycles: int = 0
    """Завершённых оборотов: продали слив и выкупили обратно."""
    flat_seconds: float = 0.0
    span_seconds: float = 0.0
    pnl: float = 0.0
    """Прибыль на весь слив, ₽, с учётом открытой позиции по справедливой цене."""
    vs_hold: float = 0.0
    """Избыток к «просто держать». Отрицательный — стратегия проиграла бездействию."""
    sleeve: int = 0

    @property
    def ticks_per_share(self) -> float:
        """Главная метрика: сколько тиков снято на бумагу сверх удержания."""
        return self.vs_hold / TICK / self.sleeve if self.sleeve else 0.0

    @property
    def cycles_per_day(self) -> float:
        days = self.span_seconds / 3600 / 8.5
        return self.cycles / days if days else 0.0

    @property
    def flat_share(self) -> float:
        return self.flat_seconds / self.span_seconds if self.span_seconds else 0.0

    def annualised_pct(self, price: float, days: int = 250) -> float:
        """Избыток в процентах годовых на капитал, равный сливу."""
        span_days = self.span_seconds / 3600 / 8.5
        capital = price * self.sleeve
        if not span_days or not capital:
            return 0.0
        return self.vs_hold / span_days * days / capital * 100


def simulate(records: list[dict], fair: FairValue, cfg: PolicyConfig) -> SimResult:
    """records — записи из сборщика: снапшоты стакана и печати сделок, в порядке времени."""
    res = SimResult()
    records = sorted(records, key=lambda r: r["ts"])
    if not records:
        return res

    resting: list[Resting] = []
    shares = cfg.sleeve          # базовое состояние — в фонде
    cash = 0.0
    flat_since: float | None = None
    book: Book | None = None
    prev_ts = records[0]["ts"]
    start_ts = records[0]["ts"]
    sold_since_cycle = False

    for rec in records:
        ts = rec["ts"]
        if shares < cfg.sleeve:
            res.flat_seconds += ts - prev_ts
        prev_ts = ts

        if rec["t"] == "trade":
            price, qty = rec["price"], int(rec["qty"])
            for r in list(resting):
                if abs(r.order.price - price) > TICK / 2:
                    continue
                take = min(r.ahead, qty)
                r.ahead -= take
                free = qty - take
                if free <= 0:
                    continue
                got = min(free, r.left)
                r.left -= got
                if r.order.side is Side.BUY:
                    shares += got
                    cash -= got * r.order.price
                    if sold_since_cycle and shares >= cfg.sleeve:
                        res.cycles += 1
                        sold_since_cycle = False
                else:
                    shares -= got
                    cash += got * r.order.price
                    sold_since_cycle = True
                res.fills.append(Fill(ts, r.order.side, r.order.price, got, False))
                if r.left <= 0:
                    resting.remove(r)
            continue

        book = Book(ts=ts, bid=rec["bid"], ask=rec["ask"],
                    bid_qty=int(rec.get("bid_qty", 0)), ask_qty=int(rec.get("ask_qty", 0)))
        if shares >= cfg.sleeve:
            flat_since = None
        elif flat_since is None:
            flat_since = ts

        for intent in decide(book, fair, Position(shares, 0), [r.order for r in resting],
                             ts, cfg, flat_since):
            if isinstance(intent, Cancel):
                resting = [r for r in resting if r.order is not intent.order]
            elif isinstance(intent, Place):
                if intent.aggressive:
                    # Переходим спред: исполняемся сразу, целиком, по цене заявки.
                    if intent.side is Side.BUY:
                        shares += intent.qty
                        cash -= intent.qty * intent.price
                        if sold_since_cycle and shares >= cfg.sleeve:
                            res.cycles += 1
                            sold_since_cycle = False
                    else:
                        shares -= intent.qty
                        cash += intent.qty * intent.price
                        sold_since_cycle = True
                    res.fills.append(Fill(ts, intent.side, intent.price, intent.qty, True))
                else:
                    ahead = book.bid_qty if intent.side is Side.BUY else book.ask_qty
                    resting.append(Resting(
                        Order(intent.side, intent.price, intent.qty, ts), ahead, intent.qty))

    end_ts = records[-1]["ts"]
    res.span_seconds = end_ts - start_ts
    f_start, f_end = fair.at(start_ts), fair.at(end_ts)
    res.pnl = cash + shares * f_end - cfg.sleeve * f_start
    res.vs_hold = res.pnl - cfg.sleeve * (f_end - f_start)
    res.sleeve = cfg.sleeve
    return res
