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
    """Режет историю на режимы: периоды, пока лучший бид стоит на одном уровне."""
    out = []
    for rec in books:
        if out and abs(out[-1]["bid"] - rec["bid"]) < TICK / 2:
            out[-1]["ts_close"] = rec["ts"]
            continue
        out.append({"bid": rec["bid"], "ts_open": rec["ts"], "ts_close": rec["ts"],
                    "qty_open": rec["bid_qty"]})
    return out


def _fit_fair(levels):
    """F(t) = a + b*t по точкам смены уровня: в момент сдвига F совпадает с новым бидом.

    Возвращает (a, b) или None, если сдвигов слишком мало для прямой.
    """
    pts = [(lvl["ts_open"], lvl["bid"]) for lvl in levels[1:]]   # первый режим обрезан началом сбора
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    var = sum((x - mx) ** 2 for x, _ in pts)
    if var == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in pts) / var
    return my - b * mx, b


def _phase_hist(vals, bins=5):
    """Гистограмма по фазе режима: 0 — начало, 1 — конец."""
    h = [0] * bins
    for v in vals:
        h[min(int(v * bins), bins - 1)] += 1
    return h


def _sessions(books, gap=3600):
    """Режет снапшоты на торговые сессии по разрывам во времени."""
    out = [[books[0]]]
    for prev, cur in zip(books, books[1:]):
        if cur["ts"] - prev["ts"] >= gap:
            out.append([])
        out[-1].append(cur)
    return out


def carry_check(books):
    """Решающий тест для стратегии бесплатного внутридневного плеча.

    Начисление фонда идёт круглосуточно. Вопрос в том, где оно проявляется в
    биржевой цене: равномерно внутри сессии или разрывом между сессиями. Плечо,
    закрываемое к вечеру, забирает только первую часть.
    """
    sess = _sessions(books)
    if len(sess) < 2:
        return None
    intraday = sum((s[-1]["bid"] - s[0]["bid"]) for s in sess)
    gaps = sum(b[0]["bid"] - a[-1]["bid"] for a, b in zip(sess, sess[1:]))
    total = intraday + gaps
    return {"sessions": len(sess), "intraday": intraday, "gaps": gaps,
            "share": intraday / total if total else 0.0,
            "hours": sum(s[-1]["ts"] - s[0]["ts"] for s in sess) / 3600 / len(sess)}


def analyze(path, size):
    books, trades = _load(path)
    if not books:
        sys.exit(f"{path}: нет снапшотов стакана")

    span_h = (books[-1]["ts"] - books[0]["ts"]) / 3600
    span_days = max(span_h / 8.5, 1e-9)                   # 8,5 торговых часов в дне
    levels = _levels(books)

    # --- спред
    spreads = defaultdict(int)
    for rec in books:
        spreads[round((rec["ask"] - rec["bid"]) / TICK)] += 1
    total = sum(spreads.values())

    print(f"окно: {span_h:.2f} ч ({span_days:.2f} торговых дня), "
          f"снапшотов {len(books)}, сделок {len(trades)}")
    print("\nспред, тиков:")
    for t in sorted(spreads):
        print(f"  {t:>3} — {100 * spreads[t] / total:5.1f}% времени")

    # --- режимы
    lifetimes = [l["ts_close"] - l["ts_open"] for l in levels[1:-1]]
    expected = TICK / (PRICE * RATE / 365 / 1440)          # минут на копейку начисления
    print(f"\nсмен уровня: {len(levels) - 1} ({(len(levels) - 1) / span_days:.1f} за сессию, "
          f"модель даёт ~{8.5 * 60 / expected:.1f})")
    if lifetimes:
        med = sorted(lifetimes)[len(lifetimes) // 2] / 60
        print(f"длина режима: медиана {med:.0f} мин (модель даёт {expected:.0f} мин)")

    fair = _fit_fair(levels)
    if not fair:
        sys.exit("\nсмен уровня меньше двух — F(t) не восстановить, нужен более длинный сбор")
    a, b = fair
    print(f"F(t) восстановлена: дрейф {b * 86400 / TICK:.1f} тика в сутки "
          f"(модель даёт {PRICE * RATE / 365 / TICK:.1f})")

    # --- внутридневной дрейф против ночного разрыва
    cc = carry_check(books)
    if cc is None:
        print("\nтест на внутридневной дрейф: нужен сбор минимум за две сессии")
    else:
        print(f"\nвнутридневной дрейф против ночного разрыва ({cc['sessions']} сессии "
              f"по {cc['hours']:.1f} ч):")
        print(f"  внутри сессий: {cc['intraday'] / TICK:+.1f} тика")
        print(f"  между сессиями: {cc['gaps'] / TICK:+.1f} тика")
        print(f"  доля начисления внутри сессии: {100 * cc['share']:.0f}%")
        carry = cc["share"] * RATE * 100
        print(f"  → бесплатное плечо, закрываемое к вечеру, приносит {carry:.1f}% годовых")
        print(f"    на заёмную сумму до стоимости входа-выхода (1 тик/день = 1,53% годовых)")
        if cc["share"] < 0.15:
            print("    ВНИМАНИЕ: начисление приходит разрывом, а не внутри сессии.")
            print("    Стратегия внутридневного плеча в этом случае не работает.")

    # --- пул уступок и отбор исполнений по фазе режима
    book_at = {r["ts"]: r for r in books}
    ts_sorted = sorted(book_at)
    pool = 0.0
    at_bid, at_ask = [], []
    seq = []
    for tr in trades:
        i = min(range(len(ts_sorted)), key=lambda k: abs(ts_sorted[k] - tr["ts"]))
        bk = book_at[ts_sorted[i]]
        f = a + b * tr["ts"]
        phase = max(0.0, min(0.999, (f - bk["bid"]) / TICK))
        if abs(tr["price"] - bk["ask"]) < TICK / 2:
            pool += (bk["ask"] - f) * tr["qty"]
            at_ask.append(phase)
            seq.append("A")
        elif abs(tr["price"] - bk["bid"]) < TICK / 2:
            pool += (f - bk["bid"]) * tr["qty"]
            at_bid.append(phase)
            seq.append("B")

    # --- потолок циклов: колебания печатей между бидом и оффером
    flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    ceiling = flips / 2 / span_days          # полный цикл — два перехода
    print(f"\nколебаний между бидом и оффером: {flips} ({flips / span_days:.0f} за сессию)")
    print(f"потолок циклов: {ceiling:.0f} в день = {ceiling * TICK * TRADING_DAYS / PRICE * 100:.1f}% "
          f"годовых, если бы исполнялась каждая заявка")
    print("  это ВЕРХНЯЯ ГРАНИЦА и она недостижима: каждое колебание — сделка с тем,")
    print("  кто стоял в очереди первым. Ваша доля считается ниже.")

    print("\nисполнения по фазе режима (0 — начало, 1 — конец):")
    for name, vals in (("по офферу", at_ask), ("по биду  ", at_bid)):
        if vals:
            h = _phase_hist(vals)
            bars = " ".join(f"{100 * c / max(sum(h), 1):4.0f}%" for c in h)
            print(f"  {name}: {bars}   всего {len(vals)}")
    print("  ожидание из модели: по офферу сдвиг вправо, по биду — влево (отбор против вас)")

    # --- доля пула и перевод в % годовых
    pool_day = pool / span_days
    depth = sorted(r["bid_qty"] for r in books)[len(books) // 2] or 1
    share = min(1.0, size / depth)
    capital = size * PRICE
    print(f"\nпул уступок: {pool_day:,.0f} ₽ в день на весь рынок".replace(",", " "))
    print(f"медианная глубина лучшего бида: {depth:,.0f} бумаг".replace(",", " "))
    print(f"ваш размер {size} бумаг → доля очереди {100 * share:.2f}%")
    print(f"расчётный заработок: {pool_day * share:,.0f} ₽ в день".replace(",", " ")
          + f" на капитал {capital:,.0f} ₽".replace(",", " "))
    print(f"  = {pool_day * share * TRADING_DAYS / capital * 100:+.2f}% годовых "
          f"сверх доходности фонда")
    print("  ВЕРХНЯЯ ОЦЕНКА: доля считается пропорционально размеру и игнорирует позицию")
    print("  в очереди. Реальный захват ниже — насколько, показывает только живой пилот.")
    print("\nкритерий этапа 1: меньше ~0,5% годовых → выгоднее просто держать фонд")


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
    a.add_argument("--size", type=int, default=6000,
                   help="размер вашей заявки в бумагах")

    args = ap.parse_args()
    if args.cmd == "collect":
        collect(args.path, args.seconds, args.interval)
    else:
        analyze(args.path, args.size)


if __name__ == "__main__":
    main()
