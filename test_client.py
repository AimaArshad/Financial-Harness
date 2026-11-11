"""
Test client: starts server.py and calls both tools on AAPL.
Run:  python test_client.py
"""
import asyncio
import json
from pathlib import Path

from fastmcp import Client


async def main():
    async with Client(Path(__file__).parent / "server.py") as client:
        tools = await client.list_tools()
        print("Tools found:", [t.name for t in tools])

        price = await client.call_tool("get_price", {"symbol": "AAPL"})
        print("\nget_price(AAPL):")
        print(json.dumps(price.data, indent=2))

        candles = await client.call_tool(
            "get_candles", {"symbol": "AAPL", "period": "1mo", "interval": "1d"}
        )
        data = candles.data
        print(f"\nget_candles(AAPL, 1mo, 1d): {data['count']} candles")
        print("First:", data["candles"][0])
        print("Last: ", data["candles"][-1])

        # Error handling check: a bad ticker should return a clean error, not crash
        try:
            await client.call_tool("get_price", {"symbol": "!!!"})
        except Exception as e:
            print("\nBad ticker handled correctly ->", e)


if __name__ == "__main__":
    asyncio.run(main())
