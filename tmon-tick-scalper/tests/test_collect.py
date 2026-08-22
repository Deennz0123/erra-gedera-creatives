"""Тесты сборщика: разбор ответов API и формат JSONL — без сети."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

import tinvest_collect as tc

BOOK = {
    "bids": [{"price": {"units": "163", "nano": 20000000}, "quantity": "41200"},
             {"price": {"units": "163", "nano": 10000000}, "quantity": "88000"}],
    "asks": [{"price": {"units": "163", "nano": 30000000}, "quantity": "38900"}],
}


def trade(nano: int, direction: str, qty: str, at: str) -> dict:
    return {"price": {"units": "163", "nano": nano}, "quantity": qty,
            "direction": direction, "time": at}


def fake_api(book=BOOK, trades=()):
    def call(method, body, token):
        if method.endswith("FindInstrument"):
            return {"instruments": [{"ticker": "TMOS", "uid": "wrong", "name": "Другой"},
                                    {"ticker": "TMON", "uid": "uid-tmon", "name": "Денежный рынок"}]}
        if method.endswith("GetOrderBook"):
            return book
        if method.endswith("GetLastTrades"):
            return {"trades": list(trades)}
        raise AssertionError(method)
    return call


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(tc, "CALL", fake_api())


def fresh_state() -> tc.PollState:
    return tc.PollState(since=datetime.now(timezone.utc) - timedelta(seconds=60))


def test_quotation_converts_to_roubles():
    assert tc.money({"units": "163", "nano": 20000000}) == pytest.approx(163.02)
    assert tc.money(None) is None


def test_resolve_uid_matches_ticker_exactly():
    """По тикеру в проде работать нельзя — сборщик обязан вернуть uid."""
    assert tc.resolve_uid("t") == ("uid-tmon", "Денежный рынок")


def test_step_writes_book_record_in_analyzer_schema(monkeypatch):
    """Формат должен читаться queue_probe.py без изменений."""
    book, _, records = tc.step("t", "uid", fresh_state())
    assert book["bid"] == pytest.approx(163.02) and book["ask"] == pytest.approx(163.03)
    rec = records[0]
    assert rec["t"] == "book" and rec["bid_qty"] == 41200 and rec["ask_qty"] == 38900
    assert set(rec) >= {"t", "ts", "bid", "ask", "bid_qty", "ask_qty"}


def test_trade_direction_maps_to_side(monkeypatch):
    monkeypatch.setattr(tc, "CALL", fake_api(trades=[
        trade(30000000, "TRADE_DIRECTION_BUY", "500", "2026-08-22T09:58:00Z"),
        trade(20000000, "TRADE_DIRECTION_SELL", "700", "2026-08-22T09:58:01Z"),
    ]))
    _, _, records = tc.step("t", "uid", fresh_state())
    trades = [r for r in records if r["t"] == "trade"]
    assert [(t["side"], t["price"], t["qty"]) for t in trades] == [
        ("B", pytest.approx(163.03), 500), ("S", pytest.approx(163.02), 700)]
    assert [t["no"] for t in trades] == [1, 2]


def test_overlapping_polls_do_not_duplicate_trades(monkeypatch):
    """Окна опроса намеренно перекрываются, чтобы не терять сделки."""
    monkeypatch.setattr(tc, "CALL", fake_api(trades=[
        trade(30000000, "TRADE_DIRECTION_BUY", "500", "2026-08-22T09:58:00Z")]))
    st = fresh_state()
    first = [r for r in tc.step("t", "uid", st)[2] if r["t"] == "trade"]
    second = [r for r in tc.step("t", "uid", st)[2] if r["t"] == "trade"]
    assert len(first) == 1 and second == []


def test_empty_book_yields_no_records(monkeypatch):
    monkeypatch.setattr(tc, "CALL", fake_api(book={"bids": [], "asks": []}))
    book, _, records = tc.step("t", "uid", fresh_state())
    assert book is None and records == []


def test_watch_line_reports_spread_and_sides():
    fresh = [trade(30000000, "TRADE_DIRECTION_BUY", "500", "x"),
             trade(20000000, "TRADE_DIRECTION_SELL", "700", "x")]
    line = tc.watch_line({"bid": 163.02, "ask": 163.03, "bid_qty": 41200,
                          "ask_qty": 38900, "ts": 1_755_000_000.0}, fresh)
    assert "163.02 / 163.03" in line and "1 тик" in line
    assert "1200 бумаг" in line and "(1 по офферу, 1 по биду)" in line


# --- поиск токена ---------------------------------------------------------------

def test_token_comes_from_environment(monkeypatch):
    monkeypatch.setenv("TINVEST_TOKEN", "  t.from-env  ")
    assert tc.load_token() == "t.from-env"


def test_token_falls_back_to_dotenv(monkeypatch, tmp_path):
    """Запуск из терминала без export должен работать так же, как из VS Code."""
    monkeypatch.delenv("TINVEST_TOKEN", raising=False)
    project = tmp_path / "tmon-tick-scalper"
    (project / "research").mkdir(parents=True)
    (project / ".env").write_text(
        '# комментарий\nTINVEST_TOKEN="t.from-file"\nДРУГОЕ=1\n', encoding="utf-8")
    monkeypatch.setattr(tc, "__file__", str(project / "research" / "tinvest_collect.py"))
    assert tc.load_token() == "t.from-file"


def test_missing_token_explains_what_to_create(monkeypatch, tmp_path):
    monkeypatch.delenv("TINVEST_TOKEN", raising=False)
    monkeypatch.setattr(tc, "__file__", str(tmp_path / "research" / "tinvest_collect.py"))
    with pytest.raises(SystemExit) as e:
        tc.load_token()
    assert "TINVEST_TOKEN=" in str(e.value) and ".env" in str(e.value)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp1251"])
def test_dotenv_read_in_any_powershell_encoding(monkeypatch, tmp_path, encoding):
    """PowerShell 5.1 и 7 сохраняют файл по-разному — токен должен читаться из любого."""
    monkeypatch.delenv("TINVEST_TOKEN", raising=False)
    project = tmp_path / encoding
    (project / "research").mkdir(parents=True)
    (project / ".env").write_bytes("TINVEST_TOKEN=t.abc\n".encode(encoding))
    monkeypatch.setattr(tc, "__file__", str(project / "research" / "tinvest_collect.py"))
    assert tc.load_token() == "t.abc"
