"""Exchange access through ccxt: public market data, orders, and keeping the trades table in sync."""
import os

import ccxt

from . import risk, store

EXCHANGE = os.getenv("EXCHANGE", "bybit")
MODE = os.getenv("EXCHANGE_MODE", "demo")
KEY, SECRET = os.getenv("EXCHANGE_API_KEY"), os.getenv("EXCHANGE_SECRET")


def _client(auth):
    opts = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
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


def resolve(tickers):
    """Model output like ["BNB", "pepe"] -> symbols that really exist as USDT perpetuals."""
    m, out = markets(), []
    for t in tickers:
        base = str(t).upper().split("/")[0].strip()
        for sym in (f"{base}/USDT:USDT", f"1000{base}/USDT:USDT"):
            if sym in m and m[sym].get("active", True) and sym not in out:
                out.append(sym)
                break
    return out


def live_positions():
    """Symbols with an open position on the exchange."""
    if not ex:
        return set()
    return {p["symbol"] for p in ex.fetch_positions() if p.get("contracts")}


def open_symbols():
    return {t["symbol"] for t in store.trades("open")} | live_positions()


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


def sync_trades():
    """Close paper trades that hit their stop/target; mark live trades the exchange has closed."""
    trades = store.trades("open")
    if not trades:
        return
    live = live_positions()
    for t in trades:
        price = pub.fetch_ticker(t["symbol"])["last"]
        long = t["side"] == "long"
        exit_price = None
        if t["dry"]:
            # ponytail: checked once per poll on last price, so a paper fill is exact at
            # the stop/target level even if price gapped through it between polls.
            if (price <= t["sl"]) if long else (price >= t["sl"]):
                exit_price = t["sl"]
            elif (price >= t["tp"]) if long else (price <= t["tp"]):
                exit_price = t["tp"]
        elif t["symbol"] not in live:
            exit_price = price  # ponytail: approximate; the real fill is in the exchange history
            _cancel_brackets(t["symbol"])
        if exit_price:
            store.close_trade(t["id"], exit_price, risk.pnl(t["side"], t["entry"], exit_price, t["notional"]))
