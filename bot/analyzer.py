"""LLM steps: batch triage of headlines, market context, and the per-symbol trade decision."""
import json
import os
import time
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from .trader import pub

TRIAGE_MODEL = os.getenv("TRIAGE_MODEL", "anthropic/claude-haiku-5.5")
DECISION_MODEL = os.getenv("DECISION_MODEL", "anthropic/claude-sonnet-5.5")


class TriageItem(BaseModel):
    i: int
    relevant: bool = False
    symbols: list[str] = []


class Triage(BaseModel):
    items: list[TriageItem]


class Decision(BaseModel):
    direction: Literal["long", "short", "none"]
    confidence: float = Field(ge=0, le=1)
    already_priced_in: bool = False
    reason: str = ""


def llm(model, system, user, schema):
    """One OpenRouter call returning JSON validated against `schema`. Raises if it doesn't parse."""
    r = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}"},
        json={
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
        timeout=60,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    # Some models wrap JSON in prose or code fences; keep only the outermost object.
    return schema.model_validate_json(text[text.find("{"): text.rfind("}") + 1])


TRIAGE_SYSTEM = """You screen news headlines for a crypto futures trading bot.
A headline is relevant only if it is fresh, concrete news likely to move a specific asset's price by
more than about 1-2% within the next hour: hacks, exploits, listings and delistings, regulatory or legal
action, ETF decisions, large liquidations, protocol failures, major partnerships, surprise macro data.
Not relevant: opinion, price recaps, technical analysis, predictions, sponsored content, old news.
For relevant headlines give the affected assets as base tickers that trade as USDT perpetual futures
(e.g. "BNB", "SOL"). For market-wide news use "BTC". At most 2 symbols per headline.
Reply with JSON only: {"items": [{"i": <headline number>, "relevant": true|false, "symbols": ["..."]}]}
Include every headline number exactly once."""

DECISION_SYSTEM = """You are the decision step of a news-driven crypto futures trading bot.
Given one headline and live market data for one symbol, decide whether to open a position now.
Rules:
- Your training data is out of date. Trust the market data and the current time given to you.
- "move_since_headline_pct" shows how far price has already moved since the news broke. If that move
  already matches or exceeds the impact you would expect, set already_priced_in to true.
- direction "none" unless the news clearly and directly affects this symbol.
- confidence is the probability (0-1) that price moves further in your direction over the next hour.
  Be conservative: rumours, unconfirmed reports and vague news deserve low confidence.
- If a position is already open in this symbol, answer "none".
- You do not choose size, leverage or stops; code does that.
Reply with JSON only:
{"direction": "long"|"short"|"none", "confidence": 0.0-1.0, "already_priced_in": true|false, "reason": "<one sentence>"}"""


def triage(events):
    """events -> list of TriageItem aligned with the input (missing answers count as not relevant)."""
    listing = "\n".join(f"{i}. [{e['source']}] {e['title']}" for i, e in enumerate(events))
    result = {t.i: t for t in llm(TRIAGE_MODEL, TRIAGE_SYSTEM, listing, Triage).items}
    return [result.get(i, TriageItem(i=i)) for i in range(len(events))]


def market_context(symbol, headline_ts_ms):
    t = pub.fetch_ticker(symbol)
    candles = pub.fetch_ohlcv(symbol, "1m", since=headline_ts_ms - 60_000, limit=5)
    price_then = candles[0][4] if candles else None  # close around headline time
    try:
        funding = pub.fetch_funding_rate(symbol)["fundingRate"]
    except Exception:
        funding = None
    return {
        "price": t["last"],
        "change_24h_pct": t["percentage"],
        "move_since_headline_pct": round((t["last"] / price_then - 1) * 100, 2) if price_then else None,
        "volume_24h_usd": t["quoteVolume"],
        "funding_rate": funding,
    }


def decide(event, symbol, ctx, has_position) -> Decision:
    age_min = round((time.time() * 1000 - event["published_ms"]) / 60_000, 1)
    user = json.dumps(
        {
            "current_time_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
            "headline": event["title"],
            "source": event["source"],
            "headline_age_minutes": age_min,
            "symbol": symbol,
            "market": ctx,
            "position_already_open": has_position,
        },
        indent=1,
    )
    return llm(DECISION_MODEL, DECISION_SYSTEM, user, Decision)
