"""
Run ptc_analysis.py inside the sandbox, with live (or fake) market data.
  python run_ptc_demo.py          # real data from Yahoo
  python run_ptc_demo.py --fake   # synthetic data, no internet needed
"""
import asyncio
import json
import os
import sys
from pathlib import Path

if "--fake" in sys.argv:
    os.environ["FH_FAKE_DATA"] = "1"

from harness_common import make_mcp_client, make_sandbox_tool_handler
from sandbox import run_code


async def main():
    code = (Path(__file__).parent / "ptc_analysis.py").read_text(encoding="utf-8")
    async with make_mcp_client() as mcp_client:
        out = await run_code(code, make_sandbox_tool_handler(mcp_client), timeout=90)
    print("STATUS:", out["status"])
    print("--- printed output ---")
    print(out.get("stdout", ""))
    if out["status"] == "ok":
        print("--- result ---")
        print(json.dumps(out["result"], indent=2))
    else:
        print("ERROR:", out.get("error"))


if __name__ == "__main__":
    asyncio.run(main())
