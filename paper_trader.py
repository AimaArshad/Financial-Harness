"""
Paper trading simulator (VIRTUAL MONEY ONLY) + the tester that checks every suggestion
before it is shown to the user.

Flow for each suggestion:
  1. harness fetches its own fresh data (never trusts numbers from the model)
  2. guardrails.plan_order() sizes the virtual order (risk cap, position cap, cash)
  3. the order is executed on a COPY of the paper account
  4. re-check limits + historical stress test (worst 5-day drop in the last 6 months)
  5. pass -> trade is committed to the paper account and the suggestion is approved
     fail -> suggestion is blocked with reasons (and is not shown as advice)
"""
import copy
import math
from datetime import datetime, timezone

import guardrails as g
from schemas import MAX_DATA_AGE_MINUTES, data_age_minutes

SLIPPAGE = 0.001  # 0.1% worse than the quoted price, like a real market order


def expected_fill(side: str, price: float) -> float:
    return round(price * (1 + SLIPPAGE) if side == "buy" else price * (1 - SLIPPAGE), 4)


class PaperAccount:
    def __init__(self, cash: float, positions: dict = None, realized_pnl: float = 0.0, trades: list = None):
        self.cash = float(cash)
        self.positions = positions or {}      # {"AAPL": {"shares": 10, "avg_cost": 200.0}}
        self.realized_pnl = realized_pnl
        self.trades = trades or []

    @classmethod
    def from_portfolio(cls, portfolio: dict) -> "PaperAccount":
        positions = {h["symbol"].upper(): {"shares": h["shares"], "avg_cost": h["avg_cost"]}
                     for h in portfolio["holdings"]}
        return cls(portfolio.get("cash", 0.0), positions)

    def copy(self) -> "PaperAccount":
        return copy.deepcopy(self)

    def commit(self, other: "PaperAccount"):
        self.cash, self.positions = other.cash, other.positions
        self.realized_pnl, self.trades = other.realized_pnl, other.trades

    def shares(self, symbol: str) -> float:
        return self.positions.get(symbol.upper(), {}).get("shares", 0)

    def value(self, prices: dict) -> float:
        return self.cash + sum(p["shares"] * prices[sym] for sym, p in self.positions.items())

    def execute(self, order: dict, price: float, mode: str = "paper") -> dict:
        """The ONLY function that moves (virtual) money. Real orders are refused here."""
        g.assert_paper_only(mode)
        sym, side, qty = order["symbol"].upper(), order["side"], order["shares"]
        fill = expected_fill(side, price)
        trade = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": "paper",
                 "symbol": sym, "side": side, "shares": qty, "fill_price": fill,
                 "value": round(qty * fill, 2)}
        if side == "buy":
            cost = qty * fill
            if cost > self.cash + 1e-9:
                raise ValueError(f"Not enough virtual cash: need {cost:.2f}, have {self.cash:.2f}")
            pos = self.positions.setdefault(sym, {"shares": 0, "avg_cost": 0.0})
            total_shares = pos["shares"] + qty
            pos["avg_cost"] = (pos["shares"] * pos["avg_cost"] + cost) / total_shares
            pos["shares"] = total_shares
            self.cash -= cost
        elif side == "sell":
            pos = self.positions.get(sym)
            if not pos or qty > pos["shares"]:
                raise ValueError(f"Cannot sell {qty} {sym}: only {pos['shares'] if pos else 0} held")
            trade["realized_pnl"] = round((fill - pos["avg_cost"]) * qty, 2)
            self.realized_pnl += (fill - pos["avg_cost"]) * qty
            pos["shares"] -= qty
            self.cash += qty * fill
            if pos["shares"] == 0:
                del self.positions[sym]
        else:
            raise ValueError(f"Unknown side {side!r}")
        self.trades.append(trade)
        return trade

    def summary(self, prices: dict) -> dict:
        rows = []
        for sym, p in self.positions.items():
            price = prices[sym]
            value = p["shares"] * price
            cost = p["shares"] * p["avg_cost"]
            rows.append({"symbol": sym, "shares": p["shares"], "avg_cost": round(p["avg_cost"], 2),
                         "price": round(price, 2), "value": round(value, 2),
                         "unrealized_pnl": round(value - cost, 2),
                         "unrealized_pnl_pct": round((value - cost) / cost * 100, 2) if cost else 0.0})
        return {"cash": round(self.cash, 2), "positions": rows,
                "realized_pnl": round(self.realized_pnl, 2), "total_value": round(self.value(prices), 2)}


def worst_n_day_return(closes: list, n: int = 5):
    """Worst n-day % return found in a list of closing prices (None if too few prices)."""
    if len(closes) <= n:
        return None
    return round(min((closes[i + n] / closes[i] - 1) * 100 for i in range(len(closes) - n)), 2)


async def test_suggestion(s: dict, account: PaperAccount, prices: dict, as_of: dict, data) -> dict:
    """Paper-test ONE suggestion. If it passes, the virtual trade is committed to `account`."""
    sym, action = s["symbol"].upper(), s["action"]
    report = {"symbol": sym, "action": action, "passed": False, "order": None, "reasons": [], "metrics": {}}

    if action == "HOLD":
        report.update(passed=True, note="No trade needed (HOLD).")
        return report

    # 1. fresh data, fetched by the harness (guardrail rule 3)
    try:
        if sym not in prices:
            px = await data.get_price(sym)
            prices[sym], as_of[sym] = float(px["price"]), px["as_of"]
        age = data_age_minutes(as_of[sym])
        if age > MAX_DATA_AGE_MINUTES:
            report["reasons"].append(f"{sym}: market data is {age:.0f} min old (max {MAX_DATA_AGE_MINUTES})")
            return report
    except Exception as e:
        report["reasons"].append(f"{sym}: could not get fresh market data ({e})")
        return report

    price = prices[sym]
    side = "sell" if action == "SELL" else "buy"
    fill = expected_fill(side, price)
    portfolio_value = account.value(prices)
    held = account.shares(sym)
    affordable = math.floor(account.cash / fill)

    # 2. harness sizes the order (guardrail rule 1)
    order, reasons, limits = g.plan_order(s, fill, portfolio_value, held * price, held, affordable)
    report["limits"] = limits
    if reasons:
        report["reasons"] = reasons
        return report

    # 3. stress data (buys only)
    worst = None
    if side == "buy":
        try:
            candles = (await data.get_candles(sym, "6mo", "1d"))["candles"]
            closes = [c["close"] for c in candles]
            if len(closes) >= g.MIN_CANDLES_FOR_STRESS:
                worst = worst_n_day_return(closes, g.STRESS_WINDOW_DAYS)
        except Exception as e:
            report["reasons"].append(f"{sym}: could not load price history for the stress test ({e})")
            return report
        if worst is None:
            report["reasons"].append(f"{sym}: not enough price history for the stress test")
            return report

    # 4. execute on a COPY (virtual money), then re-check everything
    trial = account.copy()
    try:
        trade = trial.execute(order, price, mode="paper")
    except (ValueError, g.GuardrailViolation) as e:
        report["reasons"].append(f"{sym}: {e}")
        return report

    value_after = trial.value(prices)
    weight_after = trial.shares(sym) * price / value_after * 100
    risk_pct = None
    if action == "BUY":
        risk_pct = trade["shares"] * (trade["fill_price"] - s["stop_loss"]) / portfolio_value * 100
    problems = g.check_after_trade(side, weight_after, risk_pct)

    stress_loss = None
    if side == "buy":
        stress_loss = weight_after / 100 * max(0.0, -worst)
        if stress_loss > g.MAX_STRESS_DRAWDOWN_PCT:
            problems.append(f"stress test failed: a repeat of the worst 5-day drop ({worst}%) would cost "
                            f"{stress_loss:.2f}% of the portfolio (max {g.MAX_STRESS_DRAWDOWN_PCT}%)")

    report["metrics"] = {
        "portfolio_value_before": round(portfolio_value, 2), "portfolio_value_after": round(value_after, 2),
        "position_weight_after_pct": round(weight_after, 2),
        "risk_at_stop_pct": None if risk_pct is None else round(risk_pct, 2),
        "worst_5d_return_pct": worst,
        "stress_loss_pct": None if stress_loss is None else round(stress_loss, 2),
    }
    if problems:
        report["reasons"] = [f"{sym}: {p}" for p in problems]
        return report

    account.commit(trial)  # approved: keep the virtual trade
    report.update(passed=True, order=trade)
    return report


async def review_suggestions(final: dict, data, portfolio: dict) -> dict:
    """Paper-test every suggestion. Returns the result with only APPROVED suggestions in 'suggestions'."""
    account = PaperAccount.from_portfolio(portfolio)
    prices, as_of = {}, {}
    for sym in account.positions:
        px = await data.get_price(sym)
        prices[sym], as_of[sym] = float(px["price"]), px["as_of"]
    before = account.summary(prices)

    approved, blocked = [], []
    for s in final["suggestions"]:
        report = await test_suggestion(s, account, prices, as_of, data)
        item = {**s, "paper_test": report}
        (approved if report["passed"] else blocked).append(item)

    out = {k: v for k, v in final.items() if k != "suggestions"}
    out["suggestions"] = approved
    out["blocked"] = blocked
    out["counts"] = {"submitted": len(final["suggestions"]), "approved": len(approved), "blocked": len(blocked)}
    out["paper_account"] = {"before": before, "after": account.summary(prices), "trades": account.trades}
    return out


def print_review(result: dict):
    """Readable console report."""
    c = result["counts"]
    print(f"\nPaper-test: {c['approved']} approved, {c['blocked']} blocked (of {c['submitted']} submitted)")
    for item in result["suggestions"]:
        t = item["paper_test"]
        o = t["order"]
        trade = f"virtual {o['side']} {o['shares']} @ {o['fill_price']}" if o else "no trade"
        print(f"  APPROVED  {item['symbol']:5} {item['horizon']:10} {item['action']:10} ({trade})")
    for item in result["blocked"]:
        print(f"  BLOCKED   {item['symbol']:5} {item['horizon']:10} {item['action']:10} -> {'; '.join(item['paper_test']['reasons'])}")
    a, b = result["paper_account"]["after"], result["paper_account"]["before"]
    print(f"\nVirtual account: value {b['total_value']} -> {a['total_value']}, cash {b['cash']} -> {a['cash']}, "
          f"realized P&L {a['realized_pnl']}")
