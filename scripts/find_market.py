"""Show which futures markets the exchange lists for a ticker. Run: python -m scripts.find_market NVDA"""
import sys

from bot import trader

query = (sys.argv[1] if len(sys.argv) > 1 else "").upper()
if not query:
    raise SystemExit("usage: python -m scripts.find_market NVDA")
found = sorted(s for s, m in trader.markets().items() if m.get("swap") and query in s.split("/")[0])
print("\n".join(found) or f"No futures market contains {query}.")
print("\nThe bot would trade:", trader.resolve([query], "stock") or trader.resolve([query]) or "nothing")
