"""Web server + background trading loop. Run with: python -m bot.app"""
import hashlib
import pathlib
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import analyzer, ingest, risk, store, trader

status = {"last_tick": None, "error": None, "feed_errors": {}}
wake = threading.Event()
TRIAGE_BATCH = 30


def process(s: store.Settings):
    """Run every pending headline through triage -> context -> decision -> risk -> order."""
    now = time.time() * 1000
    fresh = []
    for e in store.new_events():
        if now - e["published_ms"] > s.max_news_age_min * 60_000:
            store.set_event(e["id"], "stale")
        else:
            fresh.append(e)
    fresh = fresh[-TRIAGE_BATCH:]  # ponytail: cap one batch per tick; the rest wait for the next
    if not fresh:
        return

    for e, t in zip(fresh, analyzer.triage(fresh)):
        try:
            symbols = trader.resolve(t.symbols) if t.relevant else []
            if not symbols:
                note = f"no tradable market for {t.symbols}" if t.relevant and t.symbols else ""
                store.set_event(e["id"], "irrelevant", note)
                continue
            notes, traded = [], False
            for symbol in symbols[:2]:
                held = trader.open_symbols(s.dry_run)
                ctx = analyzer.market_context(symbol, e["published_ms"])
                d = analyzer.decide(e, symbol, ctx, symbol in held)
                plan = risk.plan_trade(s, d.direction, d.confidence, d.already_priced_in, ctx["price"], symbol, held)
                head = f"{symbol.split('/')[0]}: {d.direction} {d.confidence:.2f} — {d.reason}"
                if isinstance(plan, str):
                    notes.append(f"{head} → skipped: {plan}")
                    continue
                try:
                    trader.open_trade(e["id"], symbol, plan, ctx["price"], s.dry_run)
                    notes.append(f"{head} → {'paper ' if s.dry_run else ''}trade opened")
                    traded = True
                except Exception as err:
                    notes.append(f"{head} → order failed: {err}")
            store.set_event(e["id"], "traded" if traded else "skipped", "\n".join(notes))
        except Exception as err:
            store.set_event(e["id"], "error", f"{type(err).__name__}: {err}"[:300])


def tick():
    s = store.settings()
    status["feed_errors"] = ingest.poll()
    try:
        trader.sync_trades()
    finally:  # an exchange hiccup while syncing must not stop new headlines being handled
        if not s.paused:
            process(s)


def loop():
    while True:
        try:
            tick()
            status["error"] = None
        except Exception as e:  # keep the loop alive whatever happens
            status["error"] = f"{type(e).__name__}: {e}"[:300]
        status["last_tick"] = int(time.time() * 1000)
        wake.wait(store.settings().poll_seconds)
        wake.clear()


@asynccontextmanager
async def lifespan(_):
    store.init()
    threading.Thread(target=loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
PAGE = pathlib.Path(__file__).with_name("dashboard.html")


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.read_text()


@app.get("/api/state")
def state():
    return {
        "settings": store.settings(),
        "events": store.recent_events(),
        "trades": store.trades(),
        "status": status | {
            "exchange": trader.EXCHANGE,
            "mode": trader.MODE,
            "has_keys": trader.ex is not None,
            "models": [analyzer.TRIAGE_MODEL, analyzer.DECISION_MODEL],
        },
    }


@app.post("/api/settings")
def update_settings(s: store.Settings):
    store.save_settings(s)
    wake.set()
    return s


@app.post("/api/trades/{trade_id}/close")
def close_trade(trade_id: int):
    t = next((t for t in store.trades("open") if t["id"] == trade_id), None)
    if not t:
        raise HTTPException(404, "trade is not open")
    try:
        trader.close_now(t)
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}"[:300])
    return {"ok": True}


class Headline(BaseModel):
    title: str


@app.post("/api/headline")
def inject(h: Headline):
    """Feed a headline in by hand (for demos and testing). It goes through the normal pipeline."""
    now = int(time.time() * 1000)
    uid = hashlib.sha1(f"manual{now}{h.title}".encode()).hexdigest()
    store.add_event(uid, "manual", h.title.strip(), "", now)
    wake.set()
    return {"ok": True}


if __name__ == "__main__":
    # Bound to localhost only: the dashboard has no login.
    uvicorn.run(app, host="127.0.0.1", port=8000)
