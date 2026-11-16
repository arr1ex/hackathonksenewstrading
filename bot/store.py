"""SQLite storage: user settings, headlines (events) and trades."""
import json
import os
import sqlite3
import time

from pydantic import BaseModel, Field

DB_PATH = os.getenv("BOT_DB", "bot.db")


class Settings(BaseModel):
    paused: bool = False
    dry_run: bool = True  # paper trades only; nothing is sent to the exchange
    max_trade_usd: float = Field(100, gt=0)  # max margin put into one trade
    max_leverage: int = Field(3, ge=1, le=50)
    max_loss_usd: float = Field(10, gt=0)  # max loss per trade; sets the stop-loss distance
    reward_risk: float = Field(2, gt=0)  # take-profit distance = stop distance x this
    min_confidence: float = Field(0.7, ge=0, le=1)
    max_open_positions: int = Field(3, ge=1)
    max_news_age_min: int = Field(15, ge=1)  # older headlines are ignored
    poll_seconds: int = Field(60, ge=10)
    max_hold_min: int = Field(60, ge=1)  # close a position that hit neither stop nor target by then
    review_every: int = Field(10, ge=1)  # run a self-review after this many closed trades


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, uid TEXT UNIQUE, ts INTEGER, source TEXT,
                title TEXT, url TEXT, published_ms INTEGER,
                status TEXT DEFAULT 'new', note TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY, ts INTEGER, event_id INTEGER, symbol TEXT, side TEXT,
                amount REAL, leverage INTEGER, notional REAL, entry REAL, sl REAL, tp REAL,
                dry INTEGER, status TEXT DEFAULT 'open', exit REAL, pnl REAL);
            -- One row per (headline, symbol) the decision model looked at, traded or not.
            -- p5/p15/p60 are the prices 5, 15 and 60 minutes later, filled in afterwards.
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY, ts INTEGER, event_id INTEGER, symbol TEXT, direction TEXT,
                confidence REAL, priced_in INTEGER, reason TEXT, ctx TEXT, price REAL, outcome TEXT,
                p5 REAL, p15 REAL, p60 REAL);
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY, ts INTEGER, trades INTEGER, summary TEXT, lessons TEXT);
            """
        )
        # Columns added after the first release; databases created before then get them here.
        for col in ("closed_ts INTEGER", "close_reason TEXT", "reviewed INTEGER DEFAULT 0"):
            try:
                con.execute(f"ALTER TABLE trades ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # already there


def settings() -> Settings:
    with db() as con:
        row = con.execute("SELECT value FROM kv WHERE key='settings'").fetchone()
    return Settings(**json.loads(row["value"])) if row else Settings()


def save_settings(s: Settings):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO kv VALUES ('settings', ?)", (s.model_dump_json(),))


def add_event(uid, source, title, url, published_ms) -> bool:
    """Insert a headline. Returns False if it was already seen."""
    with db() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO events (uid, ts, source, title, url, published_ms) VALUES (?,?,?,?,?,?)",
            (uid, int(time.time() * 1000), source, title, url, published_ms),
        )
        return cur.rowcount == 1


def source_known(source) -> bool:
    with db() as con:
        return con.execute("SELECT 1 FROM events WHERE source=? LIMIT 1", (source,)).fetchone() is not None


def new_events():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM events WHERE status='new' ORDER BY id")]


def set_event(event_id, status, note=""):
    with db() as con:
        con.execute("UPDATE events SET status=?, note=? WHERE id=?", (status, note, event_id))


def recent_events(limit=100):
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


def stats():
    """All-time numbers for the dashboard: headline counts by status, and closed trades in order."""
    with db() as con:
        events = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM events GROUP BY status")}
        closed = [dict(r) for r in con.execute(
            "SELECT ts, symbol, side, pnl FROM trades WHERE status='closed' ORDER BY id")]
        waiting = con.execute("SELECT COUNT(*) FROM trades WHERE status='closed' AND reviewed=0").fetchone()[0]
    return {"events": events, "closed": closed, "unreviewed": waiting}


def add_trade(**t):
    cols = ",".join(t)
    with db() as con:
        con.execute(f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(t))})", tuple(t.values()))


def trades(status=None, limit=100):
    q = "SELECT * FROM trades" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT ?"
    with db() as con:
        return [dict(r) for r in con.execute(q, (status, limit) if status else (limit,))]


def close_trade(trade_id, exit_price, pnl, reason):
    """reason: stop | target | time | manual"""
    with db() as con:
        con.execute(
            "UPDATE trades SET status='closed', exit=?, pnl=?, close_reason=?, closed_ts=? WHERE id=?",
            (exit_price, pnl, reason, int(time.time() * 1000), trade_id),
        )


def add_decision(**d):
    with db() as con:
        con.execute(f"INSERT INTO decisions ({','.join(d)}) VALUES ({','.join('?' * len(d))})", tuple(d.values()))


def decisions(limit=50):
    q = """SELECT d.*, e.title FROM decisions d LEFT JOIN events e ON e.id = d.event_id
           ORDER BY d.id DESC LIMIT ?"""
    with db() as con:
        return [dict(r) for r in con.execute(q, (limit,))]


def pending_followups():
    """Directional decisions from the last day that still miss a later price."""
    day_ago = int(time.time() * 1000) - 86_400_000
    q = """SELECT id, ts, symbol, p5, p15, p60 FROM decisions
           WHERE direction IN ('long', 'short') AND p60 IS NULL AND ts > ? ORDER BY id LIMIT 50"""
    with db() as con:
        return [dict(r) for r in con.execute(q, (day_ago,))]


def set_followup(decision_id, col, price):
    assert col in ("p5", "p15", "p60")
    with db() as con:
        con.execute(f"UPDATE decisions SET {col}=? WHERE id=?", (price, decision_id))


def unreviewed_trades(limit=30):
    """Closed trades no review has covered yet, with their headline and the decision behind them."""
    q = """SELECT t.*, e.title, e.source, d.confidence, d.reason, d.ctx, d.price AS decision_price,
                  d.p5, d.p15, d.p60
           FROM trades t
           LEFT JOIN events e ON e.id = t.event_id
           LEFT JOIN decisions d ON d.event_id = t.event_id AND d.symbol = t.symbol AND d.outcome = 'traded'
           WHERE t.status = 'closed' AND t.reviewed = 0 GROUP BY t.id ORDER BY t.id LIMIT ?"""
    with db() as con:
        return [dict(r) for r in con.execute(q, (limit,))]


def skipped_decisions(since_ms, limit=30):
    """Directional calls the risk engine rejected since `since_ms` (the trades not taken)."""
    q = """SELECT d.*, e.title FROM decisions d LEFT JOIN events e ON e.id = d.event_id
           WHERE d.outcome != 'traded' AND d.direction IN ('long', 'short') AND d.ts > ?
           ORDER BY d.id DESC LIMIT ?"""
    with db() as con:
        return [dict(r) for r in con.execute(q, (since_ms, limit))]


def add_review(summary, lessons, trade_ids):
    with db() as con:
        con.execute(
            "INSERT INTO reviews (ts, trades, summary, lessons) VALUES (?,?,?,?)",
            (int(time.time() * 1000), len(trade_ids), summary, json.dumps(lessons)),
        )
        con.executemany("UPDATE trades SET reviewed=1 WHERE id=?", [(i,) for i in trade_ids])


def reviews(limit=10):
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,))]
    for r in rows:
        r["lessons"] = json.loads(r["lessons"])
    return rows


def lessons():
    """The current lessons: the latest review's list (each review rewrites the whole list)."""
    latest = reviews(1)
    return latest[0]["lessons"] if latest else []
