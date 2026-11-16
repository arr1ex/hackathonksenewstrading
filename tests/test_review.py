"""Decision log, follow-up prices, time exit and self-review, against a temp database and a fake exchange."""
import json
import sqlite3
import time

import pytest

from bot import analyzer, review, store, trader


class FakeExchange:
    price = 100.0

    def milliseconds(self):
        return int(time.time() * 1000)

    def fetch_ticker(self, symbol):
        return {"last": self.price}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        return [[since, 0, 0, 0, 103.0, 0]]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(trader, "pub", FakeExchange())
    monkeypatch.setattr(trader, "ex", None)
    store.init()


def trade(**over):
    t = dict(ts=int(time.time() * 1000), event_id=1, symbol="BNB/USDT:USDT", side="long", amount=1,
             leverage=3, notional=300.0, entry=100.0, sl=95.0, tp=110.0, dry=1)
    store.add_trade(**(t | over))


def decision(**over):
    d = dict(ts=int(time.time() * 1000), event_id=1, symbol="BNB/USDT:USDT", direction="long", confidence=0.8,
             priced_in=0, reason="r", ctx=json.dumps({"price": 100.0}), price=100.0, outcome="traded")
    store.add_decision(**(d | over))


def test_old_database_is_migrated(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ts INTEGER, event_id INTEGER, symbol TEXT, side TEXT,"
                " amount REAL, leverage INTEGER, notional REAL, entry REAL, sl REAL, tp REAL, dry INTEGER,"
                " status TEXT DEFAULT 'open', exit REAL, pnl REAL)")
    con.execute("INSERT INTO trades (symbol, status, pnl) VALUES ('X', 'closed', 5)")
    con.commit()
    monkeypatch.setattr(store, "DB_PATH", path)
    store.init()
    store.init()  # running it twice must be harmless
    assert store.stats()["unreviewed"] == 1


def test_time_exit_closes_old_trades_only(db):
    trade(ts=int(time.time() * 1000) - 61 * 60_000)
    trade(symbol="ETH/USDT:USDT")
    trader.sync_trades(max_hold_min=60)
    by_symbol = {t["symbol"]: t for t in store.trades()}
    assert by_symbol["BNB/USDT:USDT"]["close_reason"] == "time"
    assert by_symbol["ETH/USDT:USDT"]["status"] == "open"


def test_stop_and_target_are_labelled(db):
    trade()
    trader.pub.price = 94.0
    trader.sync_trades(60)
    t = store.trades()[0]
    assert (t["close_reason"], t["exit"], t["pnl"]) == ("stop", 95.0, -15.0)


def test_follow_up_fills_only_checkpoints_that_are_due(db):
    decision(ts=int(time.time() * 1000) - 20 * 60_000, outcome="skipped: confidence 0.50 below minimum 0.70")
    decision(direction="none")  # no direction, nothing to follow
    trader.follow_up()
    d = store.decisions()[-1]
    assert (d["p5"], d["p15"], d["p60"]) == (103.0, 103.0, None)
    assert review.move_pct(100, 103, "long") == 3.0 and review.move_pct(100, 103, "short") == -3.0


def test_review_waits_for_enough_trades_then_stores_lessons(db, monkeypatch):
    sent = {}

    def fake_llm(model, system, user, schema):
        sent["payload"] = json.loads(user)
        return schema(summary="Stops were hit quickly.", lessons=["  Lesson one  ", "", *["x"] * 9])

    monkeypatch.setattr(analyzer, "llm", fake_llm)
    decision()
    decision(direction="short", outcome="skipped: confidence 0.50 below minimum 0.70", p15=97.0)
    trade()
    trader.pub.price = 111.0
    trader.sync_trades(60)

    assert review.run() is None  # 1 closed trade, review_every is 10
    r = review.run(force=True)
    assert r["trades"] == 1 and r["lessons"][0] == "Lesson one" and len(r["lessons"]) == review.MAX_LESSONS
    p = sent["payload"]
    assert p["statistics"]["by_exit"] == {"target": {"count": 1, "pnl_usd": 30.0}}
    assert p["statistics"]["skipped_calls_right_after_15min"] == 1
    assert p["closed_trades"][0]["confidence"] == 0.8
    assert store.stats()["unreviewed"] == 0 and review.run(force=True) is None

    # The lessons now reach the decision prompt.
    monkeypatch.setattr(analyzer, "llm", lambda m, s, user, schema: sent.update(decide=json.loads(user)))
    analyzer.decide({"title": "t", "source": "s", "published_ms": time.time() * 1000}, "BNB/USDT:USDT", {}, False)
    assert sent["decide"]["lessons_from_past_reviews"][0] == "Lesson one"


def test_one_unreachable_symbol_does_not_block_the_others(db, monkeypatch):
    trade(symbol="DEAD/USDT:USDT")
    trade(symbol="ETH/USDT:USDT")
    trader.pub.price = 94.0
    real = trader.pub.fetch_ticker

    def flaky(symbol):
        if symbol.startswith("DEAD"):
            raise KeyError(symbol)
        return real(symbol)

    monkeypatch.setattr(trader.pub, "fetch_ticker", flaky, raising=False)
    with pytest.raises(KeyError):
        trader.sync_trades(60)
    assert {t["symbol"]: t["status"] for t in store.trades()} == {"DEAD/USDT:USDT": "open", "ETH/USDT:USDT": "closed"}


def test_resolve_finds_stocks_and_small_coins(monkeypatch):
    listed = ["BTC/USDT:USDT", "1000PEPE/USDT:USDT", "NVDAX/USDT:USDT", "TSLA/USDT:USDT"]
    monkeypatch.setattr(trader, "markets", lambda: {s: {"active": True} for s in listed})
    assert trader.resolve(["btc", "PEPE", "FAKE"]) == ["BTC/USDT:USDT", "1000PEPE/USDT:USDT"]
    assert trader.resolve(["NVDA", "TSLA", "AAPL"], "stock") == ["NVDAX/USDT:USDT", "TSLA/USDT:USDT"]
    assert trader.resolve(["NVDA"]) == []  # a crypto headline never lands on a stock token by accident
