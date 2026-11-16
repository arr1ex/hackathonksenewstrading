"""Check that the exchange keys in .env can trade futures. Run: python -m scripts.check_exchange

Shows the balance, then places and cancels a tiny BNB limit order far from the market price.
"""
from bot import trader

SYMBOL = "BNB/USDT:USDT"

if not trader.ex:
    raise SystemExit("EXCHANGE_API_KEY / EXCHANGE_SECRET are not set in .env")

ex = trader.ex
print(f"exchange={trader.EXCHANGE} mode={trader.MODE}")
try:
    print("USDT balance:", ex.fetch_balance()["USDT"])
    price = ex.fetch_ticker(SYMBOL)["last"]
    # A short (sell) limit 4% above market: proves the key can short, and will not fill.
    limit = float(ex.price_to_precision(SYMBOL, price * 1.04))
    amount = float(ex.amount_to_precision(SYMBOL, 10 / price))
    order = ex.create_order(SYMBOL, "limit", "sell", amount, limit)
    print(f"placed short limit order {order['id']}: {amount} BNB @ {limit}")
    ex.cancel_order(order["id"], SYMBOL)
    print("cancelled. These keys can trade futures: you can switch dry-run off in the dashboard.")
except Exception as e:
    print(f"\nFAILED: {type(e).__name__}: {e}")
    print(
        "\nIf this is an auth error, the key probably doesn't match EXCHANGE_MODE:\n"
        "  EXCHANGE_MODE=demo     keys created while in Demo Trading on the main site\n"
        "  EXCHANGE_MODE=testnet  keys from the separate testnet site\n"
        "Also check the key has contract/derivatives trading permission."
    )
    raise SystemExit(1)
