#!/usr/bin/env python3
"""Этап 1 из FEASIBILITY.md: измерение очереди заявок по TMON.

Отвечает на единственный вопрос, от которого зависит проект: сколько раз в день
пассивная заявка реально может быть исполнена, если стоять в общей очереди.

    collect  — писать снапшоты стакана и ленту сделок в JSONL;
    analyze  — посчитать по JSONL метрики очереди и перевести их в % годовых.

Источник данных — MOEX ISS (без ключа). Данные стакана там отдаются с задержкой,
для статистики очереди этого достаточно, но для проверки гонки за первое место в
очереди нужен realtime-поток T-Invest API (SubscribeOrderBook). Формат JSONL
одинаковый, так что analyze работает с обоими источниками.
"""

import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict

ISS = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQTF/securities/TMON"

PRICE = 163.01        # ₽ за бумагу, обновить перед запуском
TICK = 0.01           # ₽, шаг цены
RATE = 0.13           # нетто-доходность фонда, доля годовых
TRADING_DAYS = 250


# ---------------------------------------------------------------- collect

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tmon-queue-probe/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _rows(payload, block):
    cols = payload[block]["columns"]
    return [dict(zip(cols, row)) for row in payload[block]["data"]]


def collect(out_path, seconds, interval):
    """Пишет два типа записей: t=book (снапшот L1) и t=trade (печать сделки)."""
    deadline = time.time() + seconds
    last_trade_no = None
    written = 0

    with open(out_path, "a", buffering=1) as fh:
        while time.time() < deadline:
            now = time.time()
            try:
                book = _rows(_get(f"{ISS}/orderbook.json?iss.meta=off"), "orderbook")
                bids = [r for r in book if r.get("BUYSELL") == "B" and r.get("PRICE")]
                asks = [r for r in book if r.get("BUYSELL") == "S" and r.get("PRICE")]
                if bids and asks:
                    best_bid = max(bids, key=lambda r: r["PRICE"])
                    best_ask = min(asks, key=lambda r: r["PRICE"])
                    fh.write(json.dumps({
                        "t": "book", "ts": now,
                        "bid": best_bid["PRICE"], "bid_qty": best_bid["QUANTITY"],
                        "ask": best_ask["PRICE"], "ask_qty": best_ask["QUANTITY"],
                    }) + "\n")
                    written += 1

                url = f"{ISS}/trades.json?iss.meta=off"
                if last_trade_no is not None:
                    url += f"&tradeno={last_trade_no}&next_trade=1"
                for tr in _rows(_get(url), "trades"):
                    fh.write(json.dumps({
                        "t": "trade", "ts": now, "price": tr["PRICE"],
                        "qty": tr["QUANTITY"], "side": tr.get("BUYSELL"),
                        "no": tr["TRADENO"],
                    }) + "\n")
                    last_trade_no = tr["TRADENO"]
                    written += 1
            except Exception as exc:                      # сеть/ISS моргает — не роняем сбор
                print(f"warn: {exc}", file=sys.stderr)

            time.sleep(max(0.0, interval - (time.time() - now)))

    print(f"{out_path}: записей {written}")


# ---------------------------------------------------------------- analyze

def _load(path):
    books, trades = [], []
    seen = set()
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec["t"] == "book":
                books.append(rec)
            elif rec["no"] not in seen:                   # опрос перекрывается, дедуп по TRADENO
                seen.add(rec["no"])
                trades.append(rec)
    books.sort(key=lambda r: r["ts"])
    trades.sort(key=lambda r: r["ts"])
    return books, trades


def _levels(books):
    """Режет историю на периоды жизни лучшего бида: (bid, ts_open, ts_close, qty_at_open)."""
    out = []
    for rec in books:
        if out and abs(out[-1]["bid"] - rec["bid"]) < TICK / 2:
            out[-1]["ts_close"] = rec["ts"]
            continue
        out.append({"bid": rec["bid"], "ts_open": rec["ts"], "ts_close": rec["ts"],
                    "qty_open": rec["bid_qty"]})
    return out


def analyze(path):
    books, trades = _load(path)
    if not books:
        sys.exit(f"{path}: нет снапшотов стакана")

    span_h = (books[-1]["ts"] - books[0]["ts"]) / 3600
    span_days = max(span_h / 8.5, 1e-9)                   # 8,5 торговых часов в дне

    # 1. спред
    spreads = defaultdict(int)
    for rec in books:
        spreads[round((rec["ask"] - rec["bid"]) / TICK)] += 1
    total = sum(spreads.values())

    # 2. уровни лучшего бида и оборачиваемость очереди на каждом
    levels = _levels(books)
    turnovers, lifetimes = [], []
    for lvl in levels:
        executed = sum(tr["qty"] for tr in trades
                       if lvl["ts_open"] <= tr["ts"] <= lvl["ts_close"]
                       and abs(tr["price"] - lvl["bid"]) < TICK / 2)
        if lvl["qty_open"]:
            turnovers.append(executed / lvl["qty_open"])
        lifetimes.append(lvl["ts_close"] - lvl["ts_open"])

    # 3. циклов в день: цикл требует исполнения и по биду, и по офферу
    full_turns = [t for t in turnovers if t >= 1.0]
    cycles_per_day = len(full_turns) / span_days

    print(f"окно наблюдения: {span_h:.2f} ч ({span_days:.2f} торговых дня), "
          f"снапшотов {len(books)}, сделок {len(trades)}")
    print("\nспред, тиков:")
    for ticks in sorted(spreads):
        print(f"  {ticks:>3} — {100 * spreads[ticks] / total:5.1f}% времени")

    print(f"\nсмен уровня лучшего бида: {len(levels)} "
          f"({len(levels) / span_days:.1f} в день, ожидалось ~{PRICE * RATE / 365 / TICK:.1f})")
    if lifetimes:
        print(f"медианное время жизни уровня: {sorted(lifetimes)[len(lifetimes) // 2] / 60:.1f} мин")
    if turnovers:
        srt = sorted(turnovers)
        print(f"оборачиваемость очереди на биде: медиана {srt[len(srt) // 2]:.2f}, "
              f"максимум {srt[-1]:.2f}")
        print(f"  уровней, где очередь выбрана целиком: {len(full_turns)} из {len(turnovers)}")

    # 4. перевод в экономику из FEASIBILITY.md
    per_cycle = TICK * TRADING_DAYS / PRICE * 100         # % годовых за 1 цикл в день
    per_flat_h = PRICE * RATE / 365 / 1440 * 60 * TRADING_DAYS / PRICE * 100
    flat_h = 8.5 / 2                                      # допущение: половина сессии вне позиции
    print(f"\nоценка: {cycles_per_day:.1f} циклов в день")
    print(f"  +{cycles_per_day * per_cycle:.2f}% годовых за исполнения")
    print(f"  -{flat_h * per_flat_h:.2f}% годовых за {flat_h:.1f} ч/день вне позиции")
    print(f"  = {cycles_per_day * per_cycle - flat_h * per_flat_h:+.2f}% годовых "
          f"к «купил и держу»")
    print(f"\nпорог: нужно > {flat_h * per_flat_h / per_cycle:.1f} циклов в день, иначе "
          f"выгоднее просто держать фонд")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="писать стакан и сделки в JSONL")
    c.add_argument("path")
    c.add_argument("--seconds", type=int, default=8 * 3600)
    c.add_argument("--interval", type=float, default=1.0)

    a = sub.add_parser("analyze", help="посчитать метрики очереди по JSONL")
    a.add_argument("path")

    args = ap.parse_args()
    if args.cmd == "collect":
        collect(args.path, args.seconds, args.interval)
    else:
        analyze(args.path)


if __name__ == "__main__":
    main()
