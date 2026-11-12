"""Conversational agent: Groq (OpenAI-compatible) + harness MCP tools + Python sandbox."""
import asyncio
import json

import openai

from harness.context import compress_messages
from harness.agent import GET_PORTFOLIO_TOOL, RUN_PYTHON_TOOL, _truncate
from harness.agent_free import _trim, compact_candles, to_openai_tool
from harness.harness_common import call_mcp, log_event, make_sandbox_tool_handler

CHAT_MAX_STEPS = 10

CHAT_SYSTEM = """You are an AI stock market assistant for an educational project.
Always fetch data with the tools before answering. Never invent prices, ratios or news.
- Technical questions: use get_indicators and get_candles.
- Fundamental questions: you MUST call get_fundamentals for each symbol (valuation, profitability, growth).
- Comparisons: fetch data for every symbol and show the result as a markdown table.
- The user's holdings and goals come from get_portfolio.
- If the user states a goal in words (amount, timeframe, risk), restate it in one line, read the
  portfolio, work out the required yearly return with run_python, and relate it to the holdings.
- NEVER do arithmetic, projections or what-if calculations in your head. For ANY projection, goal
  feasibility or scenario question you MUST call run_python: write a short Python script, put every
  assumption in variables at the top, print short results, and label the result hypothetical, not a prediction.
  Do not answer these questions without calling the tool first.
- Inside run_python: tools.get_portfolio(), tools.get_price(symbol="AAPL"),
  tools.get_candles(symbol="AAPL", period="6mo", interval="1d") (candles under key "candles"),
  tools.get_fundamentals(symbol="AAPL"). Print only short summaries, never raw candles.
- If run_python returns an error, read the traceback, fix your code and run it again (at most 3 attempts).
- End every answer with a "Sources and assumptions" line naming the data source and its timestamp.
- Educational analysis only, not financial advice. Do not tell the user to buy or sell.
- Keep answers concise."""


def _notify(cb, pct, msg):
    if cb:
        cb(pct, msg)


async def run_chat(llm, model, mcp_client, history, goal_name, on_progress=None):
    """history: [{"role": "user"|"assistant", "content": str}, ...] ending with a user message.
    Returns (answer_markdown, tool_log)."""
    base, seen = [GET_PORTFOLIO_TOOL], {GET_PORTFOLIO_TOOL["name"]}
    for t in await mcp_client.list_tools():
        if t.name not in seen:
            base.append({"name": t.name, "description": t.description or "",
                         "input_schema": getattr(t, "input_schema", None) or t.inputSchema})
            seen.add(t.name)
    base.append(RUN_PYTHON_TOOL)
    tools = [to_openai_tool(t) for t in base]
    messages = [{"role": "system",
                 "content": CHAT_SYSTEM + f"\nThe user's selected goal horizon: {goal_name}."}] + list(history)
    tool_log = []
    log_event("chat_start", goal=goal_name)

    sandbox_failures = 0
    rate_hits = 0
    nudges = 0
    for step in range(CHAT_MAX_STEPS):
        _notify(on_progress, min(90, 10 + step * 8), f"Step {step + 1}: model is thinking")
        try:
            resp = await llm.chat.completions.create(model=model, messages=compress_messages(messages),
                                                     tools=tools, tool_choice="auto",
                                                     max_tokens=3000, temperature=0)
        # except openai.RateLimitError:
        #     print(f"[step {step + 1}] RATE LIMIT, waiting 30s", flush=True)
        #     _notify(on_progress, min(90, 10 + step * 8), "Rate limit hit, waiting 30s")
        #     await asyncio.sleep(30)
        #     continue
        
        except openai.RateLimitError as e:
            rate_hits += 1
            print(f"[step {step + 1}] RATE LIMIT ({rate_hits}/3): {str(e)[:300]}", flush=True)
            if rate_hits > 3:
                return "Rate limit reached repeatedly. Please try again in a few minutes.", tool_log
            _notify(on_progress, min(90, 10 + step * 8), "Rate limit hit, waiting 30s")
            await asyncio.sleep(30)
            continue
        except openai.BadRequestError as e:
            messages.append({"role": "user", "content": f"Your last tool call was invalid: {str(e)[:300]} Fix it."})
            continue

        msg = resp.choices[0].message
        entry = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            entry["tool_calls"] = [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name,
                                                 "arguments": tc.function.arguments or "{}"}}
                                   for tc in msg.tool_calls]
        messages.append(entry)

        if not msg.tool_calls:
            # The model answered without using any tool: ask it once to use the tools first.
            if not tool_log and nudges < 1:
                nudges += 1
                messages.append({"role": "user", "content":
                                 "You answered without calling any tool. Call the required tools now "
                                 "(run_python for calculations or projections, get_fundamentals for valuation, "
                                 "get_indicators for technicals), then answer."})
                continue
            log_event("chat_done", steps=step + 1)
            return (msg.content or "").strip(), tool_log

        for tc in msg.tool_calls:
            name = tc.function.name
            print(f"  tool: {name}", flush=True)
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            _notify(on_progress, min(90, 14 + step * 8), f"Calling tool: {name}")
            log_event("tool_call", name=name, input=args)
            try:
                if name == "run_python":
                    from harness.sandbox import run_code
                    tool_log.append("run_python:\n" + str(args.get("code", "")))
                    out = await run_code(args.get("code", ""), make_sandbox_tool_handler(mcp_client), timeout=90)
                    if out["status"] != "ok":
                        sandbox_failures += 1
                        if sandbox_failures >= 3:
                            return ("The calculation sandbox failed 3 times, so I could not produce verified numbers.",
                                    tool_log)
                    content = _trim(json.dumps(out, default=str))
                    tool_log.append("output:\n" + content[:1500])
                else:
                    tool_log.append(f"{name} {json.dumps(args)}")
                    result = await call_mcp(mcp_client, name, args)
                    if name == "get_candles" and isinstance(result, dict) and result.get("candles"):
                        result = compact_candles(result)
                    content = _truncate(json.dumps(result, default=str))
            except Exception as e:
                content = f"Tool error: {type(e).__name__}: {e}"

            if content.startswith("Tool error"):
                tool_log.append("ERROR: " + content[:300])
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

    return "I could not finish within the step limit. Please try a narrower question.", tool_log