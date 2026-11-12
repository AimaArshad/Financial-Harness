# PTC script: runs INSIDE the sandbox. `tools` is provided by the sandbox.
# One script loops over ALL holdings, calls the MCP tools, and computes P&L + moving averages.
# Only the compact `result` goes back to the agent (instead of hundreds of raw candles).

# pyright: reportUndefinedVariable=false
def sma(values, n):
    return round(sum(values[-n:]) / n, 2) if len(values) >= n else None

portfolio = tools.get_portfolio()
rows = []
total_value = 0.0
total_cost = 0.0

for h in portfolio["holdings"]:
    sym = h["symbol"]
    price_data = tools.get_price(symbol=sym)
    candles = tools.get_candles(symbol=sym, period="6mo", interval="1d")["candles"]
    closes = [c["close"] for c in candles]

    last = price_data["price"]
    value = round(h["shares"] * last, 2)
    cost = round(h["shares"] * h["avg_cost"], 2)
    pnl = round(value - cost, 2)
    pnl_pct = round(pnl / cost * 100, 2) if cost else 0.0

    sma20, sma50 = sma(closes, 20), sma(closes, 50)
    if sma20 and sma50 and last > sma20 > sma50:
        trend = "uptrend"
    elif sma20 and sma50 and last < sma20 < sma50:
        trend = "downtrend"
    else:
        trend = "sideways"
    month_ago = closes[-21] if len(closes) >= 21 else closes[0]
    change_1m_pct = round((last - month_ago) / month_ago * 100, 2)

    total_value += value
    total_cost += cost
    rows.append({
        "symbol": sym, "shares": h["shares"], "avg_cost": h["avg_cost"], "price": last,
        "value": value, "pnl": pnl, "pnl_pct": pnl_pct,
        "sma20": sma20, "sma50": sma50, "trend": trend, "change_1m_pct": change_1m_pct,
        "data_as_of": price_data["as_of"],
    })
    print(f"{sym}: price {last}, P&L {pnl} ({pnl_pct}%), SMA20 {sma20}, SMA50 {sma50}, {trend}")

result = {
    "holdings": rows,
    "totals": {
        "value": round(total_value, 2),
        "cost": round(total_cost, 2),
        "pnl": round(total_value - total_cost, 2),
        "pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0.0,
    },
}
