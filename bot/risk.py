"""Risk engine. Plain code decides size, leverage and stops; the model only gives direction and confidence."""
from dataclasses import dataclass

from .store import Settings


@dataclass
class Plan:
    side: str  # "long" or "short"
    leverage: int
    notional: float  # position size in USD
    amount: float  # position size in coins (before exchange rounding)
    sl: float
    tp: float


def plan_trade(s: Settings, direction, confidence, already_priced_in, price, symbol, open_symbols):
    """Return a Plan, or a string explaining why the trade is rejected."""
    if direction not in ("long", "short"):
        return "model sees no trade"
    if already_priced_in:
        return "move already priced in"
    if confidence < s.min_confidence:
        return f"confidence {confidence:.2f} below minimum {s.min_confidence:.2f}"
    if symbol in open_symbols:
        return "already have a position in this symbol"
    if len(open_symbols) >= s.max_open_positions:
        return f"max open positions reached ({s.max_open_positions})"
    if not price or price <= 0:
        return "no price"

    # ponytail: margin scales with confidence, leverage is always the user's max.
    # Loss is still capped in USD by the stop below.
    leverage = s.max_leverage
    notional = s.max_trade_usd * confidence * leverage
    # Stop distance that loses exactly max_loss_usd, kept well inside the liquidation distance.
    sl_pct = min(s.max_loss_usd / notional, 0.8 / leverage)
    tp_pct = sl_pct * s.reward_risk
    sign = 1 if direction == "long" else -1
    return Plan(
        side=direction,
        leverage=leverage,
        notional=round(notional, 2),
        amount=notional / price,
        sl=price * (1 - sign * sl_pct),
        tp=price * (1 + sign * tp_pct),
    )


def pnl(side, entry, exit_price, notional):
    return round((exit_price / entry - 1) * notional * (1 if side == "long" else -1), 2)
