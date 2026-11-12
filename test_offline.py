"""
Offline tests: NO internet and NO API key needed.
Uses synthetic market data and a scripted fake LLM.
Run:  python test_offline.py
"""
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

os.environ["FH_FAKE_DATA"] = "1"

from agent import run_agent
from harness_common import make_mcp_client, make_sandbox_tool_handler
from sandbox import run_code

PTC_CODE = (Path(__file__).parent / "ptc_analysis.py").read_text(encoding="utf-8")


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        raise SystemExit(1)


def tool_use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class FakeLLM:
    """Plays back scripted responses; records what the harness sent back."""
    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.messages = self

    async def create(self, **kwargs):
        self.seen.append(kwargs["messages"][-1])
        blocks = self.script.pop(0)()
        return NS(content=blocks, stop_reason="tool_use")


def good_submit():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [tool_use("s2", "submit_suggestions", goal_horizon="10 Months", summary="Test summary of the portfolio.",
            suggestions=[
                dict(symbol="AAPL", horizon="long_term", action="HOLD", confidence="medium",
                     reasoning="Price is above the 20 and 50 day averages, uptrend intact.", data_as_of=now),
                dict(symbol="NVDA", horizon="short_term", action="SELL", confidence="low",
                     reasoning="Downtrend with price below both moving averages.", stop_loss=95.0, data_as_of=now)])]


def bad_submit():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [tool_use("s1", "submit_suggestions", goal_horizon="10 Months", summary="Bad one without stop loss.",
            suggestions=[dict(symbol="TSLA", horizon="short_term", action="BUY", confidence="low",
                              reasoning="Momentum looks positive on the daily chart.", data_as_of=now)])]


async def main():
    async with make_mcp_client() as mcp:
        handler = make_sandbox_tool_handler(mcp)

        print("\n--- Sandbox tests ---")
        out = await run_code("result = {'x': 1 + 1}\nprint('hi')", handler, timeout=10)
        check("simple code runs", out["status"] == "ok" and out["result"] == {"x": 2} and "hi" in out["stdout"])

        out = await run_code("import socket", handler, timeout=10)
        check("import socket is blocked", out["status"] == "error" and "not allowed" in out["error"])
        out = await run_code("import os", handler, timeout=10)
        check("import os is blocked", out["status"] == "error")
        out = await run_code("open('x.txt','w')", handler, timeout=10)
        check("open() is blocked", out["status"] == "error")
        out = await run_code("1/0", handler, timeout=10)
        check("exceptions are reported, not crashed", out["status"] == "error" and "ZeroDivisionError" in out["error"])
        out = await run_code("while True:\n    pass", handler, timeout=3)
        check("infinite loop is killed by timeout", out["status"] == "timeout")
        out = await run_code("tools.place_order(symbol='AAPL', qty=1)", handler, timeout=10)
        check("non-whitelisted tool is refused", out["status"] == "error" and "not allowed" in out["error"])

        print("\n--- PTC script test ---")
        out = await run_code(PTC_CODE, handler, timeout=60)
        check("PTC script runs", out["status"] == "ok")
        rows = out["result"]["holdings"]
        check("PTC computed 3 holdings with P&L and SMA", len(rows) == 3 and all("pnl" in r and "sma20" in r for r in rows))

        print("\n--- Agent loop tests (fake LLM) ---")
        llm = FakeLLM([
            lambda: [tool_use("a", "get_portfolio")],
            lambda: [tool_use("b", "get_price", symbol="AAPL"), tool_use("c", "get_candles", symbol="AAPL", period="1mo", interval="1d")],
            bad_submit,
            good_submit,
        ])
        res = await run_agent(llm, mcp, "direct", "10 Months", verbose=False)
        check("direct mode: bad submission was rejected, then fixed", "suggestions" in res and len(res["suggestions"]) == 2)
        rejected = llm.seen[3]["content"][0]
        check("harness told the model why it was rejected", rejected["is_error"] and "stop_loss" in rejected["content"])
        check("disclaimer added by harness", res["disclaimer"].startswith("Demo only"))

        llm = FakeLLM([lambda: [tool_use("p", "run_python", code=PTC_CODE)], good_submit])
        res = await run_agent(llm, mcp, "ptc", "10 Years", verbose=False)
        check("ptc mode: sandbox run then valid submission", "suggestions" in res)

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
