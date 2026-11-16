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
            """
        )


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
    return {"events": events, "closed": closed}


def add_trade(**t):
    cols = ",".join(t)
    with db() as con:
        con.execute(f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(t))})", tuple(t.values()))


def trades(status=None, limit=100):
    q = "SELECT * FROM trades" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT ?"
    with db() as con:
        return [dict(r) for r in con.execute(q, (status, limit) if status else (limit,))]


def close_trade(trade_id, exit_price, pnl):
    with db() as con:
        con.execute("UPDATE trades SET status='closed', exit=?, pnl=? WHERE id=?", (exit_price, pnl, trade_id))
