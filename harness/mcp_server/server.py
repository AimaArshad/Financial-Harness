"""
Financial Harness - Market Data MCP Server
Tools: get_price, get_candles
Data source: Yahoo Finance via yfinance (free, ~15 min delayed; a stand-in for a licensed Nasdaq feed)

Set the environment variable FH_FAKE_DATA=1 to serve synthetic data instead
(useful for testing without internet or when Yahoo rate-limits you).
"""
import os
import random
from datetime import datetime, timedelta, timezone

import yfinance as yf
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

mcp = FastMCP("Financial Harness Market Data")

USE_FAKE = os.getenv("FH_FAKE_DATA") == "1"
VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
VALID_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"}
MAX_CANDLES = 500  # keeps tool output small so the agent's context doesn't overflow
FAKE_PERIOD_DAYS = {"1d": 1, "5d": 5, "1mo": 21, "3mo": 63, "6mo": 126, "1y": 252,
                    "2y": 504, "5y": 504, "10y": 504, "ytd": 200, "max": 504}
SOURCE = "yfinance (Yahoo Finance, may be delayed ~15 min)"
FAKE_SOURCE = "FAKE DATA (testing only)"


def _clean_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s or len(s) > 10 or not s.replace(".", "").replace("-", "").isalnum():
        raise ToolError(f"Invalid ticker symbol: {symbol!r}")
    return s


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fake_series(sym: str, n: int = 504) -> list[dict]:
    """Deterministic synthetic daily candles (same ticker -> same prices)."""
    rng = random.Random(sym)
    price = rng.uniform(80, 400)
    start = datetime.now(timezone.utc) - timedelta(days=n)
    rows = []
    for i in range(n):
        o = price
        c = o * (1 + rng.gauss(0.0004, 0.015))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.005)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.005)))
        rows.append({
            "time": (start + timedelta(days=i)).isoformat(timespec="seconds"),
            "open": round(o, 2), "high": round(h, 2), "low": round(l, 2),
            "close": round(c, 2), "volume": rng.randint(10_000_000, 90_000_000),
        })
        price = c
    return rows


@mcp.tool
def get_price(symbol: str) -> dict:
    """Get the latest price for a stock ticker (e.g. AAPL, TSLA, NVDA).

    Returns the price, currency, a UTC timestamp for when the data was fetched,
    and the data source. Data may be delayed up to ~15 minutes.
    """
    sym = _clean_symbol(symbol)
    if USE_FAKE:
        return {"symbol": sym, "price": _fake_series(sym)[-1]["close"], "currency": "USD",
                "as_of": _now_utc(), "source": FAKE_SOURCE}
    try:
        ticker = yf.Ticker(sym)
        price = ticker.fast_info.get("last_price")
        currency = ticker.fast_info.get("currency", "USD")
        if price is None or price != price:  # None or NaN
            hist = ticker.history(period="5d", interval="1d")
            closes = hist["Close"].dropna() if not hist.empty else hist
            if len(closes) == 0:
                raise ToolError(f"No price data found for {sym}")
            price = float(closes.iloc[-1])
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Failed to fetch price for {sym}: {e}")    
    # try:
    #     ticker = yf.Ticker(sym)
    #     price = ticker.fast_info.get("last_price")
    #     currency = ticker.fast_info.get("currency", "USD")
    #     if price is None:  # fallback: last close from recent history
    #         hist = ticker.history(period="5d", interval="1d")
    #         if hist.empty:
    #             raise ToolError(f"No price data found for {sym}")
    #         price = float(hist["Close"].iloc[-1])


    return {"symbol": sym, "price": round(float(price), 2), "currency": currency,
            "as_of": _now_utc(), "source": SOURCE}


@mcp.tool
def get_candles(symbol: str, period: str = "1mo", interval: str = "1d") -> dict:
    """Get OHLCV candlestick data for a stock ticker.

    Args:
        symbol: Ticker, e.g. AAPL.
        period: How far back: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max.
        interval: Candle size: 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo.
            Note: intraday intervals (1m-1h) only work for short periods (about 60 days or less).
    Returns the most recent candles (max 500), oldest first.
    """
    sym = _clean_symbol(symbol)
    if period not in VALID_PERIODS:
        raise ToolError(f"Invalid period {period!r}. Use one of {sorted(VALID_PERIODS)}")
    if interval not in VALID_INTERVALS:
        raise ToolError(f"Invalid interval {interval!r}. Use one of {sorted(VALID_INTERVALS)}")

    if USE_FAKE:
        candles = _fake_series(sym)[-FAKE_PERIOD_DAYS[period]:][-MAX_CANDLES:]
        return {"symbol": sym, "period": period, "interval": interval, "count": len(candles),
                "candles": candles, "as_of": _now_utc(), "source": FAKE_SOURCE}

    try:
        df = yf.Ticker(sym).history(period=period, interval=interval)
    except Exception as e:
        raise ToolError(f"Failed to fetch candles for {sym}: {e}")

    # if df is None or df.empty:
    #     df = df.dropna(subset=["Open", "High", "Low", "Close"])
    #     if df.empty:
    #        raise ToolError(f"No complete candle data for {sym}")
    #     # raise ToolError(f"No candle data for {sym} with period={period}, interval={interval}")
    if df is None or df.empty:
        raise ToolError(f"No candle data for {sym} with period={period}, interval={interval}")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        raise ToolError(f"No complete candle data for {sym}") 
    
    
      
    df = df.tail(MAX_CANDLES)
    candles = [
        {
            "time": idx.isoformat(),
            "open": round(float(row["Open"]), 2),
            "high": round(float(row["High"]), 2),
            "low": round(float(row["Low"]), 2),
            "close": round(float(row["Close"]), 2),
            "volume": int(row["Volume"]),
        }
        for idx, row in df.iterrows()
    ]
    return {"symbol": sym, "period": period, "interval": interval, "count": len(candles),
            "candles": candles, "as_of": _now_utc(), "source": SOURCE}

@mcp.tool()
def get_fundamentals(symbol: str) -> dict:
    """Valuation and company data: P/E, market cap, margins, revenue, earnings growth."""
    sym = _clean_symbol(symbol)
    if USE_FAKE:
        raise ToolError("Fundamentals are not available in synthetic-data mode. Tell the user.")
    try:
        info = yf.Ticker(sym).info or {}
    except Exception as e:
        raise ToolError(f"Failed to fetch fundamentals for {sym}: {e}")
    keys = ["longName", "sector", "marketCap", "trailingPE", "forwardPE", "priceToBook",
            "profitMargins", "returnOnEquity", "revenueGrowth", "earningsGrowth",
            "totalRevenue", "debtToEquity", "dividendYield"]
    return {"symbol": sym, "fundamentals": {k: info.get(k) for k in keys},
            "as_of": _now_utc(), "source": SOURCE}

@mcp.tool()
def get_indicators(symbol: str, period: str = "6mo") -> dict:
    """RSI(14), MACD, SMA20/50 from daily candles."""
    import pandas as pd
    sym = _clean_symbol(symbol)
    if period not in VALID_PERIODS:
        raise ToolError(f"Invalid period {period!r}. Use one of {sorted(VALID_PERIODS)}")
    try:
        if USE_FAKE:
            c = pd.Series([k["close"] for k in _fake_series(sym)[-FAKE_PERIOD_DAYS[period]:]])
        else:
            c = yf.Ticker(sym).history(period=period, interval="1d")["Close"].dropna()
    except Exception as e:
        raise ToolError(f"Failed to fetch data for {sym}: {e}")
    if len(c) < 30:
        raise ToolError(f"Not enough history for {sym} to compute indicators (need 30+ days, got {len(c)}).")
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + gain / loss)
    macd = c.ewm(span=12).mean() - c.ewm(span=26).mean()
    return {"symbol": sym, "last_close": round(float(c.iloc[-1]), 2),
            "rsi14": round(float(rsi.iloc[-1]), 1), "macd": round(float(macd.iloc[-1]), 3),
            "macd_signal": round(float(macd.ewm(span=9).mean().iloc[-1]), 3),
            "sma20": round(float(c.tail(20).mean()), 2), "sma50": round(float(c.tail(50).mean()), 2),
            "as_of": _now_utc(), "source": FAKE_SOURCE if USE_FAKE else SOURCE}    
    
if __name__ == "__main__":
    mcp.run()  # stdio transport (what Claude Desktop / Claude Code / your agent use)
