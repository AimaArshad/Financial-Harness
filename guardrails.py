"""
Guardrails for the Financial Harness (pure functions, easy to test).

Rule 1  Max risk per trade   -> plan_order() sizes every order; check_after_trade() re-checks it
Rule 2  No real-money orders -> assert_paper_only() is the single gate every order passes through
Rule 3  Fresh, labelled data -> schemas.py (timestamps) + paper_trader.py (re-checks data age)
"""
import math

# ---- Limits (change them here; the tests read these values) ----
MAX_RISK_PER_TRADE_PCT = 2.0     # max loss if the stop-loss is hit, as % of portfolio value
MAX_POSITION_PCT = 30.0          # max share of the portfolio in one stock
MIN_STOP_DISTANCE_PCT = 1.0      # stop closer than this to the price is just noise
SELL_FRACTION = 0.5              # a SELL suggestion sells up to 50% of the shares held
ACCUMULATE_PCT = 5.0             # an ACCUMULATE suggestion buys up to 5% of the portfolio value
MAX_STRESS_DRAWDOWN_PCT = 5.0    # worst 5-day historical drop must not cost more than this % of the portfolio
STRESS_WINDOW_DAYS = 5
MIN_CANDLES_FOR_STRESS = 30
TOLERANCE_PCT = 0.05             # rounding slack when re-checking limits


class GuardrailViolation(Exception):
    pass


class RealTradingBlocked(GuardrailViolation):
    pass


def assert_paper_only(mode: str):
    """Rule 2. There is intentionally NO flag to turn real trading on."""
    if mode != "paper":
        raise RealTradingBlocked(
            f"Order mode {mode!r} refused: real-money execution is disabled. Only 'paper' orders are allowed.")


def plan_order(s: dict, fill: float, portfolio_value: float, position_value: float,
               shares_held: float, affordable: int):
    """Decide the virtual order for one suggestion.

    Returns (order_or_None, reasons, limits). The HARNESS chooses the size, not the model.
    `fill` is the expected fill price, `affordable` the max shares the virtual cash can buy.
    """
    action = s["action"]
    sym = s["symbol"].upper()
    if action == "HOLD":
        return None, [], {}

    if action == "SELL":
        if shares_held < 1:
            return None, [f"{sym}: nothing to sell (no shares held; short selling is not allowed)"], {}
        qty = max(1, math.floor(shares_held * SELL_FRACTION))
        return {"symbol": sym, "side": "sell", "shares": qty}, [], {"sell_fraction": SELL_FRACTION}

    # ---- BUY / ACCUMULATE ----
    room_value = portfolio_value * MAX_POSITION_PCT / 100 - position_value
    by_cap = max(0, math.floor(room_value / fill))
    by_cash = max(0, affordable)

    if action == "BUY":
        stop = s.get("stop_loss")
        if stop is None or stop <= 0 or stop >= fill:
            return None, [f"{sym}: BUY needs a stop_loss below the entry price (about {fill:.2f})"], {}
        distance = fill - stop
        if distance / fill * 100 < MIN_STOP_DISTANCE_PCT:
            return None, [f"{sym}: stop_loss is only {distance / fill * 100:.2f}% below the price; "
                          f"minimum is {MIN_STOP_DISTANCE_PCT}%"], {}
        by_rule = math.floor(portfolio_value * MAX_RISK_PER_TRADE_PCT / 100 / distance)
        rule_msg = f"risk cap ({MAX_RISK_PER_TRADE_PCT}% of portfolio) allows <1 share at this stop distance"
    else:  # ACCUMULATE
        by_rule = math.floor(portfolio_value * ACCUMULATE_PCT / 100 / fill)
        rule_msg = f"accumulate size ({ACCUMULATE_PCT}% of portfolio) is less than 1 share"

    limits = {"by_rule": by_rule, "by_position_cap": by_cap, "by_cash": by_cash}
    qty = min(by_rule, by_cap, by_cash)
    if qty < 1:
        if by_cap < 1:
            why = f"position is already at the {MAX_POSITION_PCT}% cap, no room to add"
        elif by_cash < 1:
            why = "not enough virtual cash"
        else:
            why = rule_msg
        return None, [f"{sym}: {action} blocked: {why}"], limits
    return {"symbol": sym, "side": "buy", "shares": qty}, [], limits


def check_after_trade(side: str, weight_after_pct: float, risk_pct):
    """Independent re-check of the executed virtual trade (defence in depth)."""
    reasons = []
    if side == "buy":
        if weight_after_pct > MAX_POSITION_PCT + TOLERANCE_PCT:
            reasons.append(f"position would be {weight_after_pct:.1f}% of the portfolio (max {MAX_POSITION_PCT}%)")
        if risk_pct is not None and risk_pct > MAX_RISK_PER_TRADE_PCT + TOLERANCE_PCT:
            reasons.append(f"risk at the stop-loss is {risk_pct:.2f}% of the portfolio (max {MAX_RISK_PER_TRADE_PCT}%)")
    return reasons
