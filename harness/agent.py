"""
Financial Harness - agent loop.

The agent (Claude) can call tools. The HARNESS decides what tools exist, executes them,
validates the final answer, and refuses anything that breaks the guardrails.

Modes:
  direct (default): agent calls MCP tools one at a time (get_portfolio, get_price, get_candles)
  ptc:              agent writes ONE Python script (run in the sandbox) that calls the tools in a loop

Usage:
  set ANTHROPIC_API_KEY=...            (Windows)   |  export ANTHROPIC_API_KEY=...  (Mac/Linux)
  python agent.py --goal "10 Months"
  python agent.py --goal "10 Years" --mode ptc
  python agent.py --fake               # synthetic market data (no Yahoo needed)
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from harness.harness_common import (McpData, call_mcp, load_portfolio, log_event, make_mcp_client,
                            make_sandbox_tool_handler)
from harness.paper_trader import print_review, review_suggestions
from harness.schemas import validate_suggestions

MODEL = os.environ.get("FH_MODEL", "claude-sonnet-5-5")
MAX_TOOL_RESULT_CHARS = 30000

SYSTEM_BASE = """You are a cautious portfolio analysis assistant inside a safety harness.
You NEVER place real trades and have no trading tools. You only analyse and suggest.
Rules:
- Use tools to get real data. Never invent prices or numbers.
- For each holding give a LONG-TERM view (HOLD or ACCUMULATE) and, when the data supports it, a SHORT-TERM view (BUY, SELL or HOLD).
- Every BUY must include a stop_loss price below the current price. SELL, HOLD and ACCUMULATE do not need one.
- You do NOT choose order sizes. The harness sizes each order itself, tests every suggestion with virtual money (paper trading),
  and BLOCKS any suggestion that breaks the risk limits. Only suggestions that pass are shown to the user.
- data_as_of must be copied exactly from the tool output field "as_of".
- Keep reasoning short and tied to concrete numbers (P&L, moving averages, trend).
- Match your emphasis to the user's goal horizon.
- When finished, call submit_suggestions exactly once. If it returns errors, fix them and call it again."""

SYSTEM_DIRECT = SYSTEM_BASE + """
- Start with get_portfolio. Then call get_price and get_candles for each holding (use at most period "3mo", interval "1d")."""

SYSTEM_PTC = SYSTEM_BASE + """
- Do the analysis with ONE run_python script that loops over all holdings.
- Inside the script, a global object `tools` provides: tools.get_portfolio(), tools.get_price(symbol="AAPL"),
  tools.get_candles(symbol="AAPL", period="6mo", interval="1d"). Use keyword arguments only.
- Use only the Python standard library (math, statistics, json, datetime, collections, itertools). No network, no files.
- Compute P&L and moving averages in the script. print() what you need and set a final variable `result` (a JSON-serializable dict).
- Include "as_of" timestamps in your result so you can cite data_as_of."""

SUBMIT_TOOL = {
    "name": "submit_suggestions",
    "description": "Submit the final structured suggestions. Call once when your analysis is complete.",
    "input_schema": {
        "type": "object",
        "properties": {
            "goal_horizon": {"type": "string", "description": "The user's goal, e.g. '10 Months'"},
            "summary": {"type": "string", "description": "2-3 sentence overall summary"},
            "suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "horizon": {"type": "string", "enum": ["long_term", "short_term"]},
                        "action": {"type": "string", "enum": ["HOLD", "ACCUMULATE", "BUY", "SELL"]},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "reasoning": {"type": "string"},
                        "stop_loss": {"type": ["number", "null"], "description": "Required for BUY (below current price); omit otherwise"},
                        "data_as_of": {"type": "string", "description": "ISO timestamp copied from tool output"},
                    },
                    "required": ["symbol", "horizon", "action", "confidence", "reasoning", "data_as_of"],
                },
            },
        },
        "required": ["goal_horizon", "summary", "suggestions"],
    },
}

GET_PORTFOLIO_TOOL = {
    "name": "get_portfolio",
    "description": "Get the user's holdings (symbol, shares, avg_cost) and goals.",
    "input_schema": {"type": "object", "properties": {}},
}

RUN_PYTHON_TOOL = {
    "name": "run_python",
    "description": ("Run Python code in an isolated sandbox. A global `tools` object exposes "
                    "get_portfolio(), get_price(symbol=...), get_candles(symbol=..., period=..., interval=...). "
                    "Set a variable `result` with your final JSON-serializable output."),
    "input_schema": {"type": "object",
                     "properties": {"code": {"type": "string", "description": "Python source code"}},
                     "required": ["code"]},
}


def build_tools(mode: str, mcp_tools) -> list:
    if mode == "ptc":
        return [RUN_PYTHON_TOOL, SUBMIT_TOOL]
    tools = [GET_PORTFOLIO_TOOL]
    for t in mcp_tools:
        tools.append({"name": t.name, "description": t.description or "", "input_schema": t.inputSchema})
    tools.append(SUBMIT_TOOL)
    return tools


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated by harness]"


async def run_agent(llm, mcp_client, mode: str, goal: str, max_turns: int = 12, verbose: bool = True,
                    review: bool = True):
    """Run the loop. Returns the validated + paper-tested suggestion dict, or {"error": ...}."""
    portfolio = load_portfolio()
    held = {h["symbol"].upper() for h in portfolio["holdings"]}
    mcp_tools = await mcp_client.list_tools()
    tools = build_tools(mode, mcp_tools)
    system = SYSTEM_PTC if mode == "ptc" else SYSTEM_DIRECT
    messages = [{"role": "user", "content": f"Analyse my portfolio for my goal horizon: {goal}. "
                                            f"Give long-term (hold/accumulate) and short-term (buy/sell) suggestions."}]
    log_event("run_start", mode=mode, goal=goal, model=MODEL)

    def say(*a):
        if verbose:
            print(*a)

    for turn in range(1, max_turns + 1):
        resp = await llm.messages.create(model=MODEL, max_tokens=4000, system=system,
                                         tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        tool_calls = [b for b in resp.content if b.type == "tool_use"]
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                say(f"[turn {turn}] agent: {b.text.strip()[:300]}")

        if not tool_calls:  # model stopped without submitting -> nudge it
            messages.append({"role": "user", "content": "Please finish by calling submit_suggestions."})
            continue

        results = []
        final = None
        for call in tool_calls:
            say(f"[turn {turn}] tool: {call.name} {json.dumps(call.input)[:150]}")
            log_event("tool_call", turn=turn, name=call.name, input=call.input)
            is_error = False
            try:
                if call.name == "submit_suggestions":
                    ok, out = validate_suggestions(call.input, held)
                    if ok:
                        final = out
                        content = "Accepted."
                    else:
                        is_error = True
                        content = "REJECTED by harness. Fix these and call submit_suggestions again:\n- " + "\n- ".join(out)
                        say(f"[turn {turn}] harness rejected submission: {out}")
                elif call.name == "run_python":
                    from harness.sandbox import run_code  # imported here so Task 1 works before Task 2 exists
                    out = await run_code(call.input.get("code", ""), make_sandbox_tool_handler(mcp_client), timeout=90)
                    is_error = out["status"] != "ok"
                    content = _truncate(json.dumps(out, default=str))
                    say(f"[turn {turn}] sandbox status: {out['status']}")
                else:
                    if mode == "ptc":
                        raise PermissionError(f"Tool '{call.name}' is not available in ptc mode")
                    content = _truncate(json.dumps(await call_mcp(mcp_client, call.name, call.input), default=str))
            except Exception as e:
                is_error = True
                content = f"Tool error: {type(e).__name__}: {e}"
            log_event("tool_result", turn=turn, name=call.name, is_error=is_error, preview=content[:300])
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error})

        messages.append({"role": "user", "content": results})
        if final:
            if review:  # paper-test every suggestion BEFORE it is shown
                say(f"[turn {turn}] paper-testing {len(final['suggestions'])} suggestions with virtual money...")
                final = await review_suggestions(final, McpData(mcp_client), portfolio)
                log_event("paper_review", counts=final["counts"],
                          blocked=[(b["symbol"], b["action"], b["paper_test"]["reasons"]) for b in final["blocked"]])
            log_event("run_done", turns=turn)
            return final

    log_event("run_failed", reason="max_turns")
    return {"error": f"No valid submission after {max_turns} turns"}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", default="10 Months", help='"10 Months", "20 Months" or "10 Years"')
    parser.add_argument("--mode", choices=["direct", "ptc"], default="direct")
    parser.add_argument("--fake", action="store_true", help="use synthetic market data")
    args = parser.parse_args()
    if args.fake:
        os.environ["FH_FAKE_DATA"] = "1"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first (see the steps).")

    from anthropic import AsyncAnthropic
    llm = AsyncAnthropic()
    async with make_mcp_client() as mcp_client:
        result = await run_agent(llm, mcp_client, args.mode, args.goal)

    print("\n===== FINAL RESULT =====")
    print(json.dumps(result, indent=2))
    if "counts" in result:
        print_review(result)
    Path("suggestions.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\nSaved to suggestions.json (audit log: agent_log.jsonl)")


if __name__ == "__main__":
    asyncio.run(main())
