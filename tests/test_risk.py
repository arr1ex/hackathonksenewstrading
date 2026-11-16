from bot.ingest import to_ms
from bot.risk import Plan, plan_trade, pnl
from bot.store import Settings

S = Settings(max_trade_usd=100, max_leverage=5, max_loss_usd=10, reward_risk=2, min_confidence=0.7, max_open_positions=2)


def plan(direction="short", conf=1.0, priced_in=False, price=600.0, symbol="BNB/USDT:USDT", held=()):
    return plan_trade(S, direction, conf, priced_in, price, symbol, set(held))


def test_short_sizes_and_brackets():
    p = plan()
    assert isinstance(p, Plan)
    assert p.notional == 500 and p.leverage == 5
    # Stop loses exactly max_loss_usd; target is reward_risk times further, on the other side.
    assert round((p.sl / 600 - 1) * p.notional, 6) == 10
    assert p.sl > 600 > p.tp
    assert round((600 - p.tp) / (p.sl - 600), 6) == 2


def test_long_is_mirrored():
    p = plan("long")
    assert p.sl < 600 < p.tp


def test_confidence_scales_size():
    assert plan(conf=0.8).notional == 400


def test_stop_stays_inside_liquidation():
    s = Settings(max_trade_usd=10, max_leverage=20, max_loss_usd=1000)
    p = plan_trade(s, "long", 1.0, False, 100.0, "X", set())
    assert p.sl > 100 * (1 - 1 / 20)  # liquidation is roughly 1/leverage away


def test_rejections():
    assert "no trade" in plan("none")
    assert "priced in" in plan(priced_in=True)
    assert "confidence" in plan(conf=0.5)
    assert "already" in plan(held=["BNB/USDT:USDT"])
    assert "max open" in plan(held=["A", "B"])
    assert "no price" in plan(price=0)


def test_pnl():
    assert pnl("short", 600, 594, 500) == 5.0
    assert pnl("long", 600, 594, 500) == -5.0


def test_timestamps_to_utc_ms():
    assert to_ms("2026-01-01T00:00:00Z") == 1767225600000
    assert to_ms("2026-01-01T02:00:00+02:00") == 1767225600000
    assert to_ms("2026-01-01T00:00:00") == 1767225600000  # naive = UTC
    assert to_ms(1767225600) == to_ms("1767225600000") == 1767225600000
    assert to_ms("garbage") is None and to_ms(None) is None
