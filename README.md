# News Trading Bot

Reads news feeds every minute, asks an LLM whether a headline is worth trading, and opens
futures positions on crypto (and optionally on major stocks) with a stop-loss and take-profit. Runs locally with a small dashboard.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your OpenRouter key (and exchange keys) in .env
python -m bot.app
```

Open http://127.0.0.1:8000. The bot starts in **dry run**: it makes paper trades at real prices
and sends nothing to the exchange. Type a headline into "Test a headline" to watch the whole
pipeline run.

## Going from paper trades to exchange orders

1. Put `EXCHANGE_API_KEY`, `EXCHANGE_SECRET` and `EXCHANGE_MODE` in `.env`.
2. Run `python -m scripts.check_exchange`. It shows your balance and places and cancels a tiny
   short order. If it fails, the key doesn't match the mode (see `.env.example`).
3. Restart the bot and untick "Dry run" in the dashboard.

Only fake-money modes (`demo`, `testnet`) are supported. The account must be in one-way
position mode (the default on Bybit and Binance), not hedge mode.

The exchange is Bybit by default; set `EXCHANGE=binanceusdm` in `.env` to use Binance instead.

## Adding news sources

Edit `feeds.yaml` only. RSS feeds need a name and a URL; JSON APIs need a small field mapping.
Examples are at the top of the file. The bot re-reads it on every poll, so no restart is needed,
and a broken feed shows up under Status on the dashboard without stopping the others.
API keys for sources go in `.env` and are referenced as `${NAME}` in `feeds.yaml`.

Current sources:

- https://cryptocurrency.cv/api/news (JSON API)
- https://www.coindesk.com/arc/outboundfeeds/rss/
- https://cointelegraph.com/rss
- https://decrypt.co/feed
- https://www.theblock.co/rss.xml

## How a headline becomes a trade

1. **Ingest** (`bot/ingest.py`): poll all feeds, drop headlines already seen or older than the age limit.
2. **Triage** (`bot/analyzer.py`): one cheap model call for the whole batch: is it market-moving, and for which coins?
   A story another source already brought is marked as a duplicate and goes no further.
3. **Validate** (`bot/trader.py`): keep only coins that exist as USDT perpetuals on the exchange.
4. **Market context**: price, 24h change, move since the headline, volume, funding rate.
5. **Decision**: the stronger model returns direction, confidence and whether the move is already priced in.
6. **Risk engine** (`bot/risk.py`): plain code sets size, leverage, stop and target from your settings.
   The model never chooses numbers.
7. **Order**: market entry plus stop-loss and take-profit. If the stop can't be placed, the position is closed at once.

## Stocks

The bot can also trade major stocks that the exchange lists as futures (for example NVDA), but this
is **switched off by default** ("Trade stocks" in Settings).

Known issue on the current Bybit demo account: Bybit requires an extra agreement to be signed before
trading stock contracts, and signing it fails with **error 10032**. Until that is resolved, stock
orders sent through the API are refused with error 110126 ("You must sign the required agreement
before trading this contract").

To try stocks again once the agreement can be signed: switch "Trade stocks" on, uncomment the
MarketWatch feeds in `feeds.yaml`, and run `python -m scripts.find_market NVDA` to confirm the
market is listed. With dry run on, stock paper trades work regardless.

## Exits

A position closes when it hits its stop-loss or take-profit, when you press Close, or when it has
been open longer than "Max holding time" (60 minutes by default), since the effect of news fades.

## Self-review

Every call the decision model makes is logged, traded or not, together with the market data it saw
and the price 5, 15 and 60 minutes later. After every 10 closed trades (configurable) the model
reviews them: what won, what lost, how positions were closed, and whether the calls it skipped
would have paid. It writes up to five lessons, which are shown on the Review page and passed to
the decision model with every later headline. Lessons only influence direction and confidence;
size, leverage and stops stay in code. "Run review now" on the Review page triggers one early.


##
Кононов Дмитро - Пошук API для новин
Онищенко Федір - Пошук API для новин та налаштування bybit API
Мельник Нікіта - Пошук дизайн референсів, prompt engineer
## Tests

```bash
python -m pytest
```
