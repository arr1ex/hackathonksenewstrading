"""Exchange access through ccxt: public market data, orders, and keeping the trades table in sync."""
import os

import ccxt

from . import risk, store

EXCHANGE = os.getenv("EXCHANGE", "bybit")
MODE = os.getenv("EXCHANGE_MODE", "demo")
KEY, SECRET = os.getenv("EXCHANGE_API_KEY"), os.getenv("EXCHANGE_SECRET")


def _client(auth):
    opts = {"enableRateLimit": True, "timeout": 30_000, "options": {"defaultType": "swap"}}
    if auth:
        opts |= {"apiKey": KEY, "secret": SECRET}
    ex = getattr(ccxt, EXCHANGE)(opts)
    if auth:
        # Only fake-money modes are supported on purpose.
        if MODE == "testnet":
            ex.set_sandbox_mode(True)
        else:
            ex.enable_demo_trading(True)
    return ex


pub = _client(False)  # mainnet public data: real prices, no key needed
ex = _client(True) if KEY and SECRET else None  # trading client; None = dry-run only


def markets():
    return (ex or pub).load_markets()


def resolve(tickers, kind="crypto"):
    """Model output like ["BNB", "pepe"] or ["NVDA"] -> symbols that really exist as USDT perpetuals."""
    m, out = markets(), []
    for t in tickers:
        base = str(t).upper().split("/")[0].strip()
        # Exchanges list small-priced coins as 1000X and tokenised stocks with an X suffix (NVDAX).
        names = (f"{base}X", base) if kind == "stock" else (base, f"1000{base}")
        for sym in (f"{n}/USDT:USDT" for n in names):
            if sym in m and m[sym].get("active", True) and sym not in out:
                out.append(sym)
                break
    return out


def live_positions():
    """Symbols with an open position on the exchange."""
    if not ex:
        return set()
    try:
        positions = ex.fetch_positions()
    except ccxt.NetworkError:
        positions = ex.fetch_positions()  # one retry: demo endpoints time out now and then
    return {p["symbol"] for p in positions if p.get("contracts")}


def open_symbols(dry):
    """Symbols we already hold. Paper mode never touches the private exchange API."""
    held = {t["symbol"] for t in store.trades("open")}
    return held if dry else held | live_positions()


def _set_leverage(leverage, symbol):
    try:
        ex.set_leverage(leverage, symbol)
    except ccxt.BadRequest as e:
        if "not modified" not in str(e).lower():  # Bybit errors when leverage is already at this value
            raise


def _cancel_brackets(symbol):
    """Remove leftover stop-loss / take-profit orders for a symbol."""
    for params in ({}, {"trigger": True}):  # Bybit keeps trigger orders in a separate list
        try:
            ex.cancel_all_orders(symbol, params)
        except Exception:
            pass  # nothing to cancel, or the exchange has no separate trigger list


def open_trade(event_id, symbol, plan: risk.Plan, price, dry):
    """Place the entry with stop-loss and take-profit, and record it. Raises on failure."""
    market = markets()[symbol]
    amount = float(pub.amount_to_precision(symbol, plan.amount))
    min_cost = (market["limits"].get("cost") or {}).get("min") or 0
    if amount <= 0 or amount * price < min_cost:
        raise ValueError(f"size ${plan.notional} is below the exchange minimum (${min_cost})")
    sl, tp = (float(pub.price_to_precision(symbol, p)) for p in (plan.sl, plan.tp))

    if not dry:
        if not ex:
            raise RuntimeError("no exchange keys in .env; switch dry-run on")
        buy, sell = ("buy", "sell") if plan.side == "long" else ("sell", "buy")
        _set_leverage(plan.leverage, symbol)
        order = ex.create_order(symbol, "market", buy, amount)
        price = order.get("average") or price
        try:
            ex.create_order(symbol, "market", sell, amount, None, {"stopLossPrice": sl, "reduceOnly": True})
            ex.create_order(symbol, "market", sell, amount, None, {"takeProfitPrice": tp, "reduceOnly": True})
        except Exception:
            # Never leave a position without a stop: close it and re-raise.
            ex.create_order(symbol, "market", sell, amount, None, {"reduceOnly": True})
            _cancel_brackets(symbol)
            raise

    store.add_trade(
        ts=pub.milliseconds(), event_id=event_id, symbol=symbol, side=plan.side, amount=amount,
        leverage=plan.leverage, notional=round(amount * price, 2), entry=price, sl=sl, tp=tp, dry=int(dry),
    )


def close_now(t, reason="manual"):
    """Close one open trade at market (manual close from the dashboard, or the time limit)."""
    price = pub.fetch_ticker(t["symbol"])["last"]
    if not t["dry"]:
        side = "sell" if t["side"] == "long" else "buy"
        ex.create_order(t["symbol"], "market", side, t["amount"], None, {"reduceOnly": True})
        _cancel_brackets(t["symbol"])
    store.close_trade(t["id"], price, risk.pnl(t["side"], t["entry"], price, t["notional"]), reason)


def sync_trades(max_hold_min):
    """Close paper trades that hit their stop/target, mark live trades the exchange has closed,
    and close anything held longer than max_hold_min (news impact fades)."""
    trades = store.trades("open")
    if not trades:
        return
    now = pub.milliseconds()
    live = live_positions() if any(not t["dry"] for t in trades) else set()
    failed = None
    for t in trades:
        try:
            _sync_one(t, live, now, max_hold_min)
        except Exception as e:  # one unreachable symbol must not leave the other positions unchecked
            failed = e
    if failed:
        raise failed


def _sync_one(t, live, now, max_hold_min):
    price = pub.fetch_ticker(t["symbol"])["last"]
    long = t["side"] == "long"
    exit_price = reason = None
    if t["dry"]:
        # ponytail: checked once per poll on last price, so a paper fill is exact at
        # the stop/target level even if price gapped through it between polls.
        if (price <= t["sl"]) if long else (price >= t["sl"]):
            exit_price, reason = t["sl"], "stop"
        elif (price >= t["tp"]) if long else (price <= t["tp"]):
            exit_price, reason = t["tp"], "target"
    elif t["symbol"] not in live:
        exit_price = price  # ponytail: approximate; the real fill is in the exchange history
        # ponytail: the exchange closed it; which bracket fired is inferred from the sign.
        reason = "target" if risk.pnl(t["side"], t["entry"], price, t["notional"]) > 0 else "stop"
        _cancel_brackets(t["symbol"])
    if exit_price:
        store.close_trade(t["id"], exit_price, risk.pnl(t["side"], t["entry"], exit_price, t["notional"]), reason)
    elif now - t["ts"] > max_hold_min * 60_000:
        close_now(t, "time")


CHECKPOINTS = {"p5": 5, "p15": 15, "p60": 60}  # minutes after a decision


def follow_up():
    """Record where price went after each directional decision, traded or skipped."""
    now = pub.milliseconds()
    for d in store.pending_followups():
        for col, minutes in CHECKPOINTS.items():
            due = d["ts"] + minutes * 60_000
            if d[col] is None and now >= due + 60_000:  # wait for that minute's candle to close
                try:
                    candles = pub.fetch_ohlcv(d["symbol"], "1m", since=due - 60_000, limit=1)
                except Exception:
                    break  # this symbol is unreachable right now; retried next poll, others continue
                if candles:
                    store.set_followup(d["id"], col, candles[0][4])
