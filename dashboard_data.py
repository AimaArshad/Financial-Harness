"""
Data layer for the Streamlit dashboard. Connects the page to everything built so far:
  MCP server (prices/candles) -> portfolio view, charts
  agent.py                    -> AI suggestions
  schemas.py + paper_trader.py -> validation and paper-testing (used for BOTH engines)
Streamlit is synchronous, so each public function wraps its async work in asyncio.run().
"""
import asyncio
import json
import os
import threading
from datetime import datetime, timezone

from harness_common import LOG_PATH, McpData, load_portfolio, make_mcp_client
from paper_trader import review_suggestions
from schemas import validate_suggestions

ENGINES = {
    "sample": "Rule-based sample (no API key)",
    "direct": "AI agent: direct tool calls",
    "ptc": "AI agent: sandbox + PTC",
}


def _run(coro):
    return asyncio.run(coro)


def get_secret(name: str, default=None):
    """Read a setting from an environment variable, then from Streamlit secrets (used when deployed)."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        import streamlit as st
        return st.secrets.get(name, default)
    except Exception:  # no secrets file locally: that's fine
        return default


# Global (per server process) counter that limits paid AI runs, so a public link can't drain your API credit.
_ai_lock = threading.Lock()
_ai_runs = 0


def _reserve_ai_run():
    """Returns an error string if the cap is reached, else None (and counts the run)."""
    global _ai_runs
    try:
        cap = int(get_secret("FH_MAX_AI_RUNS", 20))
    except (TypeError, ValueError):
        cap = 20
    with _ai_lock:
        if _ai_runs >= cap:
            return (f"AI usage limit reached ({cap} runs since the app started). "
                    "Use the rule-based sample engine, or ask the owner to restart the app or raise FH_MAX_AI_RUNS.")
        _ai_runs += 1
    return None


# ---------- portfolio ----------
async def _portfolio_view(fake: bool) -> dict:
    pf = load_portfolio()
    rows, as_of, source = [], None, ""
    async with make_mcp_client(fake=fake) as c:
        data = McpData(c)
        for h in pf["holdings"]:
            px = await data.get_price(h["symbol"])
            price = float(px["price"])
            value, cost = h["shares"] * price, h["shares"] * h["avg_cost"]
            rows.append({"symbol": h["symbol"], "shares": h["shares"], "avg_cost": h["avg_cost"], "price": price,
                         "value": value, "cost": cost, "pnl": value - cost,
                         "pnl_pct": (value - cost) / cost * 100 if cost else 0.0})
            as_of, source = px["as_of"], px.get("source", "")
    holdings_value = sum(r["value"] for r in rows)
    total_cost = sum(r["cost"] for r in rows)
    total_value = holdings_value + pf.get("cash", 0.0)
    for r in rows:
        r["weight_pct"] = r["value"] / total_value * 100
    return {"rows": rows, "cash": pf.get("cash", 0.0), "holdings_value": holdings_value,
            "total_value": total_value, "total_cost": total_cost, "pnl": holdings_value - total_cost,
            "pnl_pct": (holdings_value - total_cost) / total_cost * 100 if total_cost else 0.0,
            "as_of": as_of, "source": source, "goals": pf["goals"]}


def get_portfolio_view(fake: bool) -> dict:
    return _run(_portfolio_view(fake))


# ---------- candles ----------
async def _candles(symbol: str, period: str, fake: bool) -> dict:
    async with make_mcp_client(fake=fake) as c:
        return await McpData(c).get_candles(symbol, period, "1d")


def get_candles(symbol: str, period: str, fake: bool) -> dict:
    return _run(_candles(symbol, period, fake))


# ---------- suggestions ----------
def _sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def _stop_pct(months: int) -> float:
    return 0.05 if months <= 10 else 0.07 if months <= 20 else 0.10


async def _sample_suggestions(goal_name: str, fake: bool) -> dict:
    """Simple moving-average rules. Lets you demo the whole pipeline without an API key."""
    pf = load_portfolio()
    months = next(g["months"] for g in pf["goals"] if g["name"] == goal_name)
    held = {h["symbol"].upper() for h in pf["holdings"]}
    suggestions = []
    async with make_mcp_client(fake=fake) as c:
        data = McpData(c)
        for h in pf["holdings"]:
            sym = h["symbol"]
            px = await data.get_price(sym)
            closes = [k["close"] for k in (await data.get_candles(sym, "6mo", "1d"))["candles"]]
            last, sma20, sma50 = float(px["price"]), _sma(closes, 20), _sma(closes, 50)
            if sma20 and sma50 and last > sma20 > sma50:
                trend = "uptrend"
            elif sma20 and sma50 and last < sma20 < sma50:
                trend = "downtrend"
            else:
                trend = "sideways"
            detail = (f"Price {last:.2f} vs 20-day average {sma20:.2f} and 50-day average {sma50:.2f}: {trend}."
                      if sma20 and sma50 else "Not enough history for moving averages.")
            suggestions.append({"symbol": sym, "horizon": "long_term",
                                "action": "ACCUMULATE" if trend == "uptrend" else "HOLD",
                                "confidence": "medium" if trend == "uptrend" else "low",
                                "reasoning": f"{detail} Long-term view for the {goal_name} goal.",
                                "data_as_of": px["as_of"]})
            if trend == "uptrend":
                short = {"action": "BUY", "stop_loss": round(last * (1 - _stop_pct(months)), 2)}
            elif trend == "downtrend":
                short = {"action": "SELL", "stop_loss": None}
            else:
                short = {"action": "HOLD", "stop_loss": None}
            suggestions.append({"symbol": sym, "horizon": "short_term", "confidence": "low",
                                "reasoning": f"{detail} Short-term momentum signal.", "data_as_of": px["as_of"], **short})
        payload = {"goal_horizon": goal_name, "suggestions": suggestions,
                   "summary": f"Rule-based sample for the {goal_name} goal using moving-average trend rules (not AI)."}
        ok, out = validate_suggestions(payload, held)
        if not ok:
            return {"error": "Sample suggestions failed validation: " + "; ".join(out)}
        result = await review_suggestions(out, data, pf)
    result["engine"] = ENGINES["sample"]
    return result


# async def _ai_suggestions(goal_name: str, mode: str, fake: bool, api_key: str) -> dict:
#     from agent import run_agent
#     from anthropic import AsyncAnthropic
#     async with make_mcp_client(fake=fake) as c:
#         result = await run_agent(AsyncAnthropic(api_key=api_key), c, mode, goal_name, verbose=False)
#     result["engine"] = ENGINES[mode]
#     return result


# def get_suggestions(goal_name: str, engine: str, fake: bool) -> dict:
#     """Returns the paper-tested result dict, or {"error": "..."}."""
#     try:
#         if engine == "sample":
#             return _run(_sample_suggestions(goal_name, fake))
#         api_key = get_secret("ANTHROPIC_API_KEY")
#         if not api_key:
#             return {"error": "ANTHROPIC_API_KEY is not set. Locally: set it in the terminal that starts Streamlit "
#                              "and restart. Deployed: add it under Settings > Secrets. "
#                              "Or choose the rule-based sample engine."}
#         limit_error = _reserve_ai_run()
#         if limit_error:
#             return {"error": limit_error}
#         return _run(_ai_suggestions(goal_name, engine, fake, api_key))
#     except Exception as e:
#         return {"error": f"{type(e).__name__}: {e}"}


# async def _ai_suggestions(goal_name: str, mode: str, fake: bool, api_key: str) -> dict:
#     import agent_free
#     os.environ["GROQ_API_KEY"] = api_key  # agent_free reads the key from the environment
#     async with make_mcp_client(fake=fake) as c:
#         # ASSUMPTION: same shape as agent.run_agent, but taking a provider name instead of an Anthropic client.
#         result = await agent_free.run_agent("groq", c, mode, goal_name, verbose=False)
#     result["engine"] = ENGINES[mode]
#     return result

async def _ai_suggestions(goal_name: str, mode: str, fake: bool, api_key: str) -> dict:
    from openai import AsyncOpenAI
    from agent_free import PROVIDERS, run_agent_free
    cfg = PROVIDERS["groq"]
    llm = AsyncOpenAI(base_url=cfg["base_url"], api_key=api_key)
    async with make_mcp_client(fake=fake) as c:
        result = await run_agent_free(llm, cfg["model"], c, mode, goal_name, verbose=False)
    result["engine"] = ENGINES[mode]
    return result

def get_suggestions(goal_name: str, engine: str, fake: bool) -> dict:
    """Returns the paper-tested result dict, or {"error": "..."}."""
    try:
        if engine == "sample":
            return _run(_sample_suggestions(goal_name, fake))
        api_key = get_secret("GROQ_API_KEY")
        if not api_key:
            return {"error": "GROQ_API_KEY is not set. Locally: run  $env:GROQ_API_KEY=\"gsk_...\"  in the terminal "
                             "that starts Streamlit, then restart it. Deployed: add it under Settings > Secrets. "
                             "Or choose the rule-based sample engine."}
        limit_error = _reserve_ai_run()
        if limit_error:
            return {"error": limit_error}
        return _run(_ai_suggestions(goal_name, engine, fake, api_key))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def read_audit_log(n: int = 15) -> list:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out
