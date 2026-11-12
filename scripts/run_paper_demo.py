"""
Try the paper trader WITHOUT the AI: feeds hand-written suggestions (good and bad) to the tester.
  python run_paper_demo.py          # live Yahoo prices
  python run_paper_demo.py --fake   # synthetic prices, no internet
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

if "--fake" in sys.argv:
    os.environ["FH_FAKE_DATA"] = "1"

from harness.harness_common import McpData, load_portfolio, make_mcp_client
from harness.paper_trader import print_review, review_suggestions


async def main():
    portfolio = load_portfolio()
    async with make_mcp_client() as mcp_client:
        data = McpData(mcp_client)
        aapl = (await data.get_price("AAPL"))["price"]
        tsla = (await data.get_price("TSLA"))["price"]
        nvda = (await data.get_price("NVDA"))["price"]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        why = "Demo suggestion written by hand to test the paper trader."
        final = {"goal_horizon": "10 Months", "summary": "demo", "suggestions": [
            dict(symbol="AAPL", horizon="long_term", action="ACCUMULATE", confidence="medium", reasoning=why, data_as_of=now),
            dict(symbol="TSLA", horizon="short_term", action="BUY", confidence="low", reasoning=why,
                 stop_loss=round(tsla * 0.95, 2), data_as_of=now),
            dict(symbol="NVDA", horizon="short_term", action="SELL", confidence="low", reasoning=why, data_as_of=now),
            dict(symbol="AAPL", horizon="short_term", action="BUY", confidence="low", reasoning=why,
                 stop_loss=round(aapl * 0.998, 2), data_as_of=now),   # stop too tight -> blocked
            dict(symbol="NVDA", horizon="short_term", action="BUY", confidence="low", reasoning=why,
                 stop_loss=round(nvda * 1.05, 2), data_as_of=now),    # stop above price -> blocked
        ]}
        result = await review_suggestions(final, data, portfolio)
    print_review(result)


if __name__ == "__main__":
    asyncio.run(main())
