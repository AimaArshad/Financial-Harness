"""
Tests for the paper trader and guardrails. NO internet, NO API key needed.
Run:  python test_guardrails.py
"""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import guardrails as g
from agent import build_tools
from paper_trader import PaperAccount, expected_fill, review_suggestions, test_suggestion as paper_test, worst_n_day_return

NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
PORTFOLIO = {"cash": 10000.0, "holdings": [
    {"symbol": "AAPL", "shares": 10, "avg_cost": 200.0},   # value 2000
    {"symbol": "TSLA", "shares": 5, "avg_cost": 250.0},    # value 1250
    {"symbol": "NVDA", "shares": 8, "avg_cost": 100.0},    # value 800
]}                                                          # total 14050


class StubData:
    """Fixed prices and candles so the numbers are predictable."""
    def __init__(self, prices=None, closes=None, as_of=None):
        self.prices = prices or {"AAPL": 200.0, "TSLA": 250.0, "NVDA": 100.0}
        self.closes = closes or [100 + i * 0.1 for i in range(120)]   # gentle uptrend
        self.as_of = as_of

    async def get_price(self, symbol):
        return {"symbol": symbol, "price": self.prices[symbol], "as_of": self.as_of or NOW()}

    async def get_candles(self, symbol, period="6mo", interval="1d"):
        return {"candles": [{"close": c} for c in self.closes]}


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {extra}" if not cond else ""))
    if not cond:
        raise SystemExit(1)


def sug(symbol, action, horizon="short_term", stop=None):
    return {"symbol": symbol, "action": action, "horizon": horizon, "confidence": "low",
            "reasoning": "test suggestion for the paper trader", "stop_loss": stop, "data_as_of": NOW()}


async def run_one(s, data=None, portfolio=PORTFOLIO):
    final = {"goal_horizon": "10 Months", "summary": "t", "suggestions": [s]}
    res = await review_suggestions(final, data or StubData(), portfolio)
    item = (res["suggestions"] or res["blocked"])[0]
    return res, item["paper_test"]


async def main():
    print("--- Rule 2: no real orders ---")
    acct = PaperAccount.from_portfolio(PORTFOLIO)
    try:
        acct.execute({"symbol": "AAPL", "side": "buy", "shares": 1}, 200.0, mode="real")
        check("real-mode order is refused", False)
    except g.RealTradingBlocked:
        check("real-mode order is refused (RealTradingBlocked)", True)
    check("no real order was recorded", len(acct.trades) == 0)
    names = [t["name"] for t in build_tools("direct", []) + build_tools("ptc", [])]
    check("agent has no order/trade tool", not any(("order" in n or "trade" in n) for n in names), str(names))
    forbidden = re.compile(r"\b(alpaca|ib_insync|robin_stocks|ccxt|tda|schwab|binance|oandapyV20)\b", re.I)
    offenders = [p.name for p in Path(__file__).parent.glob("*.py")
                 if p.name != Path(__file__).name and forbidden.search(p.read_text(encoding="utf-8"))]
    check("project code imports no broker library", not offenders, str(offenders))

    print("\n--- Paper account bookkeeping ---")
    acct = PaperAccount(cash=5000.0)
    acct.execute({"symbol": "XYZ", "side": "buy", "shares": 10}, 100.0)
    check("buy uses slippage fill and updates cash", abs(acct.cash - (5000 - 10 * expected_fill("buy", 100.0))) < 1e-6)
    t = acct.execute({"symbol": "XYZ", "side": "sell", "shares": 10}, 110.0)
    check("sell realizes P&L correctly", abs(t["realized_pnl"] - (expected_fill("sell", 110.0) - expected_fill("buy", 100.0)) * 10) < 0.01)
    try:
        acct.execute({"symbol": "XYZ", "side": "sell", "shares": 1}, 110.0)
        check("cannot sell shares you don't hold", False)
    except ValueError:
        check("cannot sell shares you don't hold (no shorting)", True)
    try:
        PaperAccount(cash=100.0).execute({"symbol": "XYZ", "side": "buy", "shares": 10}, 100.0)
        check("cannot spend more virtual cash than you have", False)
    except ValueError:
        check("cannot spend more virtual cash than you have", True)

    print("\n--- Rule 1: risk cap and position cap ---")
    res, rep = await run_one(sug("NVDA", "BUY", stop=70.0))      # stop far -> risk cap binds
    m = rep["metrics"]
    check("BUY approved with wide stop", rep["passed"], str(rep["reasons"]))
    check("risk at stop <= 2% of portfolio", m["risk_at_stop_pct"] <= g.MAX_RISK_PER_TRADE_PCT + 0.01, str(m))
    check("risk cap was the binding limit (9 shares)", rep["order"]["shares"] == 9, str(rep["order"]))

    res, rep = await run_one(sug("NVDA", "BUY", stop=95.0))      # tight stop -> position cap binds
    check("BUY approved with tight stop", rep["passed"], str(rep["reasons"]))
    check("position weight <= 30%", rep["metrics"]["position_weight_after_pct"] <= g.MAX_POSITION_PCT + 0.05, str(rep["metrics"]))
    check("position cap was the binding limit", rep["order"]["shares"] == rep["limits"]["by_position_cap"], str(rep["limits"]))

    res, rep = await run_one(sug("AAPL", "BUY", stop=190.0), portfolio={**PORTFOLIO, "holdings": [
        {"symbol": "AAPL", "shares": 30, "avg_cost": 200.0}, {"symbol": "TSLA", "shares": 5, "avg_cost": 250.0},
        {"symbol": "NVDA", "shares": 8, "avg_cost": 100.0}]})
    check("BUY blocked when position already over the cap", not rep["passed"] and "cap" in rep["reasons"][0], str(rep["reasons"]))

    _, rep = await run_one(sug("TSLA", "BUY", stop=260.0))
    check("BUY blocked when stop is above the price", not rep["passed"])
    _, rep = await run_one(sug("TSLA", "BUY", stop=249.5))
    check("BUY blocked when stop is too tight (<1%)", not rep["passed"] and "minimum" in rep["reasons"][0], str(rep["reasons"]))
    _, rep = await run_one(sug("TSLA", "BUY", stop=None))
    check("BUY blocked without a stop-loss", not rep["passed"])
    _, rep = await run_one(sug("NVDA", "BUY", stop=80.0), portfolio={**PORTFOLIO, "cash": 100.0})
    check("BUY blocked when virtual cash is too low", not rep["passed"] and "cash" in rep["reasons"][0], str(rep["reasons"]))

    print("\n--- SELL / HOLD / ACCUMULATE ---")
    _, rep = await run_one(sug("NVDA", "SELL"))
    check("SELL sells 50% of the shares held (4 of 8)", rep["passed"] and rep["order"]["shares"] == 4, str(rep))
    _, rep = await run_one(sug("NVDA", "HOLD", "long_term"))
    check("HOLD passes with no trade", rep["passed"] and rep["order"] is None)
    _, rep = await run_one(sug("TSLA", "ACCUMULATE", "long_term"))
    check("ACCUMULATE buys about 5% of the portfolio", rep["passed"] and rep["order"]["shares"] == 2, str(rep))
    _, rep = await run_one(sug("ZZZZ", "SELL"), data=StubData(prices={"AAPL": 200.0, "TSLA": 250.0, "NVDA": 100.0, "ZZZZ": 10.0}))
    check("SELL blocked for a stock you don't hold", not rep["passed"] and "nothing to sell" in rep["reasons"][0], str(rep["reasons"]))

    print("\n--- Stress test and data freshness (rule 3) ---")
    crash = [100.0] * 60 + [100, 95, 85, 75, 70, 70] + [70.0] * 54      # -30% in 5 days
    check("worst_n_day_return finds the crash", worst_n_day_return(crash, 5) <= -29.0)
    _, rep = await run_one(sug("TSLA", "BUY", stop=225.0), data=StubData(closes=crash))
    check("BUY blocked by the stress test after a historical crash", not rep["passed"] and "stress test" in rep["reasons"][0], str(rep["reasons"]))
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    res = await review_suggestions({"goal_horizon": "x", "summary": "t", "suggestions": [sug("TSLA", "BUY", stop=225.0)]},
                                   StubData(as_of=old), PORTFOLIO)
    check("BUY blocked when market data is stale", res["counts"]["blocked"] == 1 and "min old" in res["blocked"][0]["paper_test"]["reasons"][0])
    _, rep = await run_one(sug("TSLA", "BUY", stop=225.0), data=StubData(closes=[100.0] * 10))
    check("BUY blocked when there is too little history", not rep["passed"] and "history" in rep["reasons"][0])

    print("\n--- Whole review ---")
    final = {"goal_horizon": "10 Months", "summary": "t", "suggestions": [
        sug("NVDA", "BUY", stop=70.0), sug("NVDA", "SELL"), sug("TSLA", "BUY", stop=260.0), sug("AAPL", "HOLD", "long_term")]}
    res = await review_suggestions(final, StubData(), PORTFOLIO)
    check("counts add up", res["counts"] == {"submitted": 4, "approved": 3, "blocked": 1}, str(res["counts"]))
    check("blocked suggestions are NOT in the shown list", all(s["symbol"] != "TSLA" for s in res["suggestions"]))
    check("every shown suggestion carries its paper_test report", all("paper_test" in s for s in res["suggestions"]))
    check("paper account changed only by approved trades", len(res["paper_account"]["trades"]) == 2)
    check("original portfolio is untouched", PORTFOLIO["cash"] == 10000.0 and PORTFOLIO["holdings"][2]["shares"] == 8)

    print("\nALL GUARDRAIL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
