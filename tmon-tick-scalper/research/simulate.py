#!/usr/bin/env python3
"""Прогон торговой политики по собранному стакану.

    python3 simulate.py data/tmon.jsonl --sleeve 6000

Отвечает на главный вопрос проекта в тиках за день, а не в диапазонах. Считает по
очереди FIFO: пассивная заявка исполняется только после того, как встречный поток
выбрал весь объём, стоявший перед нами. Бэктест по свечам этого не воспроизводит.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tmon_bot import TICK, FairValue, PolicyConfig, Side
from tmon_bot.simulator import simulate


def load(path: str) -> list[dict]:
    out, seen = [], set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec["t"] == "trade":
                if rec["no"] in seen:
                    continue
                seen.add(rec["no"])
            out.append(rec)
    return sorted(out, key=lambda r: r["ts"])


def level_shifts(records: list[dict]) -> list[tuple[float, float]]:
    """Моменты смены лучшего бида: в них справедливая цена совпадает с новым уровнем."""
    shifts, last = [], None
    for rec in records:
        if rec["t"] != "book":
            continue
        if last is None or abs(rec["bid"] - last) > TICK / 2:
            if last is not None:
                shifts.append((rec["ts"], rec["bid"]))
            last = rec["bid"]
    return shifts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--sleeve", type=int, default=6000, help="бумаг в обороте")
    ap.add_argument("--rebuy-phase", type=float, default=0.75)
    ap.add_argument("--max-flat-minutes", type=float, default=200.0)
    args = ap.parse_args()

    records = load(args.path)
    if not records:
        sys.exit(f"{args.path}: пусто")

    shifts = level_shifts(records)
    if len(shifts) < 2:
        sys.exit(f"смен уровня всего {len(shifts)} — F(t) не восстановить, нужен более "
                 f"длинный сбор")
    fair = FairValue.calibrate(shifts)

    cfg = PolicyConfig(sleeve=args.sleeve, rebuy_phase=args.rebuy_phase,
                       max_flat_minutes=args.max_flat_minutes)
    res = simulate(records, fair, cfg)

    books = sum(1 for r in records if r["t"] == "book")
    price = fair.at(records[-1]["ts"])
    passive = sum(1 for f in res.fills if not f.aggressive)
    hours = res.span_seconds / 3600

    print(f"данные: {hours:.1f} ч, снапшотов {books}, сделок {len(records) - books}")
    print(f"F(t): дрейф {fair.ticks_per_day:.2f} тика в сутки, "
          f"режим живёт {fair.regime_minutes:.0f} мин")
    print(f"смен уровня: {len(shifts)}\n")

    print(f"исполнений: {len(res.fills)} ({passive} пассивных, "
          f"{len(res.fills) - passive} рыночных)")
    for side in (Side.SELL, Side.BUY):
        n = sum(1 for f in res.fills if f.side is side)
        print(f"  {'продажи' if side is Side.SELL else 'покупки':<9} {n}")
    print(f"завершённых циклов: {res.cycles} ({res.cycles_per_day:.1f} в день)")
    print(f"вне позиции: {100 * res.flat_share:.0f}% времени\n")

    print(f"захвачено: {res.ticks_per_share:+.2f} тика на бумагу сверх удержания")
    pct = res.annualised_pct(price)
    print(f"в процентах: {pct:+.2f}% годовых на капитал {price * args.sleeve:,.0f} ₽"
          .replace(",", " "))
    print(f"\nкритерий: меньше +0,5% годовых → выгоднее просто держать фонд")
    print("ВЕРДИКТ:", "стратегия оправдана" if pct >= 0.5 else "держать фонд и не трогать")


if __name__ == "__main__":
    main()
