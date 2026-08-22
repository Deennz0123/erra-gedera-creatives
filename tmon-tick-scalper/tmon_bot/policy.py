"""Решающий слой: что бот хочет сделать со стаканом прямо сейчас.

Чистая функция от состояния — ни сети, ни времени, ни брокера, поэтому полностью
покрывается тестами. Исполнением занимается отдельный слой.

Базовое состояние — «в фонде», а не «в деньгах»: удержание TMON приносит ~13%
годовых бесплатно, поэтому цикл всегда начинается с продажи слива по офферу и
заканчивается его выкупом. Так стратегия не может оказаться хуже бездействия.
"""

from __future__ import annotations

from dataclasses import dataclass

from .market import TICK, Book, FairValue, Order, Position, Side


@dataclass(frozen=True)
class Place:
    side: Side
    price: float
    qty: int
    reason: str
    aggressive: bool = False


@dataclass(frozen=True)
class Cancel:
    order: Order
    reason: str


Intent = Place | Cancel


@dataclass
class PolicyConfig:
    sleeve: int
    """Сколько бумаг гоняем за цикл. База сверх этого не трогается."""

    rebuy_phase: float = 0.75
    """С какой фазы режима выкупаться агрессивно: там оффер уже сравнялся с F,
    и переход спреда почти ничего не стоит. Если не выкупиться до смены уровня,
    новый бид окажется равен цене продажи и цикл даст ноль."""

    max_flat_minutes: float = 200.0
    """Потолок времени во флэте. Один тик равен ~248 минутам начисления фонда,
    так что более долгий простой съедает прибыль цикла целиком."""

    min_drift_ticks_per_day: float = 1.0
    """Ниже этого дрейфа структуры режимов внутри сессии нет, и фаза — шум.
    Тогда начисление приходит разрывом между сессиями, простой внутри дня ничего
    не стоит, и выкуп обеспечивает только правило конца сессии."""

    session_end_ts: float | None = None
    flatten_before_end_min: float = 15.0
    """К концу сессии слив должен быть выкуплен: ночь и выходные вне позиции
    стоят 2,5 и 17 успешных циклов соответственно."""


def _find(resting: list[Order], side: Side) -> Order | None:
    return next((o for o in resting if o.side is side), None)


def decide(
    book: Book,
    fair: FairValue,
    position: Position,
    resting: list[Order],
    now: float,
    cfg: PolicyConfig,
    flat_since: float | None = None,
) -> list[Intent]:
    """Возвращает намерения, а не приказы: исполняющий слой сверит их с биржей."""
    intents: list[Intent] = []
    buy = _find(resting, Side.BUY)
    sell = _find(resting, Side.SELL)

    # Спред шире тика — модель режимов не описывает происходящее, выходим из рынка.
    if book.spread_ticks != 1:
        return [Cancel(o, "спред не равен одному тику") for o in resting]

    phase = fair.phase(now, book.bid)
    holding = position.sleeve >= cfg.sleeve
    flat_minutes = (now - flat_since) / 60 if flat_since is not None else 0.0

    # Конец сессии: слив обязан быть выкуплен, во флэт на ночь уходить нельзя.
    if cfg.session_end_ts is not None:
        left_min = (cfg.session_end_ts - now) / 60
        if left_min <= cfg.flatten_before_end_min:
            if sell:
                intents.append(Cancel(sell, "конец сессии, во флэт не уходим"))
            if not holding:
                if buy:
                    intents.append(Cancel(buy, "конец сессии, выкупаемся агрессивно"))
                intents.append(Place(Side.BUY, book.ask, cfg.sleeve,
                                     "выкуп слива до конца сессии", aggressive=True))
            return intents

    if holding:
        # Держим слив — стоим пассивной продажей по офферу. Не тейк-профитом:
        # он срабатывает по цене последней сделки, а она скачет между бидом и
        # оффером, поэтому сработает сразу и продаст по цене входа.
        if sell is None:
            intents.append(Place(Side.SELL, book.ask, cfg.sleeve, "продажа слива по офферу"))
        elif abs(sell.price - book.ask) > TICK / 2:
            intents.append(Cancel(sell, "уровень сдвинулся, переставляем оффер"))
        if buy is not None:
            intents.append(Cancel(buy, "слив уже в позиции"))
        return intents

    # Во флэте: выкупаемся. Пассивно, пока режим молодой и время есть.
    # Если дрейфа внутри сессии нет, торопиться некуда: справедливая цена стоит на
    # месте, флэт бесплатен, и в позицию достаточно вернуться к закрытию.
    drifts = fair.ticks_per_day >= cfg.min_drift_ticks_per_day
    urgent = drifts and (phase >= cfg.rebuy_phase or flat_minutes >= cfg.max_flat_minutes)
    if urgent:
        if buy is not None:
            intents.append(Cancel(buy, "переходим к агрессивному выкупу"))
        why = ("режим на исходе, переход спреда почти бесплатен"
               if phase >= cfg.rebuy_phase else "флэт дороже тика")
        intents.append(Place(Side.BUY, book.ask, cfg.sleeve, why, aggressive=True))
    elif buy is None:
        intents.append(Place(Side.BUY, book.bid, cfg.sleeve, "выкуп слива по биду"))
    elif abs(buy.price - book.bid) > TICK / 2:
        intents.append(Cancel(buy, "уровень сдвинулся, переставляем бид"))
    if sell is not None:
        intents.append(Cancel(sell, "слива в позиции нет"))
    return intents
