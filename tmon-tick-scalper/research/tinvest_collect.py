#!/usr/bin/env python3
"""Сбор стакана и ленты сделок TMON через T-Invest API в реальном времени.

Запускается на машине владельца счёта: токен из этой сессии недоступен и не должен
её покидать. Пишет тот же JSONL, что читает queue_probe.py, поэтому анализ
запускается по собранному файлу без изменений.

Только стандартная библиотека — ставить ничего не нужно.

    export TINVEST_TOKEN=...
    python3 tinvest_collect.py schedule              # расписание торгов на сегодня
    python3 tinvest_collect.py watch                 # смотреть вживую
    python3 tinvest_collect.py collect day1.jsonl    # писать в файл

Токен нужен только на чтение. Права на торговлю на этом этапе не требуются.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

API = "https://invest-public-api.tinkoff.ru/rest/tinkoff.public.invest.api.contract.v1"
TICKER = "TMON"
TICK = 0.01
MSK = timezone(timedelta(hours=3))


def _read_text(path: Path) -> str:
    """Читает .env, не полагаясь на кодировку.

    Разные версии PowerShell сохраняют файл то в UTF-8, то в UTF-16, то в кодировке
    системы, и Set-Content по умолчанию ведёт себя по-разному в 5.1 и в 7.
    """
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def load_token() -> str:
    """Токен из переменной окружения, иначе из .env рядом с проектом.

    Так одинаково работают и запуск из VS Code (там .env подхватывает launch.json),
    и запуск из терминала без export.
    """
    token = os.environ.get("TINVEST_TOKEN", "").strip()
    if token:
        return token

    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in _read_text(env).splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == "TINVEST_TOKEN":
                token = value.strip().strip('"').strip("'")
                if token:
                    return token

    raise SystemExit(
        "Токен не найден.\n"
        f"Создайте файл {env}\n"
        "и впишите в него одну строку без кавычек и пробелов вокруг знака равенства:\n"
        "    TINVEST_TOKEN=ваш_токен"
    )


def http_call(method: str, body: dict, token: str) -> dict:
    """POST в REST-обёртку gRPC-API. Подменяется в тестах."""
    req = urllib.request.Request(
        f"{API}/{method}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise SystemExit(f"{method}: HTTP {e.code}\n{detail}") from None


CALL = http_call


def money(q: dict | None) -> float | None:
    """Quotation → рубли. {'units': '163', 'nano': 20000000} → 163.02"""
    if not q:
        return None
    return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1e9


def resolve_uid(token: str) -> tuple[str, str]:
    """Ищет TMON по тикеру. Работать в проде по тикеру нельзя — нужен uid."""
    found = CALL("InstrumentsService/FindInstrument",
                 {"query": TICKER, "apiTradeAvailableFlag": True}, token)
    for item in found.get("instruments", []):
        if item.get("ticker") == TICKER:
            return item["uid"], item.get("name", "")
    raise SystemExit(f"{TICKER} не найден. Ответ: {json.dumps(found)[:400]}")


def show_schedule(token: str, uid: str) -> None:
    """Отвечает на открытый вопрос: торгуется ли TMON в утреннюю сессию."""
    now = datetime.now(timezone.utc)
    resp = CALL("InstrumentsService/TradingSchedules",
                {"exchange": "MOEX",
                 "from": now.isoformat().replace("+00:00", "Z"),
                 "to": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")}, token)
    for exch in resp.get("exchanges", []):
        for day in exch.get("days", [])[:2]:
            if not day.get("isTradingDay"):
                print(f"{day.get('date', '')[:10]}: торгов нет")
                continue
            def hhmm(key: str) -> str:
                v = day.get(key)
                if not v:
                    return "—"
                return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(MSK).strftime("%H:%M")
            print(f"{day.get('date', '')[:10]} ({exch.get('exchange')}):")
            print(f"  утренняя:  {hhmm('premarketStartTime')} – {hhmm('premarketEndTime')}")
            print(f"  основная:  {hhmm('startTime')} – {hhmm('endTime')}")
            print(f"  вечерняя:  {hhmm('eveningStartTime')} – {hhmm('eveningEndTime')}")


def snapshot(token: str, uid: str, since: datetime) -> tuple[dict | None, list[dict], datetime]:
    """Один опрос: верхушка стакана и сделки с прошлого раза."""
    now = datetime.now(timezone.utc)
    ob = CALL("MarketDataService/GetOrderBook", {"instrumentId": uid, "depth": 20}, token)
    tr = CALL("MarketDataService/GetLastTrades",
              {"instrumentId": uid,
               "from": since.isoformat().replace("+00:00", "Z"),
               "to": now.isoformat().replace("+00:00", "Z")}, token)

    bids, asks = ob.get("bids", []), ob.get("asks", [])
    book = None
    if bids and asks:
        book = {"bid": money(bids[0]["price"]), "ask": money(asks[0]["price"]),
                "bid_qty": int(bids[0]["quantity"]), "ask_qty": int(asks[0]["quantity"]),
                "bid_levels": len(bids), "ask_levels": len(asks)}
    return book, tr.get("trades", []), now


@dataclass
class PollState:
    """Что переносится между опросами: окно времени, дедуп и нумерация сделок."""

    since: datetime
    seen: set = field(default_factory=set)
    counter: int = 0


def step(token: str, uid: str, st: PollState) -> tuple[dict | None, list[dict], list[dict]]:
    """Один опрос: возвращает стакан, новые сделки и готовые записи JSONL."""
    book, trades, now = snapshot(token, uid, st.since)

    fresh = []
    for t in trades:
        key = (t.get("time"), t.get("direction"), t.get("price", {}).get("units"),
               t.get("price", {}).get("nano"), t.get("quantity"))
        if key in st.seen:
            continue
        st.seen.add(key)
        fresh.append(t)
    if len(st.seen) > 20000:                           # окно дедупликации не растёт бесконечно
        st.seen = set(list(st.seen)[-5000:])
    st.since = now - timedelta(seconds=5)              # нахлёст, чтобы не терять сделки

    records: list[dict] = []
    if book:
        records.append({"t": "book", "ts": now.timestamp(), **book})
        for t in fresh:
            st.counter += 1
            records.append({"t": "trade", "ts": now.timestamp(), "price": money(t.get("price")),
                            "qty": int(t.get("quantity", 0)),
                            "side": "B" if t.get("direction", "").endswith("BUY") else "S",
                            "no": st.counter})
    return book, fresh, records


def watch_line(book: dict, fresh: list[dict]) -> str:
    at_ask = sum(1 for t in fresh if abs((money(t.get("price")) or 0) - book["ask"]) < TICK / 2)
    at_bid = sum(1 for t in fresh if abs((money(t.get("price")) or 0) - book["bid"]) < TICK / 2)
    vol = sum(int(t.get("quantity", 0)) for t in fresh)
    ts = datetime.fromtimestamp(book["ts"], MSK) if "ts" in book else datetime.now(MSK)
    return (f"{ts:%H:%M:%S}  {book['bid']:.2f} / {book['ask']:.2f}"
            f"   {round((book['ask'] - book['bid']) / TICK)} тик"
            f"   {book['bid_qty']:>7} / {book['ask_qty']:<7}"
            f"   {len(fresh):>3} шт, {vol:>7} бумаг"
            f"  ({at_ask} по офферу, {at_bid} по биду)")


def run(token: str, uid: str, out_path: str | None, interval: float, watch: bool) -> None:
    fh = open(out_path, "a", buffering=1) if out_path else None
    state = PollState(since=datetime.now(timezone.utc) - timedelta(seconds=60))
    printed_header = False

    while True:
        started = time.time()
        try:
            book, fresh, records = step(token, uid, state)
        except SystemExit:
            raise
        except Exception as exc:                       # сеть моргает — сбор не роняем
            print(f"warn: {exc}", file=sys.stderr)
            time.sleep(interval)
            continue

        if fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

        if watch and book:
            if not printed_header:
                print("  время    бид / оффер     спред  очередь бид/оффер   сделки за интервал")
                printed_header = True
            print(watch_line(book, fresh))

        time.sleep(max(0.0, interval - (time.time() - started)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schedule", help="расписание торгов TMON на сегодня и завтра")
    w = sub.add_parser("watch", help="печатать стакан и ленту вживую")
    w.add_argument("--interval", type=float, default=5.0)
    c = sub.add_parser("collect", help="писать стакан и ленту в JSONL")
    c.add_argument("path")
    c.add_argument("--interval", type=float, default=1.0)
    c.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    token = load_token()

    uid, name = resolve_uid(token)
    print(f"{TICKER} — {name}\ninstrument_uid: {uid}\n", file=sys.stderr)

    if args.cmd == "schedule":
        show_schedule(token, uid)
    elif args.cmd == "watch":
        run(token, uid, None, args.interval, watch=True)
    else:
        run(token, uid, args.path, args.interval, watch=not args.quiet)


if __name__ == "__main__":
    main()
